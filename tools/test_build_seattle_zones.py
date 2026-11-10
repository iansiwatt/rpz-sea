#!/usr/bin/env python3
"""Exercise the Seattle validation gates with synthetic features.

Run:  python3 -I tools/test_build_seattle_zones.py
Exits non-zero if any gate fails to fire, or fires on healthy data.

The first real run of build_seattle_zones.py will happen inside the GitHub
Action, because neither this container nor the device shell can reach
services.arcgis.com. That makes it worth proving, here, that each gate fires
on the fault it is meant to catch and stays quiet on healthy data -- otherwise
the first thing we'd learn in CI is whether the validator parses.
"""
import importlib.util
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
MOD = os.path.join(HERE, "build_seattle_zones.py")
spec = importlib.util.spec_from_file_location("bsz", MOD)
bsz = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bsz)


def make_feature(row_id, zone="12", lon=-122.33, lat=47.61, side="N", n=3):
    """Build one healthy feature exactly the way build() does."""
    center = [bsz.to_ft((lon + 0.0004 * i, lat)) for i in range(n)]
    offset = bsz.offset_polyline(center, 1.0, bsz.BUFFER_WIDTH_FEET)
    ring = center + list(reversed(offset)) + [center[0]]
    return {
        "type": "Feature",
        "properties": {
            "zone": zone,
            "row_id": row_id,
            "side": side,
            "bend_degrees": bsz.bend_degrees(center),
            "centerline": [bsz.to_deg(c) for c in center],
        },
        "geometry": {"type": "Polygon",
                     "coordinates": [[bsz.to_deg(c) for c in ring]]},
    }


def healthy(n=2500, zones=36):
    return [make_feature(i, zone=str(i % zones + 1),
                         lon=-122.33 + 0.0001 * (i % 300),
                         lat=47.61 + 0.0001 * (i // 300))
            for i in range(n)]


def metrics_for(features, **over):
    m = {"features": len(features),
         "zone_count": len({f["properties"]["zone"] for f in features}),
         "side_mismatch": 0, "probe_fail": 0, "clip_error_p95": 10.0}
    m.update(over)
    return m


# The synthetic fixtures below are invented ribbons in a corner of Seattle;
# they cannot contain a real field-verified point, so with the module's real
# GROUND_TRUTH in place every "healthy" case would fail the ground-truth gate.
# Park the real list and assert it against the REAL data instead, further
# down — which is a stronger test than the synthetic one it replaces.
REAL_GROUND_TRUTH = list(bsz.GROUND_TRUTH)
bsz.GROUND_TRUTH = []

FAILS = 0


def check(label, got, want_substr):
    global FAILS
    if want_substr is None:
        ok = not got
        detail = f"expected no failures, got {got}"
    else:
        ok = any(want_substr in g for g in got)
        detail = f"expected a failure mentioning {want_substr!r}, got {got}"
    print(f"  {'PASS' if ok else 'FAIL'}  {label}")
    if not ok:
        print(f"        {detail}")
        FAILS += 1


print("healthy baseline")
fs = healthy()
check("clean build passes", bsz.validate(fs, metrics_for(fs), 2500), None)
check("no baseline is not a drift failure",
      bsz.validate(fs, metrics_for(fs), 0), None)

print("structural gates")
check("too few features",
      bsz.validate(fs[:50], metrics_for(fs[:50]), 0), "floor")

bad = healthy(2500)
# Reverse one ring: same points, order the app cannot slice.
bad[7]["geometry"]["coordinates"][0] = list(
    reversed(bad[7]["geometry"]["coordinates"][0]))
check("ring the app cannot slice",
      bsz.validate(bad, metrics_for(bad), 2500), "cannot slice")

bad = healthy(2500)
# Collapse a ring to a single repeated point: well-formed shape, zero area.
c = bad[9]["properties"]["centerline"]
flat = [c[0]] * len(c)
bad[9]["properties"]["centerline"] = flat
bad[9]["geometry"]["coordinates"][0] = flat + list(reversed(flat)) + [flat[0]]
check("zero-area polygon",
      bsz.validate(bad, metrics_for(bad), 2500), "zero-area")

bad = healthy(2500)
del bad[11]["properties"]["side"]
check("missing required property",
      bsz.validate(bad, metrics_for(bad), 2500), "missing a required property")

bad = healthy(2500)
# lon/lat swapped, the classic projection fault.
bad[13]["properties"]["centerline"] = [[y, x] for x, y in
                                       bad[13]["properties"]["centerline"]]
check("feature outside Seattle",
      bsz.validate(bad, metrics_for(bad), 2500), "outside Seattle")

bad = healthy(2500)
bad[15]["properties"]["row_id"] = bad[16]["properties"]["row_id"]
check("duplicate row_id",
      bsz.validate(bad, metrics_for(bad), 2500), "duplicate row_id")

print("statistical gates")
fs = healthy()
check("zone count collapse",
      bsz.validate(fs, metrics_for(fs, zone_count=3), 2500), "distinct zones")
check("side disagreement with SDOT",
      bsz.validate(fs, metrics_for(fs, side_mismatch=400), 2500),
      "compass side disagrees")
check("side disagreement just under ceiling",
      bsz.validate(fs, metrics_for(fs, side_mismatch=120), 2500), None)
check("probe self-check failures",
      bsz.validate(fs, metrics_for(fs, probe_fail=100), 2500),
      "ribbon self-check")
check("clip error p95",
      bsz.validate(fs, metrics_for(fs, clip_error_p95=200.0), 2500),
      "clipped stretches")
check("clip error absent is not a failure",
      bsz.validate(fs, metrics_for(fs, clip_error_p95=None), 2500), None)
check("feature count drift",
      bsz.validate(fs, metrics_for(fs), 5000), "feature count moved")
check("drift just under ceiling",
      bsz.validate(fs, metrics_for(fs), 2700), None)

print("ground truth")
pt = fs[0]["properties"]["centerline"][1]
inside = bsz.to_deg((bsz.to_ft(pt)[0],
                     bsz.to_ft(pt)[1] + bsz.BUFFER_WIDTH_FEET / 2))
bsz.GROUND_TRUTH = [{"label": "synthetic kerb", "lon": inside[0],
                     "lat": inside[1], "expect_zone": fs[0]["properties"]["zone"]}]
check("correct ground truth passes", bsz.validate(fs, metrics_for(fs), 2500), None)
bsz.GROUND_TRUTH = [{"label": "synthetic kerb", "lon": inside[0],
                     "lat": inside[1], "expect_zone": "999"}]
check("wrong ground truth fails",
      bsz.validate(fs, metrics_for(fs), 2500), "GROUND TRUTH")
bsz.GROUND_TRUTH = []

print("data_version carry-forward")
import os, tempfile
from datetime import datetime, timezone
_today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
_d = tempfile.mkdtemp()
_f = healthy(200)

def _write(name, version, feats):
    env = {"type": "FeatureCollection"}
    if version:
        env["data_version"] = version
    env["features"] = feats
    path = os.path.join(_d, name)
    json.dump(env, open(path, "w"))
    return path

_stamped = _write("stamped.geojson", "2026-03-01", _f)
_unstamped = _write("unstamped.geojson", None, _f)
_corrupt = os.path.join(_d, "corrupt.geojson")
open(_corrupt, "w").write("{not json")

check("no baseline -> today",
      [] if bsz.resolve_data_version(_f, None) == _today else ["wrong"], None)
check("identical content carries the old date forward",
      [] if bsz.resolve_data_version(_f, _stamped) == "2026-03-01" else ["wrong"], None)
check("unstamped baseline -> today",
      [] if bsz.resolve_data_version(_f, _unstamped) == _today else ["wrong"], None)
check("corrupt baseline -> today",
      [] if bsz.resolve_data_version(_f, _corrupt) == _today else ["wrong"], None)
_changed = json.loads(json.dumps(_f))
_changed[0]["properties"]["zone"] = "9999"
check("changed content -> today",
      [] if bsz.resolve_data_version(_changed, _stamped) == _today else ["wrong"], None)
check("dropped feature -> today",
      [] if bsz.resolve_data_version(_f[:-1], _stamped) == _today else ["wrong"], None)
check("reordered input still carries forward",
      [] if bsz.resolve_data_version(bsz.canonical_sort(list(reversed(_f))), _stamped)
          == "2026-03-01" else ["wrong"], None)
# The same-day sequence. This is the case that actually occurred on
# 2026-11-09 and that the app could not see past.
_sameday = _write("sameday.geojson", _today, _f)
_changed2 = json.loads(json.dumps(_f))
_changed2[0]["properties"]["zone"] = "4242"
check("changed content, baseline stamped TODAY -> .01 suffix",
      [] if bsz.resolve_data_version(_changed2, _sameday) == _today + ".01" else ["wrong"], None)
_seq1 = _write("seq1.geojson", _today + ".01", _f)
check("changed content again the same day -> .02",
      [] if bsz.resolve_data_version(_changed2, _seq1) == _today + ".02" else ["wrong"], None)
check("UNchanged content on a suffixed baseline still carries forward",
      [] if bsz.resolve_data_version(_f, _seq1) == _today + ".01" else ["wrong"], None)

# Why the suffix is zero-padded: unpadded, ".10" sorts BELOW ".9" as a plain
# string, so an app holding .9 would refuse .10 — the very failure the
# sequence exists to prevent, reappearing nine steps later.
_seq9 = _write("seq09.geojson", _today + ".09", _f)
check("the sequence crosses 9 -> .10",
      [] if bsz.resolve_data_version(_changed2, _seq9) == _today + ".10" else ["wrong"], None)
_seq99 = _write("seq99.geojson", _today + ".99", _f)
try:
    bsz.resolve_data_version(_changed2, _seq99)
    check("exhausting the sequence aborts", ["did not abort"], None)
except SystemExit:
    check("exhausting the sequence aborts", [], None)
_unpadded = _write("unpadded.geojson", _today + ".1", _f)
try:
    bsz.resolve_data_version(_changed2, _unpadded)
    check("an unpadded baseline aborts rather than sorting backwards", ["did not abort"], None)
except SystemExit:
    check("an unpadded baseline aborts rather than sorting backwards", [], None)
_oldday = _write("oldday.geojson", "2026-03-01", _f)
check("changed content on an older baseline -> plain today, no suffix",
      [] if bsz.resolve_data_version(_changed2, _oldday) == _today else ["wrong"], None)

# String ordering is what both the app and the manifest rely on.
_order = ["2026-11-09", "2026-11-09.01", "2026-11-09.09", "2026-11-09.10",
          "2026-11-09.99", "2026-11-10"]
check("versions sort correctly as plain strings",
      [] if _order == sorted(_order) else ["out of order"], None)

check("fingerprint ignores the envelope",
      [] if bsz.content_fingerprint(bsz.canonical_sort(_f)) == bsz.content_fingerprint(
          bsz.canonical_sort(json.load(open(_stamped))["features"])) else ["wrong"], None)

print("real ground truth against the shipped data")
_shipped = None
for _candidate in ("seattle_rpz_zones.geojson",
                   os.path.join(os.path.dirname(HERE), "seattle_rpz_zones.geojson"),
                   os.path.join(os.path.dirname(HERE), "Seattle Parking",
                                "seattle_rpz_zones.geojson")):
    if os.path.exists(_candidate):
        _shipped = _candidate
        break

if not REAL_GROUND_TRUTH:
    print("  SKIP  GROUND_TRUTH is empty — nothing field-verified to check")
elif _shipped is None:
    # Not a pass. A missing data file must not read as "the points are fine".
    print("  SKIP  no seattle_rpz_zones.geojson found to check against")
else:
    _feats = json.load(open(_shipped))["features"]
    bsz.GROUND_TRUTH = REAL_GROUND_TRUTH
    _gt = [f for f in bsz.validate(_feats, metrics_for(_feats), 0)
           if f.startswith("GROUND TRUTH")]
    bsz.GROUND_TRUTH = []
    print(f"  ({len(REAL_GROUND_TRUTH)} field point(s) vs {len(_feats)} "
          f"features in {os.path.basename(_shipped)})")
    check("every field-verified point still holds", _gt, None)

print("canonical sort")
import random
a = healthy(400)
b = list(a)
random.Random(1).shuffle(b)
sa = json.dumps(bsz.canonical_sort(a), sort_keys=True)
sb = json.dumps(bsz.canonical_sort(b), sort_keys=True)
check("shuffled input sorts identically", [] if sa == sb else ["differs"], None)

print()
print("FAILURES:", FAILS)
sys.exit(1 if FAILS else 0)
