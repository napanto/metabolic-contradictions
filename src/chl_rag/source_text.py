"""Shared v2 source numbering and conservative result-evidence eligibility.

Preserves every original sentence/character and all context. Relative to the v1
rule, explicit background/objective/methods headings cannot become eligible just
because their text contains result-like verbs. V1 itself remains unchanged.
"""
from __future__ import annotations

import re
from copy import deepcopy

from .io import digest
from .sentences import sentences as v1_sentences

POLICY = {
    "version": "v2-source-4",
    "numbering": "unchanged v1 sentence numbering and literal text",
    "override": "explicit context-only section headings exclude result evidence",
    "context_sections": ["background", "aim", "aims", "objective", "objectives", "method", "methods",
                         "methodology", "study design", "materials and methods", "primary endpoint", "outcome measures"],
    "result_sections": ["result", "results", "conclusion", "conclusions", "finding", "findings"],
    "mixed_result_context_heading": "retain frozen base eligibility when a result/conclusion heading is also explicit",
    "other_sections": "retain existing source eligibility, including the frozen unstructured heuristic",
    "mutation": "never edit original text or strip context",
    "quote_matching": "exact substring first; otherwise collapse Unicode whitespace runs to one ASCII space in comparison only; preserve case and every non-whitespace code point; retain raw text",
    "model_display": "v2-model-source-view-1: collapse Unicode whitespace runs in displayed source sentences to ASCII space before calls; preserve raw source and all non-whitespace code points; bind both views",
}
POLICY_SHA256 = digest(POLICY)
RESULT_HEADING = re.compile(r"\b(?:RESULTS?|CONCLUSIONS?|FINDINGS?)\b", re.IGNORECASE)
CONTEXT_HEADING = re.compile(
    r"\b(?:BACKGROUND|AIMS?|OBJECTIVES?|METHODS?|METHODOLOGY)\b|"
    r"\bSTUDY\s+DESIGN\b|\bPRIMARY\s+ENDPOINTS?\b|\bOUTCOME\s+MEASURES?\b", re.IGNORECASE)


def quote_match_kind(quote, source):
    """Compare quoted text without editing either source or model response."""
    if not isinstance(quote, str) or not isinstance(source, str) or not quote.strip():
        return "missing"
    if quote in source:
        return "exact"
    if re.sub(r"\s+", " ", quote) in re.sub(r"\s+", " ", source):
        return "whitespace_only"
    return "missing"


def quote_matches(quote, source):
    return quote_match_kind(quote, source) != "missing"


def sentences(article):
    supplied = article.get("sentences")
    rows = deepcopy(supplied) if supplied else v1_sentences(article)
    ids = [row.get("id") for row in rows]
    if len(set(ids)) != len(ids) or any(type(value) is not int or value < 1 for value in ids):
        raise ValueError("Source sentence IDs must be unique positive integers")
    if any(not isinstance(row.get("text"), str) or not row["text"].strip() for row in rows):
        raise ValueError("Source sentences need nonempty original text")
    declared = article.get("eligible_result_sentence_ids")
    if declared is not None and (len(set(declared)) != len(declared) or not set(declared) <= set(ids)):
        raise ValueError("Unknown or duplicate eligible result sentence")
    fallback = None
    for row in rows:
        section = str(row.get("section", ""))
        result_heading = bool(RESULT_HEADING.search(section))
        context_heading = bool(CONTEXT_HEADING.search(section))
        if "eligible" in row:
            if type(row["eligible"]) is not bool:
                raise ValueError("Eligibility flags must be booleans")
            eligible = row["eligible"]
        elif declared is not None:
            eligible = row["id"] in declared
        elif result_heading:
            eligible = True
        elif context_heading:
            eligible = False
        else:
            if not article.get("abstract"):
                raise ValueError("Cannot derive frozen eligibility for unlabelled source sentences")
            if fallback is None:
                fallback_rows = v1_sentences(article)
                if [(r["id"], r["text"]) for r in fallback_rows] != [(r["id"], r["text"]) for r in rows]:
                    raise ValueError("Source sentence numbering differs from frozen source text")
                fallback = {r["id"]: r for r in fallback_rows}
            eligible = fallback[row["id"]]["eligible"]
        if context_heading and not result_heading:
            row["eligible"] = False
            row["eligibility_rule"] = "v2_explicit_context_section"
        else:
            row["eligible"] = eligible
            row.setdefault("eligibility_rule", "v2_preserved_base_eligibility")
        row["section"] = section
    return rows


def source_payload(article):
    """Shared whitelist for independent source-only inventories and auditing."""
    rows = sentences(article)
    publication_types = article.get("publication_types", [])
    if not isinstance(publication_types, list) or any(not isinstance(x, str) for x in publication_types):
        raise ValueError("Publication types must be the original public string list")
    return {"pmid": str(article["pmid"]), "title": article.get("title", ""),
            "publication_types": list(publication_types),
            "sentences": [{"id": row["id"], "text": row["text"], "section": row["section"]} for row in rows],
            "eligible_result_sentence_ids": sorted(row["id"] for row in rows if row["eligible"])}


def display_text(text):
    """Whitespace-only presentation transform; never alter raw stored sources."""
    return re.sub(r"\s+", " ", text)


def model_source_view(article):
    raw = source_payload(article)
    displayed = deepcopy(raw)
    changed = []
    for sentence in displayed["sentences"]:
        original = sentence["text"]
        sentence["text"] = display_text(original)
        if sentence["text"] != original:
            changed.append(sentence["id"])
    provenance = {"view_version": "v2-model-source-view-1", "policy_sha256": POLICY_SHA256,
                  "raw_source_sha256": digest(raw), "display_source_sha256": digest(displayed),
                  "changed_sentence_ids": changed, "raw_source_preserved": True,
                  "non_whitespace_codepoints_preserved": True}
    return {"source": displayed, "provenance": provenance}
