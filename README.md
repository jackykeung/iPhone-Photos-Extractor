# iPhone Photos Extractor

Extract **photos and videos 1:1** from an **unencrypted** Apple iPhone (Finder/iTunes) backup,
preserving **original filenames**, **real creation/modification dates**, and **EXIF metadata** —
bit-identical raw copies, no re-encoding, no quality loss.

Built in clean, auditable, dependency-light Python (stdlib only, `tqdm` optional). Independent
tool; the algorithm follows the approach pioneered by `joz-k/ios_backup_extractor` (Perl), rebuilt
and improved.

---

## Why this tool

iPhone backs up photos as raw bytes under the `CameraRollDomain` of the backup manifest. The
original filename (`IMG_1234.HEIC`) lives in the DCIM path, and the metadata (EXIF, date taken)
lives *inside* the file itself. So a true 1:1 extraction is just: **copy the bytes + restore the
file's real Birth & LastModified timestamps**. That's exactly what this does.

## Improvements over the Perl reference

| Capability | Perl reference | This tool |
|---|---|---|
| Copy parallelism | Serial (one file at a time) | **Parallel** via `ProcessPoolExecutor` (`--workers`) |
| Sparse handling | n/a | **Race-safe SHA-256 dedupe** with atomic `O_CREAT\|O_EXCL` markers |
| Resumable | No | **Incremental** — never re-copies or overwrites an existing identical file |
| iCloud real filenames | Joins asset DB | **Joins `ZASSET → ZEXTENDEDATTRIBUTES`** to recover `IMG_xxxx` from UUID names |
| Album organization | `--albums` | Same, via **dynamic discovery** of Apple's CoreData junction table (name changes per iOS) |
| Deleted photos | `--add-trash` | Same, from `ZTRASHEDSTATE` |
| Date folders | `--format ym/ymd/flat` | Same |
| Birth *and* LastModified | Restores via `SetFile` | **Both** on macOS using `/usr/bin/SetFile`, plus `utime` fallback |

---

## Requirements

* Python 3.8+ (stdlib only; `tqdm` optional for a progress bar)
* An **unencrypted** iPhone backup on your Mac, or a backup you can create
* On the machine running this: read access to the backup dir and, for exact creation
  dates on macOS, the `/usr/bin/SetFile` helper (built-in).

> **Encryption caveat:** this tool reads an **unencrypted** backup only. If you made the backup
> with "Encrypt local backup" checked in Finder, it will **not** work — re-create it unencrypted.
> (An unencrypted backup does **not** contain your Apple ID password, Health data, or Keychain.)

---

## Usage

```bash
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
| `--backup <dir>` | iOS backup directory (contains `Manifest.db`) — **required** |
| `-o, --output <dir>` | Output directory — **required** |
| `--format {ym,ymd,flat}` | Folder structure by date (default `ym`) |
| `--since <date>` | Only files since `YYYY-MM-DD`, `last-week`, or `last-month` |
| `--type {photo,video,audio,all}` | Filter by media type (default `all`) |
| `--albums` | Organize into your real user photo albums |
| `--add-trash` | Also extract items marked deleted (suffix `_DELETED`) |
| `--ignore-icloud-media` | Skip media sourced from iCloud |
| `--prepend-date` | Prepend creation date (`YYYY-MM-DD_`) to each filename |
| `--move` | Move files out of the backup (frees disk) instead of copy |
| `--dedupe` | Skip identical content (SHA-256, race-safe) |
| `--workers N` | Parallel copy workers (default: CPU count) |
| `--dry-run` | Scan and report only, copy nothing |

---

## How it works

1. **Scan** the backup `Manifest.db`'s `Files` table for the `CameraRollDomain`, keeping every
   `IMG_*`/`IMD_*`/etc. file that has a media extension and lives under `DCIM` or `PhotoData/CPLAssets`.
2. **Recover real names** for iCloud-origin assets (stored as UUIDs) by joining
   `ZASSET → ZEXTENDEDATTRIBUTES` in `Photos.sqlite` and reading `com.apple.assetsd.originalFilename`.
3. **Organize** into user albums (via dynamic discovery of the CoreData album↔asset junction table)
   or `YYYY-MM`/`YYYY-MM-DD` folders by the file's real LastModified/Birth.
4. **Restore dates** by parsing the manifest's binary-plist `Birth`/`LastModified` and applying them
   (`/usr/bin/SetFile` on macOS, `utime` fallback).
5. **Copy in parallel**, with atomic SHA-256 dedupe and full incremental resume.

---

## License

MIT — © 2026 Jacky Keung. See [LICENSE](LICENSE).