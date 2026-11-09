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

## PENDING FIELD CHECKS — none collected yet

**Seattle has no field-verified ground truth. Not one point.** This is the
single largest difference in trustworthiness between this pipeline and
Chicago's, and it is recorded here rather than buried in a comment because it
is easy to forget once everything looks green.

Chicago refuses any build whose four checked street corners come out wrong,
and that gate has already earned its keep twice: it caught a pipeline change
that would have shipped buffer zones as restrictions, and it caught a
43%-of-the-city over-extension bug. `GROUND_TRUTH` in
`build_seattle_zones.py` is an empty list, so Seattle currently has nothing
equivalent — every gate it does have checks the file's internal consistency,
not whether the file is right about the city.

What would fix it: stand at a Seattle kerb with an RPZ sign, note the exact
position and the zone on the sign, and add it to `GROUND_TRUTH`. Instructions
are in the script beside the list. Three or four points in different
neighbourhoods would be enough to make the gate meaningful; one would be
better than none.

Until then, treat a green build as "the pipeline did what it says" and not as
"the data is correct".

### Confirmed, not a defect

**53% of street segments are recorded on one flank only** (1,034 of 1,946, in
the 2026-11-09 build). Chicago's figure is 9.7%, so this looked like a
possible data gap. It is not: the user confirmed Seattle genuinely restricts
one side of many blocks, partly because of one-way and steep-hill streets and
partly because of the amount of commercial frontage. No pipeline change is
warranted. It does mean the app's strict-containment lookup will often put a
driving car outside the ribbon, which is what side-of-street reporting exists
to answer honestly.

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
