# BMW ETK → part fitment data

Turning a BMW dealer parts-catalogue disc (**ETK**, *Elektronischer Teilekatalog*)
into data we can query — so eBay listings can say exactly which cars a part fits.

> **New here?** Start with [`docs/RUNBOOK.md`](docs/RUNBOOK.md) for the commands, or
> [`docs/DUMP_FACTS.md`](docs/DUMP_FACTS.md) for how the disc actually works.

## Why

The eBay fitment project expands compatibility from a donor vehicle to a whole BMW
chassis family using rules. That is deliberately broad. The ETK knows the *real*
answer — which parts BMW fitted to which vehicles, down to build dates and factory
options. Distilling that gives a third, far more precise fitment source.

## Goals, in order

| # | Goal | Status |
|---|------|--------|
| 1 | **part number → vehicle/chassis** table, fed into the eBay project | in progress |
| 2 | **VIN decoder** — VIN → chassis, model, year, engine, SA option codes | not started |
| 3 | **VIN + part number → does it fit?** using build dates and option codes | not started |

Goals 2 and 3 need the same tables as goal 1 plus production-date ranges and option
codes, so the schema work serves all three.

## How it works

```
BMW ETK 2020-01.iso                     mounted read-only, never copied into git
  └── ETK-Data_*.jetarch.part1..6       5.7 GB in six parts, one custom container
        └── files/rfile000.000 .002,    a Transbase CD-ROM database
            files/rfile001.000
              └── attached by tbadmin inside a Docker container
                    └── queried with tbi  →  CSV  →  distilled tables
```

Three things had to be worked out, none of them documented publicly:

1. **The `.jetarch` container format** — decoded byte by byte from its own header.
   `scripts/jetarch.py` now reads and extracts it.
2. **Which database engine** — Transbase, a niche 2004 commercial RDBMS, shipped on
   the disc as 32-bit Intel Linux binaries.
3. **How to attach the catalogue** — BMW's own install script omits one of the four
   ROM files; the attach only succeeds with all four.

## Layout

```
bmw-etk/
├── CLAUDE.md            project memory — read this first if you are Claude
├── README.md            this file
├── docs/
│   ├── DUMP_FACTS.md    the technical record: formats, commands, decisions
│   └── RUNBOOK.md       copy-paste commands for common tasks
├── scripts/             reading the disc (Python stdlib + shell only)
├── docker/              running Transbase and querying the catalogue
├── dump/                raw extracted data — GITIGNORED, never committed
└── data/                distilled output — committed
```

## Ground rules

- **The raw catalogue is never committed.** `dump/` and every database-ish file
  extension are gitignored. Only derived tables in `data/` go into git.
- **Claude writes the code; the human runs it.** A Claude cloud session cannot see
  the Mac where the disc lives, so every step is a command you paste and a result you
  paste back.
- ETK data is BMW's licensed catalogue. Using it privately to work out fitment for
  parts you are actually selling is ordinary practice; republishing the catalogue
  itself is not the goal, which is why only distilled tables are kept.

## Current state

The catalogue **attaches and boots**. Transbase runs under Docker on Apple Silicon,
the database `etk_publ` is live, and the next step is dumping the schema to find the
part↔vehicle tables. See `docs/DUMP_FACTS.md` for everything established so far.
