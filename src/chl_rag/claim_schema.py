"""Strict v2 extraction and blind-package contracts; no inference or v1 mutation."""
from __future__ import annotations

from copy import deepcopy

from .prototype_schemas import enum, obj

VERSION = "2.0"
TEXT = {"type": "string"}
NONEMPTY = {"type": "string", "minLength": 1}
NULL_TEXT = {"type": ["string", "null"]}
ARMS = ["biomarker", "drug", "diet"]


def array(item, **kwargs):
    return {"type": "array", "items": item, **kwargs}


def nullable(schema):
    return {"anyOf": [schema, {"type": "null"}]}


def wire_schema(schema):
    """Remove only provider-unsupported uniqueItems; enforce it locally instead.

    The same transformation applies to every extraction configuration. It never
    changes the canonical data contract or the local output-validation rules.
    """
    if isinstance(schema, dict):
        return {key: wire_schema(value) for key, value in schema.items() if key != "uniqueItems"}
    if isinstance(schema, list):
        return [wire_schema(value) for value in schema]
    return schema


EVIDENCE = obj({"sentence_id": {"type": "integer", "minimum": 1}, "quote": NONEMPTY})
EVIDENCES = array(EVIDENCE)
COUNT = obj({
    "value": {"type": ["integer", "null"], "minimum": 0},
    "unit": enum(["participants", "studies", "events", "other", "not_reported"]),
    "scope": enum(["enrolled", "randomized", "analysed", "not_reported"]),
    "reported_text": NONEMPTY, "evidence": EVIDENCES,
})
GROUP = obj({
    "label": NULL_TEXT, "population": NULL_TEXT,
    "role": enum(["case", "control", "intervention", "reference", "cohort", "other", "unknown"]),
    "n": nullable(COUNT), "evidence": EVIDENCES,
})
STUDY = obj({"design": NULL_TEXT, "aims_reported": nullable(array(NONEMPTY)),
             "total_n": nullable(COUNT), "evidence": EVIDENCES})
COMPARISON = obj({
    "type": enum(["between_group", "within_group_change", "association", "unclear"]),
    "index_group": nullable(GROUP), "reference_group": nullable(GROUP),
    "comparator": NULL_TEXT, "reference_time": NULL_TEXT, "outcome_time": NULL_TEXT,
    "orientation_text": NULL_TEXT,
})
ANALYSIS = obj({
    "method": NULL_TEXT, "adjustment": enum(["adjusted", "unadjusted", "not_reported"]),
    "covariates": nullable(array(NONEMPTY)), "analysis_population": NULL_TEXT,
    "evidence": EVIDENCES,
})
RESULT = obj({
    "assertion": enum(["directional", "explicit_no_difference", "not_confirmed", "unclear"]),
    "estimate_direction": {"type": ["string", "null"], "enum": ["+", "-", "0", None]},
    "estimate": nullable(obj({"measure": NONEMPTY, "value": {"type": "number"}, "unit": NULL_TEXT})),
    "p_value": nullable(obj({"operator": enum(["=", "<", "<=", ">", ">="]),
                             "value": {"type": "number", "minimum": 0, "maximum": 1},
                             "target": NONEMPTY, "reported_text": NONEMPTY})),
    "confidence_interval": nullable(obj({
        "level": {"type": "number", "exclusiveMinimum": 0, "maximum": 100},
        "lower": {"type": "number"}, "upper": {"type": "number"},
        "target": NONEMPTY, "reported_text": NONEMPTY,
    })),
    "authors_significance": enum(["significant", "not_significant", "not_reported", "not_tested", "unclear"]),
    "significance_basis": array(enum(["p_value", "confidence_interval", "explicit_wording"]), uniqueItems=True),
    "significance_wording": NULL_TEXT, "authors_trend_wording": NULL_TEXT,
    "hedged": {"type": "boolean"}, "evidence": array(EVIDENCE, minItems=1),
})
MODEL_CLAIM = obj({
    "arm": enum(ARMS), "exposure": NONEMPTY, "exposure_class": NULL_TEXT,
    "outcome": NONEMPTY, "kind": enum(["level", "association", "effect", "risk"]),
    "comparison": COMPARISON,
    "subgroup": nullable(obj({"description": NONEMPTY, "evidence": EVIDENCES})),
    "analysis": ANALYSIS, "result": RESULT,
    "context": obj({"region": NULL_TEXT, "duration": NULL_TEXT, "dose": NULL_TEXT,
                    "sample": NULL_TEXT, "evidence": EVIDENCES}),
})
CLAIM = obj({"claim_id": {"type": "string", "pattern": "^v2-claim-[0-9a-f]{24}$"},
             **deepcopy(MODEL_CLAIM["properties"])})
MODEL_EXTRACTION = obj({"study": STUDY, "claims": array(MODEL_CLAIM)})
WIRE_MODEL_EXTRACTION = wire_schema(MODEL_EXTRACTION)
EXTRACTION = obj({"schema_version": {"const": VERSION, "type": "string"},
                  "pmid": {"type": "string", "pattern": "^[0-9]+$"},
                  "study": STUDY, "claims": array(CLAIM)})
ARTICLE = obj({
    "pmid": {"type": "string", "pattern": "^[0-9]+$"}, "title": TEXT, "abstract": TEXT,
    "year": {"type": ["integer", "null"]}, "publication_types": array(TEXT), "url": TEXT,
    "sentences": array(obj({"id": {"type": "integer", "minimum": 1},
                            "text": NONEMPTY, "section": TEXT})),
    "study": STUDY,
})
SIDE = obj({"article": ARTICLE, "claim": CLAIM})
PAIR_ITEM = obj({"item_id": {"type": "string", "pattern": "^v2-pair-[0-9]+$"},
                 "task": {"const": "pair", "type": "string"}, "arm": enum(ARMS),
                 "a": SIDE, "b": SIDE})
EXTRACTION_ITEM = obj({"item_id": {"type": "string", "pattern": "^v2-extraction-[0-9]+$"},
                       "task": {"const": "extraction", "type": "string"}, "arm": enum(ARMS),
                       "article": ARTICLE, "claims": array(CLAIM)})
PACKAGE_ITEM = {"oneOf": [PAIR_ITEM, EXTRACTION_ITEM]}

CLASSIFICATION_FIELDS = ["result", "groups", "sample_size", "comparison", "analysis", "statistical_evidence", "other_context"]
CLASSIFICATION = obj({
    "label": enum(["concordant", "contradictory", "apparent_contradiction", "not_replicated", "not_comparable"]),
    "factor": enum(["population", "region", "disease_subtype", "design", "comparator", "molecule_class",
                    "dose", "duration", "outcome_definition", "sample", "unknown", "none", "other"]),
    "rationale": NONEMPTY,
    "diagnostics": obj({
        "material_extraction_error": {"type": "boolean"},
        "error_sides": array(enum(["a", "b"]), uniqueItems=True),
        "error_fields": array(enum(CLASSIFICATION_FIELDS), uniqueItems=True),
        "uncertain_extraction": {"type": "boolean"},
        "comparison_sufficient": {"type": "boolean"},
        "source_evidence": array(obj({"side": enum(["a", "b"]), "sentence_id": {"type": "integer", "minimum": 1},
                                      "quote": NONEMPTY}), minItems=1),
        "notes": TEXT,
    }),
})
WIRE_CLASSIFICATION = wire_schema(CLASSIFICATION)
