"""V2 normalization, comparison-aware candidates and blind classifier payloads.

The canonical ClaimV2 is always preserved. Legacy normalization is reused only
for conservative entity keys; its flattened representation never reaches raters.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from copy import deepcopy

from .io import digest
from .entity_normalize import Normalizer, normal_form
from .entity_keys import compatible_key as entity_compatible_key
from .entity_keys import pubtator_baseline, sample_cross
from .extraction import pair_item
from .claim_schema import VERSION
from .source_text import display_text, model_source_view, quote_matches


def baseline_sign(claim):
    """Frozen sign-only adapter: explicit absence/nonconfirmation=0; unclear=NA."""
    result = claim["result"]
    if result["assertion"] in {"explicit_no_difference", "not_confirmed"}:
        return "0"
    if result["assertion"] == "directional" and result["estimate_direction"] in {"+", "-"}:
        return result["estimate_direction"]
    return None


def normalize_record(record, normalizer=None):
    normalizer = normalizer or Normalizer()
    claim = record["claim"]
    view = {"pmid": str(record["pmid"]), "claim_id": claim["claim_id"],
            "arm": claim["arm"], "exposure": claim["exposure"],
            "exposure_class": claim["exposure_class"], "outcome": claim["outcome"],
            "kind": claim["kind"], "comparator": claim["comparison"]["comparator"],
            "hedged": claim["result"]["hedged"], "sign": baseline_sign(claim)}
    normalized = normalizer.normalize(view)
    comparison_type = claim["comparison"]["type"]
    return {**deepcopy(record), **normalized, "schema_version": VERSION,
            "claim": deepcopy(claim), "comparison_type": comparison_type,
            "pair_key": normalized["pair_key"] + "|comparison:" + comparison_type,
            "sign_adapter": "v2-assertion-first-1",
            "comparison_identifiable": comparison_type != "unclear"}


def compatible_key(a, b, *, retrieval=False):
    if (a["comparison_type"] != b["comparison_type"]
            or not a["comparison_identifiable"] or not b["comparison_identifiable"]):
        return None
    key = entity_compatible_key(a, b, retrieval=retrieval)
    return key + "|comparison:" + a["comparison_type"] if key else None


def _differences(a, b):
    differences = []
    for path in ["comparison", "subgroup", "analysis", "context", "kind"]:
        left, right = a["claim"][path], b["claim"][path]
        if left != right:
            differences.append({"field": path, "a": deepcopy(left), "b": deepcopy(right)})
    return differences


def make_pair(a, b, key=None, *, origin="entity_key", score=None):
    key = key or compatible_key(a, b, retrieval=origin != "entity_key")
    if key is None:
        raise ValueError("Incompatible v2 comparison candidates")
    if a["claim_id"] > b["claim_id"]:
        a, b = b, a
    signs = {a["sign"], b["sign"]}
    pattern = ("unrepresented" if None in signs else "concordant" if len(signs) == 1
               else "effect_null" if "0" in signs else "opposite")
    return {"pipeline_version": VERSION,
            "pair_id": "v2-pair-source-" + digest(sorted([a["claim_id"], b["claim_id"]]))[:24],
            "claim_a": a["claim_id"], "claim_b": b["claim_id"],
            "pmid_a": a["pmid"], "pmid_b": b["pmid"], "arm": a["arm"],
            "pair_key": key, "comparison_type": a["comparison_type"],
            "sign_pattern": pattern, "origin": origin, "origins": [origin],
            "retrieval_score": score, "context_differences": _differences(a, b),
            "pubtator_baseline": pubtator_baseline(a, b)}


def generate_candidates(records, *, inconsistent_cap=30, concordant_cap=5,
                        retrieved_document_pairs=()):
    """Bounded per-key sampling plus separately supplied retrieval proposals.

    Retrieval pairs must be (PMID, PMID, score); the caller owns its frozen
    retrieval index. The same comparison/entity checks and final caps apply.
    """
    groups, by_doc = defaultdict(list), defaultdict(list)
    for row in records:
        by_doc[row["pmid"]].append(row)
        if row["comparison_identifiable"] and not row["hedged"]:
            groups[row["pair_key"]].append(row)
    proposed = {}
    populations = {}
    for key, rows in sorted(groups.items()):
        signs = defaultdict(list)
        for row in rows:
            signs[row["sign"]].append(row)
        for left, right in [("+", "-"), ("+", "0"), ("-", "0"),
                            ("+", "+"), ("-", "-"), ("0", "0"), (None, None)]:
            same = left == right
            chosen, count = sample_cross(signs[left], signs[right],
                                         concordant_cap if same else inconsistent_cap,
                                         ["v2", key, left, right], same=same)
            populations[digest([key, left, right])] = count
            for a, b in chosen:
                if compatible_key(a, b):
                    pair = make_pair(a, b)
                    proposed[pair["pair_id"]] = pair
    for left, right, score in retrieved_document_pairs:
        for a in by_doc.get(str(left), []):
            for b in by_doc.get(str(right), []):
                key = compatible_key(a, b, retrieval=True)
                if key is None:
                    continue
                pair = make_pair(a, b, key, origin="hybrid_retrieval", score=score)
                old = proposed.get(pair["pair_id"])
                if old:
                    old["origins"] = sorted(set(old["origins"] + pair["origins"]))
                    if old["retrieval_score"] is None or score > old["retrieval_score"]:
                        old["retrieval_score"] = score
                else:
                    proposed[pair["pair_id"]] = pair
    capped = defaultdict(list)
    for pair in proposed.values():
        capped[pair["pair_key"]].append(pair)
    result = []
    for key, rows in sorted(capped.items()):
        ranked = sorted(rows, key=lambda x: digest([42, "v2", x["pair_id"]]))
        inconsistent = [x for x in ranked if x["sign_pattern"] in {"opposite", "effect_null"}]
        diagnostic = [x for x in ranked if x["sign_pattern"] not in {"opposite", "effect_null"}]
        result.extend(inconsistent[:inconsistent_cap] + diagnostic[:concordant_cap])
    result.sort(key=lambda x: x["pair_id"])
    return result, {"pipeline_version": VERSION, "claims": len(records),
                    "candidate_pairs": len(result), "proposed_before_final_caps": len(proposed),
                    "sign_coverage": dict(Counter(str(r["sign"]) for r in records)),
                    "comparison_coverage": dict(Counter(r["comparison_type"] for r in records)),
                    "inconsistent_cap": inconsistent_cap, "concordant_cap": concordant_cap,
                    "per_sign_key_population": populations,
                    "normalization_policy": "No v2 entity equivalence expansion; preserve every canonical ClaimV2.",
                    "unrepresented_sign_policy": "Diagnostic only; unclear is never zero."}


def classifier_payload(pair, articles, extractions):
    """Full numbered abstracts plus rich v2 claims; no proposed sign/prediction."""
    item = pair_item("v2-pair-000000", pair["arm"], articles[pair["pmid_a"]],
                     extractions[pair["pmid_a"]], pair["claim_a"], articles[pair["pmid_b"]],
                     extractions[pair["pmid_b"]], pair["claim_b"])
    return {"pipeline_version": VERSION, "a": item["a"], "b": item["b"]}


def contrast_fingerprint(claim):
    """Audit identity, not an exact-match filter for contextual candidates."""
    comparison = claim["comparison"]
    return {"type": comparison["type"],
            "index": normal_form((comparison["index_group"] or {}).get("label")),
            "reference": normal_form((comparison["reference_group"] or {}).get("label")),
            "comparator": normal_form(comparison["comparator"]),
            "reference_time": normal_form(comparison["reference_time"]),
            "outcome_time": normal_form(comparison["outcome_time"]),
            "subgroup": deepcopy(claim["subgroup"]), "analysis": deepcopy(claim["analysis"])}


def classification_request(pair, articles, extractions, *, model=None, reasoning=None,
                           max_output_tokens=None, provider=None,
                           prompt_path="configs/classification_prompt.json"):
    """Build v2 metabolic classification input, distinct from frozen ManCon."""
    import json
    from pathlib import Path

    from .claim_schema import CLASSIFICATION, WIRE_CLASSIFICATION

    config = json.loads(Path(prompt_path).read_text())
    if config.get("pipeline_version") != VERSION or config.get("revision") not in (1, 2, 3, 4):
        raise ValueError("Unexpected v2 metabolic classifier prompt version/revision")
    payload = classifier_payload(pair, articles, extractions)
    provenance = {}
    for side, pmid in [('a', pair['pmid_a']), ('b', pair['pmid_b'])]:
        view = model_source_view(articles[pmid])
        provenance[side] = view['provenance']
        payload[side]['article']['sentences'] = view['source']['sentences']
        raw_abstract = payload[side]['article']['abstract']
        payload[side]['article']['abstract'] = display_text(raw_abstract)
        provenance[side]['raw_abstract_sha256'] = digest(raw_abstract)
        provenance[side]['display_abstract_sha256'] = digest(payload[side]['article']['abstract'])
    messages = [{"role": "system", "content": config["system"]},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}]
    return {"messages": messages, "schema": deepcopy(WIRE_CLASSIFICATION), "payload": payload,
            "source_provenance": provenance,
            "identity": digest({"prompt": config, "schema": WIRE_CLASSIFICATION,
                                "validation_schema": CLASSIFICATION, "payload": payload,
                                "source_provenance": provenance,
                                "model": model, "reasoning": reasoning,
                                "max_output_tokens": max_output_tokens, "provider": provider}),
            "prompt_sha256": digest(config), "schema_sha256": digest(WIRE_CLASSIFICATION),
            "validation_schema_sha256": digest(CLASSIFICATION)}


def validate_classification(payload, judgment):
    """Validate source citations and declared label/diagnostic consistency only."""
    from jsonschema import Draft202012Validator

    from .claim_schema import CLASSIFICATION

    Draft202012Validator(CLASSIFICATION).validate(judgment)
    diag = judgment["diagnostics"]
    for evidence in diag["source_evidence"]:
        source = {s["id"]: s["text"] for s in payload[evidence["side"]]["article"]["sentences"]}
        if (evidence["sentence_id"] not in source or not evidence["quote"].strip()
                or not quote_matches(evidence["quote"], source[evidence["sentence_id"]])):
            raise ValueError("Classifier evidence is not a literal quote of the specified source")
    if diag["material_extraction_error"]:
        if (judgment["label"] != "not_comparable" or judgment["factor"] != "none"
                or not diag["error_sides"] or not diag["error_fields"]):
            raise ValueError("Material extraction error needs not_comparable/none and explicit side/field diagnostics")
    elif diag["error_sides"] or diag["error_fields"]:
        # Nonmaterial defects can be documented here, so this is not an error.
        # The decisive distinction stays in material_extraction_error.
        pass
    if judgment["label"] != "not_comparable" and not diag["comparison_sufficient"]:
        raise ValueError("Comparable verdict requires sufficient source comparison")
    if judgment["label"] in {"contradictory", "concordant"} and judgment["factor"] != "none":
        raise ValueError("Direct contradiction/agreement does not have an explanatory factor")
    if judgment["label"] == "apparent_contradiction":
        if judgment["factor"] in {"none", "unknown"}:
            raise ValueError("Apparent contradiction needs an identified contextual factor")
        if {e["side"] for e in diag["source_evidence"]} != {"a", "b"}:
            raise ValueError("Apparent contradiction needs cited context from both sources")
    return deepcopy(judgment)
