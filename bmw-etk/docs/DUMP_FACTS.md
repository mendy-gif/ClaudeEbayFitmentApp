# What the dump actually is

Established facts about the source media. Update as we learn more.

## The media

- **File:** `/Users/mendydonin/Downloads/BMW ETK 2020-01.iso` (an ISO disc image)
- **Mounts at:** `/Volumes/BMW ETK 2020-01` (macOS mounts it natively, read-only)
- **Repo lives at:** `/Users/mendydonin/Documents/GitHub/ClaudeEbayFitmentApp`
- ETK data version **3.220.006**, disc dated April 2022, labelled "2020-01"

## The database engine: Transbase

The disc ships `transbase/` and `transbase_linux/` directories. BMW ETK is served by
**Transbase**, a commercial RDBMS from Transaction Software GmbH (Munich) -- not
SQL Server, Firebird, or anything mainstream.

Consequences:

- There is no off-the-shelf Python driver. Access is via Transbase's own tooling,
  ODBC, or (most likely) the **JDBC driver** shipped in the Java stack on the disc.
- `transbase_linux/` is a Linux server build, so a Linux container can run the
  engine even though the host is a Mac.

## The data payload

Six ~1 GB parts totalling ~5.8 GB:

```
ETK-Data_3.220.006_--.jetarch.part1 .. part6
ETK-Data_3.220.006_--.md5.part1     .. part6   (checksum sidecars)
```

`.jetarch` is an ETK-specific archive. The parts reassemble (concatenate) into one
archive that the installer restores into a Transbase database directory.

## Architecture on the disc

It is a Java web application, not a desktop app:

- `javaserver/` (469 MB), `tomcat/`, `jdk/`, `jre_1.8.0_92.*` -- the server tier
- `javaclient/` (50 MB), `javaclientws/` -- the client tier
- `standalone/` -- a single-machine mode; the most promising route if it avoids Tomcat
- `install_server.sh` -- a **Linux** install script, which is why a container is viable
- `admintool/`, `migration/`, `ticker/`, `axis/` (SOAP), `etk_nutzer/` ("ETK user")

## Route options (in preference order)

1. **Read the archive directly.** If `.jetarch` turns out to be a known container
   (zip/gzip/tar/proprietary-but-simple), extract the Transbase data files and parse
   them without ever running the engine. Cheapest, no Docker.
2. **Run Transbase Linux in a container**, restore the archive, and query over
   JDBC/ODBC to export the tables we need. Reliable, needs Docker.
3. **Install the full ETK stack** in a Linux container (Tomcat + javaserver). Heaviest;
   only if the data model is unreadable without the app's own logic.

## Open questions

- Is the Mac Apple Silicon or Intel? Transbase Linux binaries are near-certainly
  x86_64, so on Apple Silicon a container needs `--platform linux/amd64` emulation.
- Is there enough free disk? The restored database will likely exceed the 5.8 GB archive.
- Does the disc include a Transbase JDBC driver jar we can drive directly?
