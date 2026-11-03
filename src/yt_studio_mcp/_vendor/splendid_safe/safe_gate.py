"""Fail-closed gates and the one request-identity parser.

fail_closed: any exception inside a gate returns the deny value (logged with the request id), never an allow.
Identity.from_request: who sent this request, proven, or anonymous. Identity headers (X-Authentik-*) count only when
  (a) the request came from an edge proxy peer AND carries the edge secret the proxy adds, or
  (b) it carries a valid signed proxy hop (HMAC over method, target, ts, nonce, body sha and the identity headers) from a hop
      peer, keyed by hop_key (byte-compatible with Cipher Studio's mobileproxy "cipher-mobile-proxy-v1" scheme).
  A present but invalid / stale / replayed signature raises IdentityError (answer 403); no proof = anonymous.
require(identity, action): Decision; human-only actions refuse machine and delegated actors.
"""
from __future__ import annotations

import functools
import hashlib
import hmac
import logging
import re
import threading
import time
import uuid
from dataclasses import dataclass, field

__all__ = ["fail_closed", "Identity", "IdentityError", "Decision", "require", "NonceCache"]

log = logging.getLogger("splendid_safe.gate")

HOP_LABEL = b"cipher-mobile-proxy-v1"
HOP_SKEW_S = 60
ID_HEADERS = ("X-Authentik-Username", "X-Authentik-Email", "X-Authentik-Name", "X-Authentik-Groups")
SIG_HEADERS = ("X-Cipher-Sig", "X-Cipher-Sig-Ts", "X-Cipher-Sig-Nonce", "X-Cipher-Sig-Body")
NAME_RE = re.compile(r"^[A-Za-z0-9_.@+-]{1,80}$")
EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]{1,190}\.[A-Za-z]{2,24}$")


def _request_id(args, kwargs) -> str:
    rid = kwargs.get("request_id")
    if not rid:
        for a in args:
            rid = getattr(a, "request_id", None)
            if rid:
                break
    return str(rid or uuid.uuid4().hex[:12])


def fail_closed(deny=False):
    """Decorator: the wrapped gate's exceptions become `deny` (or deny(exc) when deny is callable), logged with a request id.
    A mutable deny value (dict / list) is copied per call so one caller cannot change what the next one gets."""
    def wrap(fn):
        @functools.wraps(fn)
        def inner(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except Exception as e:                              # noqa: BLE001 - that is the point: every failure denies
                rid = _request_id(args, kwargs)
                log.warning("gate %s failed closed (request %s): %s: %s", fn.__qualname__, rid, type(e).__name__, e)
                if callable(deny):
                    return deny(e)
                if isinstance(deny, (dict, list, set)):
                    import copy
                    return copy.deepcopy(deny)
                return deny
        return inner
    return wrap


class IdentityError(ValueError):
    """A request that claims an identity it cannot prove (bad / stale / replayed signature, duplicated identity header)."""


class NonceCache:
    """Seen hop nonces (in memory). A hop signed before this cache was created is refused, so a restart cannot replay."""

    def __init__(self, window: float = 2 * HOP_SKEW_S):
        self.window = window
        self.not_before = int(time.time())
        self._seen = {}
        self._lock = threading.Lock()

    def check_and_add(self, nonce: str, ts: int, now: float) -> None:
        if ts < self.not_before:
            raise IdentityError("signed before this process started")
        with self._lock:
            for k in [k for k, t in self._seen.items() if t < now - self.window]:
                del self._seen[k]
            if nonce in self._seen:
                raise IdentityError("replayed")
            self._seen[nonce] = now


_DEFAULT_NONCES = NonceCache()


def _peer(p) -> str:
    p = str(p or "")
    return p[7:] if p.startswith("::ffff:") else p


def _get(headers, name):
    """One header value; a header sent twice is refused (two parsers could each pick a different copy)."""
    get_all = getattr(headers, "get_all", None)
    if get_all is not None:
        vals = get_all(name) or []
        if len(vals) > 1:
            raise IdentityError("identity header %s sent more than once" % name)
        return vals[0] if vals else None
    v = headers.get(name)
    if v is None:
        lname = name.lower()
        for k in headers.keys():
            if k.lower() == lname:
                return headers[k]
    return v


def hop_message(method: str, target: str, ts: str, nonce: str, body_sha: str, headers) -> bytes:
    idl = "\n".join("%s=%s" % (k.lower(), _get(headers, k) or "") for k in ID_HEADERS)
    return "\n".join(["mproxy", method, target, ts, nonce, body_sha, idl]).encode()


def sign_hop(hop_key: bytes, method: str, target: str, ts: str, nonce: str, body_sha: str, headers) -> str:
    key = hmac.new(hop_key, HOP_LABEL, hashlib.sha256).digest()
    return hmac.new(key, hop_message(method, target, ts, nonce, body_sha, headers), hashlib.sha256).hexdigest()


@dataclass(frozen=True)
class Identity:
    """kind: "human" (a signed-in person), "machine" (a named machine account), "delegated" (an agent acting for a person),
    "anonymous". source: "edge", "hop" or "none". body_sha: the body hash a signed hop committed to (check it once read)."""
    name: str | None = None
    kind: str = "anonymous"
    source: str = "none"
    email: str | None = None
    groups: tuple = field(default_factory=tuple)
    peer: str = ""
    on_behalf_of: str | None = None
    body_sha: str | None = None

    @property
    def is_human(self) -> bool:
        return self.kind == "human" and bool(self.name)

    @classmethod
    def anonymous(cls, peer=""):
        return cls(peer=_peer(peer))

    @classmethod
    def from_request(cls, headers, peer, *, edge_peers, hop_key=None, edge_secret=None, edge_header="X-Cipher-Proxy",
                     hop_peers=(), method="GET", target="/", machines=(), delegate_suffix=None,
                     nonces: NonceCache | None = None, now=None):
        """The ONE identity parser. headers: a Mapping (or http.client.HTTPMessage). peer: the socket peer address.
        edge_peers: hosts the edge proxy connects from; edge_secret: the value it adds in edge_header (no secret = no edge
        identity at all). hop_key: bytes key for signed proxy hops, accepted only from hop_peers. Raises IdentityError for a
        present but unprovable claim; returns Identity.anonymous() when nothing is claimed or proven."""
        p = _peer(peer)
        nonces = _DEFAULT_NONCES if nonces is None else nonces
        user = _get(headers, "X-Authentik-Username")
        email = _get(headers, "X-Authentik-Email")
        groups_raw = _get(headers, "X-Authentik-Groups") or ""
        sig = _get(headers, "X-Cipher-Sig")
        source = None
        body_sha = None
        if sig is not None:
            if p not in set(hop_peers):
                raise IdentityError("a signed hop from a host that is not a hop peer")
            if not hop_key:
                raise IdentityError("no hop key configured")
            ts, nonce, bsha = (str(_get(headers, h) or "") for h in SIG_HEADERS[1:])
            if not (ts.isdigit() and re.fullmatch(r"[0-9a-f]{16,64}", nonce) and re.fullmatch(r"[0-9a-f]{64}", bsha)):
                raise IdentityError("malformed signature headers")
            t = time.time() if now is None else now
            if abs(t - int(ts)) > HOP_SKEW_S:
                raise IdentityError("signature expired")
            want = sign_hop(hop_key, method, target, ts, nonce, bsha, headers)
            if not hmac.compare_digest(want, str(sig)):
                raise IdentityError("bad signature")
            nonces.check_and_add(nonce, int(ts), t)
            source, body_sha = "hop", bsha
        elif edge_secret and p in set(edge_peers) and hmac.compare_digest(str(_get(headers, edge_header) or ""),
                                                                           str(edge_secret)):
            source = "edge"
        if source is None or not user:
            return cls.anonymous(p)
        if not NAME_RE.match(str(user)):
            raise IdentityError("malformed user name")
        if email and not EMAIL_RE.match(str(email)):
            email = None
        groups = tuple(g.strip() for g in str(groups_raw).split("|") if g.strip())
        name = str(user)
        kind, behalf = "human", None
        if name in set(machines):
            kind = "machine"
        elif delegate_suffix and name.endswith(delegate_suffix):
            kind, behalf = "delegated", name[: -len(delegate_suffix)]
        return cls(name=name, kind=kind, source=source, email=email, groups=groups, peer=p, on_behalf_of=behalf,
                   body_sha=body_sha)


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str
    action: str = ""
    who: str | None = None

    def __bool__(self):
        return self.allowed


@fail_closed(deny=lambda e: Decision(False, "gate error: %s" % type(e).__name__))
def require(identity, action: str, *, human_only: bool = True, allowed_kinds=None, names=None) -> Decision:
    """May this identity do `action`? Anonymous -> no. human_only (default) refuses machine and delegated actors.
    allowed_kinds overrides human_only with an explicit set of kinds; names, when given, is the allow-list of names."""
    if not isinstance(identity, Identity) or not identity.name or identity.kind == "anonymous":
        return Decision(False, "not signed in", action)
    kinds = set(allowed_kinds) if allowed_kinds is not None else ({"human"} if human_only else {"human", "machine", "delegated"})
    if identity.kind not in kinds:
        return Decision(False, "a %s actor may not %s" % (identity.kind, action), action, identity.name)
    if names is not None and identity.name not in set(names):
        return Decision(False, "%s may not %s" % (identity.name, action), action, identity.name)
    return Decision(True, "ok", action, identity.name)
