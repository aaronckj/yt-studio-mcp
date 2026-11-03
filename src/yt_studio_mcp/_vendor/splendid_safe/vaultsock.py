"""Client for the vaultproxy local socket ($XDG_RUNTIME_DIR/vaultproxy.sock, SO_PEERCRED-checked, mode 0600).

Protocol (mcp-vault-proxy src/local_socket.rs): one JSON request line in, one JSON response line out ("\\n"-terminated), then
the daemon shuts its side down. The reply is read until the newline OR end of file, never with a single recv(): a reply that
does not fit one read (an item with many or large fields) used to be cut and then failed to parse.

Values are returned to the caller only: nothing here logs, prints or puts a value in an exception message.
"""
from __future__ import annotations

import json
import os
import socket
import time

__all__ = ["VaultError", "DEFAULT_SOCKET", "MAX_RESPONSE", "socket_path", "request", "vault_fields", "vault_field"]

DEFAULT_SOCKET = "/run/user/1000/vaultproxy.sock"
MAX_RESPONSE = 16 << 20         # far above any real item; a runaway peer cannot make the client buffer without limit
_CHUNK = 65536


class VaultError(RuntimeError):
    """The lookup failed (daemon error, item/field missing, bad or oversized reply). Never carries a secret value."""


def socket_path(sock=None) -> str:
    return sock or os.environ.get("VAULT_PROXY_SOCKET", DEFAULT_SOCKET)


def _read_reply(c, max_bytes: int, deadline: float) -> bytes:
    buf = bytearray()
    while True:
        left = deadline - time.monotonic()
        if left <= 0:
            raise socket.timeout("vaultproxy reply timed out")
        c.settimeout(left)
        chunk = c.recv(_CHUNK)
        if not chunk:                                   # daemon closed: whatever came is the whole reply
            return bytes(buf)
        nl = chunk.find(b"\n")
        if nl >= 0:                                     # one response line; anything after it is not ours
            buf += chunk[:nl]
            if len(buf) > max_bytes:
                raise VaultError("vaultproxy reply larger than %d bytes" % max_bytes)
            return bytes(buf)
        buf += chunk
        if len(buf) > max_bytes:
            raise VaultError("vaultproxy reply larger than %d bytes" % max_bytes)


def request(req: dict, *, sock=None, timeout: float = 10.0, max_bytes: int = MAX_RESPONSE) -> dict:
    """Send one request object, return the parsed reply object (ok or not). timeout bounds connect + the whole reply."""
    deadline = time.monotonic() + timeout
    c = socket.socket(socket.AF_UNIX)
    try:
        c.settimeout(timeout)
        c.connect(socket_path(sock))
        c.sendall(json.dumps(req).encode() + b"\n")
        raw = _read_reply(c, max_bytes, deadline)
    finally:
        c.close()
    try:
        r = json.loads(raw)
    except ValueError:
        raise VaultError("vaultproxy reply is not JSON (%d bytes)" % len(raw)) from None
    if not isinstance(r, dict):
        raise VaultError("vaultproxy reply is not an object")
    return r


def vault_fields(item: str, fields, *, sock=None, timeout: float = 10.0) -> dict:
    """{field: value} for the named fields of a vault item. Raises VaultError with the daemon's error text when not ok."""
    r = request({"op": "get_item_fields", "item": item, "fields": list(fields)}, sock=sock, timeout=timeout)
    if not r.get("ok"):
        raise VaultError(r.get("error", "vault lookup failed"))
    f = r.get("fields")
    if not isinstance(f, dict):
        raise VaultError("vaultproxy reply has no fields")
    return f


def vault_field(item: str, field: str, *, sock=None, timeout: float = 10.0) -> str:
    """One field of a vault item (KeyError when the daemon answered ok without it)."""
    return vault_fields(item, [field], sock=sock, timeout=timeout)[field]
