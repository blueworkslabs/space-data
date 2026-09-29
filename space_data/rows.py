"""Compact rows, identical to the ones Space Watch builds and re-validates.

Elements (``ui/space-orbit.js``), 13 fields:
    [id, name, intdes|None, epochMs, n, e, i, raan, argp, M, bstar, ndot, nddot]
SATCAT (``ui/space-catalog.js``), 11 fields:
    [id, type, owner, launch, decay, periodMin, apogeeKm, perigeeKm, rcsM2, intdes, name]

A record that fails a check is dropped, exactly as the module drops it. Field
bounds and patterns here must stay equal to the module's; the module applies
them again to everything it downloads.
"""
from __future__ import annotations

import math
import re
from datetime import datetime, timezone

ELEMENT_FIELDS = ["id", "name", "intdes", "epochMs", "meanMotion", "eccentricity",
                  "inclination", "raan", "argPericenter", "meanAnomaly", "bstar",
                  "meanMotionDot", "meanMotionDdot"]
SATCAT_FIELDS = ["id", "type", "owner", "launch", "decay", "periodMin", "apogeeKm",
                 "perigeeKm", "rcsM2", "intdes", "name"]

EPOCH = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d{1,6})?Z?$")
INTDES = re.compile(r"^\d{4}-\d{3}[A-Z]{1,3}$")
DATE = re.compile(r"^\d{4}-\d\d-\d\d$")
OWNER = re.compile(r"^[A-Z]{1,6}$")
PRINTABLE = re.compile(r"[^\x20-\x7e]")
TYPES = {"PAY", "R/B", "DEB", "UNK"}
UNIX = datetime(1970, 1, 1, tzinfo=timezone.utc)


def num(x, lo, hi):
    """A JSON number within [lo, hi], else None (booleans are not numbers)."""
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        return None
    if not math.isfinite(x) or x < lo or x > hi:
        return None
    return x


def text(x, n=40):
    if not isinstance(x, str):
        return None
    return PRINTABLE.sub("", x).strip()[:n] or None


def catalog_id(x):
    if isinstance(x, bool) or not isinstance(x, int) or x < 1 or x > 999_999_999:
        return None
    return x


def epoch_ms(value):
    """Milliseconds since 1970 as JavaScript's Date.parse reads the OMM epoch
    (UTC; fractions beyond milliseconds are truncated)."""
    if not isinstance(value, str) or not EPOCH.match(value):
        return None
    s = value[:-1] if value.endswith("Z") else value
    try:
        dt = datetime.fromisoformat(s).replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    d = dt - UNIX
    return d.days * 86_400_000 + d.seconds * 1000 + d.microseconds // 1000


def element_row(o):
    if not isinstance(o, dict):
        return None
    oid, name, epoch = catalog_id(o.get("NORAD_CAT_ID")), text(o.get("OBJECT_NAME")), epoch_ms(o.get("EPOCH"))
    f = [
        num(o.get("MEAN_MOTION"), 0.05, 20),
        num(o.get("ECCENTRICITY"), 0, 0.99),
        num(o.get("INCLINATION"), 0, 180),
        num(o.get("RA_OF_ASC_NODE"), 0, 360),
        num(o.get("ARG_OF_PERICENTER"), 0, 360),
        num(o.get("MEAN_ANOMALY"), 0, 360),
        num(o.get("BSTAR"), -1, 1),
        num(o.get("MEAN_MOTION_DOT"), -1, 1),
        num(o.get("MEAN_MOTION_DDOT"), -1, 1),
    ]
    if oid is None or name is None or epoch is None or any(v is None for v in f):
        return None
    intdes = o.get("OBJECT_ID")
    intdes = intdes if isinstance(intdes, str) and INTDES.match(intdes) else None
    return [oid, name, intdes, epoch, *f]


def satcat_row(o):
    if not isinstance(o, dict):
        return None
    oid = catalog_id(o.get("NORAD_CAT_ID"))
    if oid is None:
        return None

    def match(key, pattern):
        v = o.get(key)
        return v if isinstance(v, str) and pattern.match(v) else None

    return [
        oid,
        o.get("OBJECT_TYPE") if o.get("OBJECT_TYPE") in TYPES else "UNK",
        match("OWNER", OWNER),
        match("LAUNCH_DATE", DATE),
        match("DECAY_DATE", DATE),
        num(o.get("PERIOD"), 1, 100000),
        num(o.get("APOGEE"), 0, 1000000),
        num(o.get("PERIGEE"), 0, 1000000),
        num(o.get("RCS"), 0, 100000),
        match("OBJECT_ID", INTDES),
        text(o.get("OBJECT_NAME")),
    ]


def rows(records, make):
    """Valid rows in source order, first occurrence of each catalogue number."""
    if not isinstance(records, list):
        raise ValueError("expected a JSON array")
    seen, out = set(), []
    for o in records:
        r = make(o)
        if r is None or r[0] in seen:
            continue
        seen.add(r[0])
        out.append(r)
    return out
