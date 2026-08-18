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

## The ROM files are platform-portable (important)

The disc ships **one** set of `rfile*` data files but **two** engines
(`transbase/` for Windows, `transbase_linux/`) with parallel create scripts
(`createdb.bat` / `createdb.sh`) loading the same data. BMW therefore expects that
single catalog to load under either OS. This substantially answers the earlier worry
that a Windows-authored ROM database might not load on Linux, and makes the
container route a real option rather than a weak fallback.

## The Linux side, from createdb.sh and rc.tbenv

`transbase_linux.tar.gz` holds 163 entries including the tools we need:
`tbadmin` (admin/load), `tbi` (SQL shell), `tbserver` (server), plus `utbi`,
`tbadmmsg`, `ccl`, `mkapf`, `diskrec` and the `optree/` catalog files.

```sh
# createdb.sh -- creating EMPTY databases (note lowercase -cf)
tbadmin -cf etk_publ   p=tmp h=.../etk_publ typ=E ps=4096 lc=1024 rs=512000 d=,512000 cp=utf8
tbadmin -cf etk_nutzer p=tmp h=.../etk_nutzer cp=utf8
tbadmin -cf etk_preise p=tmp h=.../etk_preise cp=utf8
tbi -f webretknutzer_tb.sql etk_nutzer tbadmin tmp
tbi -f webretkpreise_tb.sql etk_preise tbadmin tmp
```

Note the case distinction: **`-cf` creates an empty database**, while the Windows
install script uses **`-Cf` with `rf=` arguments to create from ROM files**. Three
databases exist: `etk_publ` (the catalog), `etk_nutzer` (user data), `etk_preise`
(prices).

`rc.tbenv` gives the runtime environment:

```sh
TRANSBASE=/home/bmw/transbase
TRANSBASE_SERVICENAMES=2024:2025      # the server's ports
```

**Only the database is needed** -- Tomcat, the javaserver and the javaclient are all
irrelevant to exporting tables.

## Extraction verified against real data

All four ROM files extracted cleanly, and `rfile000.000` came out at exactly
**2,147,459,072 bytes -- the size declared in its FILE record**. That confirms the
CONT part-boundary handling on the real 5.7 GB archive, not just on synthetic tests.

| File | Bytes |
|------|-------|
| rfile000.000 | 2,147,459,072 |
| rfile000.001 | 2,147,463,168 |
| rfile000.002 | 5,279,744 |
| rfile001.000 | 1,792,589,824 |

## Attaching the CD-ROM database: partial progress

`tbadmin -Cf` with BMW's exact parameters gets a long way and then fails:

```
Working ...
attach to CD-ROM database etk_publ: unexpected: c_f_c_1      (exit 42)
```

Before failing it builds the database skeleton and **one companion index file per
romfile**, so it does read them:

```
/data/etk_publ/{account,context,roms/cd}/
/data/etk_publ/roms/cd/comp000.000   1,294,336
/data/etk_publ/roms/cd/comp000.001   1,130,496
/data/etk_publ/roms/cd/comp001.000     581,632
```

Prime suspect: **rfile000.002 is never passed.** Romfiles are named
`rfile<volume>.<segment>`, so volume 000 has segments .000, .001 and **.002**, and
volume 001 has .000. BMW's `postinstallDataDB.cmd` lists only three `rf=` arguments
and omits `rfile000.002` -- consistent with only three companion files being built.

`tbadmin params -C` also revealed options the install script does not use:

```
Usage: tbadmin -C[f|F][nv] dbname [<parameter> ...]
  -f : interact at most for CD-Insert
  -F : no interaction at all
  r=<path>[,<CD-label>]    Database Romfile-Dir  (a whole directory)
  rf=<file>[,<CD-label>]   Database Romfile
```

`docker/attach.sh` therefore tries six invocations in order -- BMW's three files as a
control, all four files, `-F` instead of `-f` in case it is blocking on a CD-insert
prompt, and `r=<dir>` letting tbadmin discover the romfiles itself.

## The catalog is ATTACHED AND BOOTED (confirmed)

`tbadmin -i etk_publ` reports:

```
Database Name = etk_publ@etkdb          Status  = booted
Database Home = /data/etk_publ          Codepage = Utf8
Rom Size      = 12000 MB                DB Type = CD_Retrieval
DB-Identification = TB_2001             Page Size = 4 KB
Database Romfile(s)
  /rom/files/rfile000.000   on CD-ROM 'CD_1'
  /rom/files/rfile000.001   on CD-ROM 'CD_2'
  /rom/files/rfile000.002   on CD-ROM 'CD_3'
  /rom/files/rfile001.000   on CD-ROM 'CD_4'
```

**Four CD-ROM volumes** -- definitive confirmation that `rfile000.002` is required
and that BMW's `postinstallDataDB.cmd` is wrong for this data package.

## Use utbi, not tbi: it is a LOCAL client

`tbi` is a network client and always fails with
`server <2024> at <etkdb> not reachable` unless `tbserver` **and** `tbkernel` are both
running (see `rc.TransBase`). **`utbi` talks to the database directly and needs no
server at all** -- that is the connection route this project uses.

Its SQL is a **positional argument**; there is **no `-f` option**:

```
utbi [options] [ dbname [ uname [ passwd [ SQL command ] ] ] ]
  -Fc  separate fields by c      -h   no column headers/footers
  -qc  quote fields with c       -H   HTML output
  -cN  line width (default 80)   -a   autocommit
  -wN  column width (default 10) -CN  consistency level (default 3)
```

So a query is:

```
utbi -c 400 -w 40 etk_publ tbadmin altabe "select * from systable;"
```

and CSV export will be `-F',' -q'"' -h`.

**Trap:** passing `-f` makes utbi print its usage text. An earlier version of this
project treated "no error keywords in the output" as success, so 26 files of usage
text were written as if they were a schema dump. Any success check must reject a
tool's own usage message.

The full tool list on the disc also includes `tbarc`, `tbcheck`, `tbdiff`, `tbmkrom`,
`tbtar`, `tbstatis`, `ufi`, `tbkernel`, `tbmux`, and **`tbjdbc.jar` / `tbjdbcx.jar`**.

## The engine RUNS on Apple Silicon (confirmed)

`tbadmin` executes under Docker + QEMU on an arm64 Mac and prints its usage. All
libraries resolve, including the **old** ncurses variants:

```
libncurses.so.5 => /lib/i386-linux-gnu/libncurses.so.5
libtinfo.so.5   => /lib/i386-linux-gnu/libtinfo.so.5
/lib/ld-linux.so.2 (0x00400000)
```

The `linux/amd64` + i386-multiarch route works; the `linux/386` fallback was not
needed. Recipe: `debian:bullseye-slim`, `dpkg --add-architecture i386`, then
`libc6:i386 libstdc++6:i386 zlib1g:i386 libncurses5:i386 libtinfo5:i386`.

Its own usage text confirms the reading of the install script:

```
-C    attach to CD-ROM database
```

So the `rfile*` blobs are a **CD-ROM database** -- read-only, attached in place.
That suggests attaching does not duplicate the 5.7 GB, though this is unconfirmed.

`tbadmin` options: `-b` boot, `-s` shutdown, `-r` reboot, `-i` inform, `-c` create,
`-a` alter, `-d` delete, `-C` attach CD-ROM, `-M` migrate, `-drec` disk recovery.
`tbadmin params <option>` prints per-option documentation.

## The Transbase binaries are 32-bit i386 (not x86_64)

`file` inside the container reports:

```
/opt/transbase/tbadmin:  ELF 32-bit LSB executable, Intel 80386, version 1 (SYSV),
                         dynamically linked, interpreter /lib/ld-linux.so.2,
                         for GNU/Linux 2.2.5, stripped
```

Same for `tbi` and `tbserver`. Two consequences:

- **Rosetta does not help.** Rosetta 2 accelerates 64-bit x86 only, so Docker falls
  back to QEMU (`qemu-i386` appears in the error output). It works, just slower.
- **A 64-bit container has no 32-bit loader**, giving
  `qemu-i386: Could not open '/lib/ld-linux.so.2'`. The image must either add i386
  multiarch libraries or use a natively 32-bit base.

"for GNU/Linux 2.2.5" is a 1999-era kernel target, so these are very old binaries;
an older base distribution is the safer choice.

The image now handles both routes: `linux/amd64` with i386 multiarch (default), and
`ETK_PLATFORM=linux/386` with `i386/debian:bullseye-slim` as the fallback.

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
