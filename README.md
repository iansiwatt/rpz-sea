# rpz-sea — Seattle residential parking zone data

Public, data-only. The iOS app that consumes this lives in the private
`rpz-sea-app`.

**Why the split.** A private repo's raw URLs and release assets both require
an authenticated token, so an app polling a private repo would have to ship a
credential inside the app binary. The data therefore lives here, in the open,
and only the data. Chicago has the same split for the same reason
(`rpz-chi` public, `rpz-chi-app` private).

## What is published

| File | What it is |
|---|---|
| `seattle_rpz_zones.geojson` | the zone geometry the app uses, ~1.6 MB |
| `manifest.json` | ~1 KB pointer the app polls to decide whether to download |

Both are committed in the **same commit**, always. A manifest advertising a
version the committed file does not carry would make every app download the
file and then refuse it on a hash mismatch.

The app fetches `manifest.json` from `raw.githubusercontent.com`, compares
`data_version` against what it already has, and downloads the GeoJSON only if
the manifest is newer. It then verifies the SHA-256 and its own checks before
installing anything. Any failure leaves the app on the data it has.

## How it is built

```
python3 -I tools/build_seattle_zones.py raw out/seattle_rpz_zones.geojson \
    --refresh --report-json out/report.json --baseline seattle_rpz_zones.geojson
python3 -I tools/make_manifest.py --data out/seattle_rpz_zones.geojson \
    --out out/manifest.json --report out/report.json
```

Source: SDOT's ArcGIS feature service — layer 0 blockfaces filtered to
`PARKING_CATEGORY = 'Restricted Parking Zone'`, plus the zone polygons.

**Fail closed.** Validation runs between building and writing. Six structural
gates that must be exactly true (feature floor, a ring the app can slice,
zero-area polygons, required properties, Seattle bounding box, unique
`row_id`) and five statistical ones with generous ceilings (zone count, our
compass side against SDOT's own `SIDE` field, the ribbon self-check, clip
error against SDOT's `DISTANCE` fields, feature-count drift). On any failure
the script exits non-zero and writes nothing, so a bad fetch cannot degrade
data that is already published.

`tools/test_build_seattle_zones.py` proves each gate fires on the fault it
targets and stays quiet on healthy data. It is synthetic, needs no network,
and the workflow runs it **before** trusting the validator to gate a build.

**`GROUND_TRUTH` is empty.** Seattle has no field-verified kerbs yet. That is
the one gate Chicago has and this does not — Chicago refuses a build whose
four checked street corners come out wrong. The list and instructions for
adding a point are in `build_seattle_zones.py`.

## Automation

`.github/workflows/rebuild-data.yml` runs monthly and on demand. It tests the
gates, fetches, builds, validates, writes the manifest, and commits **only
when the regenerated data actually differs** from what is committed. Feature
order is canonicalised by content, so an unchanged city produces a
byte-identical file and the publish decision can be made by hash.

Monthly rather than quarterly on purpose: GitHub disables scheduled workflows
in a repository idle for 60 days, so a quarterly cron would switch itself off
during its own first gap.

Nothing here can be run from the authoring sandbox or from a laptop behind the
same proxy — `services.arcgis.com` is unreachable from both. A workflow run is
the only place this pipeline actually executes.
