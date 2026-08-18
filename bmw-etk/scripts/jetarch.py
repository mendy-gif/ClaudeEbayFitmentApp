#!/usr/bin/env python3
"""Read a BMW ETK ".jetarch" data package (msg systems' "Jetstream" container).

Format, decoded from the archive header and verified field by field:

    'RLFF'  u32 version
    then repeating file records:
        'FILE'  u16 name_len  name[name_len]  u64 declared_size
        then repeating chunks (until the next marker is not CHNK):
            'CHNK'  u64 chunk_len  data[chunk_len]

All integers are big-endian. The six ETK-Data_*.jetarch.part* files are treated
as one continuous stream, in part order.

Nothing here loads the archive into memory -- it streams, so it runs in a few MB
of RAM over a 5.8 GB archive.

    python3 jetarch.py probe   "/Volumes/BMW ETK 2020-01"
    python3 jetarch.py list    "/Volumes/BMW ETK 2020-01"
    python3 jetarch.py extract "/Volumes/BMW ETK 2020-01" -o out/ --only "*.sql"
"""
import argparse
import fnmatch
import hashlib
import os
import struct
import sys
from bisect import bisect_right

MAGIC = b"RLFF"
M_FILE = b"FILE"
M_CHNK = b"CHNK"
COPY_BUF = 1 << 20


def human(n):
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:,.1f} {unit}"
        n /= 1024.0


class MultiPartReader:
    """Presents an ordered list of files as one seekable read-only stream."""

    def __init__(self, paths):
        if not paths:
            raise ValueError("no parts given")
        self.paths = list(paths)
        self.sizes = [os.path.getsize(p) for p in self.paths]
        self.total = sum(self.sizes)
        self.starts = []
        off = 0
        for size in self.sizes:
            self.starts.append(off)
            off += size
        self._idx = 0
        self._fh = open(self.paths[0], "rb")
        self._pos = 0

    def close(self):
        if self._fh:
            self._fh.close()
            self._fh = None

    def tell(self):
        return self._pos

    def seek(self, pos):
        pos = max(0, min(pos, self.total))
        idx = max(0, bisect_right(self.starts, pos) - 1)
        if idx != self._idx:
            self._fh.close()
            self._idx = idx
            self._fh = open(self.paths[idx], "rb")
        self._fh.seek(pos - self.starts[idx])
        self._pos = pos

    def read(self, n):
        out = bytearray()
        while n > 0:
            buf = self._fh.read(n)
            if buf:
                out += buf
                n -= len(buf)
                self._pos += len(buf)
                continue
            if self._idx + 1 >= len(self.paths):
                break  # genuine end of the whole stream
            self._fh.close()
            self._idx += 1
            self._fh = open(self.paths[self._idx], "rb")
        return bytes(out)

    def copy_to(self, sink, count):
        """Stream `count` bytes into an open file handle (or skip if None).

        Skipping seeks rather than reads, so listing a 5.8 GB archive costs
        almost nothing.
        """
        if sink is None:
            start = self._pos
            self.seek(min(self._pos + count, self.total))
            return self._pos - start
        left = count
        while left > 0:
            buf = self.read(min(COPY_BUF, left))
            if not buf:
                break
            if sink is not None:
                sink.write(buf)
            left -= len(buf)
        return count - left


def find_parts(root):
    """Locate the ordered .jetarch.partN files, given a folder or a part path."""
    root = os.path.expanduser(root)
    if os.path.isfile(root):
        base = root
        if ".jetarch.part" in base:
            base = base.split(".jetarch.part")[0] + ".jetarch"
        folder = os.path.dirname(base)
        stem = os.path.basename(base)
    else:
        folder, stem = root, None

    parts = []
    for name in os.listdir(folder):
        if ".jetarch.part" not in name:
            continue
        if stem and not name.startswith(stem):
            continue
        try:
            num = int(name.split(".jetarch.part")[1])
        except ValueError:
            continue
        parts.append((num, os.path.join(folder, name)))
    if not parts:
        sys.exit(f"No *.jetarch.part* files found in: {folder}")
    parts.sort()
    return [p for _, p in parts]


def read_exact(rd, n, what):
    buf = rd.read(n)
    if len(buf) != n:
        raise EOFError(f"stream ended early while reading {what} "
                       f"(wanted {n}, got {len(buf)}, at offset {rd.tell()})")
    return buf


def iter_entries(rd):
    """Yield (name, declared_size, data_offset, chunks) walking the archive."""
    head = read_exact(rd, 8, "archive header")
    if head[:4] != MAGIC:
        raise ValueError(f"not a jetarch: magic is {head[:4]!r}, expected {MAGIC!r}")

    while True:
        marker = rd.read(4)
        if len(marker) < 4:
            return  # clean end of stream
        if marker != M_FILE:
            raise ValueError(
                f"expected {M_FILE!r} at offset {rd.tell() - 4}, found {marker!r}. "
                "The parts may be in the wrong order or one may be truncated.")
        name_len = struct.unpack(">H", read_exact(rd, 2, "name length"))[0]
        raw_name = read_exact(rd, name_len, "name")
        name = raw_name.decode("utf-8", "replace")
        declared = struct.unpack(">Q", read_exact(rd, 8, "declared size"))[0]
        yield name, declared, rd.tell()


def walk(rd, on_entry):
    """Walk every file record. on_entry(name, declared, chunk_reader) -> None.

    chunk_reader(sink) streams the record's payload into `sink` (or discards it
    when sink is None) and returns the number of bytes seen.
    """
    head = read_exact(rd, 8, "archive header")
    if head[:4] != MAGIC:
        raise ValueError(f"not a jetarch: magic is {head[:4]!r}, expected {MAGIC!r}")
    count = 0
    while True:
        marker = rd.read(4)
        if len(marker) < 4:
            return count
        if marker != M_FILE:
            raise ValueError(
                f"expected {M_FILE!r} at offset {rd.tell() - 4}, found {marker!r}. "
                "Parts may be out of order or truncated.")
        name_len = struct.unpack(">H", read_exact(rd, 2, "name length"))[0]
        name = read_exact(rd, name_len, "name").decode("utf-8", "replace")
        declared = struct.unpack(">Q", read_exact(rd, 8, "declared size"))[0]

        def chunk_reader(sink, _rd=rd):
            seen = 0
            while True:
                here = _rd.tell()
                mk = _rd.read(4)
                if len(mk) < 4:
                    return seen
                if mk != M_CHNK:
                    _rd.seek(here)  # belongs to the next record
                    return seen
                clen = struct.unpack(">Q", read_exact(_rd, 8, "chunk length"))[0]
                seen += _rd.copy_to(sink, clen)

        on_entry(name, declared, chunk_reader)
        count += 1


def cmd_probe(args):
    parts = find_parts(args.source)
    print(f"{len(parts)} part(s):\n")
    total = 0
    for path in parts:
        size = os.path.getsize(path)
        total += size
        with open(path, "rb") as fh:
            head = fh.read(16)
        print(f"  {os.path.basename(path)}")
        print(f"      size  {size:,} bytes ({human(size)})")
        print(f"      head  {head[:8].hex(' ')}  {head[:8]!r}")
        sidecar = path.replace(".jetarch.part", ".md5.part")
        if os.path.exists(sidecar):
            expected = open(sidecar).read().strip()
            if args.md5:
                digest = hashlib.md5()
                with open(path, "rb") as fh:
                    for block in iter(lambda: fh.read(1 << 22), b""):
                        digest.update(block)
                actual = digest.hexdigest()
                ok = "OK" if actual == expected else "*** MISMATCH ***"
                print(f"      md5   {actual}  expected {expected}  {ok}")
            else:
                print(f"      md5   expected {expected}  (re-run with --md5 to verify)")
    print(f"\nCombined: {total:,} bytes ({human(total)})")
    print("\nOnly part1 should begin with 'RLFF'. If a later part also starts with")
    print("RLFF, the parts are separate archives rather than one split archive.")


def cmd_list(args):
    parts = find_parts(args.source)
    rd = MultiPartReader(parts)
    print(f"Reading {len(parts)} part(s), {human(rd.total)} total\n")

    by_ext, by_dir = {}, {}
    entries, shown, total_bytes = 0, 0, 0

    def on_entry(name, declared, chunk_reader):
        nonlocal entries, shown, total_bytes
        actual = chunk_reader(None)  # skip the payload
        entries += 1
        total_bytes += actual
        ext = os.path.splitext(name)[1].lower() or "(none)"
        agg = by_ext.setdefault(ext, [0, 0])
        agg[0] += 1
        agg[1] += actual
        top = name.replace("\\", "/").split("/")[0] if "/" in name.replace("\\", "/") else "(root)"
        dagg = by_dir.setdefault(top, [0, 0])
        dagg[0] += 1
        dagg[1] += actual
        if shown < args.limit:
            flag = "" if actual == declared else f"  [declared {declared:,}]"
            print(f"  {human(actual):>12}  {name}{flag}")
            shown += 1
        elif shown == args.limit:
            print(f"  ... (further entries not printed; --limit to raise)")
            shown += 1
        if entries % 20000 == 0:
            print(f"  [{entries:,} entries, {human(rd.tell())} scanned]", file=sys.stderr)

    try:
        walk(rd, on_entry)
    except (ValueError, EOFError) as exc:
        print(f"\n!! Stopped: {exc}", file=sys.stderr)
    finally:
        rd.close()

    print(f"\n=== {entries:,} entries, {human(total_bytes)} of payload ===")
    print("\nBy extension:")
    for ext, (cnt, size) in sorted(by_ext.items(), key=lambda kv: -kv[1][1])[:30]:
        print(f"  {ext:<14} {cnt:>9,} files  {human(size):>12}")
    print("\nBy top-level folder:")
    for top, (cnt, size) in sorted(by_dir.items(), key=lambda kv: -kv[1][1])[:30]:
        print(f"  {top:<28} {cnt:>9,} files  {human(size):>12}")


def cmd_extract(args):
    parts = find_parts(args.source)
    rd = MultiPartReader(parts)
    out_root = os.path.abspath(os.path.expanduser(args.out))
    os.makedirs(out_root, exist_ok=True)
    written = [0, 0]  # files, bytes

    def on_entry(name, declared, chunk_reader):
        safe = name.replace("\\", "/").lstrip("/")
        if args.only and not fnmatch.fnmatch(safe.lower(), args.only.lower()):
            chunk_reader(None)
            return
        dest = os.path.normpath(os.path.join(out_root, safe))
        if not dest.startswith(out_root + os.sep) and dest != out_root:
            print(f"  skipping unsafe path: {name}", file=sys.stderr)
            chunk_reader(None)
            return
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "wb") as sink:
            got = chunk_reader(sink)
        written[0] += 1
        written[1] += got
        if written[0] <= 40 or written[0] % 5000 == 0:
            print(f"  {human(got):>12}  {safe}")

    try:
        walk(rd, on_entry)
    except (ValueError, EOFError) as exc:
        print(f"\n!! Stopped: {exc}", file=sys.stderr)
    finally:
        rd.close()
    print(f"\nExtracted {written[0]:,} files, {human(written[1])} -> {out_root}")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("probe", help="check the parts and their checksums")
    p.add_argument("source", help="folder holding the .jetarch.part files")
    p.add_argument("--md5", action="store_true", help="verify md5 of each part (slow)")
    p.set_defaults(func=cmd_probe)

    p = sub.add_parser("list", help="list what is inside without extracting")
    p.add_argument("source")
    p.add_argument("--limit", type=int, default=60, help="entries to print (default 60)")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("extract", help="extract files out of the archive")
    p.add_argument("source")
    p.add_argument("-o", "--out", required=True, help="destination folder")
    p.add_argument("--only", help="glob filter, e.g. '*.sql'")
    p.set_defaults(func=cmd_extract)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    try:
        main()
    except BrokenPipeError:
        # Output was piped into something like `head`; that is not an error.
        try:
            sys.stdout.close()
        except Exception:
            pass
    except KeyboardInterrupt:
        sys.exit("\ninterrupted")
