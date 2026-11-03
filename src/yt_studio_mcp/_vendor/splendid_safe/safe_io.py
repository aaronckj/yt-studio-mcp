"""State and ledger I/O under a Root: atomic replace, strict JSON reads, locked one-write JSONL appends, fd-to-name copies.

Every name is reached through safe_path.open_under / opendir_under (no link followed, handle verified). Writers never leave a
half-written file under the real name; readers never turn a damaged file into an empty default (StateCorrupt is raised, so a
caller that then writes cannot silently wipe the state). POSIX only (directory fds).
"""
from __future__ import annotations

import errno
import hashlib
import json
import os
import secrets
import socket
import stat
import time

from .safe_path import O_CLOEXEC, O_NOFOLLOW, UnsafePath, _as_root, _parts, open_under, opendir_under

__all__ = ["StateCorrupt", "LockTimeout", "MISSING", "atomic_write", "atomic_write_json", "read_json", "append_jsonl", "copy_into"]

MAX_JSON = 64 << 20
LOCK_TIMEOUT_S = 120.0
LOCK_STALE_S = 60.0          # < LOCK_TIMEOUT_S (a waiter breaks a crashed holder's lock before it gives up), and far above a
#                              real hold (one fsync; an array disk spinning up takes ~10 s on Tower)


class _Missing:
    def __repr__(self):
        return "MISSING"


MISSING = _Missing()


class StateCorrupt(Exception):
    """A state file exists but cannot be trusted (unreadable, not JSON, a link, too big). Never mapped to a default."""

    def __init__(self, rel, cause):
        super().__init__("state file %r cannot be read safely: %s" % (rel, cause))
        self.rel = rel
        self.cause = cause


class LockTimeout(TimeoutError):
    pass


def _split(rel: str):
    parts = _parts(rel)
    return "/".join(parts[:-1]), parts[-1]


def _write_all(fd: int, data: bytes) -> None:
    mv = memoryview(data)
    while mv:
        n = os.write(fd, mv)
        mv = mv[n:]


def _fsync_dir(dfd: int) -> None:
    try:
        os.fsync(dfd)
    except OSError:                                             # CIFS / some FUSE mounts refuse a directory fsync
        pass


def atomic_write(root, rel: str, data, mode: int = 0o644, fsync: bool = True, *, mkdirs: bool = False,
                 dir_mode: int = 0o755) -> None:
    """Replace root/rel atomically: a random O_CREAT|O_EXCL|O_NOFOLLOW temp in the same directory fd, written, fsynced, then
    renameat over the name. data: bytes, str (UTF-8), or a callable writer(fileobj) given a binary file. mode is set exactly
    (fchmod, not filtered by the umask). An existing non-regular target (a folder) is refused; an existing link is replaced,
    never written through."""
    root = _as_root(root)
    parent, name = _split(rel)
    dfd = opendir_under(root, parent, mkdirs, dir_mode=dir_mode)
    tmp = ".%s.%s.tmp" % (name[:64], secrets.token_hex(6))
    try:
        try:
            st = os.stat(name, dir_fd=dfd, follow_symlinks=False)
            if not (stat.S_ISREG(st.st_mode) or stat.S_ISLNK(st.st_mode)):
                raise UnsafePath("the target is not a regular file: refusing")
        except FileNotFoundError:
            pass
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | O_NOFOLLOW | O_CLOEXEC, mode, dir_fd=dfd)
        try:
            try:
                os.fchmod(fd, mode)
            except OSError:                                     # CIFS without unix extensions: the mount decides the mode
                pass
            if callable(data):
                with os.fdopen(os.dup(fd), "wb") as fh:
                    data(fh)
                    fh.flush()
            else:
                _write_all(fd, data.encode("utf-8") if isinstance(data, str) else bytes(data))
            if fsync:
                os.fsync(fd)
        finally:
            os.close(fd)
        os.rename(tmp, name, src_dir_fd=dfd, dst_dir_fd=dfd)
        if fsync:
            _fsync_dir(dfd)
    except BaseException:
        try:
            os.unlink(tmp, dir_fd=dfd)
        except OSError:
            pass
        raise
    finally:
        os.close(dfd)


def atomic_write_json(root, rel: str, obj, *, indent=None, ensure_ascii: bool = False, sort_keys: bool = False,
                      default=None, **kw) -> None:
    """atomic_write of json.dumps(obj). The JSON is fully serialised before anything is opened (a bad object writes nothing)."""
    text = json.dumps(obj, indent=indent, ensure_ascii=ensure_ascii, sort_keys=sort_keys, default=default)
    atomic_write(root, rel, text, **kw)


def read_json(root, rel: str, default=MISSING, *, max_bytes: int = MAX_JSON, max_nlink=1):
    """Parse root/rel. Missing file or folder -> default (FileNotFoundError when no default). Anything else that goes wrong
    (a link, not a regular file, a hard link, an I/O error, too big, not UTF-8, not JSON) -> StateCorrupt, never a default."""
    try:
        fd = open_under(_as_root(root), rel, os.O_RDONLY, max_nlink=max_nlink)
    except FileNotFoundError:
        if default is MISSING:
            raise
        return default
    except (OSError, ValueError) as e:
        raise StateCorrupt(rel, e) from e
    try:
        try:
            size = os.fstat(fd).st_size
            if size > max_bytes:
                raise StateCorrupt(rel, "file is larger than %d bytes" % max_bytes)
            chunks, n = [], 0
            while True:
                b = os.read(fd, 1 << 20)
                if not b:
                    break
                n += len(b)
                if n > max_bytes:
                    raise StateCorrupt(rel, "file is larger than %d bytes" % max_bytes)
                chunks.append(b)
        except OSError as e:
            raise StateCorrupt(rel, e) from e
    finally:
        os.close(fd)
    try:
        return json.loads(b"".join(chunks).decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError) as e:
        raise StateCorrupt(rel, e) from e


# ------------------------------------------------------------------------------------------------------------------- locking
def _lockfile_acquire(dfd: int, name: str, timeout: float, stale: float):
    """O_CREAT|O_EXCL lock file next to the ledger (atomic on the SMB server, unlike flock over CIFS). -> (dev, ino) of ours."""
    deadline = time.monotonic() + timeout
    delay = 0.005
    while True:
        try:
            fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | O_NOFOLLOW | O_CLOEXEC, 0o644, dir_fd=dfd)
        except PermissionError:                                 # SMB: the name is still "delete pending" from the last holder
            if time.monotonic() > deadline:
                raise LockTimeout("lock %s cannot be taken (permission denied for %.0f s)" % (name, timeout)) from None
            time.sleep(delay)
            delay = min(delay * 2, 0.1)
            continue
        except FileExistsError:
            try:
                st = os.stat(name, dir_fd=dfd, follow_symlinks=False)
                if time.time() - st.st_mtime > stale:          # a crashed holder: remove exactly the file we judged stale
                    st2 = os.stat(name, dir_fd=dfd, follow_symlinks=False)
                    if (st2.st_dev, st2.st_ino, st2.st_mtime_ns) == (st.st_dev, st.st_ino, st.st_mtime_ns):
                        os.unlink(name, dir_fd=dfd)
                    continue
            except FileNotFoundError:
                continue
            if time.monotonic() > deadline:
                raise LockTimeout("lock %s is held (waited %.0f s)" % (name, timeout)) from None
            time.sleep(delay)
            delay = min(delay * 2, 0.1)
            continue
        try:
            _write_all(fd, ("%d %s %d\n" % (os.getpid(), socket.gethostname(), int(time.time()))).encode())
            st = os.fstat(fd)
        finally:
            os.close(fd)
        return st.st_dev, st.st_ino


def _lockfile_release(dfd: int, name: str, ident) -> None:
    try:
        st = os.stat(name, dir_fd=dfd, follow_symlinks=False)
        if (st.st_dev, st.st_ino) == ident:
            os.unlink(name, dir_fd=dfd)
    except OSError:
        pass


def _encode_row(row) -> bytes:
    if isinstance(row, (bytes, bytearray)):
        row = bytes(row).decode("utf-8")
    if isinstance(row, str):
        line = row[:-1] if row.endswith("\n") else row
        json.loads(line)                                        # must already be one JSON value
    else:
        line = json.dumps(row, ensure_ascii=False)
    if "\n" in line or "\r" in line:
        raise ValueError("a JSONL row must be one line")
    return (line + "\n").encode("utf-8")


def append_jsonl(root, rel: str, row, lock: str = "flock", *, mode: int = 0o644, fsync: bool = True, mkdirs: bool = False,
                 timeout: float = LOCK_TIMEOUT_S, stale: float = LOCK_STALE_S) -> None:
    """Append one JSON line to root/rel with ONE write() of the full line, under a lock.

    row: a JSON-serialisable value, or an already-encoded single JSON line (str/bytes). lock="flock" (one host, local disk) or
    "lockfile" (an O_EXCL '.<name>.lock' beside the file: the only lock that holds across SMB clients). Refuses a link or a
    hard-linked ledger. A previous torn last line (no trailing newline) is closed off with a newline first, so the new row is
    never glued onto it."""
    if lock not in ("flock", "lockfile"):
        raise ValueError("lock must be 'flock' or 'lockfile'")
    data = _encode_row(row)
    root = _as_root(root)
    parent, name = _split(rel)
    held = None
    dfd = None
    if lock == "lockfile":
        dfd = opendir_under(root, parent, mkdirs)
        held = _lockfile_acquire(dfd, ".%s.lock" % name, timeout, stale)
    try:
        fd = open_under(root, rel, os.O_RDWR | os.O_APPEND | os.O_CREAT, mode=mode, mkdirs=mkdirs)
        try:
            if lock == "flock":
                import fcntl
                deadline = time.monotonic() + timeout
                while True:
                    try:
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except (BlockingIOError, PermissionError):  # CIFS answers a held byte-range lock with EACCES, not EAGAIN
                        if time.monotonic() > deadline:
                            raise LockTimeout("flock on %s is held (waited %.0f s)" % (rel, timeout)) from None
                        time.sleep(0.005)
            size = os.fstat(fd).st_size
            if size and os.pread(fd, 1, size - 1) != b"\n":
                data = b"\n" + data
            n = os.write(fd, data)
            if n != len(data):                                  # never seen on a regular file; finish under the same lock
                _write_all(fd, data[n:])
            if fsync:
                os.fsync(fd)
        finally:
            os.close(fd)                                        # closing drops the flock
    finally:
        if held is not None:
            _lockfile_release(dfd, ".%s.lock" % name, held)
        if dfd is not None:
            os.close(dfd)


# ---------------------------------------------------------------------------------------------------------------------- copy
def copy_into(src_fd: int, root, rel: str, overwrite: bool = False, *, mode: int = 0o644, mkdirs: bool = False,
              fsync: bool = True, chunk: int = 4 << 20):
    """Copy the open src_fd (from offset 0) to root/rel via a temp in the same directory fd. -> (sha256 hex, bytes).

    overwrite=False never replaces an existing name (link(2) is atomic; a filesystem without hard links falls back to
    check-then-rename on the same directory fd). overwrite=True replaces a regular file only."""
    root = _as_root(root)
    parent, name = _split(rel)
    dfd = opendir_under(root, parent, mkdirs)
    tmp = ".%s.%s.tmp" % (name[:64], secrets.token_hex(6))
    h = hashlib.sha256()
    n = 0
    try:
        try:
            st = os.stat(name, dir_fd=dfd, follow_symlinks=False)
            if not overwrite:
                raise FileExistsError(errno.EEXIST, "file already exists", rel)
            if not stat.S_ISREG(st.st_mode):
                raise UnsafePath("the target is not a regular file: refusing")
        except FileNotFoundError:
            pass
        out = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | O_NOFOLLOW | O_CLOEXEC, mode, dir_fd=dfd)
        try:
            off = 0
            while True:
                b = os.pread(src_fd, chunk, off)
                if not b:
                    break
                off += len(b)
                h.update(b)
                _write_all(out, b)
            n = off
            if fsync:
                os.fsync(out)
        finally:
            os.close(out)
        if overwrite:
            os.rename(tmp, name, src_dir_fd=dfd, dst_dir_fd=dfd)
        else:
            try:
                os.link(tmp, name, src_dir_fd=dfd, dst_dir_fd=dfd, follow_symlinks=False)
                os.unlink(tmp, dir_fd=dfd)
            except FileExistsError:
                raise FileExistsError(errno.EEXIST, "file already exists", rel) from None
            except OSError as e:
                if e.errno not in (errno.EPERM, errno.ENOTSUP, errno.EOPNOTSUPP, errno.EXDEV, errno.ENOSYS, errno.EMLINK):
                    raise
                try:
                    os.stat(name, dir_fd=dfd, follow_symlinks=False)
                    raise FileExistsError(errno.EEXIST, "file already exists", rel) from None
                except FileNotFoundError:
                    os.rename(tmp, name, src_dir_fd=dfd, dst_dir_fd=dfd)
        if fsync:
            _fsync_dir(dfd)
    except BaseException:
        try:
            os.unlink(tmp, dir_fd=dfd)
        except OSError:
            pass
        raise
    finally:
        os.close(dfd)
    return h.hexdigest(), n
