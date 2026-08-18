# What the dump actually is

Established facts about the source media. Update as we learn more.

## The media

- **File:** `/Users/mendydonin/Downloads/BMW ETK 2020-01.iso`
- **Mounts at:** `/Volumes/BMW ETK 2020-01` (macOS mounts ISOs natively, read-only)
- **Repo:** `/Users/mendydonin/Documents/GitHub/ClaudeEbayFitmentApp`
- **Host Mac:** arm64 (Apple Silicon), macOS 26.5.1, ~386 GB free -- space is not a constraint
- ETK data version **3.220.006**, publication date 20191212, disc labelled 01/2020

## The database engine: Transbase (confirmed)

`transbase/transbase.exe` (Windows) and `transbase_linux/transbase_linux.tar.gz`
(Linux) ship on the disc. Transbase is a commercial RDBMS from Transaction Software
GmbH, Munich. No Python driver exists.

**But `javaclient/libs/tbjdbc.jar` is on the disc** -- the Transbase JDBC driver.
That is the supported way to query the database without running the ETK app, and it
needs only a JVM (the disc also carries `jdk/` and `jre_1.8.0_92.tar.gz`).

Linux service scripts: `transbase_linux/rc.TransBase`, `rc.tbenv`, `rc.tbstop`,
`createdb.sh`. Schema DDL: `webretknutzer_tb.sql`, `webretkpreise_tb.sql`.

## The .jetarch container format (decoded)

**Each of the six parts carries its own 8-byte header** -- they are not a naive
split. Confirmed by `probe`: every part begins `RLFF` followed by a sequence word
`0x02000000 | part_number` (0x02000001 .. 0x02000006).

Strip those 8 bytes from each part, concatenate in part order, and the result is one
continuous record stream:

```
'FILE'  u16 name_len  name[name_len]  u64 declared_size
    then repeatedly:
        'CHNK'  u64 chunk_len  data[chunk_len]
'SIGN'  u32 len  data[len]          -- package signature block (DER)
```

`CONT` is a **part-boundary continuation marker** with no length field at all:
just `'CONT'` plus one byte. Verified: CONT at logical offset 1,073,741,843 plus
4 plus 1 = 1,073,741,848, exactly where part 1's payload ends and part 2's begins,
with `CHNK` resuming immediately after. It appears **inside** a file's chunk
sequence, so a single file's chunks span parts and the chunk loop must step over it.

**Length-field widths differ per marker.** FILE and CHNK carry u64 lengths;
**SIGN carries a u32**. Verified against the real archive: SIGN at logical offset
1671 declares 0x2e = 46 bytes of DER (payload begins `30 2c 02 14`, an ASN.1
SEQUENCE), and 1671 + 4 + 4 + 46 = 1725, which is exactly where the next FILE
record (`CustomActionData.txt`) begins.

All integers big-endian. `SIGN` records appear between file records; they hold the
package signature (`package.properties` notes that `meta-inf/Manifest.mf` content is
"omitted, as generated during signing"). For any marker whose width is not yet known,
the parser tries u32 then u64 and **accepts a width only if skipping that many bytes
lands on a known marker** -- otherwise it reports a hex window rather than silently
producing corrupt output.

`package.properties` is the first entry: `name=ETK-Data`, `version=3.220.006`,
`ostype=WIN`, `targetenv=ETK`, author `msg systems ag`. "Jetstream" is msg systems'
online update system.

`scripts/jetarch.py` implements `probe` / `list` / `dump` / `extract`. It streams, so
a 5.8 GB archive is read in a few MB of RAM, and listing seeks past payloads instead
of reading them. Verified against synthetic archives that reproduce per-part headers,
mid-chunk split boundaries, and interleaved SIGN records: extraction is byte-identical.

**This is why Docker may not be needed**: if the payload is loadable data (SQL, CSV,
table exports) rather than opaque Transbase page files, we can read the catalog
without ever starting the engine.

## What is actually inside the archive

Full payload (20 entries, 5.7 GB), from `jetarch.py list`:

```
package.properties               1.6 KB   Jetstream package metadata
CustomActionData.txt             268 B    removes superseded 3.220.001-005 packages
filelist.txt                     370 B    names the payload files
filelist_script.txt              2 B
preinstall.cmd / postinstall.cmd / prerecover.cmd / postrecover.cmd
files/postinstallDataDB.cmd      2.3 KB   *** how the data is loaded ***
files/relnotes.pdf             120.4 KB
files/updateNutzerDaten.sql      3.6 KB
files/updatePublDaten.sql        692 B
files/version.txt                218 B
files/start_publish_TransbaseDB_ab.sh   3.9 KB
files/start_publishcr_spl_TB.sh         3.8 KB
files/rfile000.000               2.0 GB   *** the database ***
files/rfile000.001               2.0 GB
files/rfile000.002               5.0 MB
files/rfile001.000               1.7 GB
```

## How the data is loaded (decoded from postinstallDataDB.cmd)

```
tbadm32.exe -Cf etk_publ h=<home>\ETK\transbase\etk_publ cp=utf8 p=altabe \
            rf=rfile000.000 rf=rfile000.001 rf=rfile001.000
```

The German comment above it is "ROM-Files einspielen" -- load ROM files. So the
`rfile*` blobs are a **Transbase read-only ROM database**, not a bulk-load format.
They are the database itself in Transbase's page format, so **the engine is
required**; there is no shortcut around it.

Decoded parameters:

| Token          | Meaning                                             |
|----------------|-----------------------------------------------------|
| `tbadm`        | Transbase admin tool (`tbadm32.exe` on Windows)      |
| `-Cf`          | create database from ROM files                      |
| `-df etk_publ` | drop the database first                             |
| `etk_publ`     | **the catalog database** (publication data)         |
| `etk_nutzer`   | the user/settings database                          |
| `h=`           | database home directory                             |
| `cp=utf8`      | codepage UTF-8                                      |
| `p=altabe`     | password                                            |
| `rf=`          | a ROM file to attach                                |
| `tbi`          | Transbase interactive SQL shell                     |

The update scripts are run as
`tbi -f updatePublDaten.sql etk_publ tbadmin altabe`, which gives the connection
parameters: database `etk_publ`, user **`tbadmin`**, password **`altabe`**. These are
the ETK product's own fixed defaults baked into the installer, not personal
credentials.

Note `filelist.txt` and the .cmd reference only rfile000.000, rfile000.001 and
rfile001.000 -- but the archive also carries **rfile000.002** (5 MB), which Transbase
presumably picks up as a continuation segment. Extract all four.

## Schema clues already visible## Schema clues already visible

`Daten/updateNutzerDaten.sql` and `Daten/updatePublDaten.sql` are real ETK SQL and
reveal the conventions:

- German names with a `w_` prefix: `w_tipp`, `w_zub_kunde`, `w_zub_kunde_fahrzeug`,
  `w_bildtafzub_marketing`, `w_btzeilenzub`, `w_marketingprodukt`
- Transbase cross-database syntax `table@database`, e.g. `w_tipp@etk_nutzer`
- `ct;` is Transbase's commit statement
- Databases seen so far: `etk_nutzer` (user data), plus a prices database
- `w_zub_kunde_fahrzeug.kundefzg_vin` -- a **VIN** column, relevant to goal 1

Vocabulary that will matter when reading the schema:

| German            | Meaning                                    |
|-------------------|--------------------------------------------|
| Teil              | part                                       |
| Bildtafel (`bt`)  | illustrated parts diagram / plate          |
| Zeile             | row (a line item on a diagram)             |
| Fahrzeug (`fzg`)  | vehicle                                    |
| Baureihe          | model series (the chassis family, e.g. E46)|
| Typ               | type code                                  |
| Sonderausstattung | "SA" special-equipment option code         |
| Preise            | prices                                     |
| Nutzer            | user                                       |

## Note on Readme.txt

`Readme.txt` on the disc is stale -- it describes a 1990s Windows 95 version
(`C:\BMW95`, floppy-era install steps) and does not match this Java/Tomcat release.
Ignore its instructions. One line is still useful confirmation of the data model:
ETK has a **"Parts Use"** function, "check which vehicles a particular part is fitted
to" -- exactly the part -> vehicles direction this project needs.

## Platform decision: Windows first, Mac as backup

The disc is a **Windows product**: `transbase/transbase.exe`, `tbadm32.exe`,
`tbi32.exe`, `createdb.bat`, `setup.exe`, `install_server.exe`, `standalone/ETK.exe`,
and the install script we decoded is a `.cmd`. The Linux build is the secondary path.

Running it on Windows removes both risk points of the container route:

| Risk on macOS | On Windows |
|---------------|------------|
| Build a container and coax 2022 x86_64 Linux binaries into running on Apple Silicon under Rosetta | Gone -- no Docker, no emulation |
| The ROM database was *built on Windows*; will it load on Linux? | Gone -- same OS it was authored for |

It also allows installing the real ETK application, giving a **reference UI to verify
our exported data against** -- look a part up in BMW's own interface and confirm the
CSV agrees. The Linux route offers no such cross-check.

Open questions about the Windows host: CPU architecture (AMD64 wanted; ARM64
reintroduces emulation), Windows version, free space (~20 GB), and whether drive
letters D:, L:, P: are available -- `postinstallDataDB.cmd` hardcodes them but honours
`ibaseInstallDriveD` / `...L` / `...P` overrides.

The macOS/Docker route below stays the documented fallback.

## Route: decided

Route 1 (read the archive directly) is **ruled out** -- the payload is a Transbase
ROM database, not loadable text. The container format is fully decoded and
`jetarch.py` extracts the ROM files cleanly, but reading them means running the engine.

**Route 2 it is:** run Transbase Linux in a Docker container, attach the ROM files
with `tbadm -Cf`, and query with `tbi` (or over JDBC via `tbjdbc.jar`) to export the
tables we need as CSV. On Apple Silicon this needs `--platform linux/amd64`, since the
Linux binaries are x86_64.

Route 3 (installing the full Tomcat + javaserver ETK stack) stays the last resort; we
only need the database, not the web application.

## Open questions

- Which tables carry part -> vehicle links, production date ranges, and SA codes?
- Does `tbadm -Cf` copy the 5.7 GB or attach the ROM files in place?
- Do the x86_64 Linux binaries run acceptably under Docker's Rosetta emulation?
