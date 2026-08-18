# BMW ETK database project

Goal: take a BMW ETK (Elektronischer Teilekatalog) database dump, understand its
schema, and distill a clean **part number → vehicle/chassis** table that can be fed
into the eBay fitment project as a third fitment source.

Later (not built yet, but the schema exploration should confirm the columns exist):

1. **VIN decoder** — VIN → chassis/type code, model, year, engine, option ("SA") codes.
2. **VIN → part compatibility checker** — given a VIN + part number, does it fit?
   Needs part↔vehicle links, production-date ranges, and option-code filters.

## Ground rules

- **The raw dump is never committed.** It lives in `bmw-etk/dump/` which is gitignored,
  along with every database-ish extension (`.bak`, `.mdf`, `.fdb`, `.db`, `.iso`, …).
  Only derived, distilled tables in `bmw-etk/data/` get committed.
- Like the eBay side: Claude writes the scripts, the human runs them on the Mac.

## Layout

- `scripts/identify_dump.py` — tells you what the dump actually is (reads only file
  headers, instant on a multi-GB file). Works on a file or a whole folder.
- `dump/` — put the raw dump here. Gitignored.
- `data/` — distilled outputs (committed).
- `docs/` — schema notes as we learn them.

## Step 1 — identify the dump

```bash
python3 bmw-etk/scripts/identify_dump.py /path/to/your/dump
```

## Step 1a — if the dump is an ISO

macOS mounts ISOs natively (read-only, copies nothing, no Docker needed):

```bash
bash bmw-etk/scripts/mount_iso.sh "/path/to/BMW ETK 2020-01.iso"
```

This mounts it, lists the contents, and runs `identify_dump.py` on the mount point
to find the real database inside. Unmount later with `hdiutil detach /Volumes/<name>`.
