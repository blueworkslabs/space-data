"""Data contract, schema 1 (see README.md). Space Watch relies on these."""

SCHEMA = 1

GP_URL = "https://celestrak.org/NORAD/elements/gp.php?GROUP={group}&FORMAT=json"
SATCAT_URL = "https://celestrak.org/satcat/records.php?GROUP={group}&FORMAT=json"

# CelesTrak groups mirrored. min_rows guards against a truncated or empty
# answer silently replacing good data; satcat: whether SATCAT rows are mirrored
# (Starlink's would be several MB for little use in the module).
GROUPS = [
    {"group": "visual", "satcat": True, "min_rows": 100},
    {"group": "last-30-days", "satcat": True, "min_rows": 1},
    {"group": "gnss", "satcat": True, "min_rows": 100},
    {"group": "geo", "satcat": True, "min_rows": 300},
    {"group": "starlink", "satcat": False, "min_rows": 5000},
]

# One published file stays well under the host's 2 MiB net.http JSON limit.
MAX_FILE_BYTES = 1_000_000
HOST_LIMIT_BYTES = 2 * 1024 * 1024

# Newest element set older than this means CelesTrak is serving stale data.
MAX_NEWEST_EPOCH_AGE_H = 72

# How often the scheduled build runs; the module treats data as stale after
# a few missed runs.
REFRESH_HOURS = 4

ATTRIBUTION = ("Orbital elements and satellite catalogue: CelesTrak (celestrak.org), "
               "based on U.S. Space Force general perturbations data.")

USER_AGENT = "space-data/1 (+https://github.com/blueworkslabs/space-data)"

# Cloudflare Pages allows one splat per rule; ":dataset" is a placeholder, and
# the rule cannot match v1/index.json (no slash after the name).
HEADERS = """/v1/index.json
  Cache-Control: public, max-age=300
  Access-Control-Allow-Origin: *
/v1/:dataset/*
  Cache-Control: public, max-age=31536000, immutable
  Access-Control-Allow-Origin: *
"""


def dataset_name(built):
    """Dataset id from the build time, e.g. 20260929T2017Z (sorts by time)."""
    return built.strftime("%Y%m%dT%H%MZ")
