"""Work out the smallest set of HDD operations that brings the backup up to date.

The source scan is compared with the index. Nothing here touches the HDD; the
only disk reads are fingerprints of source files whose status is in doubt.
"""

from collections import Counter, defaultdict


def parent(rel):
    return rel.rpartition("/")[0]


def basename(rel):
    return rel.rpartition("/")[2]


def ancestors(rel):
    """Parent folders of an index path, nearest first."""
    rel = parent(rel)
    while rel:
        yield rel
        rel = parent(rel)


def under(rel, folder):
    return rel == folder or rel.startswith(folder + "/")


class CopyOp:
    __slots__ = ("rel", "size", "mtime_ns", "kind")

    def __init__(self, rel, size, mtime_ns, kind):
        self.rel = rel
        self.size = size
        self.mtime_ns = mtime_ns
        self.kind = kind   # "new" or "changed"


class FileMove:
    __slots__ = ("old", "new", "size", "mtime_ns")

    def __init__(self, old, new, size, mtime_ns):
        self.old = old
        self.new = new
        self.size = size
        self.mtime_ns = mtime_ns


class DirMove:
    """A whole folder renamed in one step. `files` are the moves it stands for."""
    __slots__ = ("old", "new", "files")

    def __init__(self, old, new, files):
        self.old = old
        self.new = new
        self.files = files


class ArchiveDir:
    """A deleted folder, moved to the dated archive in one step."""
    __slots__ = ("rel", "files")

    def __init__(self, rel, files):
        self.rel = rel
        self.files = files   # [(index path, size)]


class Plan:
    def __init__(self):
        self.dir_moves = []
        self.file_moves = []
        self.archive_dirs = []
        self.archive_files = []   # [(index path, size)]
        self.rmdirs = []          # empty folders to remove, deepest first
        self.mkdirs = []          # new folders, parents first
        self.copies = []
        self.retimed = {}         # index path -> new modified time; contents identical
        self.unreadable = {}      # carried over from the scan for reporting

        self.scan_files = 0
        self.scan_bytes = 0
        self.unchanged_files = 0
        self.unchanged_bytes = 0
        self.new_files = 0
        self.new_bytes = 0
        self.changed_files = 0
        self.changed_bytes = 0
        self.moved_files = 0
        self.moved_bytes = 0
        self.deleted_files = 0
        self.deleted_bytes = 0
        self.checked_files = 0    # fingerprinted on the source to settle a doubt
        self.checked_bytes = 0
        self.index_files = 0      # files the index knew before this run
        self.space_needed = 0

    @property
    def copy_bytes(self):
        return self.new_bytes + self.changed_bytes

    @property
    def copy_files(self):
        return len(self.copies)

    @property
    def reorganise_ops(self):
        return (len(self.dir_moves) + len(self.file_moves) + len(self.archive_dirs)
                + len(self.archive_files) + len(self.rmdirs) + len(self.mkdirs))

    @property
    def touches_hdd(self):
        return bool(self.copies or self.reorganise_ops)

    @property
    def is_empty(self):
        return not self.touches_hdd and not self.retimed


def build_plan(scan, manifest, fingerprint=None, on_work=None):
    """Compare a ScanResult with a Manifest and return a Plan.

    `fingerprint(rel)` returns the content fingerprint of a source file, or
    None if it cannot be read. `on_work(files, bytes)` is called once, before
    any fingerprinting, with how much there is to check.
    """
    plan = Plan()
    plan.unreadable = dict(scan.unreadable)
    plan.scan_files = len(scan.files)
    plan.scan_bytes = scan.bytes
    plan.index_files = len(manifest.files)
    index = manifest.files

    # Anything the scan could not read is left exactly as it is on the HDD.
    protected = set(scan.unreadable)

    def is_protected(rel):
        if not protected:
            return False
        return rel in protected or any(a in protected for a in ancestors(rel))

    new, changed, doubtful = [], [], []
    for rel, (size, mtime_ns) in scan.files.items():
        known = index.get(rel)
        if known is None:
            new.append(rel)
        elif known[0] == size and known[1] == mtime_ns:
            plan.unchanged_files += 1
            plan.unchanged_bytes += size
        elif known[0] == size and size == 0:
            plan.retimed[rel] = mtime_ns
        elif known[0] == size and known[2] and fingerprint:
            doubtful.append(rel)
        else:
            changed.append(rel)

    gone = [rel for rel in index if rel not in scan.files and not is_protected(rel)]

    # A new path with the same size and time as a vanished one is probably a move.
    vanished = defaultdict(list)
    for rel in gone:
        size, mtime_ns, digest = index[rel]
        if size and digest:
            vanished[(size, mtime_ns)].append(rel)
    candidates = [rel for rel in new if scan.files[rel] in vanished] if fingerprint else []

    to_check = doubtful + candidates
    plan.checked_files = len(to_check)
    plan.checked_bytes = sum(scan.files[rel][0] for rel in to_check)
    if on_work:
        on_work(plan.checked_files, plan.checked_bytes)

    # Same size, different time: only the fingerprint can tell if it really changed.
    for rel in doubtful:
        size, mtime_ns = scan.files[rel]
        if fingerprint(rel) == index[rel][2]:
            plan.retimed[rel] = mtime_ns
            plan.unchanged_files += 1
            plan.unchanged_bytes += size
        else:
            changed.append(rel)

    moves = {}   # old path -> FileMove
    moved_to = set()
    for rel in candidates:
        key = scan.files[rel]
        digest = fingerprint(rel)
        if not digest:
            continue
        matches = [old for old in vanished[key] if old not in moves and index[old][2] == digest]
        if not matches:
            continue
        matches.sort(key=lambda old: basename(old) != basename(rel))   # same name first
        old = matches[0]
        moves[old] = FileMove(old, rel, key[0], key[1])
        moved_to.add(rel)
        plan.moved_files += 1
        plan.moved_bytes += key[0]

    # Folders as the HDD will have them once folder moves are done.
    dirs = set(manifest.dirs)
    for old, new_dir in _folder_moves(moves, manifest, scan):
        files = [mv for mv in moves.values() if under(mv.old, old)]
        for mv in files:
            del moves[mv.old]
        files.sort(key=lambda mv: mv.old)
        plan.dir_moves.append(DirMove(old, new_dir, files))
        for d in [d for d in dirs if under(d, old)]:
            dirs.discard(d)
            dirs.add(new_dir + d[len(old):])
    plan.file_moves = sorted(moves.values(), key=lambda mv: mv.new)
    moved_from = {mv.old for mv in moves.values()}
    for dm in plan.dir_moves:
        moved_from.update(mv.old for mv in dm.files)

    # Deletions: a vanished folder is archived whole, loose files one by one.
    gone_dirs = {d for d in dirs if d not in scan.dirs and not is_protected(d)}
    by_folder = defaultdict(list)
    for rel in gone:
        if rel in moved_from:
            continue
        size = index[rel][0]
        plan.deleted_files += 1
        plan.deleted_bytes += size
        top = None
        for folder in ancestors(rel):
            if folder in gone_dirs:
                top = folder
        if top is None:
            plan.archive_files.append((rel, size))
        else:
            by_folder[top].append((rel, size))
    plan.archive_files.sort()
    for folder in sorted(by_folder):
        plan.archive_dirs.append(ArchiveDir(folder, sorted(by_folder[folder])))

    # Vanished folders with nothing left in them are simply removed.
    carried = tuple(by_folder)
    plan.rmdirs = sorted((d for d in gone_dirs if not any(under(d, top) for top in carried)),
                         key=lambda d: (-d.count("/"), d))
    plan.mkdirs = sorted(d for d in scan.dirs if d not in dirs and not is_protected(d))

    for rel in new:
        if rel in moved_to:
            continue
        size, mtime_ns = scan.files[rel]
        plan.copies.append(CopyOp(rel, size, mtime_ns, "new"))
        plan.new_files += 1
        plan.new_bytes += size
        plan.space_needed += size
    largest_rewrite = 0
    for rel in changed:
        size, mtime_ns = scan.files[rel]
        plan.copies.append(CopyOp(rel, size, mtime_ns, "changed"))
        plan.changed_files += 1
        plan.changed_bytes += size
        plan.space_needed += max(0, size - index[rel][0])
        largest_rewrite = max(largest_rewrite, size)
    # a changed file is written beside the old copy before replacing it
    plan.space_needed += largest_rewrite
    # folder by folder keeps the HDD's heads in one area at a time
    plan.copies.sort(key=lambda op: (parent(op.rel), op.rel))
    return plan


def _folder_moves(moves, manifest, scan):
    """Folder renames that account for whole groups of file moves.

    A folder counts as moved when it is gone from the source, its new name did
    not exist before, and every file it held moved to the same place under
    the new name. Returns [(old folder, new folder)], outermost first.
    """
    if not moves:
        return []
    seen = Counter()
    for mv in moves.values():
        if basename(mv.old) != basename(mv.new):
            continue
        old, new = parent(mv.old), parent(mv.new)
        while old and new and old != new:
            seen[(old, new)] += 1
            if basename(old) != basename(new):
                break
            old, new = parent(old), parent(new)
    if not seen:
        return []

    wanted = {old for old, _ in seen}
    held = Counter()
    for rel in manifest.files:
        for folder in ancestors(rel):
            if folder in wanted:
                held[folder] += 1

    chosen = []
    for (old, new), count in sorted(seen.items(), key=lambda item: (item[0][0].count("/"), item[0])):
        if count != held[old]:
            continue
        if old not in manifest.dirs or old in scan.dirs:
            continue
        if new not in scan.dirs or new in manifest.dirs:
            continue
        if any(under(old, o) or under(new, n) or under(n, new) for o, n in chosen):
            continue
        chosen.append((old, new))
    return chosen
