#!/usr/bin/env python3
"""Build the bundled Seattle RPZ GeoJSON from SDOT's published blockface data.

Output schema is what ZonePolygonStore.swift decodes. One feature per signed
blockface (one side of one block):

    geometry   Polygon — a ribbon running from the street centerline
               BUFFER_WIDTH_FEET toward the signed kerb. The ring is built as
               centerline + reversed(offset) + [centerline[0]], and the app
               relies on that exact order to recover the offset line (see
               ZonePolygonStore.loadGeoJSON). Do not change it.
    zone       RPZ zone id as a string: "1"…"36", or "A"/"B" (Husky game day).
    centerline the street centerline, clipped to the stretch the blockface
               covers. Both flanks of one block share an identical centerline.
    side       compass side of the flank, computed by the same rule as
               ZonePolygonStore.compassSide so the two always agree.
    bend_degrees  worst segment orientation against the overall bearing.
    time_limit_minutes  posted limit for non-permit holders (omitted if none).
    limit_hours        enforcement hours, e.g. "8AM–8PM Mon–Sat" (omitted
               when SDOT has not recorded them — most blocks).
    row_id     SDOT ELMNTKEY of the blockface.

Sources (SDOT feature service "Restricted_Parking_Zone_Areas"):
    layer 0  blockfaces. Signed RPZ kerbs are PARKING_CATEGORY =
             'Restricted Parking Zone'. RPZ_ZONE alone is NOT evidence of a
             sign: it is set on any block inside a zone's permit area,
             including paid and unrestricted kerbs.
             SIDE='C' records are street centerlines, one per SEGKEY.
    layer 4  RPZ zone polygons (numbered zones).
    layer 3  Husky game day zones A and B.
    The polygon layers are used only to fill RPZ_ZONE where a signed blockface
    lacks it.

Usage:
    python3 -I tools/build_seattle_zones.py RAW_DIR OUT.geojson [--refresh]
                [--baseline PREV.geojson] [--report-json REPORT.json]

RAW_DIR caches the downloaded JSON; it is re-fetched only with --refresh or
when a file is missing. Downloaded data is parsed as JSON only.

--baseline       the currently published GeoJSON, for the feature-count drift
                 check. Missing or omitted skips that one gate; it is never
                 treated as "no drift".
--report-json    write the metrics and any failures as JSON, for CI to attach.

FAIL CLOSED. Validation runs between building and writing: on any failure the
script exits non-zero and OUT.geojson is not touched, so a bad fetch cannot
degrade data that is already published. The write itself goes to a temporary
file and is renamed, so an interrupted run cannot truncate a good file either.
Feature order is canonicalised by content, so an unchanged city produces a
byte-identical file and the publish step can decide by hash.
"""

import json
import math
import hashlib
import os
import statistics
import sys
from datetime import datetime, timezone
import urllib.parse
import urllib.request
from collections import Counter, defaultdict

SERVICE = ("https://services.arcgis.com/ZOyb2t4B0UYuYNYH/arcgis/rest/services/"
           "Restricted_Parking_Zone_Areas/FeatureServer")

# Must match Config in ZonePolygonStore.swift.
REFERENCE_LATITUDE = 47.62
FEET_PER_DEG_LAT = 364000.0
FEET_PER_DEG_LON = 364000.0 * math.cos(math.radians(REFERENCE_LATITUDE))
BUFFER_WIDTH_FEET = 50.0
GRID_TOLERANCE_DEGREES = 20.0
SIDE_PROBE_FEET = 25.0

# SDOT records the citywide 72-hour rule as a 4320-minute limit. That applies
# to every street in the city and is not an RPZ restriction, so it is dropped.
CITYWIDE_LIMIT_MINUTES = 4320

# A clipped centerline shorter than this is digitising residue, not a kerb.
MIN_STRETCH_FEET = 5.0


# ------------------------------------------------------------ validation
# Mirrors the Chicago pipeline's Step 10: the build FAILS CLOSED. Nothing is
# written when a gate trips, so a scheduled rebuild can never quietly commit
# bad data. See the gate list in validate().
#
# Two kinds of gate, deliberately:
#
#   STRUCTURAL — things that must be exactly true because the app depends on
#   them. These are certainties, not judgements, so they have no tolerance.
#
#   STATISTICAL — ratios with generous ceilings, because this pipeline has
#   never been run against a baseline and inventing a tight threshold blind
#   would fail honest builds. They exist to catch a collapse, not to police
#   small drift. Tighten them once a few runs have established the real
#   numbers, which the report records.

# Seattle's bounding box, generously drawn. A coordinate outside this is a
# projection or axis-order bug, not a real kerb.
SEATTLE_BBOX = (-122.47, 47.47, -122.21, 47.75)   # lon_min, lat_min, lon_max, lat_max

MIN_FEATURES = 2_000           # a collapse floor, not an expectation
MAX_COUNT_DRIFT = 0.10         # vs the previous build, matching Chicago
MIN_ZONES, MAX_ZONES = 20, 60  # 36 numbered zones plus Husky A/B, with slack
MAX_SIDE_MISMATCH_FRACTION = 0.05    # our compass side vs SDOT's own SIDE
MAX_PROBE_FAIL_FRACTION = 0.01       # the ribbon self-check in build()
MAX_CLIP_ERROR_P95_FEET = 60.0       # our clip vs SDOT's DISTANCE fields

# Field-verified coordinates, checked on the kerb. EMPTY ON PURPOSE.
#
# Chicago's equivalent is four points — two on W Concord Pl, one on N Haussen
# Ct, one offshore — and they have repeatedly caught real regressions that
# every statistical metric looked fine through. Seattle has none yet, so this
# pipeline has no defence against being confidently wrong in the same way.
#
# To add one: stand at the kerb, note which RPZ zone the sign says (or that
# there is no sign), and take the coordinate 25ft INTERIOR to the ribbon —
# never on its edge, which is unstable under ray casting. Then:
#
#     {"label": "1400 block of E Pine St, north side",
#      "lon": -122.3151, "lat": 47.6151, "expect_zone": "4"}
#
# `expect_zone: None` asserts the point is unrestricted.
#
# STILL EMPTY, AND THAT IS THE WEAKEST POINT OF THIS PIPELINE.
# Every other gate here checks the file against ITSELF — ring order, feature
# count, bounding box, internal consistency between our compass side and
# SDOT's. None of them can tell you the data is right about Seattle. Only a
# point someone has physically stood at can do that, and Chicago's four such
# points have already caught two bugs that every internal gate waved through.
# Adding even one Seattle point would change what a green build means here.
# See the PENDING FIELD CHECKS section of README.md.
GROUND_TRUTH = []


# ---------------------------------------------------------------- fetching

def _get(url, params, method="GET"):
    data = urllib.parse.urlencode(params).encode()
    if method == "GET":
        req = urllib.request.Request(url + "?" + data.decode())
    else:
        req = urllib.request.Request(url, data=data)
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.load(resp)


def fetch_layer(layer, where, out_fields="*", page=1000):
    feats, offset = [], 0
    while True:
        res = _get(f"{SERVICE}/{layer}/query", {
            "where": where, "outFields": out_fields, "outSR": 4326,
            "f": "geojson", "orderByFields": "OBJECTID",
            "resultOffset": offset, "resultRecordCount": page,
        }, method="POST")
        if "error" in res:
            raise RuntimeError(f"layer {layer}: {res['error']}")
        batch = res.get("features", [])
        feats.extend(batch)
        if len(batch) < page:
            return feats
        offset += page


def fetch_centerlines(segkeys):
    feats, keys = [], sorted(segkeys)
    for i in range(0, len(keys), 150):
        ids = ",".join(str(k) for k in keys[i:i + 150])
        feats += fetch_layer(0, f"SIDE='C' AND SEGKEY IN ({ids})",
                             "ELMNTKEY,SEGKEY,SIDE,DISTANCE,END_DISTANCE,UNITDESC")
    return feats


def load_or_fetch(raw_dir, name, fn, refresh):
    path = os.path.join(raw_dir, name + ".json")
    if refresh or not os.path.exists(path):
        feats = fn()
        with open(path, "w") as fh:
            json.dump(feats, fh)
    with open(path) as fh:
        return json.load(fh)


# ---------------------------------------------------------------- geometry
# Flat-earth feet frame, identical to the Swift side.

def to_ft(c):
    return (c[0] * FEET_PER_DEG_LON, c[1] * FEET_PER_DEG_LAT)


def to_deg(p):
    return [round(p[0] / FEET_PER_DEG_LON, 7), round(p[1] / FEET_PER_DEG_LAT, 7)]


def length(pts):
    return sum(math.dist(pts[i], pts[i + 1]) for i in range(len(pts) - 1))


def project(pts, q):
    """Arc-length position of q's projection onto polyline pts, plus the
    perpendicular distance and the index of the segment it landed on."""
    best = (float("inf"), 0.0, 0)
    run = 0.0
    for i in range(len(pts) - 1):
        a, b = pts[i], pts[i + 1]
        dx, dy = b[0] - a[0], b[1] - a[1]
        seg = math.hypot(dx, dy)
        if seg == 0:
            continue
        t = max(0.0, min(1.0, ((q[0] - a[0]) * dx + (q[1] - a[1]) * dy) / (seg * seg)))
        d = math.hypot(q[0] - (a[0] + t * dx), q[1] - (a[1] + t * dy))
        if d < best[0]:
            best = (d, run + t * seg, i)
        run += seg
    return best[1], best[0], best[2]


def clip(pts, s0, s1):
    """Sub-polyline of pts between arc lengths s0 < s1."""
    out, run = [], 0.0

    def lerp(a, b, t):
        return (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)

    for i in range(len(pts) - 1):
        a, b = pts[i], pts[i + 1]
        seg = math.dist(a, b)
        if seg == 0:
            continue
        lo, hi = run, run + seg
        if hi >= s0 and lo <= s1:
            if not out:
                out.append(lerp(a, b, (s0 - lo) / seg) if s0 > lo else a)
            out.append(lerp(a, b, (s1 - lo) / seg) if s1 < hi else b)
        run = hi
    # Drop consecutive duplicates — they give zero-length segments.
    dedup = [out[0]] if out else []
    for p in out[1:]:
        if math.dist(p, dedup[-1]) > 1e-6:
            dedup.append(p)
    return dedup


def left_normal(a, b):
    dx, dy = b[0] - a[0], b[1] - a[1]
    m = math.hypot(dx, dy)
    return (-dy / m, dx / m)


def offset_polyline(pts, sign, dist):
    """Per-vertex offset (mitred, capped at 2x) so the result has exactly as
    many points as pts — the app pairs them up index for index."""
    normals = [left_normal(pts[i], pts[i + 1]) for i in range(len(pts) - 1)]
    out = []
    for i, p in enumerate(pts):
        if i == 0:
            n = normals[0]
        elif i == len(pts) - 1:
            n = normals[-1]
        else:
            n1, n2 = normals[i - 1], normals[i]
            nx, ny = n1[0] + n2[0], n1[1] + n2[1]
            m = math.hypot(nx, ny)
            if m < 1e-9:
                n = n2
            else:
                nx, ny = nx / m, ny / m
                cos_half = nx * n2[0] + ny * n2[1]
                scale = min(1.0 / max(cos_half, 1e-9), 2.0)
                n = (nx * scale, ny * scale)
        out.append((p[0] + sign * dist * n[0], p[1] + sign * dist * n[1]))
    return out


def point_in_ring(q, ring):
    inside, j = False, len(ring) - 1
    for i in range(len(ring)):
        xi, yi = ring[i]
        xj, yj = ring[j]
        if (yi > q[1]) != (yj > q[1]):
            if q[0] < (xj - xi) * (q[1] - yi) / (yj - yi) + xi:
                inside = not inside
        j = i
    return inside


def orientation(a, b):
    deg = math.degrees(math.atan2(b[0] - a[0], b[1] - a[1])) % 180
    return deg


def compass_side(direction, toward):
    """Mirror of ZonePolygonStore.compassSide — keep the two in step."""
    bearing = math.degrees(math.atan2(direction[0], direction[1])) % 180
    tol = GRID_TOLERANCE_DEGREES
    if abs(bearing - 90) <= tol:
        return "N" if toward[1] > 0 else "S"
    if bearing <= tol or bearing >= 180 - tol:
        return "E" if toward[0] > 0 else "W"
    off = math.degrees(math.atan2(toward[0], toward[1])) % 360
    names = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
    return names[min(int(((off + 22.5) % 360) / 45), 7)]


def bend_degrees(pts):
    overall = orientation(pts[0], pts[-1])
    worst = 0.0
    for i in range(len(pts) - 1):
        if math.dist(pts[i], pts[i + 1]) == 0:
            continue
        raw = abs(orientation(pts[i], pts[i + 1]) - overall)
        worst = max(worst, min(raw, 180 - raw))
    return round(worst, 1)


def as_lines(geom):
    if geom is None:
        return []
    if geom["type"] == "LineString":
        return [geom["coordinates"]]
    if geom["type"] == "MultiLineString":
        return geom["coordinates"]
    return []


def polygon_rings(geom):
    if geom["type"] == "Polygon":
        return [geom["coordinates"]]
    if geom["type"] == "MultiPolygon":
        return geom["coordinates"]
    return []


def in_polygon_feature(q_deg, feat):
    for poly in polygon_rings(feat["geometry"]):
        if point_in_ring(q_deg, poly[0]) and not any(point_in_ring(q_deg, h) for h in poly[1:]):
            return True
    return False


# ---------------------------------------------------------------- formatting

def hours_label(p):
    """'8AM–8PM Mon–Sat' from SDOT's start/end fields, or None."""
    def h(s):
        # SDOT writes '08AM'; drop the leading zero.
        return s.lstrip("0") if s else s
    wkd = (p.get("START_TIME_WKD"), p.get("END_TIME_WKD"))
    sat = (p.get("START_TIME_SAT"), p.get("END_TIME_SAT"))
    sun = (p.get("START_TIME_SUN"), p.get("END_TIME_SUN"))
    if not all(wkd):
        return None
    days = "Mon–Fri"
    if sat == wkd and sun == wkd:
        days = "daily"
    elif sat == wkd:
        days = "Mon–Sat"
    return f"{h(wkd[0])}–{h(wkd[1])} {days}"


# ---------------------------------------------------------------- build

def content_fingerprint(features):
    """SHA-256 of the canonically-sorted features alone.

    Excludes the envelope on purpose, so this answers "is this the same zone
    data?" and not "is this the same file?". `data_version` lives in the
    envelope, so hashing the whole file here would make every build differ
    from every other and defeat the carry-forward below.
    """
    payload = json.dumps(features, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def resolve_data_version(features, baseline_path):
    """The date this DATA first appeared — not the date it was last rebuilt.

    Ported from Chicago's build_zones.py, including the reasoning, because
    the trap is identical. `data_version` is written into the GeoJSON, and
    the workflow decides whether to commit by comparing the output's hash
    against the committed file. A clock-derived stamp would therefore change
    the file's bytes on every monthly run and commit forever — the same
    failure canonical_sort exists to prevent, reintroduced through the
    envelope.

    So: identical features carry the baseline's version forward and the file
    stays byte-identical. Only genuinely changed data gets today's date.

    It also makes the stamp mean the right thing for the app's "Data as of"
    line: first-appearance, not last-rebuild, which would advance monthly
    while telling the user nothing had changed.
    """
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if not baseline_path or not os.path.exists(baseline_path):
        return today
    try:
        with open(baseline_path, encoding="utf-8") as fh:
            base = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return today
    previous = base.get("data_version")
    if not previous:
        return today
    # Both sides are sorted here rather than trusting the caller. `main`
    # happens to pass already-sorted features, so this is not a live bug —
    # but a caller that did not would get today's date for identical data and
    # publish for nothing, which is precisely the failure canonical_sort was
    # introduced to stop. Cheap to make the function correct on its own terms.
    if content_fingerprint(canonical_sort(base.get("features", []))) == \
            content_fingerprint(canonical_sort(features)):
        return str(previous)
    return today


def canonical_sort(features):
    """Order features by their content, not by the order SDOT served them.

    Defensive rather than known-necessary: `fetch_layer` already asks for
    `orderByFields=OBJECTID`, so the service order is probably stable. But the
    publishing decision is made by comparing the output's hash against the
    committed file, and in Chicago that comparison was broken by exactly this
    — identical data, different row order, different hash, a release cut for
    nothing. A content-derived key costs nothing and removes the whole class
    of problem.
    """
    def key(f):
        p = f["properties"]
        c = p["centerline"]
        return (str(p["row_id"]), p["zone"], p["side"],
                c[0][0], c[0][1], c[-1][0], c[-1][1], len(c))
    return sorted(features, key=key)


def ring_is_well_formed(feature):
    """The app recovers the offset line by slicing the ring, so its shape is
    load-bearing, not cosmetic. `loadGeoJSON` assumes exactly:

        ring == centerline + reversed(offset) + [centerline[0]]

    which makes len(ring) == 2 * len(centerline) + 1 and the ring's first
    len(centerline) points identical to the centerline. The module docstring
    says "Do not change it"; this is that instruction made enforceable.
    """
    centerline = feature["properties"]["centerline"]
    ring = feature["geometry"]["coordinates"][0]
    n = len(centerline)
    if len(ring) != 2 * n + 1:
        return False
    if ring[:n] != centerline:
        return False
    return ring[-1] == centerline[0]


def ring_area_sqft(feature):
    """Shoelace area in a local feet frame translated to the ring's first
    vertex. Doing this on raw lon/lat loses nearly all double precision to
    cancellation and reports healthy polygons as broken — a trap that has
    cost this project's Chicago side a diagnostic cycle more than once.
    """
    ring = feature["geometry"]["coordinates"][0]
    ox, oy = ring[0]
    pts = [((p[0] - ox) * FEET_PER_DEG_LON, (p[1] - oy) * FEET_PER_DEG_LAT)
           for p in ring]
    a = 0.0
    for i in range(len(pts) - 1):
        a += pts[i][0] * pts[i + 1][1] - pts[i + 1][0] * pts[i][1]
    return abs(a) / 2.0


def validate(features, metrics, baseline_count):
    """Return a list of failure strings. Empty means the build may be written.

    Each message says what tripped, what the number was, and what it implies —
    a gate that only says "failed" costs an hour to interpret at 07:37 UTC on
    the 3rd of the month.
    """
    fails = []
    n = len(features)

    # ---- structural: must be exactly true ---------------------------------
    if n < MIN_FEATURES:
        fails.append(
            f"only {n} features (floor {MIN_FEATURES}). The SDOT query likely "
            f"returned a partial or empty result; check for a changed layer "
            f"index or PARKING_CATEGORY value.")

    bad_ring = [f["properties"]["row_id"] for f in features
                if not ring_is_well_formed(f)]
    if bad_ring:
        fails.append(
            f"{len(bad_ring)} feature(s) have a ring the app cannot slice, "
            f"e.g. row_id {bad_ring[:5]}. ZonePolygonStore.loadGeoJSON "
            f"recovers the offset line as ring[len(centerline):-1]; if the "
            f"ring order changed, every drawn line in the app is wrong.")

    zero_area = [f["properties"]["row_id"] for f in features
                 if ring_area_sqft(f) < 1.0]
    if zero_area:
        fails.append(
            f"{len(zero_area)} zero-area polygon(s), e.g. row_id "
            f"{zero_area[:5]}. These can never contain a query point, so the "
            f"restrictions they represent would have no coverage at all. "
            f"Chicago shipped 231 of these once without noticing.")

    required = ("zone", "row_id", "side", "bend_degrees", "centerline")
    missing = [f["properties"].get("row_id") for f in features
               if any(k not in f["properties"] for k in required)]
    if missing:
        fails.append(
            f"{len(missing)} feature(s) missing a required property "
            f"{required}, e.g. row_id {missing[:5]}. ZonePolygonStore decodes "
            f"these non-optionally and the whole load fails, not just the row.")

    lon0, lat0, lon1, lat1 = SEATTLE_BBOX
    off_map = [f["properties"]["row_id"] for f in features
               if not all(lon0 <= x <= lon1 and lat0 <= y <= lat1
                          for x, y in f["properties"]["centerline"])]
    if off_map:
        fails.append(
            f"{len(off_map)} feature(s) fall outside Seattle, e.g. row_id "
            f"{off_map[:5]}. Almost always lon/lat axis order or a projection "
            f"that is not WGS84.")

    dupes = n - len({str(f["properties"]["row_id"]) for f in features})
    if dupes:
        fails.append(
            f"{dupes} duplicate row_id(s). ELMNTKEY is one blockface and is "
            f"assumed unique; duplicates mean the same kerb is counted twice.")

    # ---- statistical: generous ceilings, meant to catch a collapse --------
    zones = metrics["zone_count"]
    if not (MIN_ZONES <= zones <= MAX_ZONES):
        fails.append(
            f"{zones} distinct zones, outside {MIN_ZONES}-{MAX_ZONES}. Seattle "
            f"has 36 numbered RPZ zones plus Husky A/B; a number far from that "
            f"means zone assignment broke, not that the city re-zoned.")

    if n and metrics["side_mismatch"] / n > MAX_SIDE_MISMATCH_FRACTION:
        frac = metrics["side_mismatch"] / n
        fails.append(
            f"our compass side disagrees with SDOT's own SIDE field on "
            f"{metrics['side_mismatch']} of {n} ({frac:.1%}), ceiling "
            f"{MAX_SIDE_MISMATCH_FRACTION:.0%}. Both are derived from the same "
            f"geometry, so wholesale disagreement means the side computation "
            f"or the normal's sign is wrong — and side is what the app tells "
            f"the user.")

    if n and metrics["probe_fail"] / n > MAX_PROBE_FAIL_FRACTION:
        frac = metrics["probe_fail"] / n
        fails.append(
            f"the ribbon self-check failed on {metrics['probe_fail']} of {n} "
            f"({frac:.1%}), ceiling {MAX_PROBE_FAIL_FRACTION:.0%}. A 25ft step "
            f"toward the kerb must land inside the ribbon and a step away must "
            f"not; at this rate the app's own side probe will mis-answer too.")

    p95 = metrics.get("clip_error_p95")
    if p95 is not None and p95 > MAX_CLIP_ERROR_P95_FEET:
        fails.append(
            f"clipped stretches disagree with SDOT's DISTANCE fields by "
            f"{p95:.0f}ft at p95, ceiling {MAX_CLIP_ERROR_P95_FEET:.0f}ft. The "
            f"clip is derived independently by projecting the kerb line, so a "
            f"large gap means one of the two is misaligned and the ribbons may "
            f"cover the wrong part of the block.")

    if baseline_count:
        drift = abs(n - baseline_count) / baseline_count
        if drift > MAX_COUNT_DRIFT:
            fails.append(
                f"feature count moved {drift:.1%} ({baseline_count} -> {n}), "
                f"ceiling {MAX_COUNT_DRIFT:.0%}. Either SDOT changed something "
                f"substantial or the build broke; both deserve a human before "
                f"this is committed.")

    # ---- ground truth ------------------------------------------------------
    for g in GROUND_TRUTH:
        hit = None
        for f in features:
            if point_in_ring(to_ft((g["lon"], g["lat"])),
                             [to_ft(c) for c in f["geometry"]["coordinates"][0]]):
                hit = f["properties"]["zone"]
                break
        if hit != g["expect_zone"]:
            fails.append(
                f"GROUND TRUTH: {g['label']} resolved to {hit!r}, expected "
                f"{g['expect_zone']!r}. This point was checked in person. Do "
                f"not edit the expectation to make the build pass — that is "
                f"the suite doing its job.")

    return fails


def build(raw_dir, refresh):
    os.makedirs(raw_dir, exist_ok=True)
    signed = load_or_fetch(raw_dir, "signed_blockfaces", lambda: fetch_layer(
        0, "PARKING_CATEGORY='Restricted Parking Zone'"), refresh)
    centers = load_or_fetch(raw_dir, "centerlines", lambda: fetch_centerlines(
        {f["properties"]["SEGKEY"] for f in signed}), refresh)
    zone_polys = load_or_fetch(raw_dir, "zone_polygons", lambda: fetch_layer(4, "1=1"), refresh)
    husky_polys = load_or_fetch(raw_dir, "husky_polygons", lambda: fetch_layer(3, "1=1"), refresh)

    center_by_seg = {f["properties"]["SEGKEY"]: f for f in centers}
    stats = Counter()
    clip_error, side_mismatch, out = [], [], []
    signed_sides = defaultdict(set)

    for f in signed:
        p = f["properties"]
        stats["input"] += 1

        lines = [l for l in as_lines(f["geometry"]) if len(l) >= 2]
        if not lines:
            stats["skip: no geometry"] += 1
            continue
        if len(lines) > 1:
            stats["note: multilinestring, longest part used"] += 1
        kerb = [to_ft(c) for c in max(lines, key=lambda l: length([to_ft(c) for c in l]))]

        cfeat = center_by_seg.get(p["SEGKEY"])
        if cfeat is None or not as_lines(cfeat["geometry"]):
            stats["skip: no centerline for segment"] += 1
            continue
        cline = [to_ft(c) for c in as_lines(cfeat["geometry"])[0]]

        # Zone: the record's own, else the zone polygon its midpoint is in.
        zone = (p.get("RPZ_ZONE") or "").strip()
        if not zone:
            mid = to_deg(kerb[len(kerb) // 2])
            hits = [z["properties"]["RPZ_ZONE"] for z in zone_polys + husky_polys
                    if in_polygon_feature(mid, z)]
            if len(hits) == 1:
                zone = str(hits[0]).strip()
                stats["zone filled from polygon"] += 1
            else:
                stats[f"skip: zone missing, {len(hits)} polygon hits"] += 1
                continue

        # Clip the centerline to the stretch the kerb line actually covers,
        # by projecting the kerb's ends onto it. Cross-checked against SDOT's
        # own DISTANCE/END_DISTANCE below.
        s_a, _, _ = project(cline, kerb[0])
        s_b, _, _ = project(cline, kerb[-1])
        s0, s1 = min(s_a, s_b), max(s_a, s_b)
        if s1 - s0 < MIN_STRETCH_FEET:
            stats["skip: stretch under 5ft"] += 1
            continue
        center = clip(cline, s0, s1)
        if len(center) < 2:
            stats["skip: degenerate clip"] += 1
            continue

        c_end = cfeat["properties"].get("END_DISTANCE") or 0
        if c_end and p.get("END_DISTANCE") is not None:
            scale = length(cline) / c_end
            clip_error.append(max(abs(s0 - p["DISTANCE"] * scale),
                                  abs(s1 - p["END_DISTANCE"] * scale)))

        # Which flank: sign of the kerb midpoint against the centerline.
        km = kerb[len(kerb) // 2]
        _, _, seg_i = project(center, km)
        a, b = center[seg_i], center[seg_i + 1]
        cross = (b[0] - a[0]) * (km[1] - a[1]) - (b[1] - a[1]) * (km[0] - a[0])
        sign = 1.0 if cross > 0 else -1.0

        offset = offset_polyline(center, sign, BUFFER_WIDTH_FEET)
        ring_ft = center + list(reversed(offset)) + [center[0]]

        # Self-check mirroring the app's side probe: 25ft toward the kerb
        # from the middle of the stretch must land inside the ribbon, and
        # 25ft the other way must not.
        mid_i = (len(center) - 1) // 2
        ma, mb = center[mid_i], center[mid_i + 1]
        mp = ((ma[0] + mb[0]) / 2, (ma[1] + mb[1]) / 2)
        n = left_normal(ma, mb)
        near = (mp[0] + sign * SIDE_PROBE_FEET * n[0], mp[1] + sign * SIDE_PROBE_FEET * n[1])
        far = (mp[0] - sign * SIDE_PROBE_FEET * n[0], mp[1] - sign * SIDE_PROBE_FEET * n[1])
        if not point_in_ring(near, ring_ft) or point_in_ring(far, ring_ft):
            stats["warn: side probe self-check failed"] += 1

        direction = (center[-1][0] - center[0][0], center[-1][1] - center[0][1])
        nn = left_normal(center[0], center[-1])
        side = compass_side(direction, (sign * nn[0], sign * nn[1]))
        if side != p.get("SIDE"):
            side_mismatch.append((p.get("SIDE"), side))

        limit = p.get("PARKING_TIME_LIMIT")
        props = {
            "zone": zone,
            "row_id": p["ELMNTKEY"],
            "side": side,
            "bend_degrees": bend_degrees(center),
            "centerline": [to_deg(c) for c in center],
        }
        if limit and limit < CITYWIDE_LIMIT_MINUTES:
            props["time_limit_minutes"] = int(limit)
            hrs = hours_label(p)
            if hrs:
                props["limit_hours"] = hrs
        elif limit == CITYWIDE_LIMIT_MINUTES:
            stats["note: 72h citywide limit dropped"] += 1

        out.append({
            "type": "Feature",
            "properties": props,
            "geometry": {"type": "Polygon", "coordinates": [[to_deg(c) for c in ring_ft]]},
        })
        signed_sides[p["SEGKEY"]].add(sign)

    out = canonical_sort(out)

    # ------------------------------------------------------------ report
    #
    # NOTHING IS WRITTEN HERE. build() computes and reports; main() validates
    # and only then writes. The previous shape wrote the file at this point
    # and printed the health metrics afterwards, which meant every metric
    # below was a thing you could read about a file that had already been
    # published. Chicago learned this the hard way.
    print(f"built {len(out)} features")
    for k, v in sorted(stats.items()):
        print(f"  {k}: {v}")
    zones = Counter(f["properties"]["zone"] for f in out)
    print(f"  zones: {len(zones)} distinct")
    one_sided = sum(1 for s in signed_sides.values() if len(s) == 1)
    print(f"  segments signed on one flank only: {one_sided} of {len(signed_sides)} "
          f"({100 * one_sided / max(len(signed_sides), 1):.1f}%)")
    clip_p95 = None
    if clip_error:
        ce = sorted(clip_error)
        clip_p95 = ce[min(int(0.95 * len(ce)), len(ce) - 1)]
        print(f"  clip vs SDOT DISTANCE fields (ft): median {statistics.median(ce):.1f}, "
              f"p95 {clip_p95:.1f}, max {ce[-1]:.1f}")
    print(f"  side label disagrees with SDOT SIDE: {len(side_mismatch)} "
          f"{Counter(side_mismatch).most_common(8)}")
    bends = [f["properties"]["bend_degrees"] for f in out]
    print(f"  bend > {GRID_TOLERANCE_DEGREES:.0f}deg: {sum(b > GRID_TOLERANCE_DEGREES for b in bends)}, "
          f"> 45deg: {sum(b > 45 for b in bends)}")
    lims = Counter(f["properties"].get("time_limit_minutes") for f in out)
    print(f"  time limits: {dict(lims)}")
    print(f"  with hours: {sum('limit_hours' in f['properties'] for f in out)}")

    # Spacing to the nearest OTHER street, for the tap / address tolerances.
    # Measured from each centerline midpoint to centerlines that share no
    # endpoint with it (i.e. not the same street continuing or a cross street
    # meeting it).
    segs = list({tuple(map(tuple, f["properties"]["centerline"])) for f in out})
    segs_ft = [[to_ft(c) for c in s] for s in segs]
    cell = 600.0
    grid = defaultdict(list)
    for i, s in enumerate(segs_ft):
        for c in s:
            grid[(int(c[0] // cell), int(c[1] // cell))].append(i)
    gaps = []
    for i, s in enumerate(segs_ft):
        m = s[len(s) // 2] if len(s) > 2 else ((s[0][0] + s[-1][0]) / 2, (s[0][1] + s[-1][1]) / 2)
        cand = {j for dx in (-1, 0, 1) for dy in (-1, 0, 1)
                for j in grid[(int(m[0] // cell) + dx, int(m[1] // cell) + dy)]} - {i}
        best = float("inf")
        for j in cand:
            t = segs_ft[j]
            ends = (s[0], s[-1])
            if any(math.dist(e, u) < 40 for e in ends for u in (t[0], t[-1])):
                continue
            best = min(best, project(t, m)[1])
        if best < float("inf"):
            gaps.append(best)
    gaps.sort()
    if gaps:
        print(f"  nearest other signed street from a block midpoint (ft): "
              f"p1 {gaps[int(0.01 * len(gaps))]:.0f}, p5 {gaps[int(0.05 * len(gaps))]:.0f}, "
              f"median {statistics.median(gaps):.0f}; under 120ft: {sum(g < 120 for g in gaps)}")

    metrics = {
        "features": len(out),
        "zone_count": len(zones),
        "side_mismatch": len(side_mismatch),
        "probe_fail": stats["warn: side probe self-check failed"],
        "clip_error_p95": clip_p95,
        "one_sided_segments": one_sided,
        "segments": len(signed_sides),
        "bend_over_tolerance": sum(b > GRID_TOLERANCE_DEGREES for b in bends),
        "skips": {k: v for k, v in sorted(stats.items()) if k.startswith("skip")},
    }
    return out, metrics


def flag(name, default=None):
    """Read --name=value or --name value out of argv."""
    for i, a in enumerate(sys.argv):
        if a == name and i + 1 < len(sys.argv):
            return sys.argv[i + 1]
        if a.startswith(name + "="):
            return a.split("=", 1)[1]
    return default


def main():
    args = [a for a in sys.argv[1:]
            if not a.startswith("--") and a not in (flag("--baseline"), flag("--report-json"))]
    if len(args) != 2:
        sys.exit(__doc__)
    raw_dir, out_path = args

    baseline_path = flag("--baseline")
    baseline_count = 0
    if baseline_path and os.path.exists(baseline_path):
        with open(baseline_path) as fh:
            baseline_count = len(json.load(fh)["features"])
        print(f"baseline: {baseline_count} features in {baseline_path}")
    elif baseline_path:
        # Not fatal: the first run has nothing to compare against, and a
        # missing file must not be allowed to pass as "no drift".
        print(f"baseline: {baseline_path} not found, drift check skipped")

    features, metrics = build(raw_dir, "--refresh" in sys.argv)
    failures = validate(features, metrics, baseline_count)

    report_path = flag("--report-json")
    if report_path:
        with open(report_path, "w") as fh:
            json.dump({"metrics": metrics, "failures": failures,
                       "baseline_count": baseline_count}, fh, indent=2)
        print(f"report: {report_path}")

    if failures:
        print(f"\nVALIDATION FAILED ({len(failures)}), nothing written:")
        for f in failures:
            print(f"  - {f}")
        # Exit non-zero with NO output file touched. An existing good
        # {out_path} from a previous run is left exactly as it was, so a bad
        # fetch cannot degrade what is already published.
        sys.exit(1)

    # Write via a temporary file in the same directory and rename, so an
    # interrupted write cannot leave a truncated GeoJSON where a valid one was.
    # `data_version` is what lets the app know what it already has, which is
    # what lets the updater refuse to install something older. Keys ordered so
    # the stamp sits at the top where `head -c 200` shows it.
    data_version = resolve_data_version(features, baseline_path)
    tmp = out_path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump({"type": "FeatureCollection",
                   "data_version": data_version,
                   "features": features},
                  fh, separators=(",", ":"))
    os.replace(tmp, out_path)
    print(f"data_version: {data_version}")
    print(f"\nvalidation passed; wrote {len(features)} features to {out_path} "
          f"({os.path.getsize(out_path) / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
