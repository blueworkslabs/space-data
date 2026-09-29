"""Plan a publication of public/ over what `pages` currently serves.

    python -m space_data.publish public published
Prints "noop" when the data is unchanged, else "publish <dataset>" after
copying the currently published dataset into public/ as `previous`. Keeping
it for one more build means a phone that read the old index a moment ago can
still download that dataset's files. Older datasets drop out.
"""
from __future__ import annotations

import json
import os
import shutil
import sys

from .build import write_index
from .validate import validate


def plan(public, published):
    with open(os.path.join(public, "v1", "index.json"), encoding="utf-8") as fh:
        index = json.load(fh)
    current = None
    try:
        with open(os.path.join(published, "v1", "index.json"), encoding="utf-8") as fh:
            current = json.load(fh)
    except FileNotFoundError:
        pass
    if current is not None:
        if current.get("content") == index["content"]:
            return "noop"
        old = current.get("dataset")
        if not isinstance(old, str) or old >= index["dataset"]:
            raise ValueError(f"published dataset {old!r} is not older than {index['dataset']}")
        src = os.path.join(published, "v1", old)
        if not os.path.isdir(src):
            raise ValueError(f"published dataset {old} has no files")
        shutil.copytree(src, os.path.join(public, "v1", old))
        index["previous"] = old
        write_index(public, index)
    errs = validate(public)
    if errs:
        raise ValueError("; ".join(errs))
    return f"publish {index['dataset']}"


def main(argv=None):
    public, published = (argv or sys.argv[1:])[:2]
    try:
        print(plan(public, published))
    except ValueError as e:
        sys.exit(f"not publishable: {e}")


if __name__ == "__main__":
    main()
