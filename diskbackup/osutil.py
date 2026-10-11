"""Per-OS pieces: paths, volume identity, file attributes, key polling, console setup.

Everything that differs between Windows and Linux lives here so the rest of the
program is the same code on both.
"""

import contextlib
import os
import re
import shutil
import stat
import sys

IS_WINDOWS = os.name == "nt"

_O_BINARY = getattr(os, "O_BINARY", 0)
_O_SEQUENTIAL = getattr(os, "O_SEQUENTIAL", 0)
_O_NOATIME = getattr(os, "O_NOATIME", 0)
_O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)

_FILE_ATTRIBUTE_READONLY = 0x1
_FILE_ATTRIBUTE_HIDDEN = 0x2
_FILE_ATTRIBUTE_SYSTEM = 0x4
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400
_KEPT_ATTRIBUTES = _FILE_ATTRIBUTE_READONLY | _FILE_ATTRIBUTE_HIDDEN | _FILE_ATTRIBUTE_SYSTEM


# --------------------------------------------------------------------------- paths

def fs_path(path):
    """Absolute path for OS calls. On Windows this lifts the 260 character limit."""
    path = os.path.abspath(path)
    if IS_WINDOWS and not path.startswith("\\\\?\\"):
        if path.startswith("\\\\"):
            return "\\\\?\\UNC\\" + path[2:]
        return "\\\\?\\" + path
    return path


def join(root, rel):
    """Join a root with an index path (which always uses forward slashes)."""
    if not rel:
        return root
    return os.path.join(root, rel.replace("/", os.sep))


def is_inside(child, parent):
    """True if `child` is `parent` or lies somewhere below it."""
    child = os.path.normcase(os.path.realpath(child))
    parent = os.path.normcase(os.path.realpath(parent))
    try:
        return os.path.commonpath([child, parent]) == parent
    except ValueError:  # different drives
        return False


def same_file(a, b):
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


# --------------------------------------------------------------------------- reading

def open_read(path):
    """Open a file for sequential reading without touching its access time."""
    flags = os.O_RDONLY | _O_BINARY | _O_SEQUENTIAL
    if _O_NOATIME:
        try:
            return os.open(path, flags | _O_NOATIME)
        except PermissionError:
            pass  # only the owner may use O_NOATIME; a plain open decides if it is readable
    return os.open(path, flags)


@contextlib.contextmanager
def list_dir(path):
    """Yield an os.scandir iterator, avoiding an access-time write where the OS allows."""
    fd = None
    if _O_NOATIME and os.scandir in os.supports_fd:
        try:
            fd = os.open(path, os.O_RDONLY | _O_DIRECTORY | _O_NOATIME)
        except PermissionError:
            fd = None
    try:
        with os.scandir(path if fd is None else fd) as entries:
            yield entries
    finally:
        if fd is not None:
            os.close(fd)


def is_link(st):
    """Symlinks, junctions and other redirections, from an lstat-style result."""
    if stat.S_ISLNK(st.st_mode):
        return True
    return bool(getattr(st, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT)


# --------------------------------------------------------------------------- writing

def open_write(path):
    return os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | _O_BINARY, 0o666)


_kernel32 = None


def _k32():
    global _kernel32
    if _kernel32 is None:
        import ctypes
        _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    return _kernel32


def preallocate(fd, size):
    """Reserve room for a file about to be written so it lands in one piece.

    Windows only. It sets the allocation size, not the length, so nothing is
    zero-filled and an interrupted copy leaves no oversized file behind.
    """
    if not IS_WINDOWS:
        return
    try:
        import ctypes
        import msvcrt
        from ctypes import wintypes
        func = _k32().SetFileInformationByHandle
        func.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        func.restype = wintypes.BOOL
        info = ctypes.c_longlong(size)
        func(msvcrt.get_osfhandle(fd), 5, ctypes.byref(info), ctypes.sizeof(info))  # FileAllocationInfo
    except Exception:
        pass  # purely an optimisation


def copy_attributes(src_stat, dst):
    """Carry read-only / hidden / system flags over (Windows only)."""
    attrs = getattr(src_stat, "st_file_attributes", 0) & _KEPT_ATTRIBUTES
    if not IS_WINDOWS or not attrs:
        return
    try:
        import ctypes
        from ctypes import wintypes
        func = _k32().SetFileAttributesW
        func.argtypes = [wintypes.LPCWSTR, wintypes.DWORD]
        func.restype = wintypes.BOOL
        func(dst, attrs)
    except Exception:
        pass


def make_writable(path):
    try:
        os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
    except OSError:
        pass


def replace(src, dst):
    """os.replace that also gets past a read-only destination on Windows."""
    try:
        os.replace(src, dst)
    except PermissionError:
        if not IS_WINDOWS:
            raise
        make_writable(dst)
        os.replace(src, dst)


def remove_tree(path):
    def retry(func, target, _exc):
        make_writable(target)
        func(target)

    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=retry)
    else:
        shutil.rmtree(path, onerror=retry)


def flush_disks():
    """Push everything still in memory out to the drives (one batch, at the very end)."""
    if hasattr(os, "sync"):
        os.sync()


# --------------------------------------------------------------------------- volumes

class Volume:
    """A mounted drive: where it is, and an ID that survives a drive-letter change."""

    def __init__(self, mount, vol_id=None, label="", fstype="", device=""):
        self.mount = mount
        self.id = vol_id
        self.label = label
        self.fstype = fstype
        self.device = device

    def __repr__(self):
        return "Volume(%r, id=%r, label=%r, fstype=%r)" % (self.mount, self.id, self.label, self.fstype)


def same_id(a, b):
    """Compare volume IDs across OSes (Windows shows only the low half of an NTFS serial)."""
    if not a or not b:
        return False
    a = a.replace("-", "").upper()
    b = b.replace("-", "").upper()
    if a == b:
        return True
    short, long_ = sorted((a, b), key=len)
    return len(short) >= 8 and long_.endswith(short)


def _win_volume(root):
    import ctypes
    from ctypes import wintypes
    k32 = _k32()
    label = ctypes.create_unicode_buffer(261)
    fstype = ctypes.create_unicode_buffer(261)
    serial = wintypes.DWORD(0)
    old_mode = k32.SetErrorMode(1)  # no "insert a disk" pop-ups for empty card readers
    try:
        ok = k32.GetVolumeInformationW(ctypes.c_wchar_p(root), label, 261, ctypes.byref(serial),
                                       None, None, fstype, 261)
    finally:
        k32.SetErrorMode(old_mode)
    if not ok:
        return None
    vol_id = "%04X-%04X" % (serial.value >> 16, serial.value & 0xFFFF)
    return Volume(root, vol_id, label.value, fstype.value, root)


def _win_root(path):
    drive = os.path.splitdrive(os.path.abspath(path))[0]
    return drive + "\\" if drive else None


def _unescape_mount(text):
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), text)


def _linux_mounts():
    """[(mount point, filesystem type, device)] from the kernel's mount table."""
    mounts = []
    try:
        with open("/proc/self/mountinfo", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                left, sep, right = line.rstrip("\n").partition(" - ")
                fields, tail = left.split(" "), right.split(" ")
                if not sep or len(fields) < 5 or len(tail) < 2:
                    continue
                mounts.append((_unescape_mount(fields[4]), tail[0], _unescape_mount(tail[1])))
    except OSError:
        pass
    return mounts


def _linux_names(kind):
    """Map real device path -> UUID or label, from /dev/disk/by-uuid or by-label."""
    names = {}
    base = "/dev/disk/by-" + kind
    try:
        for name in os.listdir(base):
            names[os.path.realpath(os.path.join(base, name))] = name
    except OSError:
        pass
    return names


def _linux_volume(mount, fstype, device, uuids, labels):
    real = os.path.realpath(device) if device.startswith("/") else device
    label = labels.get(real, "")
    label = re.sub(r"\\x([0-9a-fA-F]{2})", lambda m: chr(int(m.group(1), 16)), label)
    return Volume(mount, uuids.get(real), label, fstype, device)


def volume_of(path):
    """The volume a path lives on, or None if it cannot be worked out."""
    try:
        if IS_WINDOWS:
            root = _win_root(path)
            return _win_volume(root) if root else None
        real = os.path.realpath(path)
        best = None
        for mount, fstype, device in _linux_mounts():
            if real == mount or real.startswith(mount.rstrip("/") + "/"):
                if best is None or len(mount) >= len(best[0]):
                    best = (mount, fstype, device)
        if best is None:
            return None
        return _linux_volume(best[0], best[1], best[2], _linux_names("uuid"), _linux_names("label"))
    except Exception:
        return None


def list_volumes():
    """Mounted drives a backup could sensibly use, removable ones included."""
    volumes = []
    try:
        if IS_WINDOWS:
            from ctypes import wintypes
            k32 = _k32()
            k32.GetDriveTypeW.argtypes = [wintypes.LPCWSTR]
            mask = k32.GetLogicalDrives()
            for i in range(26):
                if not mask >> i & 1:
                    continue
                root = "%s:\\" % chr(65 + i)
                if k32.GetDriveTypeW(root) not in (2, 3):  # removable, fixed
                    continue
                vol = _win_volume(root)
                if vol:
                    volumes.append(vol)
            return volumes
        uuids, labels = _linux_names("uuid"), _linux_names("label")
        seen = set()
        for mount, fstype, device in _linux_mounts():
            if not device.startswith("/dev/") or mount in seen:
                continue
            if fstype in ("squashfs", "vfat") and not mount.startswith(("/media", "/mnt", "/run/media")):
                continue  # snaps and the EFI partition
            if mount.startswith(("/boot", "/snap", "/var/lib")):
                continue
            seen.add(mount)
            volumes.append(_linux_volume(mount, fstype, device, uuids, labels))
    except Exception:
        pass
    return volumes


def disk_usage(path):
    """(total, used, free) in bytes, or None."""
    try:
        return shutil.disk_usage(path)
    except OSError:
        return None


# --------------------------------------------------------------------------- console

def enable_ansi():
    """Make the console understand colour and cursor codes. True if it does."""
    if not IS_WINDOWS:
        return os.environ.get("TERM", "") not in ("", "dumb")
    try:
        import ctypes
        from ctypes import wintypes
        k32 = _k32()
        k32.GetStdHandle.restype = wintypes.HANDLE
        handle = k32.GetStdHandle(-11)
        mode = wintypes.DWORD()
        if not k32.GetConsoleMode(wintypes.HANDLE(handle), ctypes.byref(mode)):
            return False
        if mode.value & 0x0004:
            return True
        return bool(k32.SetConsoleMode(wintypes.HANDLE(handle), mode.value | 0x0004))
    except Exception:
        return False


class KeyReader:
    """Single key presses without waiting, for pause/stop during a run."""

    def __init__(self):
        self.active = False
        self._fd = None
        self._saved = None

    def __enter__(self):
        try:
            if not sys.stdin.isatty():
                return self
        except (AttributeError, ValueError):
            return self
        if IS_WINDOWS:
            self.active = True
            return self
        try:
            import termios
            import tty
            self._fd = sys.stdin.fileno()
            self._saved = termios.tcgetattr(self._fd)
            tty.setcbreak(self._fd)
            self.active = True
        except Exception:
            self._saved = None
        return self

    def __exit__(self, *exc):
        if self._saved is not None:
            import termios
            termios.tcsetattr(self._fd, termios.TCSADRAIN, self._saved)
            self._saved = None
        self.active = False

    def poll(self):
        """The key pressed since the last call, lower-cased, or None."""
        if not self.active:
            return None
        if IS_WINDOWS:
            import msvcrt
            if not msvcrt.kbhit():
                return None
            ch = msvcrt.getwch()
            if ch in ("\x00", "\xe0"):  # arrow / function key: swallow its second half
                msvcrt.getwch()
                return None
            return ch.lower()
        import select
        ready, _, _ = select.select([self._fd], [], [], 0)
        if not ready:
            return None
        data = os.read(self._fd, 64)
        if not data or data.startswith(b"\x1b"):
            return None
        return data[:1].decode("ascii", "ignore").lower() or None
