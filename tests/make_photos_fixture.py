#!/usr/bin/env python3
"""Build a small synthetic iOS backup + Photos.sqlite for testing.

Creates a fake but structurally-correct iPhone backup (the same layout the
extractor reads): a ``Manifest.db`` with a ``Files`` table, real payload files
under ``<fileID[:2]>/<fileID>``, and a ``Photos.sqlite`` with the ZASSET /
ZADDITIONALASSETATTRIBUTES / ZEXTENDEDATTRIBUTES / ZGENERICALBUM schema used
to recover real filenames, albums, and trashed state.

Run directly to write to a temp dir and print the path, or import
``build_fixture`` in tests.

The fixture deliberately covers both real-media and edge cases so the test suite
can exercise every branch without touching a real 20 GB backup:
  * DCIM files (IMG_xxxx) with plausible dates.
  * CPLAssets (iCloud) files named as UUIDs -> recovered to IMG_xxxx names.
  * A bad (garbage) birth/modified double (-4.76e87).
  * An Apple-epoch-0 "date unset" sentinel (2001-01-01).
  * A junk future date (year ~2273).
  * A trashed asset (ZTRASHEDSTATE=1).
  * A user album (via a Z_<n>ALBUMS/Z_<n>ASSETS junction table).
  * An old-schema ZEXTENDEDATTRIBUTES.ZPLISTDATA bplist originalFilename.
"""

import os
import sqlite3
import struct
import sys
from datetime import datetime
from pathlib import Path

APPLE_EPOCH = 978307200          # seconds between 1970-01-01 and 2001-01-01
GARBAGE_DOUBLE = struct.unpack(">d", b"\xff\xff\xff\xff\xff\xff\xff\xff")[0] * 1e70


def _epoch(year, month=1, day=1, hour=0, minute=0, second=0):
    """Return an Apple-epoch float for a UTC datetime."""
    dt = datetime(year, month, day, hour, minute, second)
    return (dt.timestamp() - APPLE_EPOCH)


def make_bplist_dates(birth_epoch=None, mod_epoch=None):
    """Produce a minimal binary-plist blob that _parse_bplist_dates() can scan.

    The parser looks for byte 0x33 (NSDate marker) followed by an 8-byte
    big-endian double (Apple epoch). It extracts whichever markers it finds and
    keeps ones that convert to a plausible unix time.
    """
    blob = b""
    if birth_epoch is not None:
        blob += b"\x33" + struct.pack(">d", float(birth_epoch))
    if mod_epoch is not None:
        blob += b"\x33" + struct.pack(">d", float(mod_epoch))
    return blob


def _make_payload(root, file_id, data):
    """Write <root>/<fileID[:2]>/<fileID> and return it."""
    p = Path(root) / file_id[:2]
    p.mkdir(parents=True, exist_ok=True)
    path = p / file_id
    with open(path, "wb") as f:
        f.write(data)
    return path


def _add_row(rows, domain, rel, bplist_blob):
    """Deterministically derive a fileID from the relative path so the fixture
    is repeatable and payloads map cleanly."""
    import hashlib
    file_id = hashlib.sha1(f"{domain}|{rel}".encode()).hexdigest()
    rows.append((file_id, domain, rel, bplist_blob))
    return file_id


def build_fixture(root=None, db_dir=None):
    """Create a fake backup under ``root`` (default: a fresh temp dir).

    Returns the backup dir Path.
    """
    import tempfile
    if root is None:
        root = tempfile.mkdtemp(prefix="iphone_photos_fix_")
    root = Path(root)
    db_dir = Path(db_dir) if db_dir else root
    db_dir.mkdir(parents=True, exist_ok=True)

    # --- Build a list of (fileID, domain, relativePath, bplist) files ---
    rows = []
    DOMAIN = "CameraRollDomain"
    media = []

    # 1) A normal DCIM photo with plausible birth+modified (2026-01-15).
    bi = _epoch(2026, 1, 15, 10, 30, 0)
    mo = _epoch(2026, 1, 15, 10, 31, 0)
    fid = _add_row(rows, DOMAIN, "Media/DCIM/100APPLE/IMG_0001.HEIC",
                   make_bplist_dates(bi, mo))
    media.append({"rel": "Media/DCIM/100APPLE/IMG_0001.HEIC", "fid": fid,
                  "data": b"fake-heic-bytes", "uuid": None, "fn": "IMG_0001"})

    # 2) A normal DCIM photo with only a modified date (birth None).
    mo2 = _epoch(2026, 2, 3, 8, 0, 0)
    fid2 = _add_row(rows, DOMAIN, "Media/DCIM/100APPLE/IMG_0002.PNG",
                    make_bplist_dates(None, mo2))
    media.append({"rel": "Media/DCIM/100APPLE/IMG_0002.PNG", "fid": fid2,
                  "data": b"fakepng", "uuid": None, "fn": "IMG_0002"})

    # 3) An iCloud (CPLAssets) UUID file that should be recovered to IMG_0003.
    uuid3 = "A1B2C3D4-E5F6-7890-ABCD-EF1234567890"
    fid3 = _add_row(rows, DOMAIN, f"Media/PhotoData/CPLAssets/group101/{uuid3}.HEIC",
                    make_bplist_dates(_epoch(2026, 3, 1, 9, 0, 0),
                                      _epoch(2026, 3, 1, 9, 0, 0)))
    media.append({"rel": f"Media/PhotoData/CPLAssets/group101/{uuid3}.HEIC",
                  "fid": fid3, "data": b"icloud-heic", "uuid": uuid3,
                  "fn": "IMG_0003"})

    # 4) A garbage birth date (huge negative double) -> should be treated no-date.
    fid4 = _add_row(rows, DOMAIN, "Media/DCIM/100APPLE/IMG_0004.MOV",
                    make_bplist_dates(GARBAGE_DOUBLE, _epoch(2026, 4, 1, 12, 0, 0)))
    media.append({"rel": "Media/DCIM/100APPLE/IMG_0004.MOV", "fid": fid4,
                  "data": b"fakemov", "uuid": None, "fn": "IMG_0004"})

    # 5) An Apple-epoch-0 sentinel (2001-01-01) -> date unset -> No_Date.
    fid5 = _add_row(rows, DOMAIN, "Media/DCIM/100APPLE/IMG_0005.JPG",
                    make_bplist_dates(0, 0))
    media.append({"rel": "Media/DCIM/100APPLE/IMG_0005.JPG", "fid": fid5,
                  "data": b"fakejpg", "uuid": None, "fn": "IMG_0005"})

    # 6) A junk future date (year ~2273) -> rejected by upper bound -> No_Date.
    fid6 = _add_row(rows, DOMAIN, "Media/DCIM/101APPLE/IMG_0006.MP4",
                    make_bplist_dates(_epoch(2026, 5, 1, 0, 0, 0),
                                      9568889476.533798))
    media.append({"rel": "Media/DCIM/101APPLE/IMG_0006.MP4", "fid": fid6,
                  "data": b"fakemp4", "uuid": None, "fn": "IMG_0006"})

    # 7) A trashed asset (ZTRASHEDSTATE=1) -> only extracted with --add-trash.
    fid7 = _add_row(rows, DOMAIN, "Media/DCIM/100APPLE/IMG_0007.HEIC",
                    make_bplist_dates(_epoch(2026, 6, 1, 0, 0, 0),
                                      _epoch(2026, 6, 1, 0, 0, 0)))
    media.append({"rel": "Media/DCIM/100APPLE/IMG_0007.HEIC", "fid": fid7,
                  "data": b"trashed-heic", "uuid": None, "fn": "IMG_0007",
                  "trashed": True})

    # 8) An old-schema extended-attributes file (ZEXTENDEDATTRIBUTES.ZPLISTDATA).
    uuid8 = "BBBBBBBB-1111-2222-3333-444444444444"
    fid8 = _add_row(rows, DOMAIN, f"Media/PhotoData/CPLAssets/group102/{uuid8}.JPEG",
                    make_bplist_dates(_epoch(2026, 7, 1, 0, 0, 0),
                                      _epoch(2026, 7, 1, 0, 0, 0)))
    media.append({"rel": f"Media/PhotoData/CPLAssets/group102/{uuid8}.JPEG",
                  "fid": fid8, "data": b"old-icloud-jpeg", "uuid": uuid8,
                  "fn": "IMG_0008", "extattr": True})

    # 9) A non-media file (thumbnail) that must be skipped by the scan.
    _add_row(rows, DOMAIN, "Media/PhotoData/Thumbnails/V2/IMG_0001-thumb.jpg",
             make_bplist_dates(_epoch(2026, 1, 1, 0, 0, 0),
                               _epoch(2026, 1, 1, 0, 0, 0)))

    # 10) A file with no date at all (bplist has neither birth nor modified).
    fid10 = _add_row(rows, DOMAIN, "Media/DCIM/102APPLE/IMG_0010.DNG",
                     make_bplist_dates())
    media.append({"rel": "Media/DCIM/102APPLE/IMG_0010.DNG", "fid": fid10,
                  "data": b"fakeng", "uuid": None, "fn": "IMG_0010"})

    # 11) A file with an extension we don't recognize (should be excluded).
    _add_row(rows, DOMAIN, "Media/DCIM/100APPLE/IMG_0011.xyz",
             make_bplist_dates(_epoch(2026, 1, 1, 0, 0, 0),
                               _epoch(2026, 1, 1, 0, 0, 0)))

    # 12) A file in an album (used to test --albums).
    fid12 = _add_row(rows, DOMAIN, "Media/DCIM/100APPLE/IMG_0012.HEIC",
                     make_bplist_dates(_epoch(2026, 8, 1, 0, 0, 0),
                                       _epoch(2026, 8, 1, 0, 0, 0)))
    media.append({"rel": "Media/DCIM/100APPLE/IMG_0012.HEIC", "fid": fid12,
                  "data": b"album-heic", "uuid": None, "fn": "IMG_0012",
                  "album": "Trip"})

    # --- Write the payload files ---
    payloads = {}
    for m in media:
        payloads[m["rel"]] = _make_payload(root, m["fid"], m["data"])

    # --- Manifest.db (Files table) ---
    mdb = db_dir / "Manifest.db"
    if mdb.exists():
        mdb.unlink()
    conn = sqlite3.connect(str(mdb))
    cur = conn.cursor()
    cur.execute("CREATE TABLE Files (fileID TEXT, domain TEXT, relativePath TEXT, file BLOB)")
    cur.executemany("INSERT INTO Files VALUES (?,?,?,?)", rows)
    # A couple of rows for other domains so domain filtering is exercised.
    cur.execute("INSERT INTO Files VALUES (?,?,?,?)",
                (_add_row([], "OtherDomain", "Some/other.bin", b""), "OtherDomain",
                 "Some/other.bin", b""))
    conn.commit()
    conn.close()

    # --- Photos.sqlite ---
    psdir = db_dir / "12"
    psdir.mkdir(parents=True, exist_ok=True)
    ps = psdir / "12b144c0bd44f2b3dffd9186d3f9c05b917cee25"
    if ps.exists():
        ps.unlink()
    c = sqlite3.connect(str(ps))
    cc = c.cursor()

    # ZASSET: Z_PK (rowid), ZUUID, ZFILENAME, ZDIRECTORY, ZTRASHEDSTATE
    cc.execute("CREATE TABLE ZASSET (Z_PK INTEGER PRIMARY KEY, ZUUID TEXT, "
               "ZFILENAME TEXT, ZDIRECTORY TEXT, ZTRASHEDSTATE INTEGER)")
    # ZADDITIONALASSETATTRIBUTES: Z_PK, ZORIGINALFILENAME, ZASSET (FK)
    cc.execute("CREATE TABLE ZADDITIONALASSETATTRIBUTES (Z_PK INTEGER PRIMARY KEY, "
               "ZORIGINALFILENAME TEXT, ZASSET INTEGER)")
    # ZEXTENDEDATTRIBUTES: Z_PK, ZPLISTDATA BLOB, ZASSET (FK)  -- old schema
    cc.execute("CREATE TABLE ZEXTENDEDATTRIBUTES (Z_PK INTEGER PRIMARY KEY, "
               "ZPLISTDATA BLOB, ZASSET INTEGER)")
    # ZGENERICALBUM + a junction table with Z_<n>ALBUMS / Z_<n>ASSETS columns
    cc.execute("CREATE TABLE ZGENERICALBUM (Z_PK INTEGER PRIMARY KEY, ZTITLE TEXT, "
               "ZKIND INTEGER)")
    cc.execute("CREATE TABLE Z_29ALBUMLISTS (Z_29ALBUMS INTEGER, Z_30ASSETS INTEGER)")

    # Insert assets. rowid used as Z_PK, return pk.
    for idx, m in enumerate(media, start=1):
        dirpart = m["rel"].split("Media/")[1].rsplit("/", 1)[0]
        # ZFILENAME carries the extension (e.g. IMG_0001.HEIC) in real Photos.
        zfilename = os.path.basename(m["rel"].replace("\\", "/"))
        cc.execute("INSERT INTO ZASSET (Z_PK, ZUUID, ZFILENAME, ZDIRECTORY, ZTRASHEDSTATE) "
                   "VALUES (?,?,?,?,?)",
                   (idx, m["uuid"], zfilename, dirpart, 1 if m.get("trashed") else 0))

    # Newest-schema original filenames for iCloud assets (IMG_0003, IMG_0008).
    for m in media:
        if m["uuid"] and not m.get("extattr"):
            cc.execute("INSERT INTO ZADDITIONALASSETATTRIBUTES (ZORIGINALFILENAME, ZASSET) "
                       "VALUES (?,?)", (m["fn"] + ".HEIC", media.index(m) + 1))

    # Old-schema bplist originalFilename for the extattr asset (IMG_0008).
    for m in media:
        if m.get("extattr"):
            # 0x5D = ASCII string, length 13 ("IMG_0008.JPEG").
            blob = b"com.apple.assetsd.originalFilename" + struct.pack("B", 0x5D) \
                + b"IMG_0008.JPEG"
            cc.execute("INSERT INTO ZEXTENDEDATTRIBUTES (ZPLISTDATA, ZASSET) "
                       "VALUES (?,?)", (blob, media.index(m) + 1))

    # Albums: 'Trip' (ZKIND=2) containing asset 12 (and asset 1).
    cc.execute("INSERT INTO ZGENERICALBUM (Z_PK, ZTITLE, ZKIND) VALUES (1,'Trip',2)")
    cc.execute("INSERT INTO ZGENERICALBUM (Z_PK, ZTITLE, ZKIND) VALUES (2,'Untitled',1)")
    # junction: album1 -> asset 12 (pk 12) and asset 1 (pk 1)
    idx12 = media.index(next(m for m in media if m.get("album")))+1
    cc.execute("INSERT INTO Z_29ALBUMLISTS (Z_29ALBUMS, Z_30ASSETS) VALUES (1,?)", (idx12,))
    cc.execute("INSERT INTO Z_29ALBUMLISTS (Z_29ALBUMS, Z_30ASSETS) VALUES (1,1)")
    # A deliberately empty asset row (no media) to exercise NULL handling.
    cc.execute("INSERT INTO ZASSET (Z_PK, ZUUID, ZFILENAME, ZDIRECTORY, ZTRASHEDSTATE) "
               "VALUES (100, NULL, NULL, NULL, NULL)")
    c.commit()
    c.close()

    return root


if __name__ == "__main__":
    b = build_fixture()
    print("fixture backup:", b)