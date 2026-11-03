"""splendid-safe: the shared safety library of cipher-studio and jw-studio (stdlib only).

Vendored, never pip-installed: each repo carries a byte-identical copy under <pkg>/_vendor/splendid_safe/ with a VENDOR.sha256
manifest, and a test fails when the copy drifts. Change the library HERE (Forgejo aaron/splendid-safe), then re-vendor with
tools/vendor.py.
"""
__version__ = "1.0.0"
