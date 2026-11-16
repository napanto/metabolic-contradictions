"""V2 source-grounded request construction and deterministic validation.

These functions do not call a model, read evaluation labels or modify v1 data.
A schema-valid output is not proof of semantic support.
"""
from __future__ import annotations

import json
import math
from copy import deepcopy
from pathlib import Path

from jsonschema import Draft202012Validator

from .annotation import public_article as v1_public_article
from .io import digest
from .claim_schema import (
    CLAIM,
    EXTRACTION,
    EXTRACTION_ITEM,
    MODEL_CLAIM,
    MODEL_EXTRACTION,
    PAIR_ITEM,
    VERSION,
    WIRE_MODEL_EXTRACTION,
)
from .source_text import model_source_view, quote_match_kind, sentences

PROMPT_PATH = Path("configs/extraction_prompt.json")


def source_payload(article):
    display = model_source_view(article)["source"]
    eligible = set(display["eligible_result_sentence_ids"])
    return {"pmid": str(article["pmid"]), "title": article.get("title", ""),
            "source_query_arms": list(article.get("arms", [])),
            "publication_types": list(article.get("publication_types", [])),
            "sentences": [{**row, "eligible": row["id"] in eligible} for row in display["sentences"]]}


def extraction_request(article, *, model=None, reasoning=None, max_output_tokens=None,
                       provider=None, prompt_path=PROMPT_PATH):
    config = json.loads(Path(prompt_path).read_text())
    if config.get("schema_version") != VERSION or config.get("revision") not in (1, 2, 3, 4):
        raise ValueError("Unexpected v2 prompt version/revision")
    payload = source_payload(article)
    messages = [{"role": "system", "content": config["system"]},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}]
    provenance = model_source_view(article)["provenance"]
    return {"messages": messages, "schema": deepcopy(WIRE_MODEL_EXTRACTION), "payload": payload,
            "source_provenance": provenance,
            "identity": digest({"pipeline_version": VERSION, "prompt": config,
                                "schema": WIRE_MODEL_EXTRACTION, "validation_schema": MODEL_EXTRACTION, "source": payload,
                                "source_provenance": provenance,
                                "model": model, "reasoning": reasoning,
                                "max_output_tokens": max_output_tokens, "provider": provider}),
            "prompt_sha256": digest(config), "schema_sha256": digest(WIRE_MODEL_EXTRACTION),
            "validation_schema_sha256": digest(MODEL_EXTRACTION)}


def _finite(value, path=""):
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"Nonfinite number at {path}")
    if isinstance(value, dict):
        for key, child in value.items():
            _finite(child, f"{path}.{key}")
    if isinstance(value, list):
        for index, child in enumerate(value):
            _finite(child, f"{path}[{index}]")


def _evidence_errors(value, source, path="", *, result=False):
    errors = []
    if isinstance(value, dict):
        if set(value) == {"sentence_id", "quote"}:
            row = source.get(value["sentence_id"])
            if row is None:
                errors.append({"path": path, "reason": "nonexistent_evidence"})
            elif quote_match_kind(value["quote"], row["text"]) == "missing":
                errors.append({"path": path, "reason": "nonliteral_evidence_quote"})
            elif result and not row["eligible"]:
                errors.append({"path": path, "reason": "ineligible_result_evidence"})
            return errors
        for key, child in value.items():
            errors.extend(_evidence_errors(child, source, f"{path}.{key}",
                                            result=result or (key == "result")))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            errors.extend(_evidence_errors(child, source, f"{path}[{index}]", result=result))
    return errors


def _quote_diagnostics(value, source, path=""):
    diagnostics = []
    if isinstance(value, dict):
        if set(value) == {"sentence_id", "quote"}:
            row = source.get(value["sentence_id"])
            if row and quote_match_kind(value["quote"], row["text"]) == "whitespace_only":
                diagnostics.append({"path": path, "sentence_id": value["sentence_id"],
                                    "match_kind": "whitespace_only", "raw_text_preserved": True})
        else:
            for key, child in value.items():
                diagnostics.extend(_quote_diagnostics(child, source, f"{path}.{key}"))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            diagnostics.extend(_quote_diagnostics(child, source, f"{path}[{index}]"))
    return diagnostics


def _structure_errors(claim):
    errors = []
    result = claim["result"]
    ci = result["confidence_interval"]
    if ci is not None and ci["lower"] > ci["upper"]:
        errors.append({"path": "result.confidence_interval", "reason": "reversed_interval_bounds"})
    # A declared provenance type cannot point to a nonexistent value. This does
    # not infer significance or decide whether the value targets the right test.
    for source in result["significance_basis"]:
        key = "significance_wording" if source == "explicit_wording" else source
        if not result[key]:
            errors.append({"path": "result.significance_basis", "reason": "missing_significance_basis_value"})
    comparison = claim["comparison"]
    if comparison["type"] == "within_group_change" and (
            comparison["reference_group"] is not None or comparison["comparator"] is not None):
        errors.append({"path": "comparison", "reason": "within_group_has_between_group_reference"})
    return errors


def claim_id(pmid, claim):
    content = {key: value for key, value in claim.items() if key != "claim_id"}
    return "v2-claim-" + digest({"schema_version": VERSION, "pmid": str(pmid), "claim": content})[:24]


def validate_extraction(article, output):
    """Validate model JSON; drop only deterministic claim failures/duplicates.

    Schema and study-provenance errors reject the whole response. Claims with
    invalid evidence or structurally inconsistent fields are retained in dropped
    diagnostics, never repaired. No cap truncates the number of emitted claims.
    """
    Draft202012Validator(MODEL_EXTRACTION).validate(output)
    _finite(output)
    source = {row["id"]: row for row in sentences(article)}
    study_errors = _evidence_errors(output["study"], source, "study")
    if study_errors:
        raise ValueError("Invalid study evidence: " + json.dumps(study_errors))
    accepted, dropped, emitted, seen = [], [], [], set()
    for index, claim in enumerate(output["claims"]):
        errors = _evidence_errors(claim, source, "claim") + _structure_errors(claim)
        identifier = claim_id(article["pmid"], claim)
        if identifier in seen:
            errors.append({"path": "claim", "reason": "duplicate"})
        seen.add(identifier)
        canonical_claim = {"claim_id": identifier, **deepcopy(claim)}
        emitted.append({"emission_id": f"v2-emission-{index + 1:06d}",
                        "claim": canonical_claim, "retained": not errors,
                        "drop_reasons": deepcopy(errors)})
        if errors:
            dropped.append({"raw_index": index, "claim": deepcopy(claim), "errors": errors})
        else:
            accepted.append(canonical_claim)
    extraction = {"schema_version": VERSION, "pmid": str(article["pmid"]),
                  "study": deepcopy(output["study"]), "claims": accepted}
    Draft202012Validator(EXTRACTION).validate(extraction)
    emitted_extraction = {**deepcopy(extraction), "claims": [deepcopy(row["claim"]) for row in emitted]}
    return {"extraction": extraction, "emitted_extraction": emitted_extraction,
            "emitted_claims": emitted, "raw_claim_count": len(output["claims"]),
            "retained_claim_count": len(accepted), "dropped": dropped,
            "source_sha256": digest(source_payload(article)),
            "source_provenance": model_source_view(article)["provenance"],
            "quote_match_diagnostics": _quote_diagnostics(output, source),
            "semantic_support_validated": False}


def validate_canonical(article, extraction):
    Draft202012Validator(EXTRACTION).validate(extraction)
    if extraction["pmid"] != str(article["pmid"]):
        raise ValueError("Extraction PMID differs from source")
    raw = {"study": deepcopy(extraction["study"]),
           "claims": [{k: v for k, v in c.items() if k != "claim_id"} for c in extraction["claims"]]}
    checked = validate_extraction(article, raw)
    if checked["dropped"] or checked["extraction"] != extraction:
        raise ValueError("Canonical extraction does not match validated source/claim IDs")
    return extraction


def flatten_extraction(extraction, *, source_query_arms=()):
    Draft202012Validator(EXTRACTION).validate(extraction)
    return [{"schema_version": VERSION, "pmid": extraction["pmid"],
             "source_query_arms": list(source_query_arms), "study": deepcopy(extraction["study"]),
             "claim": deepcopy(claim)} for claim in extraction["claims"]]


def public_article(article, study):
    return {**v1_public_article(article), "study": deepcopy(study)}


def public_claim(claim):
    Draft202012Validator(CLAIM).validate(claim)
    return deepcopy(claim)


def pair_item(item_id, arm, a_article, a_extraction, a_claim_id,
              b_article, b_extraction, b_claim_id):
    sides = []
    for article, extraction, identifier in [(a_article, a_extraction, a_claim_id),
                                            (b_article, b_extraction, b_claim_id)]:
        validate_canonical(article, extraction)
        matching = [c for c in extraction["claims"] if c["claim_id"] == identifier]
        if len(matching) != 1 or matching[0]["arm"] != arm:
            raise ValueError("Pair claim absent, duplicated or in a different semantic arm")
        sides.append({"article": public_article(article, extraction["study"]),
                      "claim": public_claim(matching[0])})
    if sides[0]["article"]["pmid"] == sides[1]["article"]["pmid"]:
        raise ValueError("A literature pair must have two different sources")
    item = {"item_id": item_id, "task": "pair", "arm": arm, "a": sides[0], "b": sides[1]}
    Draft202012Validator(PAIR_ITEM).validate(item)
    return item


def extraction_item(item_id, arm, article, extraction):
    validate_canonical(article, extraction)
    item = {"item_id": item_id, "task": "extraction", "arm": arm,
            "article": public_article(article, extraction["study"]),
            "claims": [public_claim(c) for c in extraction["claims"]]}
    Draft202012Validator(EXTRACTION_ITEM).validate(item)
    return item


def verify_request(article, extraction, *, prompt=None, prompt_path="configs/verification_prompt.json"):
    """One source-complete same-model verification call; caller handles budgets."""
    validate_canonical(article, extraction)
    identifiers = [c["claim_id"] for c in extraction["claims"]]
    identifier_schema = {"type": "string", **({"enum": identifiers} if identifiers else {})}
    schema = {"type": "object", "additionalProperties": False, "required": ["claims"],
              "properties": {"claims": {"type": "array", "items": {
                  "type": "object", "additionalProperties": False,
                  "required": ["claim_id", "support", "contested_fields", "evidence"],
                  "properties": {
                      "claim_id": identifier_schema,
                      "support": {"type": "string", "enum": ["supported", "unsupported", "unclear"]},
                      "contested_fields": {"type": "array", "items": {"type": "string", "enum": list(MODEL_CLAIM["properties"]) }},
                      "evidence": {"type": "array", "items": {"type": "integer", "minimum": 1}},
                  }}}}}
    if not identifiers:
        schema["properties"]["claims"]["maxItems"] = 0
    config = json.loads(Path(prompt_path).read_text())
    if config.get("schema_version") != VERSION or config.get("revision") not in (1, 2, 3, 4):
        raise ValueError("Unexpected v2 verification prompt version/revision")
    instructions = prompt or config["system"]
    payload = {"source": source_payload(article), "extraction": extraction}
    provenance = model_source_view(article)["provenance"]
    return {"messages": [{"role": "system", "content": instructions},
                         {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
            "schema": schema, "source_provenance": provenance,
            "identity": digest({"instructions": instructions, "payload": payload, "schema": schema,
                                "source_provenance": provenance})}


def apply_verification(article, extraction, verification):
    """Drop only unsupported; preserve original claims and every verification."""
    request = verify_request(article, extraction)
    Draft202012Validator(request["schema"]).validate(verification)
    expected = {c["claim_id"] for c in extraction["claims"]}
    by_id = {row["claim_id"]: row for row in verification["claims"]}
    if len(by_id) != len(verification["claims"]) or set(by_id) != expected:
        raise ValueError("Verification must cover every original claim once")
    source_ids = {s["id"] for s in sentences(article)}
    if any(set(row["evidence"]) - source_ids for row in by_id.values()):
        raise ValueError("Verification cites a nonexistent source sentence")
    retained = [deepcopy(c) for c in extraction["claims"] if by_id[c["claim_id"]]["support"] != "unsupported"]
    return {"extraction": {**deepcopy(extraction), "claims": retained},
            "original_extraction": deepcopy(extraction), "verification": deepcopy(verification),
            "discarded_claim_ids": [c["claim_id"] for c in extraction["claims"] if by_id[c["claim_id"]]["support"] == "unsupported"],
            "unclear_claim_ids": [c["claim_id"] for c in extraction["claims"] if by_id[c["claim_id"]]["support"] == "unclear"]}


def render_claim_en(claim, study):
    """Deterministic English display, with missing fields visible and no verdict.

    This is a presentation of extracted assertions to be checked, never a source
    verification. Raw fields/evidence stay alongside it in the public package.
    """
    Draft202012Validator(CLAIM).validate(claim)

    def missing(value):
        if value is None or value == []:
            return "Not extracted"
        if isinstance(value, list):
            return ", ".join(str(v) for v in value)
        return str(value)

    def group(value):
        if value is None:
            return "Not extracted"
        return "; ".join([f"Label: {missing(value['label'])}",
                          f"Population: {missing(value['population'])}",
                          f"Role: {value['role']}"])

    def count(value):
        if value is None:
            return "Not extracted"
        return (f"{missing(value['value'])} {value['unit']}; scope: {value['scope']}; "
                f"source wording: {value['reported_text']}")

    comparison, result, analysis, context = (claim[k] for k in ("comparison", "result", "analysis", "context"))
    index, reference = comparison["index_group"], comparison["reference_group"]
    direction = {"+": "higher", "-": "lower", "0": "zero", None: "not extracted"}[result["estimate_direction"]]
    kind = comparison["type"]
    if kind == "within_group_change":
        summary = (f"Within-group change in {claim['outcome']} for {claim['exposure']}. "
                   f"Group: {missing((index or {}).get('label'))}. "
                   f"From {missing(comparison['reference_time'])} to {missing(comparison['outcome_time'])}.")
    elif kind == "between_group":
        summary = (f"Between-group comparison of {claim['outcome']} for {claim['exposure']}. "
                   f"Index: {missing((index or {}).get('label'))}; "
                   f"reference: {missing((reference or {}).get('label'))}.")
    elif kind == "association":
        summary = f"Association between {claim['exposure']} and {claim['outcome']}."
    else:
        summary = f"Finding about {claim['exposure']} and {claim['outcome']}; comparison unclear."
    assertion = {"directional": "A directional finding", "explicit_no_difference": "An explicit no-difference conclusion",
                 "not_confirmed": "An explicit failure to confirm", "unclear": "An unclear conclusion"}[result["assertion"]]
    summary += f" {assertion} was extracted; estimate direction: {direction}."
    summary += (f" Finding type: {claim['kind']}. Exposure class: {missing(claim['exposure_class'])}. "
                f"Hedged wording: {'Yes' if result['hedged'] else 'No'}.")
    if comparison["orientation_text"]:
        summary += f" Orientation: {comparison['orientation_text']}."
    basis_labels = {"p_value": "p-value", "confidence_interval": "confidence interval",
                    "explicit_wording": "explicit wording"}
    significance = [f"Authors' significance: {result['authors_significance']}",
                    f"Authors' wording: {missing(result['significance_wording'])}",
                    f"Trend wording: {missing(result['authors_trend_wording'])}",
                    f"Reported statistical basis: {missing([basis_labels[x] for x in result['significance_basis']])}"]
    p_value = result["p_value"]
    significance.append("p-value: Not extracted" if p_value is None else
                        f"p-value: {p_value['operator']}{p_value['value']}; target: {p_value['target']}; wording: {p_value['reported_text']}")
    ci = result["confidence_interval"]
    significance.append("Confidence interval: Not extracted" if ci is None else
                        f"{ci['level']}% confidence interval: [{ci['lower']}, {ci['upper']}]; target: {ci['target']}; wording: {ci['reported_text']}")
    estimate = result["estimate"]
    significance.append("Estimate: Not extracted" if estimate is None else
                        f"Estimate: {estimate['measure']} {estimate['value']} {missing(estimate['unit'])}")
    rows = [
        {"field": "result", "text": summary, "evidence": deepcopy(result["evidence"])},
        {"field": "groups", "text": f"Index group: {group(index)}. Reference group: {group(reference)}. Subgroup: {missing((claim['subgroup'] or {}).get('description'))}.",
         "evidence": deepcopy((index or {}).get("evidence", []) + (reference or {}).get("evidence", []) + (claim["subgroup"] or {}).get("evidence", []))},
        {"field": "sample_size", "text": f"Study total: {count(study['total_n'])}. Index group: {count((index or {}).get('n'))}. Reference group: {count((reference or {}).get('n'))}.",
         "evidence": deepcopy((study["total_n"] or {}).get("evidence", []) + ((index or {}).get("n") or {}).get("evidence", []) + ((reference or {}).get("n") or {}).get("evidence", []))},
        {"field": "comparison", "text": f"Type: {kind}. Comparator: {missing(comparison['comparator'])}. Reference time: {missing(comparison['reference_time'])}. Outcome time: {missing(comparison['outcome_time'])}. Orientation: {missing(comparison['orientation_text'])}.",
         "evidence": deepcopy(result["evidence"])},
        {"field": "analysis", "text": f"Study design: {missing(study['design'])}. Stated study aims: {missing(study['aims_reported'])}. Analysis method: {missing(analysis['method'])}. Adjustment: {analysis['adjustment']}. Covariates: {missing(analysis['covariates'])}. Analysis population: {missing(analysis['analysis_population'])}.",
         "evidence": deepcopy(study["evidence"] + analysis["evidence"])},
        {"field": "significance", "text": ". ".join(significance), "evidence": deepcopy(result["evidence"])},
        {"field": "context", "text": ". ".join(f"{key.title()}: {missing(context[key])}" for key in ["region", "duration", "dose", "sample"]),
         "evidence": deepcopy(context["evidence"])},
    ]
    return {"renderer_version": "v2-en-3", "summary": summary, "rows": rows,
            "notice": "Check these extracted details against the abstract. Not extracted does not mean the abstract does not report it."}
