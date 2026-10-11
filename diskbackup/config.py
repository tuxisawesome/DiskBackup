"""Per-machine settings. Kept on this computer, never on the SSD or the HDD."""

import json
import os

from . import osutil

DEFAULT_EXCLUDES = [
    "$RECYCLE.BIN",
    "System Volume Information",
    ".Trash-*",
    "*.dbktmp",
]


def default_path():
    override = os.environ.get("DISKBACKUP_CONFIG")
    if override:
        return override
    if osutil.IS_WINDOWS:
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        return os.path.join(base, "DiskBackup", "config.json")
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "diskbackup", "config.json")


class Config:
    def __init__(self):
        self.source = ""        # folder to back up (normally the SSD's root)
        self.source_id = ""     # volume ID of the drive it is on
        self.source_sub = ""    # where the folder sits inside that drive
        self.dest = ""          # backup folder on the HDD
        self.dest_id = ""
        self.dest_sub = ""
        self.excludes = list(DEFAULT_EXCLUDES)

    @property
    def ready(self):
        return bool(self.source and self.dest)

    def set_source(self, path):
        self.source, self.source_id, self.source_sub = _describe(path)

    def set_dest(self, path):
        self.dest, self.dest_id, self.dest_sub = _describe(path)


def _describe(path):
    path = os.path.abspath(path)
    vol = osutil.volume_of(path)
    if not vol:
        return path, "", ""
    try:
        sub = os.path.relpath(os.path.realpath(path), os.path.realpath(vol.mount))
    except ValueError:
        sub = ""
    if sub == "." or sub.startswith(".."):
        sub = ""
    return path, vol.id or "", sub


def load(path):
    cfg = Config()
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return cfg
    for key in ("source", "source_id", "source_sub", "dest", "dest_id", "dest_sub"):
        value = data.get(key)
        if isinstance(value, str):
            setattr(cfg, key, value)
    if isinstance(data.get("excludes"), list):
        cfg.excludes = [str(item) for item in data["excludes"]]
    return cfg


def save(cfg, path):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    data = {key: getattr(cfg, key) for key in
            ("source", "source_id", "source_sub", "dest", "dest_id", "dest_sub", "excludes")}
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2)
    os.replace(tmp, path)


def locate(path, want_id, sub):
    """Find a configured folder even if its drive letter or mount point changed.

    Returns (path, status) where status is "ok", "moved" (found elsewhere by
    volume ID), "different" (the path exists but is another drive) or "missing".
    """
    vol = osutil.volume_of(path) if os.path.isdir(path) else None
    if vol and (not want_id or not vol.id or osutil.same_id(vol.id, want_id)):
        return path, "ok"
    if want_id:
        for other in osutil.list_volumes():
            if osutil.same_id(other.id, want_id):
                candidate = os.path.join(other.mount, sub) if sub else other.mount
                if os.path.isdir(candidate):
                    return candidate, "moved"
    if os.path.isdir(path) and vol is None:
        return path, "ok"   # cannot identify drives on this system; trust the path
    return None, "different" if vol else "missing"
