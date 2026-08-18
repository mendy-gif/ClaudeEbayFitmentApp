# CLAUDE.md — BMW ETK subproject

Project memory for `bmw-etk/`. Read with the repo-root `CLAUDE.md`, which covers the
eBay fitment automation this subproject eventually feeds. The owner (`mendy`) is
**not a developer** — explain plainly and prefer running things over handing over
long commands.

## What this subproject does

Turn a BMW **ETK** (Elektronischer Teilekatalog) disc into a clean
**part number → vehicle/chassis** table, then use the same database for two further
goals:

1. **VIN decoder** — VIN → chassis/type code, model, year, engine, SA option codes.
2. **VIN → part fitment checker** — given a VIN and a part number, does it fit?
   Needs part↔vehicle links **plus** production-date ranges **plus** option-code filters.

Goal 1's output becomes a **third fitment source** for the eBay project, alongside the
chassis rules and the part-number history.

## Golden facts (the load-bearing knowledge)

1. **The source is `/Users/mendydonin/Downloads/BMW ETK 2020-01.iso`**, mounted at
   `/Volumes/BMW ETK 2020-01`. ISOs do **not** survive a reboot — remount with
   `bash bmw-etk/scripts/mount_iso.sh "<iso path>"`.
2. **Never commit the raw dump.** `.gitignore` blocks `bmw-etk/dump/` and every
   database-ish extension. Only distilled output in `bmw-etk/data/` is committed.
3. **The database engine is Transbase** (Transaction Software GmbH, Munich)
   **V6.1.2.19, built 2004** — a niche commercial RDBMS. No Python driver exists.
   Access is via `tbi` (SQL shell) or the `tbjdbc.jar` driver on the disc.
4. **The binaries are 32-bit i386, linked for GNU/Linux 2.2.5 (a 1999 kernel).**
   Rosetta does **not** help (it is 64-bit only) — Docker uses QEMU. The container
   needs i386 multiarch libs including the **old** `libncurses5`/`libtinfo5`.
5. **BMW's own installer is buggy for this data package.** `postinstallDataDB.cmd`
   passes three romfiles; the package ships **four**. The attach only succeeds with
   **all four**, `rfile000.002` included. This cost several rounds to find — do not
   "fix" the attach back to three files.
6. **Transbase keys databases as `<name>@<hostname>`**, so every container must run
   with `--hostname etkdb`. A random per-run hostname makes the database look missing.
7. **The database registry lives in `$TRANSBASE/dblist.ini`**, which is an image
   layer and would be discarded per run — `entrypoint.sh` symlinks it onto the
   `/data` volume. Without that, `tbadmin -i` reports the database missing right
   after a successful attach.
8. **Connection details** (the ETK product's own installer defaults, not personal
   credentials): database `etk_publ` (catalog), user `tbadmin`, password `altabe`,
   codepage utf8. Also `etk_nutzer` (user data) and `etk_preise` (prices).
9. **utbi options take NO space before their value** — `-c400`, not `-c 400`.
   With a space, utbi reads the number as the database name and reports
   `database <400@etkdb> does not exist`, which looks like a database fault but is
   an argument-parsing one.
10. **Both `tbi` and `utbi` are network clients** — there is no local/direct mode.
   `tbmux -tbk tbkernel -tbs tbserver` must be running **in the same container** as
   the query (kernel serves clients on 2024, server on 2025).
11. **`desc` is the catalogue** — `desc` alone lists every table, `desc <table>`
   describes one. Table type `R` means a read-only ROM table.
12. **The disc `Readme.txt` is stale** — it describes a 1990s Windows 95 version.
   Ignore its instructions.

## Canonical commands (run on the Mac, never from a Claude cloud session)

```bash
# Remount the ISO after a reboot
bash bmw-etk/scripts/mount_iso.sh "/Users/mendydonin/Downloads/BMW ETK 2020-01.iso"

# Build the Transbase container (needs the ISO mounted)
bash bmw-etk/docker/etk-db.sh build
bash bmw-etk/docker/etk-db.sh probe      # a tbadmin usage message = success

# Attach the catalog (tries each invocation; all four romfiles is the one that works)
bash bmw-etk/docker/etk-db.sh create

# Query it
bash bmw-etk/docker/etk-db.sh sql "select * from systable;"
bash bmw-etk/docker/etk-db.sh explore    # full schema dump -> bmw-etk/data/schema/

# Everything unattended, bounded, logged to ~/etk_overnight.log
bash bmw-etk/docker/overnight.sh

# Start over if the database volume gets into a bad state
bash bmw-etk/docker/etk-db.sh reset
```

## Repo map

- `scripts/jetarch.py` — reader for the `.jetarch` container (`probe`/`list`/`dump`/
  `extract`). The format is fully decoded; see `docs/DUMP_FACTS.md`.
- `scripts/mount_iso.sh` — mount the ISO read-only and report what is inside.
- `scripts/extract_rfiles.sh` — pull the four ROM files out to `dump/rfiles/`.
- `scripts/identify_dump.py` / `explore_iso.sh` — early reconnaissance, kept for reuse.
- `docker/Dockerfile` — Transbase on Debian bullseye with i386 multiarch.
- `docker/etk-db.sh` — the driver: build / probe / params / create / sql / explore /
  reset / shell.
- `docker/attach.sh` — tries each CD-ROM attach invocation in turn.
- `docker/explore.sh` — schema dump, with timeouts and caps so it is safe unattended.
- `docker/overnight.sh` — one bounded end-to-end run.
- `docs/DUMP_FACTS.md` — **the technical record**: container format, load command,
  schema vocabulary, decisions and why.

## Conventions

- Python **stdlib only**; shell scripts derive their own paths from `$0`.
- Claude writes the code and commits; the human runs it on the Mac.
- Work happens on branch `claude/bmw-etk-database-sqohoo`.
- **Never let a cap look like full coverage** — if a script truncates (row-count
  limits, time budgets), it must say so explicitly.

## German vocabulary you will need in the schema

| German | Meaning | | German | Meaning |
|---|---|---|---|---|
| Teil | part | | Baureihe | model series (chassis family) |
| Bildtafel (`bt`) | parts diagram | | Typ | type code |
| Zeile | row / line item | | Sonderausstattung (SA) | option code |
| Fahrzeug (`fzg`) | vehicle | | Preise | prices |
| Nutzer | user | | Daten | data |
