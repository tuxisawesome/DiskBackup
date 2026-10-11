"""The index: a record of what is on the HDD, so the HDD never has to be scanned.

It lives on the HDD itself at <backup folder>/.diskbackup/manifest.json.gz and is
read once at the start of a run and written once at the end.
"""

import gzip
import json
import os
import time

from . import osutil

VERSION = 1
CURRENT = "current"          # the mirror of the SSD
META = ".diskbackup"         # index and logs
INDEX_NAME = "manifest.json.gz"
LOG_DIR = "logs"
HISTORY_LIMIT = 100
LOG_LIMIT = 20


class ManifestError(Exception):
    """The index exists but cannot be read."""


def meta_dir(dest):
    return os.path.join(osutil.fs_path(dest), META)


def index_path(dest):
    return os.path.join(meta_dir(dest), INDEX_NAME)


class Manifest:
    def __init__(self):
        # index path -> [size, modified time in ns, content fingerprint or None]
        self.files = {}
        # every folder that exists under current/
        self.dirs = set()
        # "YYYY-MM-DD" -> {"files": n, "bytes": n} for each dated folder of deleted files
        self.archives = {}
        self.history = []
        self.source_id = None
        # measured on the last run, used to estimate the next one
        self.rate = None
        self.overhead = None
        self.loaded = False   # came from disk, as opposed to a brand new backup
        self.dirty = False

    # ------------------------------------------------------------------ totals

    @property
    def total_bytes(self):
        return sum(entry[0] for entry in self.files.values())

    @property
    def last_run(self):
        return self.history[-1] if self.history else None

    # ------------------------------------------------------------------ load / save

    @classmethod
    def load(cls, dest):
        """Read the index. A missing one gives an empty Manifest."""
        path = index_path(dest)
        failure = None
        for candidate in (path, path + ".bak"):
            try:
                with gzip.open(candidate, "rb") as handle:
                    data = json.loads(handle.read().decode("utf-8"))
            except FileNotFoundError:
                continue
            except (OSError, EOFError, ValueError) as exc:
                failure = exc
                continue
            return cls._from_data(data)
        if failure is not None:
            raise ManifestError("The backup index is damaged (%s)." % failure)
        return cls()

    @classmethod
    def _from_data(cls, data):
        if data.get("version", 0) > VERSION:
            raise ManifestError("The backup index was written by a newer version of DiskBackup.")
        manifest = cls()
        manifest.files = data.get("files", {})
        manifest.dirs = set(data.get("dirs", ()))
        manifest.archives = data.get("archives", {})
        manifest.history = data.get("history", [])
        manifest.source_id = data.get("source_id")
        manifest.rate = data.get("rate")
        manifest.overhead = data.get("overhead")
        manifest.loaded = True
        return manifest

    def save(self, dest):
        """Write the index in one go and flush it; the previous one is kept as .bak."""
        data = {
            "version": VERSION,
            "saved": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "source_id": self.source_id,
            "rate": self.rate,
            "overhead": self.overhead,
            "archives": self.archives,
            "history": self.history[-HISTORY_LIMIT:],
            "dirs": sorted(self.dirs),
            "files": self.files,
        }
        # ensure_ascii keeps odd file names (invalid Unicode) round-trippable
        payload = json.dumps(data, separators=(",", ":"), sort_keys=True).encode("ascii")
        path = index_path(dest)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "wb") as raw:
            with gzip.GzipFile(fileobj=raw, mode="wb", compresslevel=6, mtime=0) as zipped:
                zipped.write(payload)
            raw.flush()
            os.fsync(raw.fileno())
        if os.path.exists(path):
            osutil.replace(path, path + ".bak")
        osutil.replace(tmp, path)
        self.loaded = True
        self.dirty = False

    # ------------------------------------------------------------------ run history and logs

    def add_history(self, entry):
        self.history.append(entry)
        del self.history[:-HISTORY_LIMIT]
        self.dirty = True


def write_log(dest, started, lines):
    """One log file per run, written in a single go. Returns its path."""
    folder = os.path.join(meta_dir(dest), LOG_DIR)
    os.makedirs(folder, exist_ok=True)
    name = time.strftime("%Y-%m-%d_%H%M%S", time.localtime(started)) + ".log"
    path = os.path.join(folder, name)
    with open(path, "w", encoding="utf-8", errors="backslashreplace", newline="\n") as handle:
        handle.write("\n".join(lines) + "\n")
    try:
        logs = sorted(n for n in os.listdir(folder) if n.endswith(".log"))
        for old in logs[:-LOG_LIMIT]:
            os.remove(os.path.join(folder, old))
    except OSError:
        pass
    return path
