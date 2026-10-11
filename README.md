# DiskBackup

Backs up one drive (a fast SSD) to another (a slow HDD) and, after the first run, writes
only what changed. Runs on Windows and Linux with nothing but Python installed.

- A file you edited is the only file rewritten, wherever it sits in the folder tree.
- A file or folder you moved or renamed is renamed on the HDD, not copied again.
- A file you deleted is moved into a folder named after that day's date, not destroyed.
- If nothing changed, nothing at all is written to the HDD.

## Running it

Needs Python 3.8 or newer. There is nothing to install.

| | |
|---|---|
| Windows | double-click `backup.bat` |
| Linux | `./backup.sh` |
| Either | `python -m diskbackup` from this folder |

The first start asks which drive to back up and where the backup should go. After that you
get the menu:

| Key | |
|---|---|
| 1 | **Back up now.** Scans the SSD, shows what would change, and waits for Enter before writing. |
| 2 | **Preview changes.** The same scan and report, but it never writes. |
| 3 | **History.** Past runs: how long, how much was written, how much rewriting was avoided. |
| 4 | **Archived deletions.** The dated folders and their sizes; delete the ones you no longer want. |
| 5 | **Settings.** Change the drives or the exclusion patterns. |
| 6 | **Rebuild index.** Only for a backup made some other way, or after editing the HDD by hand. |

While a backup runs: `P` pauses and resumes, `S` (or Ctrl+C) stops once the current file is
finished, and a second `S` stops immediately. A stopped backup carries on from where it was
the next time you run it.

Options: `--ascii` for plain characters, `--no-color`, `--config FILE` for a different
settings file.

## What ends up on the HDD

```
<backup folder>/
    current/         a plain copy of the SSD as of the last backup
    2026-10-11/      files deleted from the SSD, noticed by the backup on that date,
    2026-10-18/      each still at its original folder path
    .diskbackup/     the index (manifest.json.gz) and a log per run
```

Everything is ordinary files and folders. To restore, copy what you need out of `current/`
(or out of a dated folder) with any file manager. DiskBackup is not needed for that.

A changed file replaces the old copy in `current/`; earlier versions are not kept.

## How it keeps drive wear down

**The HDD is never scanned.** An index on the HDD records the size, modified time and a
content fingerprint of every backed-up file. Each run reads that one file, compares it with
a scan of the SSD, and from then on touches the HDD only to make actual changes.

On the HDD:
- Changes are applied in one burst, folder by folder, after the scan and preview are done.
- Moves, renames and archived deletions are renames. A whole folder is a single rename.
- Large files are preallocated on Windows so they are written in one piece.
- Files are written in 8 MiB sequential chunks and flushed once, at the end.
- A new copy is written beside the old one and swapped in, so a crash never leaves a
  half-written file in place of a good one.

On the SSD:
- Nothing is ever written to it. Settings live on the computer, the index and logs on the HDD.
- The scan reads folder listings only. Files are opened only if they are new, changed, or
  need checking.
- On Linux, files and folders are opened without updating their access times.

**Fingerprints.** When a file has the same size as before but a different date, the SSD copy
is read and its fingerprint compared with the index. If the contents are identical, nothing
is written. The same check confirms a suspected move before the HDD copy is renamed. These
reads cost time but no wear, and they are what makes the program work on exFAT, where file
IDs are not stable and Windows and Linux can report timestamps differently.

## Safety checks

- Each drive is recognised by its volume ID, not just its letter or mount point. If the
  letter now belongs to another drive, the right one is looked for and otherwise the run
  is refused.
- An empty source never empties the backup: the run stops instead.
- If more than a fifth of the backup would be archived as deleted, you have to type `yes`.
- A backup that would not fit is refused before anything is written.
- Files that are in use or unreadable are skipped, listed at the end, and tried again next
  time. A folder that cannot be read is left exactly as it is in the backup.
- Symlinks and junctions are not followed.

## Exclusions

`$RECYCLE.BIN`, `System Volume Information` and `.Trash-*` are excluded to begin with.
Add your own under Settings:

- `*.tmp` or `node_modules`: a name without a slash matches a file or folder anywhere.
- `Games/Cache`: with a slash it is a path from the top of the drive.

Case is ignored. If you exclude something that was already backed up, the next run moves
the backed-up copy into that day's dated folder.

## First run on Windows

The program was developed and tested on Linux. The parts that only exist on Windows (key
presses, long paths, file attributes, preallocation, volume IDs, console colours) could not
be run there, so check them once before trusting it with the real drives:

1. Make a small test folder with a few files and subfolders, and an empty folder on the HDD.
2. Start `backup.bat` and choose those two folders by typing their paths.
3. Back up. Check the colours and boxes look right, and that `P` and `S` respond.
4. Edit one file, move another, delete a third, and back up again. The preview should list
   one changed, one moved and one deleted, and the deleted file should be in the dated folder.
5. Then point Settings at the real drives.

If the boxes show as question marks or squares, run `backup.bat --ascii`.

## Limits

- A file whose contents change while its size and modified time both stay the same is not
  noticed. Almost no software does this.
- Permissions (ACLs) and alternate data streams are not copied. exFAT has neither.
- OneDrive-style placeholder files are skipped along with other links.
- Unplug the HDD only after the summary screen. On Windows use "Safely remove hardware".

## Tests

```
python -m unittest discover -s tests -t .
```

The suite builds throwaway folders and checks, among other things, that an unchanged
backup performs no operations, that one edited file causes exactly one write, and that
moves and deletions are renames rather than copies.

## Layout of the code

| | |
|---|---|
| `scanner.py` | walks the source; fingerprints |
| `manifest.py` | the index and logs |
| `planner.py` | compares scan and index, decides the HDD operations |
| `executor.py` | carries them out |
| `rebuild.py` | recreates the index from the HDD |
| `progress.py` | speed and time-remaining estimates |
| `term.py` | colours, panels, bars, live redraw |
| `ui.py` | the screens |
| `osutil.py` | everything that differs between Windows and Linux |
| `config.py` | settings |
