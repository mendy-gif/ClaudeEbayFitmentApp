# Runbook

Copy-paste commands. All of these run **on the Mac** — a Claude cloud session cannot
reach eBay, the ISO, or Docker.

Every block starts by switching to the right branch, because this repo holds two
projects on two branches and files appear and disappear when you switch. If you ever
see *"No such file or directory"* for a `bmw-etk/` script, that is the cause.

## 0. Every session starts here

```bash
cd /Users/mendydonin/Documents/GitHub/ClaudeEbayFitmentApp
git checkout claude/bmw-etk-database-sqohoo
git pull --ff-only origin claude/bmw-etk-database-sqohoo
```

## 1. Mount the disc (needed after every reboot)

```bash
bash bmw-etk/scripts/mount_iso.sh "/Users/mendydonin/Downloads/BMW ETK 2020-01.iso"
```

Read-only; copies nothing. Unmount with `hdiutil detach "/Volumes/BMW ETK 2020-01"`.

## 2. The unattended run (recommended)

```bash
bash bmw-etk/docker/overnight.sh
```

Builds the container, attaches the catalogue, dumps the schema. Logs to
`~/etk_overnight.log`, writes schema files to `bmw-etk/data/schema/`, and commits
them. **It cannot run away**: every stage has a wall-clock ceiling enforced by a
watchdog, every query has a timeout, and row counting is capped. It uses **no Claude
usage** — it is just Docker on your machine.

To stop it: `Ctrl-C`, or `docker kill etk-overnight`.

## 3. Step by step instead

```bash
bash bmw-etk/docker/etk-db.sh build     # build the image (needs the ISO mounted)
bash bmw-etk/docker/etk-db.sh probe     # a tbadmin usage message means SUCCESS
bash bmw-etk/docker/etk-db.sh create    # attach the catalogue
bash bmw-etk/docker/etk-db.sh explore   # dump the schema
```

## 4. Ask the database something

```bash
bash bmw-etk/docker/etk-db.sh sql "select * from systable;"
bash bmw-etk/docker/etk-db.sh shell     # poke around inside the container
```

Connection details, from BMW's own installer: database `etk_publ`, user `tbadmin`,
password `altabe`.

## 5. Read the disc without the database

```bash
python3 bmw-etk/scripts/jetarch.py list    "/Volumes/BMW ETK 2020-01"
python3 bmw-etk/scripts/jetarch.py extract "/Volumes/BMW ETK 2020-01" \
        -o bmw-etk/dump/small --max-bytes 2000000     # small files only
python3 bmw-etk/scripts/jetarch.py dump    "/Volumes/BMW ETK 2020-01" --at 1671
```

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `No such file or directory: bmw-etk/...` | Wrong branch. Run step 0. |
| `docker not found` | Docker Desktop not installed, or its CLI is not on this terminal's PATH. Open a fresh Terminal; the scripts also search Docker's own install locations. |
| `docker is installed but the engine is not running` | Launch Docker Desktop; wait for the whale icon to stop animating. |
| `ISO not mounted at ...` | Run step 1. ISOs do not survive a reboot. |
| `Database <etk_publ@...> does not exist` | The container hostname was not pinned, or the registry was not persisted. Both are handled now; if it recurs, run `etk-db.sh reset` then `create`. |
| Attach fails with `unexpected: c_f_c_1` | A romfile is missing from the `rf=` list. All **four** are required, including `rfile000.002`, which BMW's own script omits. |
| Everything is very slow | Expected. The binaries are 32-bit Intel and run under QEMU emulation on Apple Silicon; Rosetta cannot help because it only accelerates 64-bit. |
