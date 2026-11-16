"""Check that no site reload outside the binding's segments changed the instrument export.

For every v2.1 site tag it recomputes sha256(docs/instrument-v2.json) at the tag and
compares it with the segment in force at that tag. It also records the tag's line in the
site deploy log. With --write it stores the check in the binding. Read-only on the site
repository and on the deploy log.

    python scripts/verify_instrument_timeline.py --site <site repo> \
        --deploy-log ../metabolic-evidence-deploy.md --binding configs/human_analysis_binding.json --write
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path

# Journal reload instants (gunicorn "Handling signal: hup", CET) relayed by the coordinator on
# 16 November 2026 for reloads that are not segment boundaries; 15 November times from the deploy log.
JOURNAL = {
    "v2.1-programmes-20261114": "2026-11-14T14:29:57+01:00",
    "v2.1-gutter-20261114": "2026-11-14T15:42:02+01:00",
    "v2.1-corrections-20261114": "2026-11-14T16:13:33+01:00",
    "v2.1-nocount-20261114": "2026-11-14T17:01:09+01:00",
    "v2.1-boxpos-20261114": "2026-11-14T17:08:11+01:00",
    "v2.1-note-20261114": "2026-11-14T18:06:01+01:00",
    "v2.1-block4-20261114": "2026-11-14T18:17:31+01:00",
    "v2.1-selfcorr-20261114": "2026-11-14T19:35:27+01:00",
}
DEPLOY_LOG_TIMES = {
    "v2.1-paytext-20261115": "2026-11-15T17:27:34+01:00",
    "v2.1-homeoffer-20261115": "2026-11-15T17:36:27+01:00",
    "v2.1-practice1-20261115": "2026-11-15T17:42:00+01:00",
    "v2.1-stale-20261115": "2026-11-15T17:50:01+01:00",
    "v2.1-onepair-20261115": "2026-11-15T18:12:43+01:00",
    "v2.1-fourth-20261115": "2026-11-15T18:57:08+01:00",
    "v2.1-retry-20261115": "2026-11-15T19:14:03+01:00",
}


def git(site, *args):
    return subprocess.run(["git", "-C", str(site), *args], check=True, capture_output=True).stdout


def verify(site, deploy_log, binding):
    segments = binding["instrument_timeline"]["segments"]
    by_tag = {segment["tag"]: segment for segment in segments}
    tags = git(site, "tag", "--list", "v2.1-*", "--sort=creatordate").decode().split()
    log = Path(deploy_log).read_text().splitlines()
    in_force, rows, ok = None, [], True
    for tag in tags:
        export = hashlib.sha256(git(site, "show", f"{tag}:docs/instrument-v2.json")).hexdigest()
        commit = git(site, "rev-parse", "--short", f"{tag}^{{commit}}").decode().strip()
        if tag in by_tag:
            in_force = by_tag[tag]
            ok &= export == in_force["instrument_export_sha256"]
            continue
        if in_force is None:
            continue  # tags before the first segment precede recruited collection
        line = next((text.strip() for text in log if f"`{tag}`" in text), None)
        equal = export == in_force["instrument_export_sha256"]
        ok &= equal and line is not None
        rows.append({"tag": tag, "commit": commit,
                     "reload": JOURNAL.get(tag) or DEPLOY_LOG_TIMES.get(tag),
                     "reload_source": "service journal" if tag in JOURNAL else
                     "site deploy log" if tag in DEPLOY_LOG_TIMES else "not recorded to the second",
                     "instrument_export_sha256": export, "segment_in_force": in_force["id"],
                     "equal_to_segment": equal, "deploy_log_line": line})
    return ok, rows


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--site", required=True, type=Path)
    parser.add_argument("--deploy-log", required=True, type=Path)
    parser.add_argument("--binding", required=True, type=Path)
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()
    binding = json.loads(args.binding.read_text())
    ok, rows = verify(args.site, args.deploy_log, binding)
    if args.write:
        binding["instrument_timeline"]["unchanged_instrument_reloads"] = {
            "check": "sha256(docs/instrument-v2.json) at every v2.1 site tag that is not a segment "
                     "boundary equals the export of the segment in force, and the tag has a line "
                     "in the site deploy log; scripts/verify_instrument_timeline.py",
            "passed": ok, "tags": rows}
        args.binding.write_text(json.dumps(binding, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps({"passed": ok, "tags_checked": len(rows)}))
    raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    main()
