# space-data

A cache of [CelesTrak](https://celestrak.org)'s public orbit data for **Space
Watch**, the Construct module that shows what is passing overhead
(`blueworkslabs/construct`, `docs/space-watch.md`). A scheduled job downloads
each list once, turns it into the compact rows the module uses, and publishes
static files on Cloudflare Pages. Phones then read our copy instead of each
querying CelesTrak. CelesTrak's [usage policy](https://celestrak.org/usage-policy.php)
asks anyone serving many devices to cache like this.

Nothing here is location-specific: every phone downloads the same whole lists
and works out positions itself.

## Source and cadence

- CelesTrak GP data (OMM JSON) and SATCAT records, by group. See the table below.
- The job runs every **4 hours** at :37. CelesTrak updates GP data every 2 hours and asks for at
  most one download per update. Requests go one at a time, 2 s apart.
- **Persisted cadence.** `space_data.run` keeps its state in `state.json` on the
  orphan branch `state`, which Pages does not serve. Before any request it loads
  that state and applies a **minimum interval of 3 hours since the last attempt**.
  That covers cron, manual dispatch and reruns alike. The attempt is saved
  *before* the first request, so a cancelled or crashed run still counts. If the
  state cannot be read or saved, nothing is requested.
- **Stop on anything unexpected, and stay stopped.** Any answer other than HTTP
  200 JSON ends the run: no retry, no redirect following, nothing published.
  That covers redirects, 403/404, 5xx, HTML notices, timeouts, and data below
  the minimums. It also saves a **hold**. Every later run fails immediately,
  without a request, until a human investigates and runs the workflow manually
  with `clear_hold` set to a short note. Clearing does not bypass the interval.
  The previous data stays online throughout.
- **SATCAT follows its own cadence.** CelesTrak updates it manually once or
  twice a day. Each run makes one request to `satcat/jsonDir.php` (the
  documented update check). When `satcat.csv`'s mtime and size match the last
  successful run, the published catalogue rows are revalidated and reused.
  Only on an update, or if the published rows are missing or damaged, are the
  four group lists downloaded. A typical run is 1 + 5 requests; with a
  SATCAT update, 1 + 9.
- Unchanged data is not republished (same content digest).
- Attribution: *Orbital elements and satellite catalogue: CelesTrak
  (celestrak.org), based on U.S. Space Force general perturbations data.*

| group | CelesTrak `GROUP` | SATCAT | minimum element sets |
|---|---|---|---:|
| visual | `visual` | yes | 100 |
| last-30-days | `last-30-days` | yes | 1 |
| gnss | `gnss` | yes | 100 |
| geo | `geo` | yes | 300 |
| starlink | `starlink` | no (several MB, little use) | 5000 |

A group with fewer valid element sets than its minimum stops the run, as does
an empty or wholly invalid required SATCAT list. These guards catch empty and
severely truncated responses; they cannot prove a valid JSON list is complete.

## Data contract, schema 1

Space Watch relies on this. Changing field meaning or layout requires
`schema: 2` and a module update.

```
public/
  _headers
  index.html                  (what this is, attribution)
  v1/
    index.json
    <dataset>/<group>/elements-<n>.json
    <dataset>/<group>/satcat-<n>.json
```

- `<dataset>` is the build time in UTC, `YYYYMMDDTHHMMZ` (sorts by time).
  Dataset paths are immutable. The current and the previous dataset are
  served; the previous stays for one more build. That way a phone that read the
  old index a moment ago can still fetch its files. Older datasets are removed.
- Every file is at most **1,000,000 bytes**, well under the host's 2 MiB
  `net.http` JSON limit. Larger groups are split into `-1`, `-2`, … in source order.
- `_headers`: `v1/index.json` is cached for 5 minutes, dataset files for a year
  (immutable, rule `/v1/:dataset/*`; Cloudflare allows one splat per rule). Both allow any origin.
  A change to `_headers` republishes even when the data is unchanged.

### `v1/index.json`

```json
{
  "schema": 1,
  "dataset": "20260929T2100Z",
  "built": "2026-09-29T21:00:12Z",
  "path": "20260929T2100Z/",
  "refreshHours": 4,
  "content": "<sha256 over the dataset's files>",
  "previous": "20260929T1700Z",
  "fields": {"elements": ["id", "name", "…"], "satcat": ["id", "type", "…"]},
  "groups": {
    "visual": {
      "elements": [{"file": "visual/elements-1.json", "rows": 156, "bytes": 21830, "sha256": "…"}],
      "satcat": [{"file": "visual/satcat-1.json", "rows": 158, "bytes": 11000, "sha256": "…"}],
      "newestEpochMs": 1790660760000
    },
    "starlink": {"elements": [{"file": "starlink/elements-1.json", "…": "…"}, {"file": "starlink/elements-2.json", "…": "…"}], "newestEpochMs": 0}
  },
  "source": {"name": "CelesTrak", "url": "https://celestrak.org"},
  "attribution": "Orbital elements and satellite catalogue: CelesTrak (celestrak.org), based on U.S. Space Force general perturbations data."
}
```

`groups` lists the groups in the table's order. `previous` is `null` on the first publication.
`content` changes exactly when any file's bytes change.

### Data files

`{"schema": 1, "kind": "elements" | "satcat", "rows": [...]}`. Rows are
exactly Space Watch's compact rows. The module validates every row again on download.

- **elements**: `[id, name, intdes|null, epochMs, meanMotion, eccentricity,
  inclination, raan, argPericenter, meanAnomaly, bstar, meanMotionDot,
  meanMotionDdot]`. OMM units (rev/day, degrees). `epochMs` is Unix
  milliseconds of the UTC epoch, truncated like JavaScript's `Date.parse`.
  Records outside the module's bounds are dropped (e.g. eccentricity > 0.99,
  mean motion outside 0.05–20 rev/day), as is a repeated catalogue number.
- **satcat**: `[id, type, owner, launch, decay, periodMin, apogeeKm,
  perigeeKm, rcsM2, intdes, name]`. `type` is `PAY`, `R/B`, `DEB` or
  `UNK`; unknown types become `UNK`, and invalid optional fields become `null`.
- Names are printable ASCII, at most 40 characters. Catalogue numbers are
  1–999,999,999 (six-digit numbers began on 2026-07-11).

`tests/fixtures/expected-module-rows.json` holds Space Watch 0.4.1's own parser
output for the fixtures, and the tests require identical rows.

## Running it

```
python -m unittest discover -s tests -t .
python -m space_data.run                                      # the job: gated, queries CelesTrak, publishes
python -m space_data.build --from work/raw --out public       # rebuild from answers the job saved
python -m space_data.validate public
scripts/publish.sh public --dry-run
```

Standard library only (Python 3.12). Cloudflare Pages serves the orphan
`pages` branch: no build command, output directory `/`.

GitHub pauses scheduled workflows in repositories without activity for 60
days. The data's age is in `index.json` and shown by the module; Space Watch 0.5.0 is planned to fall back to CelesTrak when the mirror is stale.
