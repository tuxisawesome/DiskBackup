"""Reading the source drive: the folder walk and content fingerprints.

Nothing in here writes. On Linux, folders and files are opened in a way that
does not update their access times either.
"""

import fnmatch
import hashlib
import os
import re
import stat

from . import osutil

CHUNK = 8 * 1024 * 1024


class Excluder:
    """Decides which names are left out of the backup.

    A pattern without a slash is matched against every file and folder name
    ("*.tmp", "node_modules"). A pattern with a slash is matched against the
    path from the top of the drive ("Games/Cache"). Case is ignored.
    """

    def __init__(self, patterns):
        names, paths = [], []
        for pattern in patterns:
            pattern = pattern.strip().replace("\\", "/").strip("/").lower()
            if pattern:
                (paths if "/" in pattern else names).append(fnmatch.translate(pattern))
        self._name = re.compile("|".join(names)).match if names else None
        self._path = re.compile("|".join(paths)).match if paths else None

    def __call__(self, name, rel):
        if self._name and self._name(name.lower()):
            return True
        return bool(self._path and self._path(rel.lower()))


class ScanResult:
    def __init__(self):
        self.files = {}        # index path -> (size, modified time in ns)
        self.dirs = set()
        self.bytes = 0
        self.unreadable = {}   # index path -> reason; contents unknown, so left alone
        self.links = 0         # symlinks, junctions and special files, not followed
        self.excluded = 0


def reason(exc):
    """A short human explanation of an OS error."""
    if isinstance(exc, FileNotFoundError):
        return "no longer exists"
    if isinstance(exc, PermissionError):
        return "in use or access denied"
    return exc.strerror or str(exc)


def scan(root, excludes=(), on_progress=None):
    """Walk `root` and return every file's size and modified time.

    `on_progress(result, folder)` is called once per folder.
    An unreadable top folder raises OSError; anything unreadable below it is
    recorded in result.unreadable.
    """
    root = osutil.fs_path(root)
    excluded = Excluder(excludes)
    result = ScanResult()
    stack = [("", root)]
    while stack:
        rel_dir, path = stack.pop()
        try:
            with osutil.list_dir(path) as entries:
                for entry in entries:
                    name = entry.name
                    rel = rel_dir + "/" + name if rel_dir else name
                    if excluded(name, rel):
                        result.excluded += 1
                        continue
                    try:
                        st = entry.stat(follow_symlinks=False)
                    except OSError as exc:
                        result.unreadable[rel] = reason(exc)
                        continue
                    if osutil.is_link(st):
                        result.links += 1
                    elif stat.S_ISDIR(st.st_mode):
                        result.dirs.add(rel)
                        stack.append((rel, os.path.join(path, name)))
                    elif stat.S_ISREG(st.st_mode):
                        result.files[rel] = (st.st_size, st.st_mtime_ns)
                        result.bytes += st.st_size
                    else:
                        result.links += 1
        except OSError as exc:
            if not rel_dir:
                raise
            result.unreadable[rel_dir] = reason(exc)
        if on_progress:
            on_progress(result, rel_dir)
    return result


def fingerprint(path, on_bytes=None, buf=None):
    """BLAKE2 fingerprint of a file's contents, as hex."""
    buf = buf if buf is not None else bytearray(CHUNK)
    view = memoryview(buf)
    digest = hashlib.blake2b(digest_size=16)
    with os.fdopen(osutil.open_read(path), "rb", buffering=0) as handle:
        while True:
            count = handle.readinto(buf)
            if not count:
                break
            digest.update(view[:count])
            if on_bytes:
                on_bytes(count)
    return digest.hexdigest()
