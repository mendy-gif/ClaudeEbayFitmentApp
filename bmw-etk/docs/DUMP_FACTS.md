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

Top-level payload (from `jetarch.py list`):

```
package.properties            1.6 KB   installer metadata
CustomActionData.txt          268 B
filelist.txt                  370 B    <- names the payload files
filelist_script.txt           2 B
files/                        0 B      directory entry
files/postinstallDataDB.cmd   2.3 KB   <- HOW the data is loaded into Transbase
files/relnotes.pdf          120.4 KB   release notes
files/rfile000.000          ~2.0 GB    <- the bulk data; more rfileNNN.NNN expected
```

So the catalog is **not** shipped as loose SQL/CSV. It is a small number of large
`rfileNNN.NNN` blobs plus a `postinstallDataDB.cmd` script that loads them. Reading
that .cmd file is the next step: it names the tool and arguments used to load the
data, which tells us whether the blobs are a Transbase archive (needs the engine) or
a bulk-loader format (readable directly).

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
