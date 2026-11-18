# iPhone Photos Extractor

Extract **photos and videos 1:1** from an **unencrypted** Apple iPhone (Finder/iTunes) backup,
preserving **original filenames**, **real creation/modification dates**, and **EXIF metadata** —
bit-identical raw copies, no re-encoding, no quality loss.

Built in clean, auditable, dependency-light Python (stdlib only — the progress bar is built in, no
third-party packages). Independent tool; the algorithm follows the approach pioneered by an existing
open-source iPhone-backup photo extractor, rebuilt and improved.

---

## Why this tool

iPhone backs up photos as raw bytes under the `CameraRollDomain` of the backup manifest. The
original filename (`IMG_1234.HEIC`) lives in the DCIM path, and the metadata (EXIF, date taken)
lives *inside* the file itself. So a true 1:1 extraction is just: **copy the bytes + restore the
file's real Birth & LastModified timestamps**. That's exactly what this does.

## Improvements over the original extractor

| Capability | Original | This tool |
|---|---|---|
| Copy parallelism | Serial (one file at a time) | **Parallel** via `ProcessPoolExecutor` (`--workers`) |
| Sparse handling | n/a | **Race-safe SHA-256 dedupe** with atomic `O_CREAT\|O_EXCL` markers |
| Resumable | No | **Incremental** — never re-copies or overwrites an existing identical file |
| iCloud real filenames | Joins asset DB | **Joins `ZASSET` → `ZADDITIONALASSETATTRIBUTES.ZORIGINALFILENAME`** (iOS 18+) with fallbacks to `ZEXTENDEDATTRIBUTES` bplist and `ZFILENAME` |
| Album organization | `--albums` | Same, via **dynamic discovery** of Apple's CoreData junction table (name changes per iOS) |
| Deleted photos | `--add-trash` | Same, from `ZTRASHEDSTATE` |
| Date folders | `--format ym/ymd/flat` | Same |
| Birth *and* LastModified | Restores via `SetFile` | **Both** on macOS using `/usr/bin/SetFile`, plus `utime` fallback |

---

## Requirements

* Python 3.8+ (stdlib only; the terminal progress bar is built in — no third-party packages)
* An **unencrypted** iPhone backup on your Mac, or a backup you can create
* On the machine running this: read access to the backup dir and, for exact creation
  dates on macOS, the `/usr/bin/SetFile` helper (built-in).

> **Encryption caveat:** this tool reads an **unencrypted** backup only. If you made the backup
> with "Encrypt local backup" checked in Finder, it will **not** work — re-create it unencrypted.
> (An unencrypted backup does **not** contain your Apple ID password, Health data, or Keychain.)

---

## Troubleshooting: "Operation not permitted"

On modern macOS the backup folder sits behind **TCC** (Transparency, Consent, Control). A shell
without Full Disk Access is refused by the OS:

```
Cannot read /Users/<you>/Library/Application Support/MobileSync/Backup

macOS is blocking access to the backup folder (Operation not permitted).
```

This is a privacy gate, not a broken install and not a normal Unix permission problem. The
diagnostic signature is distinctive: `stat` on the folder **succeeds** (metadata readable, owner
correct, mode `drwxr-xr-x`) while listing it fails. A real permission problem would deny the `stat`
too.

**Fix:** System Settings > Privacy & Security > **Full Disk Access** > add the app you run the
command from (Terminal, iTerm2, your IDE), then **fully quit and reopen that app** — the grant is
captured at process launch, so a new tab in the running terminal will keep failing.

The tool tells these two states apart deliberately. "No backups found" means the folder was read
and held no backups; "Cannot read" means it was never readable. If you see the first when you know
a backup exists, check Full Disk Access before anything else.

---

## Usage

```bash
# Interactive: no arguments — it discovers your backups, lets you pick one
# (locked/encrypted devices are shown but can't be selected), suggests an output
# folder, previews, and confirms before copying.
python3 iphone_photos_extractor.py

# Scan only (no files copied)
python3 iphone_photos_extractor.py --backup ~/Library/Application\ Support/MobileSync/Backup/<DEVICE> --dry-run

# Full extract into date folders (YYYY-MM)
python3 iphone_photos_extractor.py --backup "<backup dir>" -o ~/Pictures/iPhone/ --format ym

# Organize into your real photo albums
python3 iphone_photos_extractor.py --backup "<backup dir>" -o ~/Pictures/iPhone/ --albums

# Only videos
python3 iphone_photos_extractor.py --backup "<backup dir>" -o ~/Pictures/iPhone/ --type video

# Only items from the last month
python3 iphone_photos_extractor.py --backup "<backup dir>" -o ~/Pictures/iPhone/ --since last-month

# Include deleted-photo recovery, rename files with date prefix
python3 iphone_photos_extractor.py --backup "<backup dir>" -o ~/Pictures/iPhone/ --add-trash --prepend-date
```

### Options

| Flag | Description |
|---|---|
| `--backup <dir>` | iOS backup directory (contains `Manifest.db`) — omit to use interactive mode |
| `-o, --output <dir>` | Output directory — **required unless `--dry-run`** (or interactive mode) |
| `--interactive` | Force the interactive backup selector even if `--backup` is given |
| `--non-interactive` | Disable the auto interactive wizard (for scripts/CI) |
| `--format {ym,ymd,flat}` | Folder structure by date (default `ym`) |
| `--since <date>` | Only files since `YYYY-MM-DD`, `last-week`, or `last-month` |
| `--type {photo,video,audio,all}` | Filter by media type (default `all`) |
| `--albums` | Organize into your real user photo albums |
| `--add-trash` | Also extract items marked deleted (suffix `_DELETED`) |
| `--ignore-icloud-media` | Skip media sourced from iCloud |
| `--no-infer-sibling-date` | Don't infer an undated asset's date from a same-stem (Live Photo) sibling (**default on**) |
| `--prepend-date` | Prepend creation date (`YYYY-MM-DD_`) to each filename |
| `--move` | Move files out of the backup (frees disk) instead of copy |
| `--dedupe` | Skip identical content (SHA-256, race-safe) |
| `--workers N` | Parallel copy workers (default: CPU count) |
| `--dry-run` | Scan and report only — never copies, in the plain CLI **and** the interactive wizard; does not require `-o` |

### Interactive mode (run with no arguments)

`python3 iphone_photos_extractor.py` with **no `--backup`** (and a real terminal) launches an
on-screen wizard:

1. **Discovers** your Finder/iTunes backups in the default `MobileSync/Backup` folder.
2. Shows a **numbered selector with friendly device names** (e.g. "Stanley's iPhone", model, last
   backup date) — **locked/encrypted backups are listed but can't be selected**. If there's exactly
   one unlocked backup it's picked automatically.
3. **Suggests an output folder** in your home dir (`~/Pictures/iPhone/<device name>/`) — accept or
   type another.
4. Asks a few **options** (date format, albums, deleted recovery, date prefix).
5. **Previews** a dry-run summary (file count, size, folder breakdown), then **confirms** before
   copying.

`--interactive` forces the wizard even when `--backup` is given; `--non-interactive` turns it off
(required for scripts/CI). The wizard is **stdlib-only** (plain prompt menus).

---

## How it works

1. **Scan** the backup `Manifest.db`'s `Files` table for the `CameraRollDomain`, keeping every
   `IMG_*`/`IMD_*`/etc. file that has a media extension and lives under `DCIM` or `PhotoData/CPLAssets`.
2. **Recover real names** for iCloud-origin assets (stored as UUIDs) from `Photos.sqlite`, trying the
   newest schema (`ZADDITIONALASSETATTRIBUTES.ZORIGINALFILENAME`, iOS 18+), then the older
   `ZEXTENDEDATTRIBUTES` bplist (`com.apple.assetsd.originalFilename`), then `ZFILENAME` as a fallback.
3. **Organize** into user albums (via dynamic discovery of the CoreData album↔asset junction table)
   or `YYYY-MM`/`YYYY-MM-DD` folders by the file's real LastModified/Birth. An asset with **no
   usable date** (common for the `.mov` half of a HEIC+MOV **Live Photo**) first **inherits the
   timestamp of its same-stem sibling**, so the pair is classified together instead of being
   split into `No_Date`. Truly-unpaired files still fall back to `No_Date` (never guessed).
4. **Restore dates** by parsing the manifest's binary-plist `Birth`/`LastModified` and applying them
   (`/usr/bin/SetFile` on macOS, `utime` fallback).
5. **Copy in parallel**, with atomic SHA-256 dedupe and full incremental resume.

---

## Changelog

The full, dated, versioned release history lives in **[CHANGELOG.md](CHANGELOG.md)** (Keep a
Changelog / SemVer). The current release is **v1.3.1**. Check the version of an installed copy with `python3 iphone_photos_extractor.py --version`.

### v1.3.1
- **`--dry-run` no longer extracts.** The interactive wizard ran its preview and then asked
  "Extract now?", so a documented scan-only flag could copy files to disk on a default-yes prompt.
  `--dry-run` now scans and stops, in wizard mode exactly as on the plain CLI path, and no longer
  requires an output folder.
- **Clear message when macOS blocks the backup folder.** Reading `MobileSync/Backup` without Full
  Disk Access raised `PermissionError: [Errno 1] Operation not permitted` and crashed with a
  traceback out of `find_backups()`. Discovery now treats an unreadable folder as "none found",
  and the wizard distinguishes *empty* from *unreadable*, reporting the privacy gate and its fix
  instead of telling you to connect a phone that is already backed up. New `backup_dir_readable()`
  helper. See [Troubleshooting](#troubleshooting-operation-not-permitted).
- **`--version`** flag, and a real `__version__` constant (previously the version lived only in the
  module docstring).

### v1.3.0
- **Interactive mode** — run with no arguments: it discovers your backups, shows **friendly device
  names** (locked/encrypted ones can't be selected), suggests an output folder
  (`~/Pictures/iPhone/<device>`), previews, and confirms before copying. `--interactive` forces it;
  `--non-interactive` disables it. `--backup` is no longer required.

### v1.2.0
- **Live-Photo (HEIC/MOV) sibling-date pairing** — an asset with no usable date now inherits the
  timestamp of its same-stem sibling (the `.mov` of a HEIC+MOV Live Photo picks up the `.heic`'s
  date), so pairs are classified into the correct `YYYY-MM` folder instead of being split into
  `No_Date`. On by default; disable with `--no-infer-sibling-date`. Truly-unpaired files (no dated
  sibling) still fall back to `No_Date` — never guessed. Validated on Stanley's backup (3,920
  candidates): `No_Date` dropped from **1,583 → 6** true orphans.
- **`--since` now judges an undated item on its inferred date** (a paired-MOV with a recent sibling
  is kept rather than dropped).

### v1.1.1
- **TTY-aware live progress bar** — when piped to a file/log (not a terminal) it now emits a
  throttled discrete line every ~5 s instead of flooding the capture with one `\r` line per file.

### v1.1.0
- **Robust date handling** — birth/modified timestamps are validated against a plausible window
  (Jan 2007 – Jan 2100). Garbage doubles (e.g. year ~2273 / huge floats) and the "date unset"
  sentinel (2001-01-01) are folded into a `No_Date` folder instead of crashing with an
  `OverflowError`. `--since` now also excludes items with no usable date.
- **Portable iCloud filename recovery** — reads `ZADDITIONALASSETATTRIBUTES.ZORIGINALFILENAME`
  (newest, iOS 18+), with fallbacks to the `ZEXTENDEDATTRIBUTES` bplist and `ZFILENAME`.
- **`--format flat` fix** — flat output now lands at the top level (no spurious `No_Date` foldering).
- **Type-consistent helpers** — `_as_path()` normalizes `str`/`Path`/`None` inputs across the
  manifest/Photos helpers.
- **`--dry-run` no longer requires `-o`**.
- **Built-in live progress bar** — moving bar, percentage, throughput (MB/s) and ETA; replaces the
  optional `tqdm` dependency (runtime is now pure stdlib).
- **Test suite** — `tests/` with a synthetic-backup fixture generator; **100% line and branch
  coverage** of `iphone_photos_extractor.py` (pytest + coverage — see `requirements-dev.txt`).

Earlier releases: **v1.0.1** (docs) and **v1.0.0** (initial release) — see `CHANGELOG.md`.

---

## License

MIT — © 2026 Jacky Keung. See [LICENSE](LICENSE).