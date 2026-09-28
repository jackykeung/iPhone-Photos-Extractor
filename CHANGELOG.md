# Changelog

All notable changes to **iPhone Photos Extractor** are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

- **Added** — new capabilities or features.
- **Changed** — behavioural changes / improvements to existing functionality.
- **Fixed** — bug resolutions.
- **Removed** — deprecated/removed capabilities.

---

## [v1.2.0] — 2026-09-28

### Added
- **Live-Photo (HEIC/MOV) sibling-date pairing.** An asset with no usable date now inherits the
  timestamp of its same-stem sibling, so the `.mov` half of a HEIC+MOV Live Photo picks up the
  `.heic`'s date and the pair is classified into the same `YYYY-MM` folder instead of being split
  into `No_Date`. This is **on by default**; disable with `--no-infer-sibling-date`.
  - Keys on the resolved filename stem (after iCloud recovery), ignoring `_DELETED` and case.
  - Never overrides an existing date, never guesses from an undated sibling, and marks stems whose
    dated siblings disagree as ambiguous (left in `No_Date`).
  - Validated on a real 3,920-candidate backup: `No_Date` dropped from **1,583 → 6** true orphans.
- **`_infer_sibling_dates()`** and **`_usable_date()`** helpers; `main_with_args()` refactored into
  a resolve → pair → filter/map pipeline.
- **Tests** for pairing (MOV-follows-HEIC, garbage-date, orphan, ambiguous, iCloud-resolved stem,
  `--since` interplay, opt-out, plus unit tests) — 105 tests, 100% line + branch coverage.

### Changed
- **`--since` now judges an undated item on its inferred date** — a paired-MOV with a recent
  sibling is kept rather than dropped as "no date".

---

## [v1.1.1] — 2026-09-28

### Changed
- **TTY-aware live progress bar.** When the terminal is interactive (a TTY) the bar rewrites a
  single line with `\r` exactly as before. When output is **piped to a file or log** (not a TTY) it
  now emits one self-contained line at most every 5 seconds using `\n` instead of flooding the
  capture with one `\r` line per file (observed log flooding during a full ~3.9k-file extraction).

### Added
- **`CHANGELOG.md`** — canonical release history (Keep a Changelog / SemVer).
- **Release tags** for `v1.0.0`, `v1.0.1`, `v1.1.0`, and `v1.1.1`.
- **Test coverage for the non-TTY path** (`test_progress_nontty_throttled`) — branch coverage stays
  at 100%.

---

## [v1.1.0] — 2026-09-28

### Fixed
- **Robust date handling** — birth/modified timestamps are validated against a plausible window
  (Jan 2007 – Jan 2100, `_ABS_MIN_TS`/`_ABS_MAX_TS`). Garbage doubles (e.g. year ~2273 / huge
  floats) and the "date unset" sentinel (2001-01-01) fold into a `No_Date` folder instead of
  crashing with `OverflowError`.
- **`--format flat`** — flat output now lands at the top level (no spurious `No_Date` foldering).
  Also fixed `--since` to exclude items with no usable date.

### Added
- **Portable iCloud filename recovery** — reads `ZADDITIONALASSETATTRIBUTES.ZORIGINALFILENAME`
  (newest, iOS 18+), with fallbacks to the `ZEXTENDEDATTRIBUTES` bplist (`com.apple.assetsd.
  originalFilename`) and `ZFILENAME`.
- **`_as_path()`** — type-consistent `str`/`Path`/`None` normalization across the manifest/Photos
  helpers.
- **Built-in live progress bar** — moving bar, percentage, throughput (MB/s) and ETA; replaces the
  optional `tqdm` dependency, so runtime is now **pure stdlib**.
- **Test suite** — `tests/` with a synthetic-backup fixture generator (`make_photos_fixture.py`);
  **100% line and branch coverage** of `iphone_photos_extractor.py` (pytest + coverage, see
  `requirements-dev.txt`).

---

## [v1.0.1] — 2026-09-28

### Changed
- Docs: made the provenance wording generic (no specific source tool named).

---

## [v1.0.0] — 2026-09-27

### Added
- **Initial release.** High-performance extractor that copies photos/videos 1:1 from an unencrypted
  Apple iPhone (Finder/iTunes) backup, preserving original filenames, real creation/modification
  dates, and EXIF metadata — bit-identical, no re-encoding.
  - Parallel copy via `ProcessPoolExecutor` (`--workers`).
  - Race-safe SHA-256 dedupe with atomic `O_CREAT|O_EXCL` marker reservation.
  - Fully resumable / incremental (never re-copies or overwrites an existing identical file).
  - Real-filename recovery for iCloud-origin assets.
  - Album organization via dynamic CoreData junction-table discovery; `--add-trash` recovery.
  - Date-folder formats `ym`/`ymd`/`flat`; type/date/iCloud/trash filters; `--dry-run`; `--move`.
  - Restores both Birth and LastModified via `/usr/bin/SetFile` (macOS) with `utime` fallback.