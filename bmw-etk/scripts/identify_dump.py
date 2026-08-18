#!/usr/bin/env python3
"""Identify what a BMW ETK dump actually is, without loading it.

Reads only the first few KB of each file (plus a couple of known offsets), so it
is instant even on a multi-GB dump. Works on a single file or a whole folder.

    python3 bmw-etk/scripts/identify_dump.py /path/to/dump
    python3 bmw-etk/scripts/identify_dump.py            # scans ./bmw-etk/dump
"""
import os
import sys

# (label, offset, magic bytes) -- checked in order, first match wins.
MAGIC = [
    ("SQLite 3 database", 0, b"SQLite format 3\x00"),
    ("MS SQL Server backup (MTF .bak)", 0, b"TAPE"),
    ("PostgreSQL custom dump (pg_restore)", 0, b"PGDMP"),
    ("7-Zip archive", 0, b"7z\xbc\xaf\x27\x1c"),
    ("ZIP archive", 0, b"PK\x03\x04"),
    ("RAR archive", 0, b"Rar!\x1a\x07"),
    ("gzip archive", 0, b"\x1f\x8b"),
    ("bzip2 archive", 0, b"BZh"),
    ("XZ archive", 0, b"\xfd7zXZ"),
    ("MS Access (Jet) .mdb", 4, b"Standard Jet DB"),
    ("MS Access (ACE) .accdb", 4, b"Standard ACE DB"),
    ("tar archive", 257, b"ustar"),
    ("ISO 9660 disc image", 0x8001, b"CD001"),
    ("UDF disc image", 0x8001, b"BEA01"),
]

# Substrings searched for in the first 64 KB when no magic number matched.
SNIFF = [
    ("MS SQL Server data file (.mdf/.ndf)", [b"Microsoft SQL Server"]),
    ("Sybase SQL Anywhere / Adaptive Server database", [b"SQL Anywhere", b"Adaptive Server", b"Watcom"]),
    ("Firebird / InterBase database", [b"Firebird", b"InterBase"]),
    ("MySQL / MariaDB SQL text dump", [b"MySQL dump", b"MariaDB dump", b"CREATE TABLE"]),
    ("Microsoft Cabinet installer payload", [b"MSCF"]),
]

INTERESTING_EXT = {
    ".bak", ".mdf", ".ldf", ".ndf", ".trn", ".fdb", ".gdb", ".fbk", ".db",
    ".sqlite", ".sqlite3", ".mdb", ".accdb", ".dbf", ".iso", ".7z", ".zip",
    ".rar", ".gz", ".tar", ".sql", ".dat", ".idx", ".csv", ".xml", ".json",
}


def human(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:,.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024.0


def identify(path):
    """Return a human description of one file, reading only its head."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            head = fh.read(65536)
            tail_probe = b""
            if size > 0x8010:
                fh.seek(0x8001)
                tail_probe = fh.read(16)
    except OSError as exc:
        return f"unreadable ({exc.strerror})", 0

    for label, off, magic in MAGIC:
        chunk = tail_probe if off == 0x8001 else head[off:off + len(magic)]
        if off == 0x8001:
            if chunk.startswith(magic):
                return label, size
        elif chunk == magic:
            return label, size

    for label, needles in SNIFF:
        if any(n in head for n in needles):
            return label, size

    # Firebird/InterBase header page: pag_type 0x01 with an ODS version word.
    if len(head) > 20 and head[0] == 0x01 and head[1] == 0x00 and os.path.splitext(path)[1].lower() in (".fdb", ".gdb"):
        return "Firebird / InterBase database (by header + extension)", size

    printable = sum(1 for b in head[:4096] if 9 <= b <= 13 or 32 <= b <= 126)
    if head and printable / max(1, len(head[:4096])) > 0.90:
        return "plain text / SQL script", size

    return f"unknown binary (first bytes: {head[:12].hex(' ')})", size


def main():
    root = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "dump")
    root = os.path.expanduser(root)

    if not os.path.exists(root):
        sys.exit(f"Nothing at: {root}\nPass the path to the dump, e.g.\n"
                 f"  python3 {sys.argv[0]} ~/Downloads/etk")

    print(f"Scanning: {root}\n")

    if os.path.isfile(root):
        label, size = identify(root)
        print(f"  {human(size):>12}  {label}\n      {root}")
        return

    files, by_ext, total = [], {}, 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in (".git", "__MACOSX")]
        for name in filenames:
            if name.startswith("."):
                continue
            full = os.path.join(dirpath, name)
            try:
                size = os.path.getsize(full)
            except OSError:
                continue
            ext = os.path.splitext(name)[1].lower()
            files.append((size, full, ext))
            agg = by_ext.setdefault(ext or "(none)", [0, 0])
            agg[0] += 1
            agg[1] += size
            total += size

    if not files:
        sys.exit("Folder is empty — is the dump somewhere else?")

    print(f"{len(files):,} files, {human(total)} total\n")
    print("By extension:")
    for ext, (count, size) in sorted(by_ext.items(), key=lambda kv: -kv[1][1])[:25]:
        star = " *" if ext in INTERESTING_EXT else ""
        print(f"  {ext:<12} {count:>7,} files  {human(size):>12}{star}")

    files.sort(reverse=True)
    print("\n30 largest files (* = likely the database):")
    for size, full, ext in files[:30]:
        label, _ = identify(full)
        print(f"  {human(size):>12}  {label}")
        print(f"                {os.path.relpath(full, root)}")


if __name__ == "__main__":
    main()
