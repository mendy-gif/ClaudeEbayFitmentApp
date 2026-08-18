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

The six parts are one archive split at ~1 GiB. Format, verified field by field
against the real header:

```
'RLFF'  u32 version (0x02000001)
repeating file records:
    'FILE'  u16 name_len  name[name_len]  u64 declared_size
    repeating chunks, until the next marker is not CHNK:
        'CHNK'  u64 chunk_len  data[chunk_len]
```

All integers big-endian. `package.properties` is the first entry and identifies the
package: `name=ETK-Data`, `version=3.220.006`, `ostype=WIN`, `targetenv=ETK`,
author `msg systems ag`. "Jetstream" is msg systems' online update system.

`scripts/jetarch.py` implements this: `probe` / `list` / `extract`. It streams, so it
runs in a few MB of RAM, and treats the six parts as one continuous stream.
Verified on synthetic archives split mid-chunk -- extraction is byte-identical.

**This is why Docker may not be needed**: if the archive holds loadable data
(SQL, CSV, or table exports) rather than opaque Transbase page files, we can read the
catalog without ever starting the engine.

## Schema clues already visible

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

## Route options (in preference order)

1. **Read the archive directly** with `jetarch.py`. Now viable -- format is decoded.
   Depends on what the payload turns out to be.
2. **Run Transbase Linux in a container**, restore, query over JDBC. Needs Docker,
   and on Apple Silicon needs `--platform linux/amd64` emulation since the Linux
   binaries are near-certainly x86_64.
3. **Install the full ETK stack** (Tomcat + javaserver). Heaviest; last resort.

## Open questions

- What is actually inside the .jetarch? (next step: `jetarch.py list`)
- Are the payload files raw Transbase database files, or loadable SQL/CSV?
- Which tables carry part -> vehicle links, production date ranges, and SA codes?
