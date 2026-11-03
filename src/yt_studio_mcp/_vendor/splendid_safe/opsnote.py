"""Post a note to the Control Room feed (The Nest, POST /ntfy/<topic>): the Python twin of ubuntu-dev ~/.local/bin/opsnote.

    from splendid_safe import opsnote
    opsnote.post("cipher-ops", "body", priority="high", title="Cipher stream: ...", token_files=[CR_TOKEN_FILE])

    python3 -m splendid_safe.opsnote <topic> "<message>" [priority] [title]
    python3 -m splendid_safe.opsnote --topic T [--priority P] [--title X] "<message>"      (message "-" = stdin)

priority: 1-5 or min|low|default|high|urgent (default 3); anything else is refused BEFORE a request is made (the chores
netwatch flag/positional mix-up of 2026-11-03 posted priority-5 alerts as priority 3). post() raises OpsnoteError on a bad
priority or topic, a missing token, or a failed post; the caller decides whether that is fatal (most callers log and fall back).
Token: token= if given; else the first non-empty readable file of token_files; with neither, $OPSNOTE_TOKEN and then the CLI's
file list. The token is only ever put in the Authorization header: never logged, never in an exception message.
"""
from __future__ import annotations

import http.client
import os
import re
import socket
import sys
import urllib.request

__all__ = ["OpsnoteError", "DEFAULT_URL", "TOKEN_FILES", "PRIORITIES", "check_priority", "check_topic", "find_token", "post",
           "main"]

DEFAULT_URL = "http://controlroom.lan:8397"
TOKEN_FILES = ("~/.config/opsnote/token", "/mnt/cache/appdata/ntfy/.pubtoken", "/mnt/user/screenshots/juniper/.feed-token",
               "~/controlroom/ingest-token")
PRIORITIES = ("1", "2", "3", "4", "5", "min", "low", "default", "high", "urgent")
_TOPIC = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class OpsnoteError(Exception):
    """Bad input, no token, or the post did not succeed."""


def check_priority(p) -> str:
    """The header value for priority p (int 1-5 or one of PRIORITIES), else OpsnoteError."""
    if isinstance(p, bool):
        raise OpsnoteError("bad priority %r" % (p,))
    s = str(p).strip() if isinstance(p, (int, str)) else None
    if s not in PRIORITIES:
        raise OpsnoteError("bad priority %r" % (p,))
    return s


def check_topic(topic) -> str:
    if not isinstance(topic, str) or not _TOPIC.match(topic):
        raise OpsnoteError("bad topic %r" % (topic,))
    return topic


def find_token(token_files=None) -> str:
    if token_files is None:
        env = (os.environ.get("OPSNOTE_TOKEN") or "").strip()
        if env:
            return env
        token_files = TOKEN_FILES
    for f in token_files:
        try:
            with open(os.path.expanduser(f)) as fh:
                tok = fh.read().strip()
        except OSError:
            continue
        if tok:
            return tok
    raise OpsnoteError("no opsnote token")


def post(topic: str, message: str, priority=3, title=None, *, url=None, base_url=None, token=None, token_files=None,
         timeout: float = 10.0, max_body=None, max_title=None, headers=None) -> None:
    """POST message to <base_url>/ntfy/<topic> (or to url as given). title None = no Title header. max_body cuts the UTF-8
    bytes, max_title the title characters. headers: extra headers (e.g. Tags, Content-Type)."""
    prio = check_priority(priority)
    if url is None:
        url = "%s/ntfy/%s" % ((base_url or os.environ.get("OPSNOTE_URL") or DEFAULT_URL).rstrip("/"), check_topic(topic))
    tok = token if token else find_token(token_files)
    data = message.encode("utf-8") if isinstance(message, str) else bytes(message)
    if max_body is not None:
        data = data[:max_body]
    h = {"Authorization": "Bearer " + tok, "Priority": prio}
    if title is not None:
        h["Title"] = title[:max_title] if max_title is not None else title
    h.update(headers or {})
    req = urllib.request.Request(url, data=data, method="POST", headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            r.read()
            code = r.status
    except (OSError, http.client.HTTPException) as e:     # URLError/HTTPError/timeouts; the token is never in the message
        raise OpsnoteError("post failed: %s" % type(e).__name__) from None
    if not 200 <= code < 300:
        raise OpsnoteError("post failed: HTTP %d" % code)


def _parse(argv):
    flags, pos, i = {}, [], 0
    while i < len(argv):
        a = argv[i]
        if a == "--":
            pos += argv[i + 1:]; break
        name = a[2:].partition("=")[0] if a.startswith("--") else None
        if name in ("topic", "priority", "title"):
            if "=" in a:
                flags[name] = a.partition("=")[2]; i += 1
            elif i + 1 < len(argv):
                flags[name] = argv[i + 1]; i += 2
            else:
                raise OpsnoteError("--%s needs a value" % name)
            continue
        if a.startswith("-") and a != "-" and not pos:
            raise OpsnoteError("unknown option %s" % a)
        pos += argv[i:]; break
    if "topic" in flags:
        topic, rest = flags["topic"], pos
        msg = rest[0] if rest else None
        prio, title = flags.get("priority", rest[1] if len(rest) > 1 else "3"), flags.get("title", rest[2] if len(rest) > 2 else None)
    else:
        if not pos:
            raise OpsnoteError('usage: opsnote <topic> "<message>" [priority] [title]')
        topic, msg = pos[0], pos[1] if len(pos) > 1 else None
        prio, title = flags.get("priority", pos[2] if len(pos) > 2 else "3"), flags.get("title", pos[3] if len(pos) > 3 else None)
    if msg is None:
        raise OpsnoteError("message required")
    return topic, msg, prio, title


def main(argv=None, stdin=None) -> int:
    try:
        topic, msg, prio, title = _parse(list(sys.argv[1:] if argv is None else argv))
        check_priority(prio)
        if msg == "-":
            msg = (stdin or sys.stdin).read()
        if title is None:
            title = socket.gethostname().split(".")[0]
        post(topic, msg, prio, title)
    except OpsnoteError as e:
        print("opsnote: %s" % e, file=sys.stderr)
        return 1 if str(e).startswith("post failed") else 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
