"""Addendum 08C: choose the reasoning effort of the corpus classifier from the oracle reference.

Offline only; no model call. Students play no part: no export is read.

- Reference: the category on which Astra and Fable agree, with validity under addendum 08B
  (two_judge_final_08b.jsonl of each judge). A pair without such a reference is left out.
- Primary measure, per setting: reference pairs whose binary flag equals the reference flag
  (flagged = contradictory, apparent_contradiction or not_replicated); an invalid label is a
  mismatch. Secondary measure: category matches (reported, does not decide).
- Rule: in the order low, high, max, the lowest effort whose primary measure is at most one
  pair below the best.

    PYTHONPATH=src uv run python -m chl_rag.classifier_effort \
        --classification-root data/derived/study_pair_classification \
        --oracle-root annotation/oracles_two_judges \
        --output configs/corpus_classifier_effort.json --macros report/effort_macros.tex
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from . import human_analysis as h

ADDENDUM = "PROTOCOL_ADDENDUM_08C_CLASSIFIER_EFFORT.md"
ADDENDUM_SHA256 = "2e1cd95551e623287e6dfd85940b4819c10388bab06cb17b2eeceed2fe58381a"
ORDER = ("low", "high", "max")
MARGIN = 1
WORDS = {"low": "low", "high": "high", "max": "maximum"}
TRUE_FLAGS = h.TRUE_FLAGS
RULE = ("in the order low, high, max, the lowest effort whose primary measure is at most one pair "
        "below the best (addendum 08C)")


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def directory_sha256(directory):
    """Digest of the per-pair records of one setting: sorted (file name, file sha256)."""
    files = sorted(Path(directory).glob("v2-pair-*.json"))
    return _sha(canonical([[f.name, _sha(f.read_bytes())] for f in files]))


def oracle_reference(oracle_root, live):
    judges = {}
    for judge in h.JUDGES:
        labels, validity = h.load_oracle(Path(oracle_root) / judge / "max", judge, live, mode="08b")
        h._check(validity["validity_rule"].startswith("addendum 08B"), f"effort.{judge}.not_08b")
        judges[judge] = {k: v["label"] for k, v in labels.items()}
    astra, fable = judges["astra"], judges["fable"]
    reference = {k: astra[k] for k in sorted(set(astra) & set(fable)) if astra[k] == fable[k]}
    return reference, judges


def decide(settings, reference):
    """settings: {name: {item_id: six-way label of a valid judgment}}; a missing pair is invalid."""
    h._check(set(settings) == set(ORDER), "effort.settings")

    def flag(label):
        return label in TRUE_FLAGS

    primary = {name: sum(k in labels and flag(labels[k]) == flag(ref) for k, ref in reference.items())
               for name, labels in settings.items()}
    secondary = {name: sum(labels.get(k) == ref for k, ref in reference.items())
                 for name, labels in settings.items()}
    best = max(primary.values())
    chosen = next(name for name in ORDER if primary[name] >= best - MARGIN)
    return chosen, primary, secondary, best


def run(classification_root, oracle_root, *, binding_path=h.BINDING, package_dir=None):
    binding = h.load_binding(binding_path)
    live, _ = h.load_package(package_dir or binding["package"]["directory"], binding)
    settings, validity = {}, {}
    for name in ORDER:
        directory = Path(classification_root) / name
        h._check(directory.is_dir(), f"effort.{name}.missing_directory")
        settings[name], validity[name] = h.load_classification(directory, name, live)
        h._check(validity[name]["missing"] == 0, f"effort.{name}.incomplete")
    reference, judges = oracle_reference(oracle_root, live)
    h._check(bool(reference), "effort.no_reference")
    chosen, primary, secondary, best = decide(settings, reference)
    oracle_files = {f"oracle_{judge}_08b": _sha((Path(oracle_root) / judge / "max" / h.FINAL_08B).read_bytes())
                    for judge in h.JUDGES}
    decision = {
        "effort": chosen, "addendum": "08C", "addendum_sha256": ADDENDUM_SHA256,
        "reference_pairs": len(reference),
        "primary": {"measure": "reference pairs whose binary flag equals the reference flag; "
                               "an invalid label counts as a mismatch",
                    "counts": primary, "best": best, "threshold": best - MARGIN,
                    "invalid_labels": {name: validity[name]["system_failures"] for name in ORDER}},
        "secondary": {"measure": "reference pairs whose category matches (reported, does not decide)",
                      "counts": secondary},
        "rule": RULE,
        "reference": "category on which Astra and Fable agree, validity under addendum 08B",
        "students_used": False,
        "inputs_sha256": {**{f"classification_{name}": directory_sha256(Path(classification_root) / name)
                             for name in ORDER},
                          **oracle_files,
                          "package": binding["package"]["package_sha256"]}}
    decision["decision_sha256"] = _sha(canonical(decision))
    return decision, settings, judges


def agreement_with(labels, judge_labels):
    both = [k for k in labels if k in judge_labels]
    if not both:
        return None
    return sum((labels[k] in TRUE_FLAGS) == (judge_labels[k] in TRUE_FLAGS) for k in both) / len(both)


def macros(decision, settings, judges):
    pending = r"\pending{}"
    counts = decision["primary"]["counts"]
    m = {"HAEffortChosen": WORDS[decision["effort"]],
         "HAEffortReferencePairs": str(decision["reference_pairs"]),
         **{f"HAEffortMatch{name.capitalize()}": str(counts[name]) for name in ORDER},
         "HAMaxFlagged": str(sum(v in TRUE_FLAGS for v in settings["max"].values())),
         "HAMaxBinaryAgreeStudents": pending}  # students: only from the human analysis at the freeze
    for judge in h.JUDGES:
        value = agreement_with(settings["max"], judges[judge])
        m[f"HAMaxBinaryAgree{judge.capitalize()}"] = pending if value is None else f"{value:.2f}"
    return "".join("\\providecommand{\\" + k + "}{" + v + "}\n" for k, v in m.items())


def verify(decision):
    body = {k: v for k, v in decision.items() if k != "decision_sha256"}
    return decision.get("decision_sha256") == _sha(canonical(body))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--classification-root", default="data/derived/study_pair_classification")
    parser.add_argument("--oracle-root", default="annotation/oracles_two_judges")
    parser.add_argument("--binding", default=h.BINDING)
    parser.add_argument("--output", default="configs/corpus_classifier_effort.json")
    parser.add_argument("--macros", default="report/effort_macros.tex")
    args = parser.parse_args(argv)
    try:
        decision, settings, judges = run(args.classification_root, args.oracle_root, binding_path=args.binding)
    except h.AnalysisError as exc:
        print(json.dumps({"status": "validation_error", "field": exc.field}))
        raise SystemExit(2) from None
    Path(args.output).write_text(json.dumps(decision, indent=2, sort_keys=True, ensure_ascii=False) + "\n")
    Path(args.macros).parent.mkdir(parents=True, exist_ok=True)
    Path(args.macros).write_text(macros(decision, settings, judges))
    print(json.dumps({"effort": decision["effort"], "reference_pairs": decision["reference_pairs"],
                      "primary": decision["primary"]["counts"], "secondary": decision["secondary"]["counts"],
                      "invalid_labels": decision["primary"]["invalid_labels"]}))


if __name__ == "__main__":
    main()
