"""src/yt_studio_mcp/_vendor/splendid_safe: a byte-identical copy of Forgejo aaron/splendid-safe,
pinned by src/yt_studio_mcp/_vendor/VENDOR.sha256. Change it upstream and re-vendor with
splendid-safe tools/vendor.py --repo <this> --manifest src/yt_studio_mcp/_vendor/VENDOR.sha256
--lib src/yt_studio_mcp/_vendor/splendid_safe."""
import hashlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "src/yt_studio_mcp/_vendor/VENDOR.sha256"
LIB = "src/yt_studio_mcp/_vendor/splendid_safe"


def _manifest() -> dict:
    rows = {}
    for line in MANIFEST.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            h, _, p = line.partition("  ")
            rows[p] = h
    return rows


def test_every_vendored_file_matches_the_manifest():
    def sha(p):
        return hashlib.sha256((ROOT / p).read_bytes()).hexdigest()

    drift = [p for p, h in _manifest().items() if not (ROOT / p).is_file() or sha(p) != h]
    assert not drift, f"vendored files changed (edit upstream in splendid-safe): {drift}"


def test_no_unlisted_file_in_the_vendored_package():
    rows = _manifest()
    for f in (ROOT / LIB).iterdir():
        if f.name != "__pycache__":
            assert f"{LIB}/{f.name}" in rows, f"unlisted file in {LIB}: {f.name}"
