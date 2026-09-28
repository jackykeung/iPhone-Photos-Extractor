#!/usr/bin/env python3
"""Unit + integration tests for iphone_photos_extractor.py.

Run with:
    python -m pytest tests/ -v
    python -m coverage run --branch -m pytest tests/ && python -m coverage report
"""

import os
import plistlib
import struct
import sqlite3
import stat
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

# Make the module importable from the repo root.
REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

import iphone_photos_extractor as p  # noqa: E402
import make_photos_fixture as mf  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def backup():
    """Build a synthetic backup once for the whole module."""
    return Path(mf.build_fixture())


@pytest.fixture
def photos_db(backup):
    return backup / "12" / "12b144c0bd44f2b3dffd9186d3f9c05b917cee25"


@pytest.fixture
def tmp_out(tmp_path):
    return tmp_path / "out"


# ---------------------------------------------------------------------------
# sanitize
# ---------------------------------------------------------------------------

def test_sanitize_basic():
    assert p.sanitize("My Photos") == "My Photos"
    assert p.sanitize("  padded  ") == "padded"
    assert p.sanitize('a<b>c:d"e/f') == "a_b_c_d_e_f"
    assert p.sanitize("") == "Unknown"
    assert p.sanitize("   ") == "Unknown"
    assert p.sanitize("____") == "Unknown"
    assert p.sanitize("-leading") == "_-leading"  # guard prefixes "_"
    assert p.sanitize(None) == "Unknown"
    assert p.sanitize("x" * 300) == "x" * 240
    assert p.sanitize("a", "FB") == "a"
    assert p.sanitize("", "FB") == "FB"


# ---------------------------------------------------------------------------
# time helpers
# ---------------------------------------------------------------------------

def test_apple_to_unix():
    assert p.apple_to_unix(0) == 978307200.0
    assert p.apple_to_unix(None) is None
    assert p.apple_to_unix("garbage") is None
    assert p.apple_to_unix(100) == 978307300.0


def test_fmt_dt():
    # A truthy, in-range value formats correctly.
    assert p.fmt_dt(datetime(2020, 1, 2, 3, 4, 5).timestamp()) == "2020-01-02 03:04:05"
    # None and falsy 0 -> "" (0 is treated as "no date").
    assert p.fmt_dt(None) == ""
    assert p.fmt_dt(0) == ""
    # Out-of-range -> "" via except.
    assert p.fmt_dt(1e100) == ""


def test_as_path():
    assert p._as_path(None) is None
    assert p._as_path("some/dir") == Path("some/dir")
    assert p._as_path(Path("x/y")) == Path("x/y")


# ---------------------------------------------------------------------------
# fmt helpers
# ---------------------------------------------------------------------------

def test_fmt_bytes():
    assert p._fmt_bytes(0) == "0 B"
    assert p._fmt_bytes(500) == "500 B"
    assert p._fmt_bytes(2048) == "2.0 KB"
    assert p._fmt_bytes(1024 * 1024) == "1.0 MB"
    assert p._fmt_bytes(1024 ** 3) == "1.0 GB"
    assert p._fmt_bytes(1024 ** 4) == "1.0 TB"
    # huge -> stays TB
    assert p._fmt_bytes(1024 ** 5) == "1024.0 TB"


def test_fmt_eta():
    assert p._fmt_eta(0) == "00:00"
    assert p._fmt_eta(59) == "00:59"
    assert p._fmt_eta(60) == "01:00"
    assert p._fmt_eta(3661) == "01:01:01"
    # negative -> clamped to 0
    assert p._fmt_eta(-5) == "00:00"


# ---------------------------------------------------------------------------
# _parse_bplist_dates
# ---------------------------------------------------------------------------

def test_parse_normal_dates():
    b_ep = mf._epoch(2026, 1, 15, 10, 30, 0)
    m_ep = mf._epoch(2026, 1, 15, 10, 31, 0)
    blob = mf.make_bplist_dates(b_ep, m_ep)
    b, m = p._parse_bplist_dates(blob)
    assert b == b_ep + mf.APPLE_EPOCH
    assert m == m_ep + mf.APPLE_EPOCH
    assert b < m


def test_parse_single_date():
    m_ep = mf._epoch(2026, 2, 3, 8, 0, 0)
    blob = mf.make_bplist_dates(None, m_ep)
    b, m = p._parse_bplist_dates(blob)
    assert b is not None and m is not None
    assert b == m == m_ep + mf.APPLE_EPOCH


def test_parse_no_date_marker():
    assert p._parse_bplist_dates(b"") == (None, None)
    assert p._parse_bplist_dates(None) == (None, None)
    assert p._parse_bplist_dates(b"\x00\x01\x02\x03") == (None, None)


def test_parse_garbage_double_rejected():
    m_ep_valid = mf._epoch(2026, 4, 1, 12, 0, 0)
    blob = mf.make_bplist_dates(mf.GARBAGE_DOUBLE, m_ep_valid)
    b, m = p._parse_bplist_dates(blob)
    # garbage birth rejected; the valid modified date survives as the only one.
    assert m == m_ep_valid + mf.APPLE_EPOCH
    assert b == m


def test_parse_future_junk_rejected():
    b_ep_valid = mf._epoch(2026, 5, 1, 0, 0, 0)
    blob = mf.make_bplist_dates(b_ep_valid, 9568889476.533798)
    b, m = p._parse_bplist_dates(blob)
    # junk year-2273 dropped; the valid birth survives as the only date.
    assert b == b_ep_valid + mf.APPLE_EPOCH
    assert m == b


def test_parse_epoch0_sentinel_rejected():
    blob = mf.make_bplist_dates(0, 0)
    b, m = p._parse_bplist_dates(blob)
    assert b is None and m is None


def test_struct_unpack_d():
    assert p.struct_unpack_d(struct.pack(">d", 1.0)) == 1.0


# ---------------------------------------------------------------------------
# find_payload_path / open_manifest_db / locate_photos_db
# ---------------------------------------------------------------------------

def test_find_payload_path(backup):
    # find a known fileID from the manifest
    m = sqlite3.connect(f"file:{backup}/Manifest.db?mode=ro", uri=True)
    row = m.execute("SELECT fileID FROM Files WHERE domain='CameraRollDomain' "
                    "LIMIT 1").fetchone()
    m.close()
    pw = p.find_payload_path(backup, row[0])
    assert pw is not None and pw.is_file()
    # str path also works
    assert p.find_payload_path(str(backup), row[0]) is not None
    # None fileID
    assert p.find_payload_path(backup, None) is None
    # missing payload
    assert p.find_payload_path(backup, "0000000000000000000000000000000000000000") is None
    # missing fileID (doesn't exist)
    assert p.find_payload_path(backup, "ffffffffffffffffffffffffffffffffffffffff") is None


def test_open_manifest_db(backup):
    conn = p.open_manifest_db(backup)
    assert conn is not None
    conn.close()
    with pytest.raises(FileNotFoundError):
        p.open_manifest_db(backup / "does-not-exist")


def test_locate_photos_db(backup):
    ps = p.locate_photos_db(backup)
    assert ps is not None and ps.is_file()


# ---------------------------------------------------------------------------
# load_icloud_filename_map
# ---------------------------------------------------------------------------

def test_load_icloud_new_schema(backup, photos_db):
    m = p.load_icloud_filename_map(photos_db)
    assert m["A1B2C3D4-E5F6-7890-ABCD-EF1234567890"] == "IMG_0003"


def test_load_icloud_old_bplist_schema(backup, photos_db):
    m = p.load_icloud_filename_map(photos_db)
    assert m["BBBBBBBB-1111-2222-3333-444444444444"] == "IMG_0008"


def test_load_icloud_missing_db(tmp_path):
    assert p.load_icloud_filename_map(tmp_path / "nope") == {}
    assert p.load_icloud_filename_map(tmp_path) == {}


def test_load_icloud_str_path(photos_db):
    # must not crash when given a str path
    m = p.load_icloud_filename_map(str(photos_db))
    assert m["A1B2C3D4-E5F6-7890-ABCD-EF1234567890"] == "IMG_0003"


# ---------------------------------------------------------------------------
# _extract_original_filename
# ---------------------------------------------------------------------------

def test_extract_original_filename_found():
    # key + a short ASCII bplist string object (0x5C = tag 5, length 12)
    blob = (b"com.apple.assetsd.originalFilename" + b"\x5c" + b"IMG_1234.JPG")
    assert p._extract_original_filename(blob) == "IMG_1234"


def test_extract_original_filename_heic():
    # 0x5D = tag 5, length 13 ("IMG_9999.HEIC")
    blob = (b"com.apple.assetsd.originalFilename" + b"\x5d" + b"IMG_9999.HEIC")
    assert p._extract_original_filename(blob) == "IMG_9999"


def test_extract_original_filename_utf16():
    # tag 0x69 = 0x60|0x09 (UTF-16 short string, length 9)
    blob = (b"com.apple.assetsd.originalFilename" + b"\x69"
            + "SELFI_123".encode("utf-16-be"))
    assert p._extract_original_filename(blob) is not None


def test_extract_original_filename_long_form():
    # long form: 0x5F tag, then a length-int object (0x10 + byte)
    name = "IMG_" + "A" * 20  # 24 chars, needs long form
    blob = (b"com.apple.assetsd.originalFilename" + b"\x5f" + b"\x10"
            + bytes([len(name)]) + name.encode())
    assert p._extract_original_filename(blob) == name


def test_extract_original_filename_fallback_img_run():
    blob = b"some data IMG_7777.JPG more data"
    assert p._extract_original_filename(blob) == "IMG_7777"


def test_extract_original_filename_jpeg_suffix_accepted():
    blob = (b"com.apple.assetsd.originalFilename" + b"\x5c" + b"IMG_4444.JPEG")
    assert p._extract_original_filename(blob) == "IMG_4444"


def test_extract_original_filename_none():
    assert p._extract_original_filename(None) is None
    assert p._extract_original_filename(b"\x00\x01") is None
    # key present but no valid string after
    assert p._extract_original_filename(b"com.apple.assetsd.originalFilename") is None


# ---------------------------------------------------------------------------
# load_trashed_map
# ---------------------------------------------------------------------------

def test_load_trashed_map(backup, photos_db):
    t = p.load_trashed_map(photos_db)
    assert t.get("Media/DCIM/100APPLE/IMG_0007.HEIC") is True


def test_load_trashed_missing(tmp_path):
    assert p.load_trashed_map(tmp_path / "x") == {}


def test_load_trashed_str_path(photos_db):
    t = p.load_trashed_map(str(photos_db))
    assert t.get("Media/DCIM/100APPLE/IMG_0007.HEIC") is True


# ---------------------------------------------------------------------------
# load_album_map
# ---------------------------------------------------------------------------

def test_load_album_map(backup, photos_db):
    a = p.load_album_map(photos_db)
    assert a.get("Media/DCIM/100APPLE/IMG_0012.HEIC") == "Trip"
    assert a.get("Media/DCIM/100APPLE/IMG_0001.HEIC") == "Trip"
    # album with ZKIND != 2 untitled excluded
    assert "Media/DCIM/100APPLE/IMG_0001.HEIC" in a


def test_load_album_missing(tmp_path):
    assert p.load_album_map(tmp_path / "x") == {}


def test_load_album_str_path(photos_db):
    a = p.load_album_map(str(photos_db))
    assert a.get("Media/DCIM/100APPLE/IMG_0012.HEIC") == "Trip"


# ---------------------------------------------------------------------------
# scan_camera_roll
# ---------------------------------------------------------------------------

def test_scan_camera_roll(backup):
    items = p.scan_camera_roll(backup)
    rels = {it["rel"] for it in items}
    assert "Media/DCIM/100APPLE/IMG_0001.HEIC" in rels
    # thumbnail excluded
    assert not any("thumb" in r for r in rels)
    # unknown extension excluded
    assert not any(r.endswith(".xyz") for r in rels)
    # a recognized iCloud file is included and flagged
    icloud = [it for it in items if it["is_icloud"]]
    assert icloud and "CPLAssets" in icloud[0]["rel"]


def test_scan_camera_roll_str_path(backup):
    items = p.scan_camera_roll(str(backup))
    assert items


# ---------------------------------------------------------------------------
# copy_photo / sha256_file
# ---------------------------------------------------------------------------

def test_copy_photo_copied(tmp_path):
    src = tmp_path / "src.bin"
    src.write_bytes(b"hello world")
    bplist = mf.make_bplist_dates(mf._epoch(2026, 1, 1), mf._epoch(2026, 1, 1))
    dest_dir = tmp_path / "dest"
    st, base, dest, size = p.copy_photo((str(src), dest_dir, "a.jpg", bplist, False, None))
    assert st == "copied"
    assert (dest_dir / "a.jpg").read_bytes() == b"hello world"


def test_copy_photo_dedupe_reserve(tmp_path):
    # Fresh dedupe dir -> marker is created (os.open+os.close path, line 538),
    # and a valid bplist date triggers os.utime + the SetFile birth/metadata path.
    src = tmp_path / "src.bin"
    src.write_bytes(b"reserve dedupe content")
    bplist = mf.make_bplist_dates(mf._epoch(2026, 2, 2), mf._epoch(2026, 2, 2))
    dest_dir = tmp_path / "dest"
    dedupe = tmp_path / ".hashes"
    st, base, dest, size = p.copy_photo((str(src), dest_dir, "v.jpg", bplist, False, dedupe))
    assert st == "copied"
    h = p.sha256_file(src)
    assert (dedupe / (h + ".jpg")).exists()
    assert (dest_dir / "v.jpg").read_bytes() == b"reserve dedupe content"


def test_copy_photo_skipped(tmp_path):
    src = tmp_path / "src.bin"
    src.write_bytes(b"x" * 100)
    dest_dir = tmp_path / "dest"
    dest_dir.mkdir()
    (dest_dir / "a.jpg").write_bytes(b"x" * 100)
    st, base, dest, size = p.copy_photo((str(src), dest_dir, "a.jpg", b"", False, None))
    assert st == "skipped"


def test_copy_photo_missing_src(tmp_path):
    st, base, dest, size = p.copy_photo((str(tmp_path / "nope"), tmp_path, "a.jpg", b"", False, None))
    assert st == "missing"


def test_copy_photo_error(tmp_path):
    # cause the open/write to fail by making dest a directory-name collision
    dest_dir = tmp_path / "dest"
    dest_dir.mkdir()
    # make dest path a directory so open(dest,'wb') fails
    (dest_dir / "a.jpg").mkdir()
    src = tmp_path / "src.bin"
    src.write_bytes(b"data")
    st, base, dest, size = p.copy_photo((str(src), dest_dir, "a.jpg", b"", False, None))
    assert st == "error"


def test_copy_photo_deduped(tmp_path):
    src = tmp_path / "src.bin"
    src.write_bytes(b"dup-content")
    dest_dir = tmp_path / "dest"
    dedupe = tmp_path / ".hashes"
    dedupe.mkdir()
    h = p.sha256_file(src)
    # pre-create the marker so the worker dedupes
    (dedupe / (h + ".jpg")).touch()
    st, base, dest, size = p.copy_photo((str(src), dest_dir, "a.jpg", b"", False, dedupe))
    assert st == "deduped"


def test_copy_photo_move(tmp_path):
    src = tmp_path / "src.bin"
    src.write_bytes(b"move me")
    dest_dir = tmp_path / "dest"
    st, base, dest, size = p.copy_photo((str(src), dest_dir, "a.jpg", b"", False, False))
    p.copy_photo((str(src), dest_dir, "b.jpg", b"", True, None))
    # moved: original deleted, only b.jpg remains
    assert not src.exists()
    assert (dest_dir / "b.jpg").exists()
    assert not (dest_dir / "a.jpg").exists()


def test_sha256_file(tmp_path):
    f = tmp_path / "x.bin"
    f.write_bytes(b"abc")
    assert p.sha256_file(f) == __import__("hashlib").sha256(b"abc").hexdigest()


# ---------------------------------------------------------------------------
# _Progress
# ---------------------------------------------------------------------------

def test_progress_updates(capsys):
    prog = p._Progress(total_files=3, total_bytes=300)
    # Simulate a real terminal so every update rewrites the live bar (`\r`).
    prog.tty = True
    prog.update(100)
    prog.update(100)
    prog.update(100)
    prog.finish()
    err = capsys.readouterr().err
    assert "100.0%" in err or "100%" in err


def test_progress_zero_total(capsys):
    prog = p._Progress(total_files=0, total_bytes=0)
    prog.update(0)
    prog.finish()
    err = capsys.readouterr().err
    assert err  # rendered something


def test_progress_by_bytes_when_no_files(capsys):
    prog = p._Progress(total_files=0, total_bytes=1000)
    # force a byte-based percentage and a speed to exercise ETA branch
    prog.update(500)
    # manually set a speed to render ETA
    prog.speed_bytes = 1000.0
    prog._render(time.time())
    err = capsys.readouterr().err
    assert "ETA" in err


def test_progress_nontty_throttled(capsys):
    """Piped/logged output: render a discrete line, throttled, not one per file."""
    prog = p._Progress(total_files=5, total_bytes=500)
    prog.tty = False
    prog.next_nontty = 0.0  # render the first line, then back off
    for _ in range(5):
        prog.update(100)
    lines = [ln for ln in capsys.readouterr().err.splitlines() if ln]
    # The very first update renders; the rest are throttled out on non-TTY.
    assert len(lines) == 1
    assert "Copying" in lines[0] and "\r" not in lines[0]  # discrete line, no carriage return
    # A final finish() leaves a summary line.
    prog.finish()
    err = capsys.readouterr().err
    assert "files" in err or "B" in err


# ---------------------------------------------------------------------------
# parse_since
# ---------------------------------------------------------------------------

def test_parse_since():
    assert p.parse_since(None) is None
    assert p.parse_since("last-week") is not None
    assert p.parse_since("last-month") is not None
    assert p.parse_since("not-a-date") is None


def test_parse_since_exact():
    ts = p.parse_since("2026-03-15")
    assert ts == datetime(2026, 3, 15).replace(tzinfo=timezone.utc).timestamp()


# ---------------------------------------------------------------------------
# CLI end-to-end (main)
# ---------------------------------------------------------------------------

def test_cli_missing_backup(tmp_path, capsys):
    with pytest.raises(SystemExit) as e:
        p.main_with_args(["--backup", str(tmp_path / "nope"),
                          "-o", str(tmp_path / "out")])
    assert e.value.code == 1


def test_cli_missing_output(backup, tmp_path, capsys):
    # backup exists, no -o, not dry-run -> error
    with pytest.raises(SystemExit) as e:
        p.main_with_args(["--backup", str(backup)])
    assert e.value.code == 1


def test_cli_dry_run_no_output(backup, capsys):
    with pytest.raises(SystemExit) as e:
        p.main_with_args(["--backup", str(backup), "--dry-run"])
    assert e.value.code == 0
    out = capsys.readouterr().out
    assert "DRY-RUN complete" in out


def test_cli_full_extract(backup, tmp_path, capsys):
    out = tmp_path / "out"
    p.main_with_args(["--backup", str(backup), "-o", str(out)])
    # a normal DCIM file should be copied into a date folder
    found = list(out.rglob("IMG_0001*"))
    assert found, "IMG_0001 should have been copied"
    assert found[0].read_bytes() == b"fake-heic-bytes"


def test_cli_albums(backup, tmp_path):
    out = tmp_path / "out"
    p.main_with_args(["--backup", str(backup), "-o", str(out), "--albums"])
    trip = out / "Trip"
    assert list(trip.glob("IMG_0012*"))
    assert list(trip.glob("IMG_0001*"))


def test_cli_add_trash(backup, tmp_path):
    out1 = tmp_path / "a"
    p.main_with_args(["--backup", str(backup), "-o", str(out1)])
    assert not list(out1.rglob("IMG_0007*"))  # trashed skipped by default
    out2 = tmp_path / "b"
    p.main_with_args(["--backup", str(backup), "-o", str(out2), "--add-trash"])
    assert list(out2.rglob("IMG_0007_DELETED*"))


def test_cli_type_photo(backup, tmp_path):
    out = tmp_path / "out"
    p.main_with_args(["--backup", str(backup), "-o", str(out), "--type", "photo"])
    assert list(out.rglob("IMG_0001*"))
    # the mp4 (video) should not appear
    assert not list(out.rglob("IMG_0006*"))


def test_cli_type_video(backup, tmp_path):
    out = tmp_path / "out"
    p.main_with_args(["--backup", str(backup), "-o", str(out), "--type", "video"])
    assert list(out.rglob("IMG_0006*"))
    assert not list(out.rglob("IMG_0001*"))  # photo excluded


def test_cli_since_filter(backup, tmp_path):
    out = tmp_path / "out"
    # since a far future date should exclude everything dated before it
    p.main_with_args(["--backup", str(backup), "-o", str(out), "--since", "2030-01-01"])
    assert not list(out.iterdir())


def test_cli_ignore_icloud(backup, tmp_path):
    out = tmp_path / "out"
    p.main_with_args(["--backup", str(backup), "-o", str(out), "--ignore-icloud-media"])
    # the recovered iCloud name IMG_0003 comes from CPLAssets -> should be skipped
    assert not list(out.rglob("IMG_0003*"))


# ---------------------------------------------------------------------------
# Live-Photo sibling (date) pairing
# ---------------------------------------------------------------------------

def test_pairing_mov_follows_dated_heic(backup, tmp_path):
    """A no-date .mov inherits its dated .heic sibling's month folder."""
    out = tmp_path / "out"
    p.main_with_args(["--backup", str(backup), "-o", str(out)])
    heic = list(out.rglob("IMG_0020.heic"))
    mov = list(out.rglob("IMG_0020.mov"))
    assert heic, "IMG_0020.heic should be present"
    assert mov, "IMG_0020.mov should be present (paired out of No_Date)"
    assert mov[0].parent.name == heic[0].parent.name == "2026-04"
    assert not list((out / "No_Date").glob("IMG_0020.*"))


def test_pairing_garbage_date_inherits_sibling(backup, tmp_path):
    """A .mov whose only date is a garbage double inherits the dated sibling."""
    out = tmp_path / "out"
    p.main_with_args(["--backup", str(backup), "-o", str(out)])
    mov = list(out.rglob("IMG_0021.mov"))
    assert mov, "IMG_0021.mov should be paired"
    assert mov[0].parent.name == "2026-05"
    assert not list((out / "No_Date").glob("IMG_0021.*"))


def test_pairing_orphan_stays_no_date(backup, tmp_path):
    """An undated .mov with no dated sibling is NOT guessed -> stays in No_Date."""
    out = tmp_path / "out"
    p.main_with_args(["--backup", str(backup), "-o", str(out)])
    mov = list((out / "No_Date").glob("IMG_0022.*"))
    assert mov, "IMG_0022.mov (orphan) should remain in No_Date"
    assert mov[0].name == "IMG_0022.mov"


def test_pairing_ambiguous_no_guess(backup, tmp_path):
    """Two dated siblings with differing dates -> do not guess -> No_Date."""
    out = tmp_path / "out"
    p.main_with_args(["--backup", str(backup), "-o", str(out)])
    mov = list((out / "No_Date").glob("IMG_0023.*"))
    assert mov, "IMG_0023.mov (ambiguous) should stay in No_Date"
    assert mov[0].name == "IMG_0023.mov"


def test_pairing_since_keeps_paired_late(backup, tmp_path):
    """--since judges an undated item on its INFERRED date, not as no-date."""
    out = tmp_path / "out"
    p.main_with_args(["--backup", str(backup), "-o", str(out), "--since", "2026-03-01"])
    # IMG_0020 pair is dated 2026-04-12 (>= cutoff) so both survive the filter.
    heic = list(out.rglob("IMG_0020.heic"))
    mov = list(out.rglob("IMG_0020.mov"))
    assert heic and mov, "both IMG_0020 members should pass the since filter"
    # The orphan (no date, no sibling) is excluded by the since filter.
    assert not list(out.rglob("IMG_0022.*"))


def test_pairing_disabled_via_flag(backup, tmp_path):
    """--no-infer-sibling-date disables pairing -> MOV stays in No_Date."""
    out = tmp_path / "out"
    p.main_with_args(["--backup", str(backup), "-o", str(out), "--no-infer-sibling-date"])
    heic = list(out.rglob("IMG_0020.heic"))
    mov = list((out / "No_Date").glob("IMG_0020.*"))
    assert heic, "IMG_0020.heic present"
    assert mov, "with pairing disabled, IMG_0020.mov lands in No_Date"


def test_pairing_icloud_resolved_stem(backup, tmp_path):
    """Two iCloud UUIDs resolving to the same real stem pair correctly."""
    out = tmp_path / "out"
    p.main_with_args(["--backup", str(backup), "-o", str(out)])
    heic = list(out.rglob("IMG_0024.heic"))
    mov = list(out.rglob("IMG_0024.mov"))
    assert heic, "IMG_0024.heic (iCloud) present"
    assert mov, "IMG_0024.mov (iCloud) paired out of No_Date"
    assert mov[0].parent.name == heic[0].parent.name == "2026-07"
    assert not list((out / "No_Date").glob("IMG_0024.*"))


def test_infer_sibling_dates_unit():
    """Direct unit test of the pairing helper."""
    HEIC = 1167609600 + 60 * 86400  # a plausible 2007 date
    items = [
        # dated image + undated video of the same stem -> video inherits
        {"stem": "IMG_A", "ext": "heic", "deleted": False, "birth": HEIC, "modif": HEIC},
        {"stem": "IMG_A", "ext": "mov", "deleted": False, "birth": None, "modif": None},
        # already-dated item must NOT be overridden
        {"stem": "IMG_B", "ext": "png", "deleted": False, "birth": HEIC, "modif": None},
        {"stem": "IMG_B", "ext": "mov", "deleted": False, "birth": HEIC + 5000, "modif": None},
        # orphan (no dated sibling) stays undated
        {"stem": "IMG_C", "ext": "mov", "deleted": False, "birth": None, "modif": None},
        # a deleted item is never a date source, but can be filled
        {"stem": "IMG_D", "ext": "heic", "deleted": False, "birth": HEIC, "modif": HEIC},
        {"stem": "IMG_D", "ext": "mov", "deleted": True, "birth": None, "modif": None},
    ]
    p._infer_sibling_dates(items)
    # IMG_A mov inherits
    assert items[1]["modif"] == HEIC and items[1]["birth"] == HEIC
    # IMG_B mov already dated -> not overridden
    assert items[3]["modif"] is None and items[3]["birth"] == HEIC + 5000
    # IMG_C orphan stays undated
    assert items[4]["birth"] is None and items[4]["modif"] is None
    # IMG_D deleted mov filled from non-deleted heic
    assert items[6]["modif"] == HEIC


def test_infer_sibling_dates_ambiguous():
    """Differing dated siblings for one stem -> the undated item is not filled."""
    HEIC = 1167609600 + 60 * 86400
    OTHER = 1167609600 + 90 * 86400
    items = [
        {"stem": "IMG_E", "ext": "heic", "deleted": False, "birth": HEIC, "modif": HEIC},
        {"stem": "IMG_E", "ext": "jpg", "deleted": False, "birth": OTHER, "modif": OTHER},
        {"stem": "IMG_E", "ext": "mov", "deleted": False, "birth": None, "modif": None},
    ]
    p._infer_sibling_dates(items)
    assert items[2]["birth"] is None and items[2]["modif"] is None


def test_infer_no_sibling_is_noop():
    """No dated sibling at all -> nothing changes."""
    items = [{"stem": "IMG_X", "ext": "mov", "deleted": False, "birth": None, "modif": None}]
    p._infer_sibling_dates(items)
    assert items[0]["birth"] is None and items[0]["modif"] is None


def test_infer_ignores_empty_stem():
    """A dated item with an empty stem is skipped when building the index."""
    HEIC = 1167609600 + 60 * 86400
    items = [
        {"stem": "", "ext": "heic", "deleted": False, "birth": HEIC, "modif": HEIC},
        {"stem": "IMG_Z", "ext": "mov", "deleted": False, "birth": None, "modif": None},
    ]
    p._infer_sibling_dates(items)
    # The undated IMG_Z has no usable sibling (the empty stem went nowhere).
    assert items[1]["birth"] is None and items[1]["modif"] is None


def test_cli_dedupe(backup, tmp_path):
    out = tmp_path / "out"
    p.main_with_args(["--backup", str(backup), "-o", str(out), "--dedupe"])
    assert (out / ".hashes").is_dir()
    # files still copied
    assert list(out.rglob("IMG_0001*"))


def test_cli_prepend_date(backup, tmp_path):
    out = tmp_path / "out"
    p.main_with_args(["--backup", str(backup), "-o", str(out), "--prepend-date"])
    f = list(out.rglob("*IMG_0001*"))
    assert f and f[0].name.startswith("2026-01-15_")


def test_cli_prepend_date_none_sep(backup, tmp_path):
    out = tmp_path / "out"
    p.main_with_args(["--backup", str(backup), "-o", str(out),
                      "--prepend-date", "--prepend-date-separator", "none"])
    f = list(out.rglob("*IMG_0001*"))
    # separator "none": date directly followed by the name (no trailing sep)
    assert f[0].name.startswith("2026-01-15")
    assert "2026-01-15_" not in f[0].name
    assert f[0].name.endswith(".heic")


def test_cli_prepend_date_underscore(backup, tmp_path):
    out = tmp_path / "out"
    p.main_with_args(["--backup", str(backup), "-o", str(out),
                      "--prepend-date", "--prepend-date-separator", "underscore"])
    f = list(out.rglob("*IMG_0001*"))
    assert f[0].name.startswith("2026-01-15_")


def test_cli_format_flat(backup, tmp_path):
    out = tmp_path / "out"
    p.main_with_args(["--backup", str(backup), "-o", str(out), "--format", "flat"])
    # no subdate dirs; files at the top level
    assert list(out.glob("IMG_0001*"))


def test_cli_format_ymd(backup, tmp_path):
    out = tmp_path / "out"
    p.main_with_args(["--backup", str(backup), "-o", str(out), "--format", "ymd"])
    assert list((out / "2026-01-15").glob("IMG_0001*"))


def test_cli_empty_plan(backup, tmp_path, capsys):
    # --type audio yields 0 files; must not crash
    p.main_with_args(["--backup", str(backup), "-o", str(tmp_path / "o"),
                      "--type", "audio"])
    assert (tmp_path / "o").is_dir()

# ---------------------------------------------------------------------------
# Coverage: ad-hoc backups / Photos.sqlite builders + defensive branches
# ---------------------------------------------------------------------------

def _mkbackup(tmp_path, manifest_rows, payloads=None):
    """Build a minimal backup dir with Manifest.db. manifest_rows are
    (fileID, domain, relativePath, blob); payloads maps fileID->bytes."""
    bdir = tmp_path / ("bk_%s" % abs(hash(str(manifest_rows))))
    bdir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(bdir / "Manifest.db"))
    cur = conn.cursor()
    cur.execute("CREATE TABLE Files (fileID TEXT, domain TEXT, relativePath TEXT, file BLOB)")
    cur.executemany("INSERT INTO Files VALUES (?,?,?,?)", manifest_rows)
    conn.commit()
    conn.close()
    if payloads:
        for fid, data in payloads.items():
            d = bdir / fid[:2]
            d.mkdir(parents=True, exist_ok=True)
            (d / fid).write_bytes(data)
    return bdir


def _mkphotos(tmp_path, create, rows, name=None, root=None):
    """Build a Photos.sqlite at <root or tmp_path>/12/<name> from `create`(make tables)+rows."""
    name = name or "12b144c0bd44f2b3dffd9186d3f9c05b917cee25"
    d = (root or tmp_path) / "12"
    d.mkdir(parents=True, exist_ok=True)
    pth = d / name
    conn = sqlite3.connect(str(pth))
    cur = conn.cursor()
    create(cur)
    for sql, values in rows:
        cur.execute(sql, values)
    conn.commit()
    conn.close()
    return pth


def _add_asset_cols(cur):
    cur.execute("CREATE TABLE ZASSET (Z_PK INTEGER PRIMARY KEY, ZUUID TEXT, "
                "ZFILENAME TEXT, ZDIRECTORY TEXT, ZTRASHEDSTATE INTEGER)")


# ---------------------------------------------------------------------------
# _parse_bplist_dates inner exception
# ---------------------------------------------------------------------------
def test_parse_inner_exception(monkeypatch):
    def boom(_):
        raise struct.error("boom")
    monkeypatch.setattr(p, "struct_unpack_d", boom)
    blob = mf.make_bplist_dates(mf._epoch(2026, 1, 1), mf._epoch(2026, 1, 2))
    # every date scan hits the inner except -> no dates -> (None, None)
    assert p._parse_bplist_dates(blob) == (None, None)


# ---------------------------------------------------------------------------
# _extract_original_filename: except + long-form + no-match
# ---------------------------------------------------------------------------
def test_extract_original_filename_except_and_long():
    # str blob -> bytes() raises -> except -> None
    assert p._extract_original_filename("not a bytes blob") is None
    assert p._extract_original_filename(None) is None
    # long-form ASCII string object: 0x5F + 0x10 + len + bytes
    val = b"IMG_0123456789abcdefghij"  # 23 chars (> 15 -> long form)
    blob = b"com.apple.assetsd.originalFilename" + b"\x5f\x10" + bytes([len(val)]) + val
    assert p._extract_original_filename(blob) == val.decode()
    # key present but no valid token after it -> None (last-resort IMG search fails)
    assert p._extract_original_filename(b"com.apple.assetsd.originalFilename") is None
    # no key in blob and no IMG_ run -> None
    assert p._extract_original_filename(b"\x00\x01\x02\x03") is None


# ---------------------------------------------------------------------------
# load_icloud_filename_map schema variants
# ---------------------------------------------------------------------------
def test_map_store_empty_value(tmp_path):
    def t(cur):
        _add_asset_cols(cur)
        cur.execute("CREATE TABLE ZADDITIONALASSETATTRIBUTES (Z_PK INTEGER PRIMARY KEY, "
                    "ZORIGINALFILENAME TEXT, ZASSET INTEGER)")
    db = _mkphotos(tmp_path, t, [
        ("INSERT INTO ZASSET (Z_PK,ZUUID,ZFILENAME,ZDIRECTORY,ZTRASHEDSTATE) "
         "VALUES (1,?,?,?,0)", ("MYU1", "IMG_FALLBACK.HEIC", "DCIM/100APPLE")),
        ("INSERT INTO ZADDITIONALASSETATTRIBUTES (ZORIGINALFILENAME,ZASSET) "
         "VALUES (?,1)", ("",)),  # empty -> _store returns at line 267
    ])
    mp = p.load_icloud_filename_map(db)
    # empty original filename ignored; fallback ZFILENAME wins
    assert mp.get("MYU1") == "IMG_FALLBACK"


def test_map_duplicate_uuid(tmp_path):
    def t(cur):
        _add_asset_cols(cur)
        cur.execute("CREATE TABLE ZADDITIONALASSETATTRIBUTES (Z_PK INTEGER PRIMARY KEY, "
                    "ZORIGINALFILENAME TEXT, ZASSET INTEGER)")
    db = _mkphotos(tmp_path, t, [
        ("INSERT INTO ZASSET (Z_PK,ZUUID,ZFILENAME,ZDIRECTORY,ZTRASHEDSTATE) VALUES (1,?,?,?,0)",
         ("DUP", "IMG_FALLBACK.HEIC", "D")),
        ("INSERT INTO ZASSET (Z_PK,ZUUID,ZFILENAME,ZDIRECTORY,ZTRASHEDSTATE) VALUES (2,?,?,?,0)",
         ("DUP", "IMG_FALLBACK.HEIC", "D")),
        ("INSERT INTO ZADDITIONALASSETATTRIBUTES (ZORIGINALFILENAME,ZASSET) VALUES (?,1)",
         ("IMG_AAA.HEIC",)),
        ("INSERT INTO ZADDITIONALASSETATTRIBUTES (ZORIGINALFILENAME,ZASSET) VALUES (?,2)",
         ("IMG_BBB.HEIC",)),
    ])
    mp = p.load_icloud_filename_map(db)
    # first value wins; the duplicate is skipped (line 271->exit)
    assert mp.get("DUP") == "IMG_AAA"


def test_map_uuid_format_rejected(tmp_path):
    def t(cur):
        _add_asset_cols(cur)
        cur.execute("CREATE TABLE ZADDITIONALASSETATTRIBUTES (Z_PK INTEGER PRIMARY KEY, "
                    "ZORIGINALFILENAME TEXT, ZASSET INTEGER)")
    uuid_stem = "00000000-0000-0000-0000-000000000000"
    db = _mkphotos(tmp_path, t, [
        ("INSERT INTO ZASSET (Z_PK,ZUUID,ZFILENAME,ZDIRECTORY,ZTRASHEDSTATE) VALUES (1,?,?,?,0)",
         ("U1", "IMG_OK.HEIC", "D")),
        ("INSERT INTO ZADDITIONALASSETATTRIBUTES (ZORIGINALFILENAME,ZASSET) VALUES (?,1)",
         (uuid_stem + ".JPG",)),  # splitext -> 36-char UUID -> rejected
    ])
    mp = p.load_icloud_filename_map(db)
    assert "U1" not in mp or mp["U1"] == "IMG_OK"  # UUID value ignored


def test_map_no_originalfilename_col(tmp_path):
    # AA table exists but has NO ZORIGINALFILENAME column -> newest skipped (282->295).
    def t(cur):
        _add_asset_cols(cur)
        cur.execute("CREATE TABLE ZADDITIONALASSETATTRIBUTES (Z_PK INTEGER PRIMARY KEY, ZASSET INTEGER)")
    db = _mkphotos(tmp_path, t, [
        ("INSERT INTO ZASSET (Z_PK,ZUUID,ZFILENAME,ZDIRECTORY,ZTRASHEDSTATE) VALUES (1,?,?,?,0)",
         ("N1", "IMG_N1.HEIC", "D")),
        ("INSERT INTO ZADDITIONALASSETATTRIBUTES (ZASSET) VALUES (1)", ()),
    ])
    mp = p.load_icloud_filename_map(db)
    assert mp.get("N1") == "IMG_N1"  # from fallback


def test_map_no_zplistdata_col(tmp_path):
    def t(cur):
        _add_asset_cols(cur)
        cur.execute("CREATE TABLE ZADDITIONALASSETATTRIBUTES (Z_PK INTEGER PRIMARY KEY, "
                    "ZORIGINALFILENAME TEXT, ZASSET INTEGER)")
        cur.execute("CREATE TABLE ZEXTENDEDATTRIBUTES (Z_PK INTEGER PRIMARY KEY, ZASSET INTEGER)")
    db = _mkphotos(tmp_path, t, [
        ("INSERT INTO ZASSET (Z_PK,ZUUID,ZFILENAME,ZDIRECTORY,ZTRASHEDSTATE) VALUES (1,?,?,?,0)",
         ("Z1", "IMG_Z1.HEIC", "D")),
        ("INSERT INTO ZEXTENDEDATTRIBUTES (ZASSET) VALUES (1)", ()),
    ])
    mp = p.load_icloud_filename_map(db)
    assert mp.get("Z1") == "IMG_Z1"  # older skipped (no ZPLISTDATA) -> fallback


def test_map_ext_no_asset_col(tmp_path):
    # EXT has ZPLISTDATA but no ZASSET column -> older skipped (303->319) -> fallback.
    def t(cur):
        _add_asset_cols(cur)
        cur.execute("CREATE TABLE ZADDITIONALASSETATTRIBUTES (Z_PK INTEGER PRIMARY KEY, "
                    "ZORIGINALFILENAME TEXT, ZASSET INTEGER)")
        cur.execute("CREATE TABLE ZEXTENDEDATTRIBUTES (Z_PK INTEGER PRIMARY KEY, ZPLISTDATA BLOB)")
    db = _mkphotos(tmp_path, t, [
        ("INSERT INTO ZASSET (Z_PK,ZUUID,ZFILENAME,ZDIRECTORY,ZTRASHEDSTATE) VALUES (1,?,?,?,0)",
         ("A1", "IMG_A1.HEIC", "D")),
        ("INSERT INTO ZEXTENDEDATTRIBUTES (ZPLISTDATA) VALUES (?)", (b"IMG_OLD.JPEG",)),
    ])
    mp = p.load_icloud_filename_map(db)
    assert mp.get("A1") == "IMG_A1"


def test_map_older_row_already_mapped(tmp_path):
    # newest populated the uuid, older yields the same uuid -> skipped (313->310).
    def t(cur):
        _add_asset_cols(cur)
        cur.execute("CREATE TABLE ZADDITIONALASSETATTRIBUTES (Z_PK INTEGER PRIMARY KEY, "
                    "ZORIGINALFILENAME TEXT, ZASSET INTEGER)")
        cur.execute("CREATE TABLE ZEXTENDEDATTRIBUTES (Z_PK INTEGER PRIMARY KEY, "
                    "ZPLISTDATA BLOB, ZASSET INTEGER)")
    db = _mkphotos(tmp_path, t, [
        ("INSERT INTO ZASSET (Z_PK,ZUUID,ZFILENAME,ZDIRECTORY,ZTRASHEDSTATE) VALUES (1,?,?,?,0)",
         ("SAME", "IMG_FALLBACK.HEIC", "D")),
        ("INSERT INTO ZADDITIONALASSETATTRIBUTES (ZORIGINALFILENAME,ZASSET) VALUES (?,1)",
         ("IMG_MAIN.HEIC",)),
        ("INSERT INTO ZEXTENDEDATTRIBUTES (ZPLISTDATA,ZASSET) VALUES (?,1)",
         (b"IMG_OLD.JPEG",)),
    ])
    mp = p.load_icloud_filename_map(db)
    assert mp.get("SAME") == "IMG_MAIN"  # newer wins, older skipped


def test_map_no_asset_fallback_cols(tmp_path):
    def t(cur):
        # ZASSET has neither ZUUID nor ZFILENAME -> fallback block skipped (322->331).
        cur.execute("CREATE TABLE ZASSET (Z_PK INTEGER PRIMARY KEY, ZDIRECTORY TEXT)")
    db = _mkphotos(tmp_path, t, [("INSERT INTO ZASSET (ZDIRECTORY) VALUES (?)", ("D",))])
    assert p.load_icloud_filename_map(db) == {}


def test_map_outer_except(photos_db, monkeypatch):
    def boom(*_a, **_k):  # noqa: ANN001
        raise RuntimeError("no db")
    monkeypatch.setattr(p.sqlite3, "connect", boom)
    # photos_db is a real file -> passes the is_file guard -> hits the try/except.
    assert p.load_icloud_filename_map(photos_db) == {}


# ---------------------------------------------------------------------------
# load_trashed_map variants
# ---------------------------------------------------------------------------
def test_trashed_no_ztrashed_col(tmp_path):
    def t(cur):
        cur.execute("CREATE TABLE ZASSET (Z_PK INTEGER PRIMARY KEY, ZUUID TEXT, "
                    "ZFILENAME TEXT, ZDIRECTORY TEXT)")
    db = _mkphotos(tmp_path, t, [("INSERT INTO ZASSET (ZDIRECTORY,ZFILENAME) VALUES (?,?)",
                                  ("DCIM/100APPLE", "IMG_0001.HEIC"))])
    assert p.load_trashed_map(db) == {}


def test_trashed_empty_dir_or_file(tmp_path):
    def t(cur):
        _add_asset_cols(cur)
    db = _mkphotos(tmp_path, t, [
        ("INSERT INTO ZASSET (Z_PK,ZUUID,ZFILENAME,ZDIRECTORY,ZTRASHEDSTATE) VALUES (1,?,?,?,1)",
         ("U", "", "D")),  # empty ZFILENAME -> skipped (414->411)
        ("INSERT INTO ZASSET (Z_PK,ZUUID,ZFILENAME,ZDIRECTORY,ZTRASHEDSTATE) VALUES (2,?,?,?,1)",
         ("U2", "IMG_0002.HEIC", "")),  # empty ZDIRECTORY -> skipped
    ])
    assert p.load_trashed_map(db) == {}


def test_trashed_outer_except(photos_db, monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("no db")
    monkeypatch.setattr(p.sqlite3, "connect", boom)
    assert p.load_trashed_map(photos_db) == {}


# ---------------------------------------------------------------------------
# load_album_map variants
# ---------------------------------------------------------------------------
def test_album_no_junction(tmp_path):
    # No Z_% junction table -> returns {} (438->447, 448-449).
    def t(cur):
        cur.execute("CREATE TABLE ZGENERICALBUM (Z_PK INTEGER PRIMARY KEY, ZTITLE TEXT, ZKIND INTEGER)")
    db = _mkphotos(tmp_path, t, [("INSERT INTO ZGENERICALBUM (ZTITLE,ZKIND) VALUES (?,2)",
                                  ("Empty",))])
    assert p.load_album_map(db) == {}


def test_album_junction_table_no_match(tmp_path):
    # A Z_% table that is NOT an album junction -> loop skips it (line 444 False).
    def t(cur):
        cur.execute("CREATE TABLE ZGENERICALBUM (Z_PK INTEGER PRIMARY KEY, ZTITLE TEXT, ZKIND INTEGER)")
        cur.execute("CREATE TABLE Z_29NOTALBUM (Z_FOO INTEGER)")
    db = _mkphotos(tmp_path, t, [])
    assert p.load_album_map(db) == {}


def test_album_empty_rel_or_album(tmp_path):
    def t(cur):
        _add_asset_cols(cur)
        cur.execute("CREATE TABLE ZGENERICALBUM (Z_PK INTEGER PRIMARY KEY, ZTITLE TEXT, ZKIND INTEGER)")
        cur.execute("CREATE TABLE Z_29ALBUMLISTS (Z_29ALBUMS INTEGER, Z_30ASSETS INTEGER)")
    db = _mkphotos(tmp_path, t, [
        ("INSERT INTO ZASSET (Z_PK,ZUUID,ZFILENAME,ZDIRECTORY,ZTRASHEDSTATE) VALUES (1,?,?,?,0)",
         ("U", "IMG_0001.HEIC", "DCIM/100APPLE")),
        # Empty (non-NULL) title -> row["album"] is falsy -> joined row skipped (460->459).
        ("INSERT INTO ZGENERICALBUM (Z_PK,ZTITLE,ZKIND) VALUES (1,?,2)", ("",)),
        ("INSERT INTO Z_29ALBUMLISTS (Z_29ALBUMS,Z_30ASSETS) VALUES (1,1)", ()),
    ])
    assert p.load_album_map(db) == {}


def test_album_outer_except(photos_db, monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("no db")
    monkeypatch.setattr(p.sqlite3, "connect", boom)
    assert p.load_album_map(photos_db) == {}


# ---------------------------------------------------------------------------
# scan_camera_roll: empty rel, unmatched path, missing payload in main
# ---------------------------------------------------------------------------
def test_scan_empty_rel(tmp_path):
    bdir = _mkbackup(tmp_path, [
        ("fid1", "CameraRollDomain", "", mf.make_bplist_dates()),  # empty rel -> skip (488)
        ("fid2", "CameraRollDomain", "Media/DCIM/100APPLE/IMG_1000.HEIC",
         mf.make_bplist_dates(mf._epoch(2026, 1, 1), mf._epoch(2026, 1, 1))),
    ])
    items = p.scan_camera_roll(bdir)
    assert len(items) == 1
    assert items[0]["rel"] == "Media/DCIM/100APPLE/IMG_1000.HEIC"


def test_scan_unmatched_path(tmp_path):
    # A media-ish ext but path that matches neither DCIM nor CPLAssets -> skip (498).
    bdir = _mkbackup(tmp_path, [
        ("fid1", "CameraRollDomain", "Media/PhotoData/foo/IMG_9.HEIC",
         mf.make_bplist_dates(mf._epoch(2026, 1, 1), mf._epoch(2026, 1, 1))),
    ])
    assert p.scan_camera_roll(bdir) == []


def test_cli_missing_payload_skipped(tmp_path, capsys):
    # A candidate whose payload file isn't on disk -> payload is None (line 760) -> skipped.
    bdir = _mkbackup(tmp_path, [
        ("deadbeefdeadbeefdeadbeefdeadbeefdeadbeef", "CameraRollDomain",
         "Media/DCIM/100APPLE/IMG_7777.HEIC",
         mf.make_bplist_dates(mf._epoch(2026, 1, 1), mf._epoch(2026, 1, 1))),
        ("facefeedfacefeedfacefeedfacefeedfacefeed", "CameraRollDomain",
         "Media/DCIM/100APPLE/IMG_8888.HEIC",
         mf.make_bplist_dates(mf._epoch(2026, 1, 1), mf._epoch(2026, 1, 1))),
    ], payloads={"facefeedfacefeedfacefeedfacefeedfacefeed": b"present"})
    p.main_with_args(["--backup", str(bdir), "-o", str(tmp_path / "o")])
    out = capsys.readouterr().out
    assert "1 files pass filters" in out  # only the present one survives
    assert list((tmp_path / "o").rglob("IMG_8888*"))


def test_cli_since_nodate_excluded(backup, tmp_path):
    # --since excludes an item with no usable date (modif is None) too (766->769),
    # while keeping items dated at/after the cutoff (the "keep recent" arm).
    out = tmp_path / "out"
    p.main_with_args(["--backup", str(backup), "-o", str(out), "--since", "2026-01-02"])
    # IMG_0001 (2026-01-15) is at/after the cutoff -> kept.
    assert list(out.rglob("IMG_0001*"))
    # Date-less items (IMG_0005, IMG_0010) must be excluded by a since filter.
    assert not list(out.rglob("IMG_0005*"))
    assert not list(out.rglob("IMG_0010*"))


def test_cli_icloud_substring_name(tmp_path):
    # Exercise the iCloud fallback name-replacement loop (778-781): a candidate
    # whose filename stem isn't an exact map key but CONTAINS a map key as a
    # substring -> the substring match renames it to the real filename.
    # A leading NON-matching map key (ZZZ) forces the loop's False arm (779->778)
    # before the matching key (ABC) hits the True arm + break.
    def t(cur):
        _add_asset_cols(cur)
        cur.execute("CREATE TABLE ZADDITIONALASSETATTRIBUTES (Z_PK INTEGER PRIMARY KEY, "
                    "ZORIGINALFILENAME TEXT, ZASSET INTEGER)")
    fid = "feed0000feed0000feed0000feed0000feed0000"
    bdir = _mkbackup(tmp_path, [
        (fid, "CameraRollDomain", "Media/PhotoData/CPLAssets/group1/ABCDEF-1234.HEIC",
         mf.make_bplist_dates(mf._epoch(2026, 1, 1), mf._epoch(2026, 1, 1))),
    ], payloads={fid: b"substring-icloud"})
    # Photos.sqlite inside the backup dir so locate_photos_db() finds it.
    _mkphotos(tmp_path, t, [
        ("INSERT INTO ZASSET (Z_PK,ZUUID,ZFILENAME,ZDIRECTORY,ZTRASHEDSTATE) VALUES (1,?,?,?,0)",
         ("ZZZ", "IMG_ZZZ.PNG", "DCIM/100APPLE")),
        ("INSERT INTO ZASSET (Z_PK,ZUUID,ZFILENAME,ZDIRECTORY,ZTRASHEDSTATE) VALUES (2,?,?,?,0)",
         ("ABC", "IMG_SUB.PNG", "DCIM/100APPLE")),
        ("INSERT INTO ZADDITIONALASSETATTRIBUTES (ZORIGINALFILENAME,ZASSET) VALUES (?,1)",
         ("IMG_ZZZ.PNG",)),
        ("INSERT INTO ZADDITIONALASSETATTRIBUTES (ZORIGINALFILENAME,ZASSET) VALUES (?,2)",
         ("IMG_SUB.PNG",)),
    ], root=bdir)
    out = tmp_path / "o"
    p.main_with_args(["--backup", str(bdir), "-o", str(out)])
    # "ABC" is a substring of candidate name "ABCDEF-1234" -> renamed to IMG_SUB.
    assert list(out.rglob("IMG_SUB*"))


def test_cli_icloud_no_substring(tmp_path):
    # When the candidate name matches neither an exact key nor any key as a
    # substring, the loop EXHAUSTS without breaking (778->783) and the UUID name
    # is kept as-is.
    def t(cur):
        _add_asset_cols(cur)
        cur.execute("CREATE TABLE ZADDITIONALASSETATTRIBUTES (Z_PK INTEGER PRIMARY KEY, "
                    "ZORIGINALFILENAME TEXT, ZASSET INTEGER)")
    fid = "feed9999feed9999feed9999feed9999feed9999"
    bdir = _mkbackup(tmp_path, [
        (fid, "CameraRollDomain", "Media/PhotoData/CPLAssets/group1/NOMATCHID.HEIC",
         mf.make_bplist_dates(mf._epoch(2026, 1, 1), mf._epoch(2026, 1, 1))),
    ], payloads={fid: b"nomatch-icloud"})
    _mkphotos(tmp_path, t, [
        ("INSERT INTO ZASSET (Z_PK,ZUUID,ZFILENAME,ZDIRECTORY,ZTRASHEDSTATE) VALUES (1,?,?,?,0)",
         ("ZZZ", "IMG_ZZZ.PNG", "DCIM/100APPLE")),
        ("INSERT INTO ZADDITIONALASSETATTRIBUTES (ZORIGINALFILENAME,ZASSET) VALUES (?,1)",
         ("IMG_ZZZ.PNG",)),
    ], root=bdir)
    out = tmp_path / "o"
    p.main_with_args(["--backup", str(bdir), "-o", str(out)])
    # "ZZZ" is not a substring of "NOMATCHID" -> name stays UUID.
    assert list(out.rglob("NOMATCHID*"))


# ---------------------------------------------------------------------------
# CLI status-branch handling (copied/skipped/deduped/errored) via a fake executor
# ---------------------------------------------------------------------------
def test_cli_status_handling(backup, tmp_path, monkeypatch, capsys):
    import collections
    from concurrent.futures import Future
    # Deterministic statuses via a fake in-process ProcessPoolExecutor.
    seq = collections.deque([
        ("copied", "IMG_0001.heic", "/d/IMG_0001.heic", 5),
        ("skipped", "IMG_0002.heic", "/d/IMG_0002.heic", 5),
        ("deduped", "IMG_0003.heic", "/d/IMG_0003.heic", 5),
        ("error", "IMG_0004.heic", "/d/IMG_0004.heic", 0),
    ])

    class _Fut(Future):
        def __init__(self, res):
            super().__init__()
            self.set_result(res)

    class _Ex:
        def __init__(self, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def submit(self, _fn, _t):
            return _Fut(seq.popleft() if seq else ("copied", "IMG_x.heic", "/d/x.heic", 5))

    monkeypatch.setattr(p.concurrent.futures, "ProcessPoolExecutor", _Ex)
    p.main_with_args(["--backup", str(backup), "-o", str(tmp_path / "o"), "--workers", "1"])
    out = capsys.readouterr().out
    assert "copied=" in out and "skipped=" in out and "deduped=" in out and "errored=" in out
    assert "Some files failed" in out  # line 881 (errored > 0)


# ---------------------------------------------------------------------------
# _Progress.update speed branch (dt >= 0.5)
# ---------------------------------------------------------------------------
def test_progress_update_speed():
    prog = p._Progress(10, 1000, desc="T")
    prog.update(100)
    time.sleep(0.55)
    prog.update(100)
    assert prog.speed_bytes > 0
    prog.finish()


# ---------------------------------------------------------------------------
# main() entrypoint + __main__ guard (run as a module/script)
# ---------------------------------------------------------------------------
def test_main_function(backup, monkeypatch, capsys):
    # Covers main() (line 887) which delegates to main_with_args(sys.argv[1:]).
    monkeypatch.setattr(sys, "argv", ["prog", "--backup", str(backup), "--dry-run"])
    with pytest.raises(SystemExit) as e:
        p.main()
    assert e.value.code == 0
    assert "DRY-RUN" in capsys.readouterr().out


def test_main_guard(backup, monkeypatch, capsys):
    # Execute the module as __main__ (line 902) via runpy so the guard's body
    # is exercised in-process and counted by coverage.
    import runpy
    monkeypatch.setattr(sys, "argv", ["prog", "--backup", str(backup), "--dry-run"])
    with pytest.raises(SystemExit) as e:
        runpy.run_path("iphone_photos_extractor.py", run_name="__main__")
    assert e.value.code == 0


# ---------------------------------------------------------------------------
# Interactive backup selector (v1.3.0)
# ---------------------------------------------------------------------------

@pytest.fixture
def multi_backup(tmp_path):
    return Path(mf.build_multi_backup_fixture(tmp_path))


def test_find_backups_discovers_and_sorts(multi_backup):
    got = p.find_backups(multi_backup)
    names = [b["name"] for b in got]
    # three backups; newest last-backup is first.
    assert len(got) == 3
    assert names[0] == "Stanley\u2019s iPhone"   # 2026-09-27 (dev-CCC)
    assert names[1] == "Stanley\u2019s iPhone"   # 2026-04-27 (dev-AAA)
    assert names[2] == "Amy Yeung"               # 2025-10-23 (dev-BBB)
    assert got[2]["encrypted"] is True
    assert got[0]["encrypted"] is False
    assert got[0]["model"] == "iPhone 15 Pro Max"


def test_find_backups_missing_dir(tmp_path):
    assert p.find_backups(tmp_path / "nope") == []


def test_find_backups_skips_non_backup(tmp_path):
    (tmp_path / "not-a-backup").mkdir()
    assert p.find_backups(tmp_path) == []


def test_load_backup_info_requires_manifest(tmp_path):
    (tmp_path / "Info.plist").write_bytes(b"x")
    assert p.load_backup_info(tmp_path) is None


def test_load_backup_info_reads_fields(multi_backup):
    dev = p.find_backups(multi_backup)[2]
    assert dev["name"] == "Amy Yeung"
    assert dev["serial"] == "BBB222"
    assert dev["encrypted"] is True
    assert dev["last_backup"].year == 2025


def test_sanitize_folder():
    assert p.sanitize_folder("Stanley\u2019s iPhone") == "Stanley_s_iPhone"
    assert p.sanitize_folder("iPhone 15 ProMax") == "iPhone_15_ProMax"
    assert p.sanitize_folder("a/b:c") == "a_b_c"
    assert p.sanitize_folder("!!!") == "iPhone_Photos"  # empty fallback


def test_suggest_output(monkeypatch, tmp_path):
    fake_home = tmp_path / "home"
    monkeypatch.setattr(p.Path, "home", classmethod(lambda cls: fake_home))
    out = p.suggest_output({"name": "Stanley\u2019s iPhone"})
    assert out == fake_home / "Pictures" / "iPhone" / "Stanley_s_iPhone"


def test_prompt_pick_backup_auto_single_unlocked(monkeypatch, tmp_path):
    # Only ONE unlocked backup -> auto-selected without a prompt.
    mf.make_selector_backup(tmp_path, name="Alone", model="iPhone",
                            encrypted=False)
    mf.make_selector_backup(tmp_path, name="Locked", model="iPhone",
                            encrypted=True)
    monkeypatch.setattr("builtins.input",
                        lambda _: (_ for _ in ()).throw(AssertionError("should not prompt")))
    sel = p.prompt_pick_backup(p.find_backups(tmp_path))
    assert sel["name"] == "Alone"
    assert sel["encrypted"] is False


def test_prompt_pick_backup_menu_blocks_locked(multi_backup, monkeypatch, capsys):
    inputs = iter(["3", ""])
    monkeypatch.setattr("builtins.input", lambda _: next(inputs))
    sel = p.prompt_pick_backup(p.find_backups(multi_backup))
    assert sel is None
    assert "Amy Yeung is encrypted and cannot be selected" in capsys.readouterr().out


def test_prompt_pick_backup_choose_unlocked(multi_backup, monkeypatch):
    monkeypatch.setattr("builtins.input", lambda _: "1")
    sel = p.prompt_pick_backup(p.find_backups(multi_backup))
    assert sel is not None
    assert sel["encrypted"] is False
    assert sel["model"] == "iPhone 15 Pro Max"


def test_prompt_pick_backup_invalid_then_valid(multi_backup, monkeypatch):
    inputs = iter(["zz", "2"])
    monkeypatch.setattr("builtins.input", lambda _: next(inputs))
    sel = p.prompt_pick_backup(p.find_backups(multi_backup))
    assert sel["model"] == "iPhone 12 Pro Max"


def test_prompt_pick_backup_abort(multi_backup, monkeypatch):
    monkeypatch.setattr("builtins.input", lambda _: "")
    assert p.prompt_pick_backup(p.find_backups(multi_backup)) is None


def test_prompt_pick_backup_all_locked(monkeypatch, tmp_path, capsys):
    mf.make_selector_backup(tmp_path, name="Locked1", model="iPhone", encrypted=True)
    mf.make_selector_backup(tmp_path, name="Locked2", model="iPhone", encrypted=True)
    got = p.find_backups(tmp_path)
    assert len(got) == 2
    assert p.prompt_pick_backup(got) is None
    assert "All discovered backups are encrypted" in capsys.readouterr().out


def test_prompt_output_accept_default(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda _: "")
    assert p.prompt_output(Path("/a/b")) == Path("/a/b")


def test_prompt_output_custom(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda _: "/custom/out")
    assert p.prompt_output(Path("/a/b")) == Path("/custom/out").expanduser()


def test_prompt_yes_no(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda _: "y")
    assert p.prompt_yes_no("?") is True
    monkeypatch.setattr("builtins.input", lambda _: "n")
    assert p.prompt_yes_no("?") is False
    monkeypatch.setattr("builtins.input", lambda _: "")
    assert p.prompt_yes_no("?", default="no") is False


def test_interactive_main_no_backups(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(p, "default_backup_dir", lambda: tmp_path / "empty")
    # No input needed; returns None on no backups.
    assert p.interactive_main([]) is None
    assert "No iPhone/iPad backups found" in capsys.readouterr().out


def test_interactive_main_roundtrip(monkeypatch, multi_backup, tmp_path):
    fake_home = tmp_path / "home"
    monkeypatch.setattr(p, "default_backup_dir", lambda: multi_backup)
    monkeypatch.setattr(p.Path, "home", classmethod(lambda cls: fake_home))
    # pick backup #1 (newest unlocked Stanley iPhone 15), accept output,
    # format=ym, no albums, no trash, no prepend-date, then extract now.
    inputs = iter(["1", "", "", "n", "n", "n", "y"])
    monkeypatch.setattr("builtins.input", lambda _: next(inputs))
    p.interactive_main([])
    # output folder is inside the fake home; files were extracted.
    out = fake_home / "Pictures" / "iPhone" / "Stanley_s_iPhone"
    assert out.is_dir()
    assert list(out.rglob("IMG_0001*"))


def test_interactive_main_cancel_at_confirm(monkeypatch, multi_backup, tmp_path):
    monkeypatch.setattr(p, "default_backup_dir", lambda: multi_backup)
    monkeypatch.setattr(p.Path, "home", classmethod(lambda cls: tmp_path / "home"))
    inputs = iter(["1", "", "", "n", "n", "n", "n"])  # last: don't extract
    monkeypatch.setattr("builtins.input", lambda _: next(inputs))
    p.interactive_main([])
    # no extraction occurred (cancelled before copy)
    assert not (tmp_path / "home" / "Pictures").exists()


def test_main_noninteractive_requires_backup(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["prog", "--non-interactive"])
    with pytest.raises(SystemExit) as e:
        p.main()
    assert e.value.code == 1


def test_main_interactive_flag_forces_wizard(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(p, "default_backup_dir", lambda: tmp_path / "empty")
    monkeypatch.setattr(sys, "argv", ["prog", "--interactive"])
    # --interactive skips the TTY gate; no backups -> returns cleanly.
    p.main()
    assert "No iPhone/iPad backups found" in capsys.readouterr().out


def test_main_auto_wizard_on_missing_backup(monkeypatch, tmp_path, capsys):
    import types
    monkeypatch.setattr(p, "default_backup_dir", lambda: tmp_path / "empty")
    monkeypatch.setattr(sys, "argv", ["prog"])  # no --backup, no --non-interactive
    # Pretend stdin is a TTY so the auto-wizard triggers.
    monkeypatch.setattr(p.sys, "stdin", types.SimpleNamespace(isatty=lambda: True))
    p.main()
    assert "No iPhone/iPad backups found" in capsys.readouterr().out


def test_main_uses_normal_path_with_backup(monkeypatch, backup, capsys):
    monkeypatch.setattr(sys, "argv", ["prog", "--backup", str(backup), "--dry-run"])
    with pytest.raises(SystemExit) as e:
        p.main()
    assert e.value.code == 0
    assert "DRY-RUN" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Extra coverage for interactive helpers (v1.3.0)
# ---------------------------------------------------------------------------

def test_load_backup_info_corrupt_plists(tmp_path):
    # Present Manifest.db but unreadable/invalid plists -> fall back to unnamed.
    (tmp_path / "Manifest.db").touch()
    (tmp_path / "Info.plist").write_bytes(b"not a plist")
    (tmp_path / "Manifest.plist").write_bytes(b"nope")
    info = p.load_backup_info(tmp_path)
    assert info is not None
    assert info["name"] == "Unnamed Device"
    assert info["model"] == "iPhone"
    assert info["encrypted"] is False
    assert info["serial"] == ""


def test_load_backup_info_size_zero_when_no_subdirs(tmp_path):
    (tmp_path / "Manifest.db").touch()
    (tmp_path / "Info.plist").write_bytes(plistlib.dumps({"Device Name": "X"}))
    (tmp_path / "Manifest.plist").write_bytes(plistlib.dumps({"IsEncrypted": False}))
    info = p.load_backup_info(tmp_path)
    assert info["size"] == 0


def test_read_returns_none_on_eof(monkeypatch):
    def boom(_):
        raise EOFError
    monkeypatch.setattr("builtins.input", boom)
    assert p._read("?") is None


def test_read_returns_none_on_keyboard_interrupt(monkeypatch):
    def boom(_):
        raise KeyboardInterrupt
    monkeypatch.setattr("builtins.input", boom)
    assert p._read("?") is None


def test_prompt_pick_backup_no_backups(monkeypatch, capsys):
    monkeypatch.setattr("builtins.input", lambda _: (_ for _ in ()).throw(AssertionError))
    assert p.prompt_pick_backup([]) is None
    assert "No backups found." in capsys.readouterr().out


def test_prompt_output_abort_on_eof(monkeypatch):
    def boom(_):
        raise EOFError
    monkeypatch.setattr("builtins.input", boom)
    assert p.prompt_output(Path("/a/b")) is None


def test_prompt_yes_no_eof_default(monkeypatch):
    def boom(_):
        raise EOFError
    monkeypatch.setattr("builtins.input", boom)
    assert p.prompt_yes_no("?", default="yes") is True
    assert p.prompt_yes_no("?", default="no") is False


def test_interactive_main_with_backup_and_output(monkeypatch, backup, tmp_path):
    # --backup and -o supplied -> no discovery/prompting for them.
    out = tmp_path / "out"
    monkeypatch.setattr(p.Path, "home", classmethod(lambda cls: tmp_path / "home"))
    inputs = iter(["", "n", "n", "n", "y"])   # format default, no albums/trash/prepend, extract
    monkeypatch.setattr("builtins.input", lambda _: next(inputs))
    p.interactive_main(["--backup", str(backup), "-o", str(out)])
    assert list(out.rglob("IMG_0001*"))


def test_interactive_main_format_flat(monkeypatch, backup, tmp_path):
    out = tmp_path / "out"
    monkeypatch.setattr(p.Path, "home", classmethod(lambda cls: tmp_path / "home"))
    inputs = iter(["flat", "n", "n", "n", "y"])
    monkeypatch.setattr("builtins.input", lambda _: next(inputs))
    p.interactive_main(["--backup", str(backup), "-o", str(out)])
    # flat puts files at the top level, not in a YYYY-MM subfolder.
    assert list(out.glob("IMG_0001*"))


def test_interactive_main_present_flags_skip_prompts(monkeypatch, backup, tmp_path):
    out = tmp_path / "out"
    monkeypatch.setattr(p.Path, "home", classmethod(lambda cls: tmp_path / "home"))
    # All option flags already present -> no prompting for them.
    inputs = iter(["y"])  # only the "Extract now?" confirm remains
    monkeypatch.setattr("builtins.input", lambda _: next(inputs))
    p.interactive_main(["--backup", str(backup), "-o", str(out),
                        "--format=flat", "--albums", "--add-trash", "--prepend-date"])
    assert any(out.rglob("*.*"))  # files were copied (into album/flat folders)


def test_interactive_main_aborts_at_output_none(monkeypatch, multi_backup, tmp_path):
    monkeypatch.setattr(p, "default_backup_dir", lambda: multi_backup)
    monkeypatch.setattr(p.Path, "home", classmethod(lambda cls: tmp_path / "home"))
    # Choose backup #1, then EOF on the output prompt -> abort.
    monkeypatch.setattr("builtins.input", lambda _: (_ for _ in ()).throw(EOFError))
    assert p.interactive_main([]) is None


def test_default_backup_dir():
    from pathlib import Path as _P
    assert p.default_backup_dir() == _P.home() / "Library" / "Application Support" / "MobileSync" / "Backup"


def test_find_backups_skips_plain_file(tmp_path):
    (tmp_path / "a-file.txt").touch()          # not a dir -> skipped
    mf.make_selector_backup(tmp_path, name="RealDevice", encrypted=False)
    got = p.find_backups(tmp_path)
    assert [b["name"] for b in got] == ["RealDevice"]


def test_fmt_date_unknown():
    assert p._fmt_date(None) == "unknown"


def test_prompt_pick_backup_chosen_out_of_range(multi_backup, monkeypatch):
    inputs = iter(["99", "1"])
    monkeypatch.setattr("builtins.input", lambda _: next(inputs))
    sel = p.prompt_pick_backup(p.find_backups(multi_backup))
    assert sel is not None  # invalid then valid


def test_load_backup_info_size_exception(monkeypatch, tmp_path):
    (tmp_path / "Manifest.db").touch()
    (tmp_path / "Info.plist").write_bytes(plistlib.dumps({"Device Name": "X"}))
    (tmp_path / "Manifest.plist").write_bytes(plistlib.dumps({"IsEncrypted": False}))
    monkeypatch.setattr(p.os, "listdir", lambda _: (_ for _ in ()).throw(OSError("boom")))
    assert p.load_backup_info(tmp_path)["size"] == 0


def test_tree_size_exception(monkeypatch, tmp_path):
    (tmp_path / "a").mkdir()
    monkeypatch.setattr(p.Path, "rglob",
                        lambda self, _: (_ for _ in ()).throw(OSError("boom")))
    assert p._tree_size(tmp_path) == 0


def test_tree_size_recursive(tmp_path):
    # A nested directory (is_file() False branch) is walked, not just files.
    (tmp_path / "aa" / "nested").mkdir(parents=True)
    (tmp_path / "aa" / "nested" / "a.bin").write_bytes(b"1234567890")
    (tmp_path / "aa" / "b.bin").write_bytes(b"12345")
    assert p._tree_size(tmp_path) == 15


def test_interactive_main_backup_equals_form_and_o_abort(monkeypatch, backup, tmp_path):
    # --backup=<path> (equals form) with NO -o -> hits out-scan else, then
    # the output prompt aborts on EOF -> return None (line 1255).
    monkeypatch.setattr(p.Path, "home", classmethod(lambda cls: tmp_path / "home"))
    def boom(_):
        raise EOFError
    monkeypatch.setattr("builtins.input", boom)
    assert p.interactive_main([f"--backup={backup}"]) is None


def test_interactive_main_compact_o_and_output_equals(monkeypatch, backup, tmp_path):
    # --backup (space) + -o<path> (compact) -> out-scan line 1247 + cleaning 1269.
    out = tmp_path / "out"
    monkeypatch.setattr(p.Path, "home", classmethod(lambda cls: tmp_path / "home"))
    inputs = iter(["", "n", "n", "n", "y"])
    monkeypatch.setattr("builtins.input", lambda _: next(inputs))
    p.interactive_main(["--backup", str(backup), f"-o{out}"])
    assert list(out.rglob("IMG_0001*"))


def test_interactive_main_output_equals_form(monkeypatch, backup, tmp_path):
    out = tmp_path / "out"
    monkeypatch.setattr(p.Path, "home", classmethod(lambda cls: tmp_path / "home"))
    inputs = iter(["", "n", "n", "n", "y"])
    monkeypatch.setattr("builtins.input", lambda _: next(inputs))
    p.interactive_main(["--backup", str(backup), f"--output={out}"])
    assert list(out.rglob("IMG_0001*"))


def test_interactive_main_yes_options(monkeypatch, backup, tmp_path):
    out = tmp_path / "out"
    monkeypatch.setattr(p.Path, "home", classmethod(lambda cls: tmp_path / "home"))
    # format default, then yes to albums / trash / prepend, then extract.
    inputs = iter(["", "y", "y", "y", "y"])
    monkeypatch.setattr("builtins.input", lambda _: next(inputs))
    p.interactive_main(["--backup", str(backup), "-o", str(out)])
    assert any(out.rglob("*.*"))


def test_interactive_main_dry_run_failure_returns(monkeypatch, tmp_path):
    # A bad --backup leads main_with_args(--dry-run) to exit non-zero -> return.
    # Pre-provision all option flags so no prompt intervenes before the dry-run.
    monkeypatch.setattr(p.Path, "home", classmethod(lambda cls: tmp_path / "home"))
    monkeypatch.setattr("builtins.input", lambda _: (_ for _ in ()).throw(AssertionError))
    assert p.interactive_main(["--backup", str(tmp_path / "nope"), "-o", str(tmp_path / "o"),
                               "--format=ym", "--albums", "--add-trash", "--prepend-date"]) is None
