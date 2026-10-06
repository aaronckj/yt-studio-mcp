"""Pluggable secret storage: file (default), env, vaultproxy.

Selected via YT_MCP_SECRETS. No secret material is ever logged.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .paths import credentials_path


def __getattr__(name: str):
    # Back-compat: DEFAULT_PATH used to be a module constant.
    if name == "DEFAULT_PATH":
        return credentials_path()
    raise AttributeError(name)


class SecretStore:
    def get(self, key: str) -> str | None:
        raise NotImplementedError

    def set(self, key: str, value: str) -> None:
        raise NotImplementedError


class FileStore(SecretStore):
    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else credentials_path()

    def _load(self) -> dict:
        if not self.path.exists():
            return {}
        return json.loads(self.path.read_text())

    def get(self, key: str) -> str | None:
        return self._load().get(key)

    def set(self, key: str, value: str) -> None:
        data = self._load()
        data[key] = value
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        # Write a 0600 temp file and rename it into place: the old
        # write_text()+chmod() left the token world-readable (umask 022) for a
        # moment, and a crash mid-write truncated the only copy of the
        # refresh token.
        tmp = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w") as fh:
                fh.write(json.dumps(data, indent=2))
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        finally:
            if tmp.exists():
                tmp.unlink()


class EnvStore(SecretStore):
    """Read-only store backed by YT_MCP_<KEY> environment variables."""

    def get(self, key: str) -> str | None:
        return os.environ.get(f"YT_MCP_{key.upper()}")

    def set(self, key: str, value: str) -> None:
        raise NotImplementedError("env secret backend is read-only")


class VaultproxyStore(SecretStore):
    """Vaultwarden-backed store via a local vaultproxy HTTP API."""

    def __init__(self, base_url: str | None = None):
        self.base_url = (
            base_url or os.environ.get("VAULTPROXY_URL", "http://127.0.0.1:8199")
        ).rstrip("/")
        # One vault namespace per instance when several channels share a vault.
        self.prefix = os.environ.get("YT_MCP_VAULTPROXY_PREFIX", "yt-studio-mcp")

    def _request(self, method: str, key: str, body: dict | None = None) -> dict:
        req = Request(
            f"{self.base_url}/items/{self.prefix}%2F{key}",
            method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"content-type": "application/json"},
        )
        try:
            with urlopen(req, timeout=10) as resp:
                raw = resp.read()
        except HTTPError as exc:
            # HTTPError is a URLError subclass: a 404 for a missing item used
            # to be reported as "vaultproxy unreachable".
            if exc.code == 404 and method == "GET":
                return {}
            raise RuntimeError(
                f"vaultproxy {method} {self.prefix}/{key} failed: HTTP {exc.code}"
            ) from exc
        except URLError as exc:
            raise RuntimeError(
                f"vaultproxy unreachable at {self.base_url}: {exc.reason}. "
                "Is vaultproxy running, or did you mean YT_MCP_SECRETS=file?"
            ) from exc
        return json.loads(raw) if raw else {}

    def get(self, key: str) -> str | None:
        try:
            return self._request("GET", key).get("value")
        except RuntimeError:
            raise
        except Exception:
            return None

    def set(self, key: str, value: str) -> None:
        self._request("PUT", key, {"value": value})


def get_store() -> SecretStore:
    backend = os.environ.get("YT_MCP_SECRETS", "file")
    if backend == "file":
        return FileStore()
    if backend == "env":
        return EnvStore()
    if backend == "vaultproxy":
        return VaultproxyStore()
    raise ValueError(f"unknown YT_MCP_SECRETS backend: {backend!r} (file|env|vaultproxy)")
