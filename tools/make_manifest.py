#!/usr/bin/env python3
"""
make_manifest.py — write the small manifest the Seattle iOS app fetches to
decide whether newer zone data exists.

HOW THIS DIFFERS FROM CHICAGO'S, AND WHY
----------------------------------------
Chicago publishes its ~8.6 MB GeoJSON as a GitHub Release asset and commits
only the manifest, because adding 8.6 MB to git history on every rebuild is
a real cost and a Release asset is immutable.

Seattle's file is 1.6 MB. At that size committing it straight into this repo
is cheaper than the Release machinery is worth, and `raw.githubusercontent.com`
serves it with no API rate limit. So there is **no release and no release
tag** here: the committed file IS the published artefact, and `data_url`
points at it on the default branch.

The one thing that costs us: a `main` raw URL is a MUTABLE pointer. An app
that fetches the manifest and then the data could, in principle, catch a new
commit in between and get a file whose hash does not match the manifest it
just read. That is a window of seconds, a few times a year, and the app's
gate refuses on a hash mismatch and simply tries again at the next check —
so the failure is benign and self-correcting. Pinning the URL to a commit
SHA would close it, but the manifest is written before that commit exists.
Not worth a dated-file scheme to fix a benign race.

NO CLOCK FIELD IN THIS FILE
---------------------------
Chicago's manifest carries `built_at`. This one deliberately does not. The
workflow decides whether to commit by comparing what it built against what
is already committed, so a field that changes on every run would commit on
every run — the same trap `data_version`'s carry-forward and `canonical_sort`
both exist to avoid, re-entered through the manifest instead of the data.
Every field here is derived from the data. If you want to know when a run
happened, that is in the workflow's log and its build-report artifact.

`data_version` is read OUT OF the GeoJSON rather than taken from the clock,
so the file and the manifest can never disagree. See resolve_data_version in
build_seattle_zones.py for why the stamp is the date the data first appeared
rather than the date it was last rebuilt.

Standard library only, to match build_seattle_zones.py.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os

SCHEMA_VERSION = 1

# Bumping this is a breaking change for every installed app: the app checks
# for an exact match and refuses anything else, because guessing at a
# contract it does not know is how a silent updater installs something
# wrong. Add fields freely — the app ignores unknown ones — but do not
# rename or remove one without shipping an app update first.


def sha256_of(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data", required=True,
                    help="the built GeoJSON this manifest describes")
    ap.add_argument("--out", required=True, help="where to write manifest.json")
    ap.add_argument("--repo", default="iansiwatt/rpz-sea",
                    help="owner/name of the PUBLIC repo serving the data")
    ap.add_argument("--branch", default="main")
    ap.add_argument("--report", help="the build's report JSON, for health fields")
    args = ap.parse_args()

    with open(args.data, encoding="utf-8") as fh:
        payload = json.load(fh)
    features = payload.get("features", [])

    data_version = payload.get("data_version")
    if not data_version:
        # Not a fallback to today, on purpose. An unstamped file means the
        # pipeline did not run resolve_data_version, which means something is
        # wrong upstream; inventing a version here would hide it and hand the
        # app a number the data itself does not carry.
        raise SystemExit(
            f"{args.data} has no top-level data_version. The app compares "
            f"versions to decide what to install, so a manifest cannot "
            f"honestly describe an unstamped file. Rebuild with "
            f"build_seattle_zones.py."
        )

    filename = os.path.basename(args.data)
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "data_version": data_version,
        "data_url": (
            f"https://raw.githubusercontent.com/{args.repo}/"
            f"{args.branch}/{filename}"
        ),
        "filename": filename,
        "bytes": os.path.getsize(args.data),
        "sha256": sha256_of(args.data),
        "feature_count": len(features),
        "zone_count": len({f["properties"]["zone"] for f in features}),
    }

    if args.report and os.path.exists(args.report):
        with open(args.report, encoding="utf-8") as fh:
            report = json.load(fh)
        m = report.get("metrics", {})
        # Published for a human reading the repo, and for diagnosing a bad
        # build after the fact. The app deliberately does NOT act on these:
        # it re-runs its own checks against the parsed file rather than
        # trusting the publisher's report of them.
        manifest["checks"] = {
            "side_mismatch": m.get("side_mismatch"),
            "probe_fail": m.get("probe_fail"),
            "clip_error_p95_feet": m.get("clip_error_p95"),
            "one_sided_segments": m.get("one_sided_segments"),
            "segments": m.get("segments"),
        }

    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2, sort_keys=True)
        fh.write("\n")

    print(f"wrote {args.out}")
    for k in ("data_version", "feature_count", "zone_count", "bytes", "sha256"):
        print(f"  {k}: {manifest[k]}")
    print(f"  data_url: {manifest['data_url']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
