#!/usr/bin/env python3
"""Read a BMW ETK ".jetarch" data package (msg systems' "Jetstream" container).

Container layout, decoded from the real archive:

  Every part file begins with its own 8-byte header:
      'RLFF'  u32  (0x02000000 | part_number)
  Stripping those 8 bytes from each part and concatenating in part order gives
  one continuous record stream:

      'FILE'  u16 name_len  name[name_len]  u64 declared_size
          then, repeatedly:
              'CHNK'  u64 chunk_len  data[chunk_len]
      'SIGN'  u64 len  data[len]        -- package signature block
      (other 4-char markers are assumed to follow the same MARKER+u64+payload
       shape, and the guess is validated by checking we land on a known marker)

All integers are big-endian. Everything streams, so a 5.8 GB archive is read in a
few MB of RAM, and listing seeks past payloads instead of reading them.

    python3 jetarch.py probe   "/Volumes/BMW ETK 2020-01"
    python3 jetarch.py list    "/Volumes/BMW ETK 2020-01"
    python3 jetarch.py dump    "/Volumes/BMW ETK 2020-01" --at 1679
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
PART_HEADER = 8
M_FILE, M_CHNK, M_SIGN, M_CONT = b"FILE", b"CHNK", b"SIGN", b"CONT"
KNOWN = {M_FILE, M_CHNK, M_SIGN, M_CONT}
# Markers with no length field: the marker is followed by a fixed number of
# bytes. CONT sits at a part boundary and means "this file continues in the
# next part" -- verified: CONT at 1,073,741,843 + 4 + 1 = 1,073,741,848, exactly
# where part 1's payload ends and part 2's begins.
FIXED_TAIL = {M_CONT: 1}
# Width in bytes of the length field that follows each marker. FILE and CHNK
# carry u64 lengths; SIGN carries a u32 (verified against the real archive:
# SIGN at 1671 + 4 + 4 + 46 lands exactly on the FILE record at 1725).
LENGTH_WIDTH = {M_CHNK: 8, M_SIGN: 4}
COPY_BUF = 1 << 20


def human(n):
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:,.1f} {unit}"
        n /= 1024.0


class MultiPartReader:
    """The parts, minus their per-part headers, as one seekable logical stream."""

    def __init__(self, paths, header=PART_HEADER):
        self.paths = list(paths)
        self.header = header
        self.phys_sizes = [os.path.getsize(p) for p in self.paths]
        self.data_sizes = [max(0, s - header) for s in self.phys_sizes]
        self.total = sum(self.data_sizes)
        self.starts, off = [], 0
        for size in self.data_sizes:
            self.starts.append(off)
            off += size
        self._fh = None
        self._idx = -1
        self._pos = 0
        self._open(0, 0)

    def part_headers(self):
        """Return [(basename, magic, seq_word)] for sanity checking."""
        out = []
        for p in self.paths:
            with open(p, "rb") as fh:
                head = fh.read(self.header)
            seq = struct.unpack(">I", head[4:8])[0] if len(head) >= 8 else None
            out.append((os.path.basename(p), head[:4], seq))
        return out

    def _open(self, idx, data_off):
        if self._fh:
            self._fh.close()
        self._idx = idx
        self._fh = open(self.paths[idx], "rb")
        self._fh.seek(self.header + data_off)

    def close(self):
        if self._fh:
            self._fh.close()
            self._fh = None

    def tell(self):
        return self._pos

    def seek(self, pos):
        pos = max(0, min(pos, self.total))
        idx = max(0, bisect_right(self.starts, pos) - 1)
        # Skip over any zero-length parts.
        while idx < len(self.paths) - 1 and self.data_sizes[idx] == 0:
            idx += 1
        self._open(idx, pos - self.starts[idx])
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
                break
            self._open(self._idx + 1, 0)
        return bytes(out)

    def copy_to(self, sink, count):
        """Stream `count` bytes to `sink`; if sink is None, seek past them."""
        if sink is None:
            start = self._pos
            self.seek(min(self._pos + count, self.total))
            return self._pos - start
        left = count
        while left > 0:
            buf = self.read(min(COPY_BUF, left))
            if not buf:
                break
            sink.write(buf)
            left -= len(buf)
        return count - left


def find_parts(root):
    root = os.path.expanduser(root)
    if os.path.isfile(root):
        base = root.split(".jetarch.part")[0] + ".jetarch"
        folder, stem = os.path.dirname(base), os.path.basename(base)
    else:
        folder, stem = root, None
    parts = []
    for name in os.listdir(folder):
        if ".jetarch.part" not in name:
            continue
        if stem and not name.startswith(stem):
            continue
        try:
            parts.append((int(name.split(".jetarch.part")[1]), os.path.join(folder, name)))
        except ValueError:
            continue
    if not parts:
        sys.exit(f"No *.jetarch.part* files found in: {folder}")
    parts.sort()
    return [p for _, p in parts]


def read_exact(rd, n, what):
    buf = rd.read(n)
    if len(buf) != n:
        raise EOFError(f"stream ended early reading {what} "
                       f"(wanted {n}, got {len(buf)}, at logical offset {rd.tell()})")
    return buf


def hexwin(rd, center, before=48, after=112):
    """Render a hex/ASCII window around a logical offset (for diagnosis)."""
    keep = rd.tell()
    start = max(0, center - before)
    rd.seek(start)
    data = rd.read(before + after)
    rd.seek(keep)
    lines = []
    for i in range(0, len(data), 16):
        row = data[i:i + 16]
        off = start + i
        hx = " ".join(f"{b:02x}" for b in row).ljust(47)
        asc = "".join(chr(b) if 32 <= b < 127 else "." for b in row)
        mark = " <<<" if off <= center < off + 16 else ""
        lines.append(f"  {off:>12,}  {hx}  {asc}{mark}")
    return "\n".join(lines)


def resolve_block(rd, marker, here):
    """Work out the length field for a non-FILE marker at logical offset `here`.

    Tries the known width for this marker first, then the alternatives, and
    accepts a width only if skipping that many bytes lands on a marker we
    recognise (or on clean end-of-stream). Returns (length, width, end) or None.
    """
    candidates = []
    known = LENGTH_WIDTH.get(marker)
    if known:
        candidates.append(known)
    for width in (4, 8):
        if width not in candidates:
            candidates.append(width)

    for width in candidates:
        rd.seek(here + 4)
        raw = rd.read(width)
        if len(raw) < width:
            continue
        length = int.from_bytes(raw, "big")
        end = here + 4 + width + length
        if end > rd.total:
            continue
        rd.seek(end)
        probe = rd.read(4)
        if not probe or probe in KNOWN:
            return length, width, end
    return None


def walk(rd, on_file, on_other=None, strict=False):
    """Walk the record stream. Returns (file_count, marker_census, problems)."""
    census, problems = {}, []
    files = 0
    while True:
        here = rd.tell()
        marker = rd.read(4)
        if len(marker) < 4:
            break
        census[marker] = census.get(marker, 0) + 1

        if marker == M_FILE:
            name_len = struct.unpack(">H", read_exact(rd, 2, "name length"))[0]
            name = read_exact(rd, name_len, "name").decode("utf-8", "replace")
            declared = struct.unpack(">Q", read_exact(rd, 8, "declared size"))[0]

            def chunk_reader(sink, _rd=rd, _census=census):
                seen = 0
                while True:
                    at = _rd.tell()
                    mk = _rd.read(4)
                    if len(mk) < 4:
                        return seen
                    if mk in FIXED_TAIL:
                        # A file's chunks continue across a part boundary.
                        _census[mk] = _census.get(mk, 0) + 1
                        _rd.read(FIXED_TAIL[mk])
                        continue
                    if mk != M_CHNK:
                        _rd.seek(at)
                        return seen
                    _census[mk] = _census.get(mk, 0) + 1
                    clen = struct.unpack(">Q", read_exact(_rd, 8, "chunk length"))[0]
                    seen += _rd.copy_to(sink, clen)

            on_file(name, declared, chunk_reader)
            files += 1
            continue

        if marker in FIXED_TAIL:
            rd.read(FIXED_TAIL[marker])
            continue

        # Any other marker: resolve its length field by trying candidate widths
        # and keeping only the one that lands on a known marker. Never guess.
        resolved = resolve_block(rd, marker, here)
        if resolved is None:
            problems.append(
                f"{marker!r} at {here:,}: could not read its length as u32 or u64 "
                f"-- neither lands on a known marker\n" + hexwin(rd, here))
            break
        length, width, end = resolved
        rd.seek(end)
        if on_other:
            on_other(marker, here, length, width)
    return files, census, problems


def report_problems(problems):
    if not problems:
        return
    print("\n!! Anomalies:", file=sys.stderr)
    for p in problems:
        print("  " + p.replace("\n", "\n  "), file=sys.stderr)


def cmd_probe(args):
    parts = find_parts(args.source)
    rd = MultiPartReader(parts)
    print(f"{len(parts)} part(s):\n")
    good = True
    for i, (name, magic, seq) in enumerate(rd.part_headers(), 1):
        phys = rd.phys_sizes[i - 1]
        expect_seq = 0x02000000 | i
        ok_magic = magic == MAGIC
        ok_seq = seq == expect_seq
        good &= ok_magic and ok_seq
        print(f"  {name}")
        print(f"      size    {phys:,} bytes ({human(phys)}), payload {human(rd.data_sizes[i-1])}")
        print(f"      magic   {magic!r} {'OK' if ok_magic else '*** expected RLFF ***'}")
        print(f"      seq     0x{seq:08x} {'OK' if ok_seq else f'*** expected 0x{expect_seq:08x} ***'}")
        sidecar = parts[i - 1].replace(".jetarch.part", ".md5.part")
        if os.path.exists(sidecar):
            expected = open(sidecar).read().strip()
            if args.md5:
                digest = hashlib.md5()
                with open(parts[i - 1], "rb") as fh:
                    for block in iter(lambda: fh.read(1 << 22), b""):
                        digest.update(block)
                actual = digest.hexdigest()
                print(f"      md5     {actual} {'OK' if actual == expected else '*** MISMATCH ***'}")
                good &= actual == expected
            else:
                print(f"      md5     expected {expected}  (--md5 to verify)")
    rd.close()
    print(f"\nLogical payload after stripping {PART_HEADER}-byte part headers: "
          f"{rd.total:,} bytes ({human(rd.total)})")
    print("\nAll part headers valid and in order." if good
          else "\n*** Part headers look wrong -- check order/completeness. ***")


def cmd_dump(args):
    rd = MultiPartReader(find_parts(args.source))
    print(f"Logical stream: {human(rd.total)}\n")
    print(hexwin(rd, args.at, before=args.before, after=args.after))
    rd.close()


def cmd_list(args):
    parts = find_parts(args.source)
    rd = MultiPartReader(parts)
    print(f"Reading {len(parts)} part(s), {human(rd.total)} of payload\n")
    by_ext, by_dir = {}, {}
    state = {"n": 0, "shown": 0, "bytes": 0}

    def on_file(name, declared, chunk_reader):
        actual = chunk_reader(None)
        state["n"] += 1
        state["bytes"] += actual
        ext = os.path.splitext(name)[1].lower() or "(none)"
        a = by_ext.setdefault(ext, [0, 0]); a[0] += 1; a[1] += actual
        norm = name.replace("\\", "/")
        top = norm.split("/")[0] if "/" in norm else "(root)"
        d = by_dir.setdefault(top, [0, 0]); d[0] += 1; d[1] += actual
        if state["shown"] < args.limit:
            flag = "" if actual == declared else f"   [declared {declared:,}]"
            print(f"  {human(actual):>12}  {name}{flag}")
            state["shown"] += 1
        elif state["shown"] == args.limit:
            print("  ... (raise --limit to print more)")
            state["shown"] += 1
        if state["n"] % 25000 == 0:
            print(f"  [{state['n']:,} files, {human(rd.tell())} scanned]", file=sys.stderr)

    files, census, problems = walk(rd, on_file)
    rd.close()

    print(f"\n=== {files:,} files, {human(state['bytes'])} of payload ===")
    print("\nRecord markers seen:")
    for mk, cnt in sorted(census.items(), key=lambda kv: -kv[1]):
        print(f"  {mk!r:<10} {cnt:>10,}")
    print("\nBy extension:")
    for ext, (cnt, size) in sorted(by_ext.items(), key=lambda kv: -kv[1][1])[:30]:
        print(f"  {ext:<16} {cnt:>9,} files  {human(size):>12}")
    print("\nBy top-level folder:")
    for top, (cnt, size) in sorted(by_dir.items(), key=lambda kv: -kv[1][1])[:30]:
        print(f"  {top:<30} {cnt:>9,} files  {human(size):>12}")
    report_problems(problems)


def cmd_extract(args):
    rd = MultiPartReader(find_parts(args.source))
    out_root = os.path.abspath(os.path.expanduser(args.out))
    os.makedirs(out_root, exist_ok=True)
    done = {"n": 0, "bytes": 0, "skipped": 0}
    patterns = [g.strip().lower() for g in args.only.split(",")] if args.only else None

    def wanted(safe, declared):
        if args.max_bytes and declared > args.max_bytes:
            return False
        if not patterns:
            return True
        low = safe.lower()
        return any(fnmatch.fnmatch(low, g) or fnmatch.fnmatch(os.path.basename(low), g)
                   for g in patterns)

    def on_file(name, declared, chunk_reader):
        safe = name.replace("\\", "/").lstrip("/")
        dest = os.path.normpath(os.path.join(out_root, safe))
        if dest != out_root and not dest.startswith(out_root + os.sep):
            print(f"  refusing unsafe path: {name}", file=sys.stderr)
            chunk_reader(None)
            return
        # Entries ending in "/" are directory markers, not files. Creating a
        # regular file for them would block the real directory of that name.
        if safe.endswith("/"):
            os.makedirs(dest, exist_ok=True)
            chunk_reader(None)
            return
        if not wanted(safe, declared):
            done["skipped"] += 1
            chunk_reader(None)
            return
        os.makedirs(os.path.dirname(dest) or out_root, exist_ok=True)
        with open(dest, "wb") as sink:
            got = chunk_reader(sink)
        done["n"] += 1
        done["bytes"] += got
        if done["n"] <= 40 or done["n"] % 5000 == 0:
            note = "" if got == declared else f"   [declared {declared:,}]"
            print(f"  {human(got):>12}  {safe}{note}")

    _, _, problems = walk(rd, on_file)
    rd.close()
    print(f"\nExtracted {done['n']:,} files, {human(done['bytes'])} -> {out_root}")
    if done["skipped"]:
        print(f"Skipped {done['skipped']:,} entries not matching the filters.")
    report_problems(problems)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("probe", help="validate part headers and checksums")
    p.add_argument("source"); p.add_argument("--md5", action="store_true")
    p.set_defaults(func=cmd_probe)

    p = sub.add_parser("list", help="list contents without extracting")
    p.add_argument("source"); p.add_argument("--limit", type=int, default=60)
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("dump", help="hex window at a logical offset")
    p.add_argument("source"); p.add_argument("--at", type=int, required=True)
    p.add_argument("--before", type=int, default=48)
    p.add_argument("--after", type=int, default=112)
    p.set_defaults(func=cmd_dump)

    p = sub.add_parser("extract", help="extract files")
    p.add_argument("source"); p.add_argument("-o", "--out", required=True)
    p.add_argument("--only", help="comma-separated globs, e.g. '*.sql,*.txt'; "
                                  "matched against the full path and the basename")
    p.add_argument("--max-bytes", type=int, default=0,
                   help="skip entries whose declared size exceeds this")
    p.set_defaults(func=cmd_extract)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    try:
        main()
    except BrokenPipeError:
        try:
            sys.stdout.close()
        except Exception:
            pass
    except KeyboardInterrupt:
        sys.exit("\ninterrupted")
