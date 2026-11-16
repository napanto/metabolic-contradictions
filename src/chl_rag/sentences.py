"""Stable source sentence IDs and conservative evidence eligibility.

Unstructured abstracts use explicit result/conclusion cues. All text remains
available as context, but only marked sentences can support extracted claims.
"""

import re

BOUNDARY = re.compile(r"(?<=[.!?])\s+(?=[A-Z(\d])")
RESULT = re.compile(
    r"\b(results?|conclu(?:de|ded|sions?)|we found|we observed|showed|revealed|"
    r"was associated|were associated|significantly|no significant|not associated|"
    r"confidence interval|hazard ratio|odds ratio|relative risk|risk ratio|"
    r"compared with|compared to|higher|lower|increased|decreased|reduced|elevated)\b|"
    r"\b[Pp]\s*[<=>]|\b(?:HR|OR|RR|CI|WMD|SMD)[\s(:=\[]*\d"
)
METHOD = re.compile(
    r"^(?:aim|objective|background|methods?|we (?:aimed|sought|designed|searched|recruited))\b",
    re.IGNORECASE,
)
INLINE = re.compile(
    r"\b(Background|Objective|Methods|Results|Conclusions|BACKGROUND|OBJECTIVE|METHODS|RESULTS|CONCLUSIONS)(?::\s*|\s+(?=[A-Z]))"
)


def split_text(text):
    # Keep common abbreviations intact without changing original sentence text.
    start = 0
    for match in BOUNDARY.finditer(text):
        preceding = text[max(0, match.start() - 12) : match.start()]
        if re.search(r"(?:\bet al|\be\.g|\bi\.e|\bvs|\bDr|\bFig|\bNo)\.$", preceding):
            continue
        value = text[start : match.start()].strip()
        if value:
            yield value
        start = match.end()
    value = text[start:].strip()
    if value:
        yield value


def sentences(article):
    sections = article.get("sections") or [
        {"text": article["abstract"], "label": "", "nlm_category": ""}
    ]
    rows = []
    for section_index, section in enumerate(sections):
        category = (section.get("nlm_category") or "").upper()
        label = (section.get("label") or "").upper()
        structured = category in {"BACKGROUND", "OBJECTIVE", "METHODS", "RESULTS", "CONCLUSIONS"}
        result_section = category in {"RESULTS", "CONCLUSIONS"} or bool(
            re.search(r"RESULT|CONCLU|FINDING", label)
        )
        text = section["text"]
        markers = list(INLINE.finditer(text)) if not structured and not label else []
        if len({m.group(1).upper() for m in markers}) >= 2:
            spans = [
                (
                    m.start(),
                    markers[i + 1].start() if i + 1 < len(markers) else len(text),
                    m.group(1).upper(),
                )
                for i, m in enumerate(markers)
            ]
            if markers[0].start():
                spans.insert(0, (0, markers[0].start(), "BACKGROUND"))
        else:
            spans = [(0, len(text), label or category or "UNSTRUCTURED")]
        for begin, end, span_label in spans:
            offset = begin
            is_result = result_section or bool(re.search(r"RESULT|CONCLU|FINDING", span_label))
            is_structured = structured or span_label in {
                "BACKGROUND",
                "OBJECTIVE",
                "METHODS",
                "RESULTS",
                "CONCLUSIONS",
            }
            for value in split_text(text[begin:end]):
                start = text.index(value, offset)
                offset = start + len(value)
                eligible = is_result or (
                    not is_structured and bool(RESULT.search(value)) and not METHOD.search(value)
                )
                rows.append(
                    {
                        "id": len(rows) + 1,
                        "text": value,
                        "section": span_label,
                        "section_index": section_index,
                        "start": start,
                        "end": offset,
                        "eligible": bool(eligible),
                        "eligibility_rule": "section"
                        if is_result
                        else "lexical"
                        if eligible
                        else "excluded",
                    }
                )
    return rows
