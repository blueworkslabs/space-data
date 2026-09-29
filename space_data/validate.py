"""Check a public/ tree against the data contract before it is published.

    python -m space_data.validate public
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone

from . import contract as C
from .rows import ELEMENT_FIELDS, SATCAT_FIELDS, element_row, satcat_row

DATASET = re.compile(r"^\d{8}T\d{4}Z$")


def element_dict(r):
    epoch = datetime.fromtimestamp(r[3] / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z" \
        if isinstance(r[3], int) and not isinstance(r[3], bool) else None
    return dict(zip(["NORAD_CAT_ID", "OBJECT_NAME", "OBJECT_ID", "EPOCH", "MEAN_MOTION", "ECCENTRICITY",
                     "INCLINATION", "RA_OF_ASC_NODE", "ARG_OF_PERICENTER", "MEAN_ANOMALY", "BSTAR",
                     "MEAN_MOTION_DOT", "MEAN_MOTION_DDOT"], [r[0], r[1], r[2], epoch, *r[4:]]))


def satcat_dict(r):
    return dict(zip(["NORAD_CAT_ID", "OBJECT_TYPE", "OWNER", "LAUNCH_DATE", "DECAY_DATE", "PERIOD", "APOGEE",
                     "PERIGEE", "RCS", "OBJECT_ID", "OBJECT_NAME"], r))


CHECK = {
    "elements": (len(ELEMENT_FIELDS), lambda r: element_row(element_dict(r))),
    "satcat": (len(SATCAT_FIELDS), lambda r: satcat_row(satcat_dict(r))),
}


def validate(public):
    """Returns a list of problems; empty means publishable."""
    errs = []
    try:
        with open(os.path.join(public, "_headers"), encoding="utf-8") as fh:
            if fh.read() != C.HEADERS:
                errs.append("_headers differs from the contract")
    except OSError:
        errs.append("_headers missing")
    try:
        with open(os.path.join(public, "v1", "index.json"), encoding="utf-8") as fh:
            index = json.load(fh)
    except (OSError, ValueError) as e:
        return errs + [f"v1/index.json unreadable: {e}"]
    if index.get("schema") != C.SCHEMA:
        return errs + [f"schema {index.get('schema')!r}, expected {C.SCHEMA}"]
    dataset = index.get("dataset")
    if not isinstance(dataset, str) or not DATASET.match(dataset) or index.get("path") != f"{dataset}/":
        return errs + [f"bad dataset/path {dataset!r} {index.get('path')!r}"]
    try:
        built = datetime.strptime(index["built"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (KeyError, TypeError, ValueError):
        return errs + ["bad built time"]
    if C.dataset_name(built) != dataset:
        errs.append("dataset name does not match build time")
    if index.get("fields") != {"elements": ELEMENT_FIELDS, "satcat": SATCAT_FIELDS}:
        errs.append("fields differ from the contract")
    if index.get("refreshHours") != C.REFRESH_HOURS or index.get("attribution") != C.ATTRIBUTION:
        errs.append("refreshHours/attribution differ from the contract")
    groups = index.get("groups")
    if not isinstance(groups, dict) or list(groups) != [g["group"] for g in C.GROUPS]:
        return errs + ["groups differ from the contract"]
    digest = hashlib.sha256()
    for g in C.GROUPS:
        name, entry = g["group"], groups[g["group"]]
        kinds = ["elements", "satcat"] if g["satcat"] else ["elements"]
        if sorted(k for k in entry if k in CHECK) != sorted(kinds):
            errs.append(f"{name}: expected files {kinds}")
            continue
        for kind in kinds:
            width, check = CHECK[kind]
            seen, total = set(), 0
            for i, f in enumerate(entry[kind], 1):
                rel = f.get("file")
                if rel != f"{name}/{kind}-{i}.json":
                    errs.append(f"{name}: unexpected file name {rel!r}")
                    continue
                path = os.path.join(public, "v1", dataset, rel)
                try:
                    with open(path, "rb") as fh:
                        body = fh.read()
                except OSError:
                    errs.append(f"{rel} missing")
                    continue
                sha = hashlib.sha256(body).hexdigest()
                digest.update(f"{rel} {sha}\n".encode())
                if len(body) != f.get("bytes") or sha != f.get("sha256"):
                    errs.append(f"{rel}: size or hash differs from the index")
                if len(body) > C.MAX_FILE_BYTES:
                    errs.append(f"{rel}: {len(body)} bytes, limit {C.MAX_FILE_BYTES}")
                doc = json.loads(body)
                if doc.get("schema") != C.SCHEMA or doc.get("kind") != kind or not isinstance(doc.get("rows"), list):
                    errs.append(f"{rel}: bad header")
                    continue
                if len(doc["rows"]) != f.get("rows"):
                    errs.append(f"{rel}: row count differs from the index")
                for r in doc["rows"]:
                    if not isinstance(r, list) or len(r) != width or check(r) != r:
                        errs.append(f"{rel}: invalid row {str(r)[:80]}")
                        break
                    if r[0] in seen:
                        errs.append(f"{rel}: duplicate catalogue number {r[0]}")
                        break
                    seen.add(r[0])
                total += len(doc["rows"])
            if kind == "satcat" and total == 0:
                errs.append(f"{name}: no valid catalogue records")
            if kind == "elements":
                if total < g["min_rows"]:
                    errs.append(f"{name}: {total} element sets, expected at least {g['min_rows']}")
                newest = entry.get("newestEpochMs")
                age_h = (built.timestamp() * 1000 - newest) / 3_600_000 if isinstance(newest, int) else None
                if age_h is None or age_h > C.MAX_NEWEST_EPOCH_AGE_H or age_h < -24:
                    errs.append(f"{name}: newest element set is {age_h if age_h is None else round(age_h, 1)} h old")
    if index.get("content") != digest.hexdigest():
        errs.append("content digest differs")
    allowed = {"index.json", dataset}
    previous = index.get("previous")
    if previous is not None:
        if not isinstance(previous, str) or not DATASET.match(previous) or previous >= dataset:
            errs.append(f"bad previous dataset {previous!r}")
        elif not os.path.isdir(os.path.join(public, "v1", previous)):
            errs.append(f"previous dataset {previous} missing")
        allowed.add(previous)
    extra = sorted(set(os.listdir(os.path.join(public, "v1"))) - allowed)
    if extra:
        errs.append(f"unexpected entries in v1/: {extra}")
    return errs


def main(argv=None):
    public = (argv or sys.argv[1:] or ["public"])[0]
    errs = validate(public)
    for e in errs:
        print("invalid:", e)
    if errs:
        sys.exit(1)
    print(f"{public}: valid")


if __name__ == "__main__":
    main()
