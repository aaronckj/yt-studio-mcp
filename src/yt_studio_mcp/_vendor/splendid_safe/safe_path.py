"""Paths under a fixed root, opened without ever following a link.

One parser (canonical_rel) turns untrusted input into a relative path; one opener (open_under / opendir_under) walks it from the
root one component at a time through directory fds with O_NOFOLLOW, then proves where the handle landed:

  * every component is opened with O_NOFOLLOW relative to the previous directory fd (no path string is re-resolved);
  * the final handle is fstat'ed (regular file, hard-link count) on the fd, never on the name;
  * the kernel's own path for the handle (/proc/self/fd, F_GETPATH, GetFinalPathNameByHandleW) must equal the expected path,
    which catches a directory renamed or swapped mid-walk;
  * the name is re-checked against the handle's inode, and the link count is read again (CIFS can report it late).

Windows has no dir_fd: there the same checks run on the full path (pre-check every component for links / junctions / reparse
points, open, then verify the handle's final path is inside the root). Directory fds (opendir_under) are POSIX only.
"""
from __future__ import annotations

import errno
import os
import re
import stat
import sys
import unicodedata
import urllib.parse

__all__ = ["UnsafePath", "Root", "canonical_rel", "open_under", "opendir_under", "subprocess_path", "pass_fds",
           "final_path", "is_link", "inside"]

FILE_ATTRIBUTE_REPARSE_POINT = 0x400
O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
O_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
O_BINARY = getattr(os, "O_BINARY", 0)
O_NOINHERIT = getattr(os, "O_NOINHERIT", 0)
_DIR_FLAGS = os.O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC
_PCT = re.compile(r"%[0-9A-Fa-f]{2}")
_STRAY_PCT = re.compile(r"%(?![0-9A-Fa-f]{2})")
_ENC_SEP = re.compile(r"%(2[fF]|5[cC])")              # an encoded '/' or '\' is one segment to a router, two to the filesystem
_BIDI = set(range(0x202A, 0x202F)) | set(range(0x2066, 0x206A)) | {0x200E, 0x200F, 0x061C, 0x2028, 0x2029, 0xFEFF}
MAX_NAME = 255
MAX_REL = 4096


class UnsafePath(OSError):
    """A path or handle that failed a safety check (link, hard link, outside the root, not canonical, swapped)."""


def _win() -> bool:
    return sys.platform == "win32"


def _norm(p) -> str:
    p = os.fspath(p)
    if _win():
        if p.startswith("\\\\?\\UNC\\"):
            p = "\\\\" + p[8:]
        elif p.startswith("\\\\?\\"):
            p = p[4:]
        return os.path.normcase(os.path.normpath(p))
    return os.path.normpath(p)


def is_link(path) -> bool:
    """Symlink, junction or any reparse point (Windows); never follows."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return False
    if stat.S_ISLNK(st.st_mode):
        return True
    return bool(getattr(st, "st_file_attributes", 0) & FILE_ATTRIBUTE_REPARSE_POINT)


def inside(path: str, root_real: str) -> bool:
    try:
        return os.path.commonpath([path, root_real]) == root_real
    except ValueError:
        return False


def final_path(fd: int) -> str:
    """The path the open handle really refers to, as the OS sees it now."""
    if sys.platform.startswith("linux"):
        try:
            p = os.readlink("/proc/self/fd/%d" % fd)
        except OSError as e:
            raise UnsafePath("cannot read the opened file's path (%s)" % e.__class__.__name__) from e
        return _norm(p)
    if sys.platform == "darwin":
        import fcntl
        return _norm(fcntl.fcntl(fd, 50, b"\0" * 1024).split(b"\0", 1)[0].decode())     # F_GETPATH
    if _win():
        import ctypes
        import msvcrt
        from ctypes import wintypes
        h = msvcrt.get_osfhandle(fd)
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        fn = k32.GetFinalPathNameByHandleW
        fn.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
        fn.restype = wintypes.DWORD
        buf = ctypes.create_unicode_buffer(32768)
        n = fn(h, buf, 32768, 0)
        if not n or n >= 32768:
            raise UnsafePath("cannot resolve the opened file's final path")
        return _norm(buf.value)
    raise UnsafePath("no way to verify the opened file's path on this OS")


# ---------------------------------------------------------------------------------------------------------------------- parser
def canonical_rel(raw, source: str = "query", *, deny_dot: bool = False):
    """The ONE parser for an untrusted relative path. -> canonical 'a/b/c' (NFC, '/'-separated) or None.

    source="query": the raw, still percent-encoded value (a URL path or an undecoded query value); it is decoded exactly once.
    source="json":  an already-decoded string (JSON body, parse_qs output, a ledger field); it is never decoded.
    Either way a value that still contains %XX afterwards is refused (double encoding is ambiguous: some layer would decode it
    again), and so is an encoded separator (%2F, %5C) in a query value. Refused: non-str, empty, NUL and other control characters, bidi/line-separator controls, backslash, a leading '/',
    empty / '.' / '..' components, components over 255 bytes, over 4096 bytes in total, and dot-names when deny_dot=True."""
    if source not in ("query", "json"):
        raise ValueError("source must be 'query' or 'json'")
    if not isinstance(raw, str) or not raw:
        return None
    s = raw
    if source == "query" and "%" in s:
        if _STRAY_PCT.search(s) or _ENC_SEP.search(s):
            return None
        try:
            s = urllib.parse.unquote(s, encoding="utf-8", errors="strict")
        except UnicodeDecodeError:
            return None
    if _PCT.search(s):
        return None
    for ch in s:
        o = ord(ch)
        if o < 0x20 or 0x7F <= o <= 0x9F or o in _BIDI or 0xD800 <= o <= 0xDFFF:
            return None
    if "\\" in s:
        return None
    s = unicodedata.normalize("NFC", s)
    if s.startswith("/") or len(s.encode("utf-8")) > MAX_REL:
        return None
    parts = s.split("/")
    for p in parts:
        if p in ("", ".", ".."):
            return None
        if deny_dot and p.startswith("."):
            return None
        if len(p.encode("utf-8")) > MAX_NAME:
            return None
    return s


def _parts(rel) -> list:
    c = canonical_rel(rel, source="json")
    if c is None or c != rel:
        raise UnsafePath("not a canonical relative path")
    return c.split("/")


# ------------------------------------------------------------------------------------------------------------------------ root
class Root:
    """A fixed directory everything is opened under. realpath'd once (at startup); a root whose own name is a symlink is refused.

    aliases: other names of the same tree (e.g. /mnt/user/x for /mnt/x on Unraid); canon() maps them onto the root's name."""
    __slots__ = ("path", "aliases")

    def __init__(self, path, aliases=()):
        p = os.fspath(path)
        if not isinstance(p, str) or not p or "\x00" in p or not os.path.isabs(p):
            raise UnsafePath("a root must be an absolute path")
        if os.path.islink(p.rstrip("/\\") or p):
            raise UnsafePath("the root is a symlink: refusing")
        self.path = _norm(os.path.realpath(p))
        self.aliases = tuple(_norm(os.path.realpath(os.fspath(a))) for a in aliases if a)

    def __repr__(self):
        return "Root(%r)" % self.path

    def __eq__(self, other):
        return isinstance(other, Root) and (self.path, self.aliases) == (other.path, other.aliases)

    def __hash__(self):
        return hash((self.path, self.aliases))

    def canon(self, p: str) -> str:
        """p named under an alias -> the same path named under the root (anything else unchanged)."""
        p = os.fspath(p)
        for a in self.aliases:
            if p == a or p.startswith(a + os.sep):
                return self.path + p[len(a):]
        return p

    def contains(self, p: str) -> bool:
        """p (already realpath'd) is strictly inside the tree under any of its names (the root itself is not 'inside')."""
        p = os.fspath(p)
        return any(p.startswith(r + os.sep) for r in (self.path,) + self.aliases)

    def rel(self, p: str):
        """The canonical relative path of absolute p under this root (aliases mapped), or None when it is not inside / not canonical."""
        if not isinstance(p, str) or "\x00" in p or not os.path.isabs(p):
            return None
        p = self.canon(_norm(p))
        if not p.startswith(self.path + os.sep):
            return None
        r = p[len(self.path) + 1:].replace(os.sep, "/")
        return r if canonical_rel(r, source="json") == r else None

    def join(self, rel: str) -> str:
        """Display / logging path of a canonical rel (never open this string: use open_under)."""
        return os.path.join(self.path, *_parts(rel))


def _as_root(root) -> Root:
    if isinstance(root, Root):
        return root
    if type(root).__name__ == "Root" and isinstance(getattr(root, "path", None), str):
        # a Root from another copy of this module (two vendored copies in one process, or a re-import): rebuilt here, so the
        # root is checked again by this copy's rules
        return Root(root.path, getattr(root, "aliases", ()))
    return Root(root)


# ---------------------------------------------------------------------------------------------------------------------- opener
def _open_dir_at(dfd: int, name: str, mkdirs: bool, dir_mode: int) -> int:
    try:
        return os.open(name, _DIR_FLAGS, dir_fd=dfd)
    except FileNotFoundError:
        if not mkdirs:
            raise FileNotFoundError(errno.ENOENT, "folder does not exist", name) from None
    except OSError as e:
        if e.errno in (errno.ELOOP, errno.ENOTDIR, errno.EMLINK):
            raise UnsafePath(errno.ELOOP, "a link or a file where a folder should be: refusing", name) from None
        raise
    try:
        os.mkdir(name, dir_mode, dir_fd=dfd)
    except FileExistsError:
        pass
    return _open_dir_at(dfd, name, False, dir_mode)        # re-open no-follow: a link planted after the mkdir is refused


def _walk(root: Root, parts, mkdirs: bool, dir_mode: int) -> int:
    try:
        dfd = os.open(root.path, _DIR_FLAGS)
    except OSError as e:
        if e.errno in (errno.ELOOP, errno.ENOTDIR):
            raise UnsafePath(errno.ELOOP, "the root is a link or not a folder: refusing", root.path) from None
        raise
    try:
        for c in parts:
            nfd = _open_dir_at(dfd, c, mkdirs, dir_mode)
            os.close(dfd)
            dfd = nfd
    except BaseException:
        os.close(dfd)
        raise
    return dfd


def _check_landed(fd: int, expected: str) -> None:
    got = final_path(fd)
    if got != _norm(expected):
        raise UnsafePath("the opened handle is outside its root or was moved while opening: refusing")


def _check_nlink(st, max_nlink) -> None:
    if max_nlink is not None and stat.S_ISREG(st.st_mode) and not (1 <= st.st_nlink <= max_nlink):
        raise UnsafePath("hard-linked (or unlinked) file: refusing")


def open_under(root, rel: str, flags: int = os.O_RDONLY, *, mode: int = 0o644, mkdirs: bool = False, regular: bool = True,
               max_nlink=1, dir_mode: int = 0o755) -> int:
    """Open root/rel without following any link. -> fd (caller closes).

    rel must already be canonical (canonical_rel(..., "json") returns it unchanged). Missing file / folder -> FileNotFoundError,
    O_EXCL on an existing name -> FileExistsError, every safety failure -> UnsafePath. O_TRUNC is applied only after the checks
    (a hard-linked victim is never truncated). mkdirs creates missing parent folders (dir_mode), each re-opened no-follow."""
    root = _as_root(root)
    parts = _parts(rel)
    if _win():
        return _open_under_win(root, parts, flags, mode, mkdirs, regular, max_nlink)
    trunc = flags & os.O_TRUNC
    oflags = (flags & ~os.O_TRUNC) | O_NOFOLLOW | O_CLOEXEC
    created = bool(flags & os.O_CREAT and flags & os.O_EXCL)
    dfd = _walk(root, parts[:-1], mkdirs, dir_mode)
    try:
        last = parts[-1]
        try:
            fd = os.open(last, oflags, mode, dir_fd=dfd)
        except FileNotFoundError:
            raise FileNotFoundError(errno.ENOENT, "file does not exist", rel) from None
        except FileExistsError:
            raise FileExistsError(errno.EEXIST, "file already exists", rel) from None
        except OSError as e:
            if e.errno in (errno.ELOOP, errno.EMLINK):
                raise UnsafePath(errno.ELOOP, "the file is a link: refusing", rel) from None
            raise
        try:
            st = os.fstat(fd)
            if regular and not stat.S_ISREG(st.st_mode):
                raise UnsafePath("not a regular file: refusing")
            _check_nlink(st, max_nlink)
            _check_landed(fd, os.path.join(root.path, *parts))
            try:
                st2 = os.stat(last, dir_fd=dfd, follow_symlinks=False)
            except FileNotFoundError:
                raise UnsafePath("the file was moved while opening: refusing") from None
            if (st2.st_dev, st2.st_ino) != (st.st_dev, st.st_ino):
                raise UnsafePath("the name no longer points at the opened file: refusing")
            _check_nlink(os.fstat(fd), max_nlink)                       # read again: CIFS reports the count late (flapping)
            if trunc:
                os.ftruncate(fd, 0)
        except BaseException:
            os.close(fd)
            if created:
                try:
                    st3 = os.stat(last, dir_fd=dfd, follow_symlinks=False)
                    if (st3.st_dev, st3.st_ino) == (st.st_dev, st.st_ino):
                        os.unlink(last, dir_fd=dfd)
                except (OSError, NameError):
                    pass
            raise
        return fd
    finally:
        os.close(dfd)


def opendir_under(root, rel: str = "", mkdirs: bool = False, *, dir_mode: int = 0o755) -> int:
    """fd of the folder root/rel ('' = the root itself), walked like open_under. POSIX only."""
    if _win():
        raise UnsafePath("directory handles are not available on Windows")
    root = _as_root(root)
    parts = _parts(rel) if rel != "" else []
    dfd = _walk(root, parts, mkdirs, dir_mode)
    try:
        _check_landed(dfd, os.path.join(root.path, *parts))
    except BaseException:
        os.close(dfd)
        raise
    return dfd


def _open_under_win(root: Root, parts, flags, mode, mkdirs, regular, max_nlink) -> int:
    """Windows: no dir_fd. Pre-check every component for links / junctions, open, then verify the handle's final path (authority)."""
    if is_link(root.path) or not os.path.isdir(root.path):
        raise UnsafePath("the root is missing or a link: refusing")
    cur = root.path
    for c in parts[:-1]:
        cur = os.path.join(cur, c)
        if not os.path.lexists(cur):
            if not mkdirs:
                raise FileNotFoundError(errno.ENOENT, "folder does not exist", c)
            try:
                os.mkdir(cur, 0o755)
            except FileExistsError:
                pass
        if is_link(cur) or not os.path.isdir(cur):
            raise UnsafePath("a link or a file where a folder should be: refusing")
    path = os.path.join(cur, parts[-1])
    if is_link(path):
        raise UnsafePath("the file is a link: refusing")
    trunc = flags & os.O_TRUNC
    created = bool(flags & os.O_CREAT and flags & os.O_EXCL)
    try:
        fd = os.open(path, (flags & ~os.O_TRUNC) | O_BINARY | O_NOINHERIT | O_NOFOLLOW, mode)
    except FileNotFoundError:
        raise FileNotFoundError(errno.ENOENT, "file does not exist", parts[-1]) from None
    except FileExistsError:
        raise FileExistsError(errno.EEXIST, "file already exists", parts[-1]) from None
    try:
        st = os.fstat(fd)
        if regular and not stat.S_ISREG(st.st_mode):
            raise UnsafePath("not a regular file: refusing")
        _check_nlink(st, max_nlink)
        if not inside(final_path(fd), root.path):
            raise UnsafePath("the opened file is outside its root (link or junction swapped in): refusing")
        if trunc:
            os.ftruncate(fd, 0)
    except BaseException:
        os.close(fd)
        if created:
            try:
                os.unlink(path)
            except OSError:
                pass
        raise
    return fd


# ------------------------------------------------------------------------------------------------------------- subprocess paths
def subprocess_path(fd: int) -> str:
    """A path a child process can open to reach exactly this handle (pass the fd with pass_fds(fd))."""
    if not isinstance(fd, int) or fd < 0:
        raise ValueError("fd must be a non-negative int")
    if sys.platform.startswith("linux"):
        return "/proc/self/fd/%d" % fd
    if sys.platform == "darwin":
        return "/dev/fd/%d" % fd
    raise UnsafePath("no fd paths on this OS")


def pass_fds(*fds: int) -> dict:
    """subprocess keyword arguments that hand these fds to the child under the same numbers: subprocess.run(cmd, **pass_fds(fd))."""
    return {"pass_fds": tuple(int(f) for f in fds), "close_fds": True}
