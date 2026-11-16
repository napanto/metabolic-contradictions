"""Strict output schemas shared by extraction, classification and QA."""


def obj(properties):
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


def enum(values):
    return {"type": "string", "enum": values}


TEXT = {"type": "string"}
NULL_TEXT = {"type": ["string", "null"]}
CONTEXT = obj(
    {
        "population": NULL_TEXT,
        "region": NULL_TEXT,
        "design": NULL_TEXT,
        "n": {"type": ["integer", "null"]},
        "duration": NULL_TEXT,
        "dose": NULL_TEXT,
        "sample": NULL_TEXT,
    }
)
CLAIM = obj(
    {
        "arm": enum(["biomarker", "drug", "diet"]),
        "exposure": TEXT,
        "exposure_class": NULL_TEXT,
        "comparator": NULL_TEXT,
        "outcome": TEXT,
        "kind": enum(["level", "association", "effect", "risk"]),
        "sign": enum(["+", "-", "0"]),
        "significant": {"type": ["boolean", "null"]},
        "hedged": {"type": "boolean"},
        "context": CONTEXT,
        "evidence": {"type": "array", "items": {"type": "integer"}, "minItems": 1},
    }
)
EXTRACTION = obj({"claims": {"type": "array", "items": CLAIM}})
PAIR_LABELS = [
    "concordant",
    "contradictory",
    "apparent_contradiction",
    "not_replicated",
    "not_comparable",
]
FACTORS = [
    "population",
    "region",
    "disease_subtype",
    "design",
    "comparator",
    "molecule_class",
    "dose",
    "duration",
    "outcome_definition",
    "sample",
    "unknown",
    "none",
]
PAIR = obj({"label": enum(PAIR_LABELS), "factor": enum(FACTORS), "rationale": TEXT})
QA = obj(
    {
        "answer": TEXT,
        "evidence_for": {"type": "array", "items": obj({"pmid": TEXT, "statement": TEXT})},
        "evidence_against": {"type": "array", "items": obj({"pmid": TEXT, "statement": TEXT})},
        "conflict_present": {"type": "boolean"},
        "limitations": TEXT,
    }
)
