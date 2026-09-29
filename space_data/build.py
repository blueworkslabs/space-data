"""Build one dataset: fetch every group, compact, shard, write public/.

    python -m space_data.build --out public [--save-raw work/raw | --from work/raw]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone

from . import contract as C
from . import fetch as F
from .rows import ELEMENT_FIELDS, SATCAT_FIELDS, element_row, rows, satcat_row


def dumps(obj):
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def shards(row_list, kind, limit=None):
    """Split rows into bodies below limit bytes, in order. Each body is
    {"schema", "kind", "rows"}; the dataset name is not inside, so identical
    data gives identical bytes."""
    limit = limit or C.MAX_FILE_BYTES
    head = len(dumps({"schema": C.SCHEMA, "kind": kind, "rows": []}))
    out, current, size = [], [], head
    for r in row_list:
        n = len(dumps(r)) + 1
        if current and size + n > limit:
            out.append(current)
            current, size = [], head
        current.append(r)
        size += n
    out.append(current)
    return [dumps({"schema": C.SCHEMA, "kind": kind, "rows": part}).encode() for part in out]


def collect(fetch):
    """Rows per group. Raises F.Stop or ValueError; nothing is written then."""
    data = {}
    for g in C.GROUPS:
        name = g["group"]
        elements = rows(fetch(C.GP_URL.format(group=name)), element_row)
        if len(elements) < g["min_rows"]:
            raise ValueError(f"{name}: {len(elements)} valid element sets, expected at least {g['min_rows']}")
        satcat = rows(fetch(C.SATCAT_URL.format(group=name)), satcat_row) if g["satcat"] else None
        data[name] = {"elements": elements, "satcat": satcat}
    return data


def write(public, data, built):
    dataset = C.dataset_name(built)
    base = os.path.join(public, "v1", dataset)
    groups, digest = {}, hashlib.sha256()
    for name in [g["group"] for g in C.GROUPS]:
        entry = {}
        for kind in ("elements", "satcat"):
            row_list = data[name][kind]
            if row_list is None:
                continue
            files = []
            bodies = shards(row_list, kind)
            for i, body in enumerate(bodies, 1):
                rel = f"{name}/{kind}-{i}.json"
                os.makedirs(os.path.join(base, name), exist_ok=True)
                with open(os.path.join(base, rel), "wb") as fh:
                    fh.write(body)
                sha = hashlib.sha256(body).hexdigest()
                digest.update(f"{rel} {sha}\n".encode())
                files.append({"file": rel, "rows": len(json.loads(body)["rows"]), "bytes": len(body), "sha256": sha})
            entry[kind] = files
        epochs = [r[3] for r in data[name]["elements"]]
        entry["newestEpochMs"] = max(epochs)
        groups[name] = entry
    index = {
        "schema": C.SCHEMA,
        "dataset": dataset,
        "built": built.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "path": f"{dataset}/",
        "refreshHours": C.REFRESH_HOURS,
        "content": digest.hexdigest(),
        "previous": None,
        "fields": {"elements": ELEMENT_FIELDS, "satcat": SATCAT_FIELDS},
        "groups": groups,
        "source": {"name": "CelesTrak", "url": "https://celestrak.org"},
        "attribution": C.ATTRIBUTION,
    }
    write_index(public, index)
    with open(os.path.join(public, "_headers"), "w", encoding="utf-8") as fh:
        fh.write(C.HEADERS)
    return index


def write_index(public, index):
    os.makedirs(os.path.join(public, "v1"), exist_ok=True)
    with open(os.path.join(public, "v1", "index.json"), "w", encoding="utf-8") as fh:
        fh.write(dumps(index))
    counts = ", ".join(f"{name} {sum(f['rows'] for f in g['elements'])}" for name, g in index["groups"].items())
    with open(os.path.join(public, "index.html"), "w", encoding="utf-8") as fh:
        fh.write(LANDING.format(dataset=index["dataset"], built=index["built"], counts=counts,
                                attribution=index["attribution"], hours=index["refreshHours"]))


def report(index):
    lines = [f"## space-data {index['dataset']}", "", "| group | element sets | files | bytes | newest epoch |", "|---|---:|---:|---:|---|"]
    for name, g in index["groups"].items():
        files = g["elements"] + g.get("satcat", [])
        newest = datetime.fromtimestamp(g["newestEpochMs"] / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M")
        lines.append(f"| {name} | {sum(f['rows'] for f in g['elements'])} | {len(files)} | {sum(f['bytes'] for f in files)} | {newest} |")
    return "\n".join(lines) + "\n"


LANDING = """<!doctype html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Space Watch orbit data</title>
<style>body{{font:16px/1.5 system-ui,sans-serif;max-width:40rem;margin:2rem auto;padding:0 1rem}}</style>
<h1>Space Watch orbit data</h1>
<p>A cache of CelesTrak's public orbit data for the Space Watch module of
<a href="https://github.com/blueworkslabs/construct">Construct</a>, refreshed every {hours} hours so
that phones do not each query CelesTrak. Dataset {dataset}, built {built}: {counts} element sets.</p>
<p>{attribution} Please use <a href="https://celestrak.org">CelesTrak</a> directly for anything else.
Index: <a href="v1/index.json">v1/index.json</a>. Pipeline:
<a href="https://github.com/blueworkslabs/space-data">blueworkslabs/space-data</a>.</p>
"""


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--out", default="public")
    p.add_argument("--work", default="work")
    src = p.add_mutually_exclusive_group()
    src.add_argument("--from", dest="source", help="read saved responses instead of CelesTrak")
    src.add_argument("--save-raw", help="also save CelesTrak responses here")
    p.add_argument("--built", help="build time (ISO, UTC); default now")
    a = p.parse_args(argv)
    fetch = F.saved(a.source) if a.source else F.live()
    if a.save_raw:
        fetch = F.saving(fetch, a.save_raw)
    built = (datetime.fromisoformat(a.built.replace("Z", "+00:00")) if a.built else datetime.now(timezone.utc)).astimezone(timezone.utc)
    if os.path.exists(a.out) and os.listdir(a.out):
        sys.exit(f"{a.out} is not empty")
    try:
        data = collect(fetch)
    except (F.Stop, ValueError) as e:
        sys.exit(f"stopped: {e}")
    index = write(a.out, data, built)
    os.makedirs(a.work, exist_ok=True)
    with open(os.path.join(a.work, "report.md"), "w", encoding="utf-8") as fh:
        fh.write(report(index))
    print(report(index))


if __name__ == "__main__":
    main()
