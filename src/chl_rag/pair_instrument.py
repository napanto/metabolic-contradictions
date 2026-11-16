# Vendored pure validator; no app, server, network or database imports.
# Source: metabolic-evidence commit 61c08a1e77824b39cd7d4f3f1f27ec7d5682fa20 (tag v2.1-layout-20261115) app/instrument.py
# Full source SHA256: a95aa23b3d01ef736c8ea64f20098f1bdaf1aa2ff6195333b55537dcd4dbd880
# Selected source bytes SHA256: 9b5c3a28bfe92bd809b67b2a03984d36d8346d02f055826f24f469e13bbda8d9
"""Versioned v2 instrument: human wording and authoritative answer-path validation.

The browser submits choices, never a category.  ``parse_answer`` derives the
legacy label/factor, retaining field diagnostics separately from the pair label.
"""

import json

INSTRUMENT_VERSION = "2.0"
FIELDS = ("result", "groups", "sample_size", "comparison", "analysis", "significance", "context")
# The order in which the checks are shown (Antonio, 15 November): what the result sentence
# states, then the methods in reading order. Codes and validation use FIELDS unchanged.
DISPLAY_ORDER = ("result", "comparison", "significance", "analysis", "groups", "sample_size", "context")
ANSWERS = ("yes", "no", "cannot_tell")
COMMON_PROBLEMS = (
    "wrong_value",
    "not_supported_by_abstract",
    "missing_from_extraction",
    "wrong_group_time",
)
RESULT_PROBLEMS = COMMON_PROBLEMS + (
    "wrong_direction",
    "wrong_exposure_outcome",
    "planned_as_observed",
)
FACTORS = (
    "population",
    "region",
    "disease_subtype",
    "design",
    "comparator",
    "molecule_class",
    "dose",
    "duration",
    "outcome_definition",
    "biological_sample",
    "statistical_analysis",
    "sample_size",
    "other",
    "missing_context",
    "cannot_determine",
)


def _object(value, required=(), optional=()):
    if not isinstance(value, dict) or not set(required) <= value.keys():
        raise ValueError("Incomplete answer path")
    if value.keys() - set(required) - set(optional):
        raise ValueError("Unexpected or obsolete answer branch")
    return value


def _choice(value, choices):
    if not isinstance(value, str) or value not in choices:
        raise ValueError("Invalid answer choice")
    return value


def _detail(value, required=False):
    if not isinstance(value, str) or len(value) > 200:
        raise ValueError("Invalid explanation")
    value = value.strip()
    if required and not value:
        raise ValueError("Explain the other difference")
    return value


def _factor(value, detail=""):
    _choice(value, FACTORS)
    if value in ("missing_context", "cannot_determine"):
        return "unknown", value
    if value in ("statistical_analysis", "sample_size"):
        return "other", value
    if value == "other":
        return "other", _detail(detail, required=True)
    if value == "biological_sample":
        # Clearer answer code; the exported legacy factor keeps the shared taxonomy name.
        if detail:
            raise ValueError("Explanation without an other difference")
        return "sample", ""
    if detail:
        raise ValueError("Explanation without an other difference")
    return value, ""


def validate_path(path, rationale=""):
    """Validate a complete path, reject stale branches, derive its terminal label.

    A material identified error takes precedence over uncertainty on the other
    side.  Supporting errors remain diagnostics and do not force extraction_error.
    """
    _object(path, ("q1",), ("q1_relevance", "q2", "q2_primary", "q3", "q4", "q5"))
    if not isinstance(rationale, str) or len(rationale) > 1500:
        raise ValueError("Invalid note")
    q1 = _object(path["q1"], ("a", "b"))
    has_error = False
    has_uncertainty = False
    problems = {"a": {}, "b": {}}
    canonical = {"q1": {"a": {}, "b": {}}}
    allowed = {"q1"}
    for side in ("a", "b"):
        rows = _object(q1[side], FIELDS)
        for field in FIELDS:
            row = _object(rows[field], ("answer", "problems"))
            answer = _choice(row["answer"], ANSWERS)
            choices = row["problems"]
            permitted = RESULT_PROBLEMS if field == "result" else COMMON_PROBLEMS
            if not isinstance(choices, list) or any(not isinstance(x, str) for x in choices):
                raise ValueError("Invalid field problems")
            if len(choices) != len(set(choices)) or any(x not in permitted for x in choices):
                raise ValueError("Invalid field problems")
            if (answer == "no") != bool(choices):
                raise ValueError("A No needs a problem; other answers cannot have problems")
            has_error |= answer == "no"
            has_uncertainty |= answer == "cannot_tell"
            clean = {"answer": answer, "problems": [x for x in permitted if x in choices]}
            canonical["q1"][side][field] = clean
            if answer != "yes":
                problems[side][field] = clean

    def finish(label, factor="none", detail="", relevance="not_needed"):
        if path.keys() - allowed:
            raise ValueError("Unexpected or obsolete answer branch")
        return {
            "instrument_version": INSTRUMENT_VERSION,
            "label": label,
            "factor": factor,
            "binary_inconsistent": label
            in ("not_replicated", "apparent_contradiction", "contradictory"),
            "rationale": rationale.strip(),
            "path": canonical,
            "field_problems": problems,
            "relevance": relevance,
            "other_detail": detail,
        }

    relevance = "not_needed"
    if has_error or has_uncertainty:
        allowed.add("q1_relevance")
        relevance = _choice(
            path.get("q1_relevance"), ("material", "not_material", "cannot_establish_comparison")
        )
        canonical["q1_relevance"] = relevance
        if relevance == "material":
            if not has_error:
                raise ValueError("An extraction error requires an identified problem")
            return finish("extraction_error", relevance=relevance)
        if relevance == "cannot_establish_comparison":
            return finish("not_comparable", "unknown", relevance=relevance)
    elif "q1_relevance" in path:
        raise ValueError("Relevance is only asked for a problem or uncertainty")

    allowed.add("q2")
    q2 = _object(path.get("q2"), ("exposure", "outcome", "comparison"))
    canonical["q2"] = {}
    failures = {}
    for field in ("exposure", "outcome", "comparison"):
        row = _object(q2[field], ("answer",), ("reason", "detail"))
        answer = _choice(row["answer"], ANSWERS)
        clean = {"answer": answer}
        if answer == "yes":
            if row.keys() - {"answer"}:
                raise ValueError("A matching check cannot have a reason")
        elif answer == "cannot_tell":
            if row.keys() - {"answer"}:
                raise ValueError("Uncertainty has no selected context difference")
            failures[field] = ("unknown", "")
        else:
            reason = row.get("reason")
            detail = _detail(row.get("detail", ""))
            if field == "exposure":
                _choice(reason, ("different_exposure", "molecule_class"))
                if detail:
                    raise ValueError("Unexpected exposure detail")
                failures[field] = (
                    ("other", "different_exposure")
                    if reason == "different_exposure"
                    else ("molecule_class", "")
                )
            elif field == "outcome":
                _choice(reason, ("outcome_definition",))
                if detail:
                    raise ValueError("Unexpected outcome detail")
                failures[field] = ("outcome_definition", "")
            else:
                failures[field] = _factor(reason, detail)
            clean["reason"] = reason
            if reason == "other":
                clean["detail"] = detail
            elif "detail" in row:
                raise ValueError("Unexpected detail")
        canonical["q2"][field] = clean
    if failures:
        allowed.add("q2_primary")
        primary = path.get("q2_primary")
        if primary is None and len(failures) == 1:
            primary = next(iter(failures))
        _choice(primary, failures)
        canonical["q2_primary"] = primary
        factor, detail = failures[primary]
        return finish("not_comparable", factor, detail, relevance)
    if "q2_primary" in path:
        raise ValueError("No failed comparison check to select")

    allowed.add("q3")
    q3 = _choice(path.get("q3"), ANSWERS)
    canonical["q3"] = q3
    if q3 == "yes":
        return finish("concordant", relevance=relevance)
    if q3 == "cannot_tell":
        return finish("not_comparable", "unknown", relevance=relevance)
    allowed.add("q4")
    q4 = _choice(path.get("q4"), ("effect_vs_no_effect", "opposite", "size_only", "insufficient"))
    canonical["q4"] = q4
    if q4 == "effect_vs_no_effect":
        return finish("not_replicated", relevance=relevance)
    if q4 == "size_only":
        return finish("concordant", relevance=relevance)
    if q4 == "insufficient":
        return finish("not_comparable", "unknown", relevance=relevance)
    allowed.add("q5")
    q5 = _object(path.get("q5"), ("answer",), ("factor", "detail"))
    answer = _choice(q5["answer"], ANSWERS)
    canonical["q5"] = {"answer": answer}
    if answer != "yes":
        if q5.keys() - {"answer"}:
            raise ValueError("A difference can only be selected after Yes")
        return finish(
            "contradictory" if answer == "no" else "not_comparable",
            "none" if answer == "no" else "unknown",
            relevance=relevance,
        )
    factor, detail = _factor(q5.get("factor"), _detail(q5.get("detail", "")))
    if "detail" in q5 and q5["factor"] != "other":
        raise ValueError("Unexpected detail")
    canonical["q5"]["factor"] = q5["factor"]
    if q5["factor"] == "other":
        canonical["q5"]["detail"] = detail
    return finish(
        "not_comparable" if factor == "unknown" else "apparent_contradiction",
        factor,
        detail,
        relevance,
    )


def parse_answer(form):
    raw = form.get("path", "")
    if not isinstance(raw, str) or len(raw) > 14000:
        raise ValueError("Invalid answer path")
    try:
        path = json.loads(raw)
    except (ValueError, TypeError, RecursionError) as error:
        raise ValueError("Invalid answer path") from error
    return validate_path(path, form.get("rationale", ""))
