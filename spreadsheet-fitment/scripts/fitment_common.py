#!/usr/bin/env python3
"""Shared helpers for the spreadsheet-driven fitment pipeline.

Part-number rule (BMW): the practical number is the last 7 digits; the full OEM
number is 11 digits. We key everything on the 7-digit form and keep any 11-digit
forms alongside. A single cell may hold several part numbers (comma/semicolon/
slash separated) and may use BMW's spaced/dotted formatting (e.g. "63.21-8 383 099").
"""
import re

_SPLIT = re.compile(r"[,\;/\n]+")


def part_keys(raw):
    """Yield 7-digit keys from one raw cell value.

    Returns a list of (key7, full11_or_None). Junk (letters, zeros, <7 digits)
    is dropped. 11+ digit numbers contribute their last 7 as the key and (up to)
    the last 11 as the full form.
    """
    if raw is None:
        return []
    if isinstance(raw, float) and raw.is_integer():
        raw = int(raw)
    s = str(raw).strip()
    if not s:
        return []
    out = []
    for tok in _SPLIT.split(s):
        if re.search(r"[A-Za-z]", tok):        # wheel sizes, alpha codes -> not a BMW number
            continue
        digits = re.sub(r"\D", "", tok)         # strip spaces / dashes / dots
        if not digits or int(digits) == 0:
            continue
        n = len(digits)
        if n < 7:
            continue
        key7 = digits[-7:]
        full11 = digits[-11:] if n >= 11 else None
        out.append((key7, full11))
    return out


def parse_car(car):
    """'2014 BMW 228i' -> ('2014','BMW','228i'); returns None if not year make model."""
    if not car:
        return None
    toks = str(car).strip().split()
    if len(toks) < 3 or not re.fullmatch(r"\d{4}", toks[0]):
        return None
    year, make = toks[0], toks[1]
    model = re.sub(r"\s+", " ", " ".join(toks[2:])).strip()
    return (year, make, model)
