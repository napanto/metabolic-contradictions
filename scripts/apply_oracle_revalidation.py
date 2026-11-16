"""Apply addendum 08B to the saved two-judge outputs. Prints counts only.

    PYTHONPATH=src uv run python scripts/apply_oracle_revalidation.py --root annotation/oracles_two_judges

No model call and no new request. Existing run files are only read; the new files are
<root>/<judge>/max/two_judge_final_08b.jsonl and two_judge_final_08b_manifest.json.
Labels, categories and annotations are never printed.
"""
from __future__ import annotations

import argparse
import json

from chl_rag.oracle_revalidation import JUDGES, apply


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default="annotation/oracles_two_judges")
    parser.add_argument("--judge", action="append", choices=JUDGES)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    out = {}
    for judge in args.judge or JUDGES:
        _, manifest = apply(args.root, judge, write=not args.dry_run)
        out[judge] = {**manifest["counts"], "output_sha256": manifest["output_sha256"]}
    print(json.dumps(out, indent=1, sort_keys=True))


if __name__ == "__main__":
    main()
