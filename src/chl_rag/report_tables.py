"""Generate report/slide tables and a provenance-bearing results snapshot.

Absent results remain pending. LLM scores are read only from explicitly selected
test metric paths, never from smoke runs, development scores or obsolete prompts.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import unicodedata
from datetime import UTC, datetime
from pathlib import Path

from .io import file_sha, read_jsonl, write_json

MODELS = {
    "z-ai/glm-5.3-flash": "GLM-5.3 Flash",
    "deepseek/deepseek-v4.1-flash-1017": "DeepSeek-v4.1 Flash",
    "qwen/qwen3.8-flash": "Qwen3.8 Flash",
    "qwen/qwen3.6-35b-a3b": "Qwen3.6 35B A3B",
}
ARMS = {"biomarker": "Biomarkers", "drug": "Drugs", "diet": "Diet"}

GREEK_MATH = {
    "α": "alpha", "β": "beta", "γ": "gamma", "δ": "delta", "ε": "varepsilon",
    "ϵ": "epsilon", "ζ": "zeta", "η": "eta", "θ": "theta", "ϑ": "vartheta",
    "ι": "iota", "κ": "kappa", "ϰ": "varkappa", "λ": "lambda", "μ": "mu",
    "µ": "mu", "ν": "nu", "ξ": "xi", "π": "pi", "ϖ": "varpi", "ρ": "rho",
    "ϱ": "varrho", "σ": "sigma", "ς": "varsigma", "τ": "tau", "υ": "upsilon",
    "φ": "varphi", "ϕ": "phi", "χ": "chi", "ψ": "psi", "ω": "omega",
    "Γ": "Gamma", "Δ": "Delta", "Θ": "Theta", "Λ": "Lambda", "Ξ": "Xi",
    "Π": "Pi", "Σ": "Sigma", "Υ": "Upsilon", "Φ": "Phi", "Ψ": "Psi", "Ω": "Omega",
}
UNICODE_MATH = {
    "≤": r"\leq", "≥": r"\geq", "≠": r"\neq", "≈": r"\approx", "≃": r"\simeq",
    "≅": r"\cong", "≡": r"\equiv", "∼": r"\sim", "∝": r"\propto", "∞": r"\infty",
    "±": r"\pm", "∓": r"\mp", "×": r"\times", "÷": r"\div", "·": r"\cdot",
    "⋅": r"\cdot", "−": "-", "→": r"\rightarrow", "←": r"\leftarrow",
    "↔": r"\leftrightarrow", "⇒": r"\Rightarrow", "⇐": r"\Leftarrow",
    "⇔": r"\Leftrightarrow", "↑": r"\uparrow", "↓": r"\downarrow", "↦": r"\mapsto",
    "⟶": r"\longrightarrow", "⟷": r"\longleftrightarrow", "∈": r"\in", "∉": r"\notin",
    "∩": r"\cap", "∪": r"\cup", "⊂": r"\subset", "⊆": r"\subseteq",
    "∑": r"\sum", "∏": r"\prod", "√": r"\surd", "∂": r"\partial",
    "∇": r"\nabla", "∆": r"\Delta", "°": r"{}^{\circ}", "ℓ": r"\ell",
    "′": r"{}^{\prime}", "″": r"{}^{\prime\prime}", "✓": r"\checkmark", "✗": r"\times",
}
SUPERSCRIPTS = dict(zip("⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾", "0123456789+-=()", strict=True))
SUBSCRIPTS = dict(zip("₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎", "0123456789+-=()", strict=True))


def tex(value):
    """Escape untrusted prose and preserve common scientific Unicode in pdfLaTeX.

    Unknown non-Latin glyphs fail with a code point, instead of disappearing or
    being guessed. NFC combines accents without flattening scientific scripts.
    """
    substitutions = {"\\": r"\textbackslash{}", "&": r"\&", "%": r"\%", "$": r"\$",
                     "#": r"\#", "_": r"\_", "{": r"\{", "}": r"\}",
                     "~": r"\textasciitilde{}", "^": r"\textasciicircum{}",
                     "–": "--", "—": "---", "‐": "-", "‑": r"\mbox{-}",
                     "‘": "`", "’": "'", "“": "``", "”": "''", "…": r"\ldots{}",
                     "•": r"\textbullet{}", "†": r"\textdagger{}", "‡": r"\textdaggerdbl{}",
                     "‰": r"\textperthousand{}", "\u00a0": "~", "\u00ad": r"\-",
                     "\u200b": r"\allowbreak{}", "\ufeff": "",
                     "ﬁ": "fi", "ﬂ": "fl", "ﬀ": "ff", "ﬃ": "ffi", "ﬄ": "ffl",
                     "¼": r"\ensuremath{\tfrac{1}{4}}", "½": r"\ensuremath{\tfrac{1}{2}}",
                     "¾": r"\ensuremath{\tfrac{3}{4}}"}
    pieces = []
    for char in unicodedata.normalize("NFC", str(value)):
        if char in substitutions:
            pieces.append(substitutions[char])
        elif char in GREEK_MATH:
            pieces.append("\\ensuremath{\\" + GREEK_MATH[char] + "}")
        elif char in UNICODE_MATH:
            pieces.append(r"\ensuremath{" + UNICODE_MATH[char] + "}")
        elif char in SUPERSCRIPTS:
            pieces.append(r"\textsuperscript{" + SUPERSCRIPTS[char] + "}")
        elif char in SUBSCRIPTS:
            pieces.append(r"\textsubscript{" + SUBSCRIPTS[char] + "}")
        elif char.isspace():
            pieces.append("\n" if char == "\n" else " ")
        elif 32 <= ord(char) <= 0x024F and not unicodedata.category(char).startswith("C"):
            pieces.append(char)
        else:
            raise ValueError(f"Unsupported report glyph U+{ord(char):04X} ({unicodedata.name(char, 'unnamed')}); add an explicit meaning-preserving TeX mapping")
    return "".join(pieces)


def number(value, digits=None):
    if value is None:
        return r"\pending"
    return f"{value:,.{digits}f}" if digits is not None else f"{int(value):,}"


def interval(estimate, bounds=None, digits=3, *, n=None, complete=False):
    if estimate is None:
        if complete and n == 0:
            return r"\textit{not estimable}"
        return r"\pending"
    value = number(estimate, digits)
    if bounds and len(bounds) == 2:
        value += " [" + ", ".join(number(v, digits) for v in bounds) + "]"
        if bounds[0] == bounds[1]:
            value += r"\textsuperscript{d}"
    return value


def kappa_text(result, *, complete=False, paired_n=None):
    if result and result.get("n", 0) > 0 and result.get("kappa") is None:
        return r"\textit{undefined}"
    if complete and paired_n == 0:
        return r"\textit{not estimable}"
    return number((result or {}).get("kappa"), 3)


def amendment_reference_view(result):
    """Select the declared reference without presenting an unfinished pilot as results."""
    validity = result.get("reference_validity", {})
    oracle, human = validity.get("oracle", {}), validity.get("human", {})
    oracle_finished = bool(oracle.get("expected") and
        oracle.get("valid", 0) + len(oracle.get("invalid_ids", [])) == oracle["expected"])
    human_complete = bool(human.get("expected") and human.get("valid") == human["expected"])
    reliability = result.get("reliability", {})
    primary = result.get("primary_estimates", reliability.get("primary_estimates", "oracle_provisional"))
    if primary not in {"oracle", "oracle_provisional", "human_subset"}:
        raise ValueError("Unknown amendment04 primary reference")
    source = "human_subset" if primary == "human_subset" else "oracle"
    if source == "human_subset" and (not human_complete or not oracle_finished or
                                    reliability.get("status") != "oracle_unreliable"):
        raise ValueError("Human-primary report requires the completed reliability fallback")
    ready = oracle_finished and (source == "oracle" or human_complete)
    label = ("Human subset reference" if source == "human_subset" else
             "Judge reference" if primary == "oracle" else "PROVISIONAL judge reference")
    return {"source": source, "label": label, "ready": ready,
            "oracle_finished": oracle_finished, "human_complete": human_complete,
            "metrics": result.get(source, {}) if ready else {},
            "agreement_ready": oracle_finished and human_complete,
            "reliability": reliability.get("status", "pending_human_validation")}


def student_reporting_notice(config, result, *, config_sha256=None, plan=None):
    """Describe the pending plan; never activate student metrics through a draft."""
    if not plan and not config:
        return {}
    if plan and (plan.get("status") != "pending_configuration"
                 or plan.get("registered") is not False
                 or plan.get("metric_reference_amendment") != 4):
        raise ValueError("Human validation plan must remain explicitly unregistered")
    confirmed = (config.get("amendment") == 5 and config.get("status") == "registered"
                 and config.get("configuration_confirmed") is True)
    bound_result = (confirmed and bool(config_sha256) and result.get("amendment") == 5
                    and result.get("source_sha256", {}).get("amendment05_config") == config_sha256)
    if confirmed:
        return {"status": "pending_final_reporting_integration",
                "registered_config": True, "result_bound_to_config": bound_result,
                "metrics_activated": False,
                "pending": "final student-reference reporting integration",
                "text": "Student-reference reporting integration is pending; displayed estimates "
                        "still use amendment 04. No student metrics are activated here.",
                "short": "Student-reference integration pending; amendment 04 remains displayed."}
    return {"status": "pending_configuration_registration", "registered_config": False,
            "result_bound_to_config": False, "metrics_activated": False,
            "pending": "confirmation and registration of the planned student configuration",
            "text": "Amendment 05 remains an unregistered plan pending final site configuration: "
                    "the author retains six extraction abstracts (31 claims); paid students of "
                    "biomedical programmes would annotate the 40 pairs. The author is the fallback "
                    "if two valid student reads per pair are unavailable by 15 November 2026, "
                    "12:00 CET. No student data are reported; consensus would not establish clinical truth.",
            "short": "Planned: author, six abstracts; paid biomedical students, 40 pairs, with "
                     "author fallback. Amendment 05 is unregistered pending final configuration."}


def agreement_cells(result, *, ready):
    if not ready:
        return [r"\pending", r"\pending", r"\pending"]
    n = result.get("n", 0)
    raw = interval(result.get("agreement"), result.get("raw_agreement_interval", {}).get("ci95"),
                   n=n, complete=True)
    kappa = (interval(result["kappa"], result.get("kappa_interval", {}).get("ci95"))
             if result.get("kappa") is not None else kappa_text(result, complete=True, paired_n=n))
    return [number(n), raw, kappa]


def amendment_agreement_table(agreement, *, ready):
    fields = [("Pair: six-way", agreement.get("categories", {})),
              ("Pair: true flag", agreement.get("binary_true_flag", {}))]
    fields += [(field.title(), agreement.get("extraction_fields", {}).get(field, {}))
               for field in ["exposure", "outcome", "sign", "kind", "comparator", "context"]]
    fields.append(("Evidence support", agreement.get("extraction_evidence_support", {})))
    rows = [[name, *agreement_cells(stat, ready=ready)] for name, stat in fields]
    rendered = table("lrrr", ["Agreement", "$n$", "Raw [95\\% CI]", "$\\kappa$ [95\\% CI]"], rows)
    if not ready:
        return rendered, r"\pending{} Complete matched-reference validation awaits all human items and the oracle run."
    diagnostics = []
    for name, stat in fields:
        raw, kappa = stat.get("raw_agreement_interval", {}), stat.get("kappa_interval", {})
        diagnostics.append(name + ": " + number(raw.get("undefined_resamples")) + "/"
                           + number(kappa.get("undefined_resamples")))
    note = ("Undefined bootstrap resamples (raw/$\\kappa$): " + "; ".join(diagnostics)
            + ". Undefined resamples are excluded, not set to zero; "
            + r"\textsuperscript{d} marks equal interval endpoints. "
            + "Matched items: " + number(agreement.get("matched_items")) + "/"
            + number(agreement.get("expected_items")) + "; missing matches: "
            + number(len(agreement.get("missing_match_ids", []))) + ".")
    return rendered, note


def amendment_reliability_text(view):
    status = view["reliability"]
    if status == "oracle_unreliable":
        return ("Oracle--human binary kappa is below 0.40: judge estimates are unreliable; "
                "human-subset estimates are primary.")
    if status == "threshold_met":
        return ("Oracle--human binary kappa meets the prespecified 0.40 point-estimate threshold; "
                "judge-reference estimates remain primary, without establishing clinical truth.")
    if status == "inconclusive_undefined_kappa":
        return ("Validation is inconclusive: binary kappa is undefined, which is not a pass. "
                "Judge-reference estimates remain PROVISIONAL.")
    return ("Human validation is pending; judge-reference estimates are PROVISIONAL. "
            "Binary kappa below 0.40 will make human-subset estimates primary.")


def baseline_precision_text(result):
    if (result.get("status") in {"no_baseline_positive_pairs_in_sample", "not_estimable"}
            or result.get("precision_denominator") == 0):
        return r"\textit{not estimable}"
    return interval(result.get("precision"), result.get("wilson95"))


def observed_list_count(record, key):
    value = record.get(key)
    return len(value) if record.get("status") == "observed" and isinstance(value, list) else None


def coverage_text(record):
    covered, applicable = record.get("covered"), record.get("applicable")
    if covered is None or applicable is None:
        return r"\pending"
    if applicable == 0:
        return r"\textit{not applicable}"
    return number(covered) + "/" + number(applicable)


def fraction_text(successes, total, *, complete=False, percent=False):
    if successes is None or total is None:
        return r"\pending"
    if total == 0:
        return r"\textit{not estimable}" if complete else r"\pending"
    result = successes / total
    return number(100 * result, 2) + r"\%" if percent else number(result, 3)


def extraction_drop_counts(summary):
    """Do not mislabel duplicate removal as failure of the sentence check."""
    if "evidence_check_dropped" in summary:
        return summary["evidence_check_dropped"], summary.get("duplicates_removed"), "separate_summary_fields"
    if summary.get("legacy_summary") is True and isinstance(summary.get("drop_reasons"), dict):
        reasons = summary["drop_reasons"]
        evidence = reasons.get("nonexistent_evidence", 0) + reasons.get("ineligible_evidence_section", 0)
        return evidence, reasons.get("duplicate", 0), "explicit_legacy_reason_breakdown"
    # An older aggregate `dropped` field is ambiguous, even when its total is known.
    return None, summary.get("duplicates_removed"), "unavailable_separation"


def control_review_snippets(review):
    """Render the source audit without promoting it to human evaluation.

    Partial extras can supply observed examples, but the excluded main/control
    overlaps stay explicit. Example text and PMIDs must refer to reviewed pairs.
    """
    pending = (r"\pending{} Prediction-aware LLM-assisted purposive source audit "
               "is unavailable; human-confirmed errors remain separate.")
    examples = review.get("report_examples", [])
    status = review.get("status")
    if status not in {"partial_extra_controls_only", "complete"} or len(examples) < 2:
        return pending, r"\pending{} Model-assisted source-audit examples."
    if review.get("is_human_gold") is not False or review.get("blinded") is not False:
        raise ValueError("Control source review must explicitly remain non-gold and prediction-aware")
    scope = review.get("scope", {})
    reviewed = {p["pair_id"]: p for p in review.get("pair_reviews", [])}
    count = scope.get("flagged_pairs_reviewed", scope.get("flagged_extra_pairs_reviewed"))
    if count is not None and count != len(reviewed):
        raise ValueError("Control review count differs from its reviewed pair IDs")
    selected = examples[:3]
    for example in selected:
        pair = reviewed.get(example.get("pair_id"))
        if not pair or not example.get("finding") or not example.get("pmids"):
            raise ValueError("Report example must identify a reviewed pair, finding and PMIDs")
        if not set(map(str, example["pmids"])) <= set(map(str, pair.get("pmids", []))):
            raise ValueError("Report example PMID is absent from the reviewed pair")
    text = ("A prediction-aware, LLM-assisted purposive audit reviewed " + number(count)
            + (" flagged extra-control pairs" if status == "partial_extra_controls_only"
               else " flagged control pairs")
            + " involving " + number(scope.get("distinct_flagged_claims_reviewed", scope.get("distinct_claims_reviewed")))
            + " extracted claims. This is not human gold or a precision estimate. ")
    if status == "partial_extra_controls_only":
        text += ("The scope is partial: "
                 + number(scope.get("main_candidate_control_overlaps_not_reviewed"))
                 + " main/control overlaps are excluded. ")
    elif scope.get("all_targeted_flags_reviewed"):
        text += ("All targeted flags were reviewed, plus " + number(scope.get("nonflagged_pairs_reviewed"))
                 + " nonflagged pairs; other nonflags were not adjudicated. ")
    for example in selected:
        text += (tex(example["finding"]).rstrip(".") + " ("
                 + ("PMID " if len(example["pmids"]) == 1 else "PMIDs ")
                 + ", ".join(tex(p) for p in example["pmids"]) + "). ")
    text += ("Literal sentence provenance therefore does not establish semantic support, "
             "and valid output structure does not ensure a grounded explanation. Reused "
             "claims make pair failures dependent. These observed examples motivate "
             "source verification; no aggregate error rate or post-test correction is "
             "inferred. Human-confirmed error results remain separate.")
    first = selected[0]
    slide = (r"\textbf{Observed, model-assisted:} " + tex(first["finding"]).rstrip(".")
             + " (PMID" + ("s " if len(first["pmids"]) > 1 else " ")
             + ", ".join(tex(p) for p in first["pmids"]) + ").")
    return text, slide


def human_error_text(metabolic, *, human_complete):
    """Keep unfinished human annotations and model-assisted audits out of errors."""
    if not human_complete:
        return (r"\pending{} Human-confirmed error counts and source-backed examples "
                "await completed annotation.")
    errors = metabolic.get("errors", {})
    counts = errors.get("counts")
    if not isinstance(counts, dict):
        return r"\pending{} Completed human labels await the error-count aggregation."
    if any(isinstance(n, bool) or not isinstance(n, int) or n < 0 for n in counts.values()):
        raise ValueError("Human error counts must be nonnegative integer counts")
    pair_n = metabolic.get("pairs", {}).get("overall", {}).get("sample_precision", {}).get("n")
    if pair_n == 0:
        return "No flagged pairs were human-annotated; false-positive counts are not estimable."
    total = sum(counts.values())
    if not total:
        return ("Human-confirmed false-positive flags in the annotated sample: "
                + number(total) + ". This observed zero does not establish absence "
                "of errors outside the sample.")
    names = {"extraction_error": "extraction error", "not_comparable": "not comparable",
             "concordant": "concordant"}
    text = ("Human-confirmed false-positive flags in the annotated sample: " + number(total)
            + " (" + "; ".join(tex(names.get(k, k.replace("_", " "))) + ": " + number(v)
                                 for k, v in sorted(counts.items())) + "). "
            + "Counts use human verdict categories. ")
    for example in errors.get("examples", [])[:3]:
        evidence = example.get("evidence") or {}
        pmids = list(dict.fromkeys(str(pmid) for side in ["a", "b"]
                    if (pmid := evidence.get(side, {}).get("article", {}).get("pmid")) is not None))
        source = (("PMID " if len(pmids) == 1 else "PMIDs ") + ", ".join(tex(p) for p in pmids)
                  if pmids else "source PMIDs unavailable")
        category = example.get("category", "error")
        text += (r"\textbf{" + tex(names.get(category, category.replace("_", " ")))
                 + "}: " + tex(example.get("rationale") or "Rationale unavailable")
                 + " (" + source + "). ")
    if not errors.get("examples"):
        text += "Source-backed examples are unavailable in the current evaluation artifact."
    return text.rstrip()


def fixed_control_review_text(review):
    fixed = review.get("fixed_positive_controls", {})
    gap = fixed.get("normalization_gap", {})
    if fixed.get("status") not in {"partial_pending_main_overlaps", "complete"} or not gap:
        return r"\pending{} Fixed-positive-control source and normalization review."
    text = ("Fixed-source review found " + number(gap.get("raw_relevant_extracted_claims"))
            + " extracted OBSERVE-4D amputation claims (PMID " + tex(gap.get("pmid", "unavailable"))
            + "), but " + number(gap.get("strict_linked_claims"))
            + " entered the canonical amputation control frame. Their unresolved outcome key "
            "caused a normalization/linkage failure, not complete extraction omission; "
            "source-field direction/significance errors were also observed. Repair is deferred "
            "to a future version. ")
    if fixed["status"] == "partial_pending_main_overlaps":
        pending = sum(c.get("pending_main_overlap_count", 0) for c in fixed.get("by_case", {}).values())
        text += number(pending) + " fixed-control main-overlap verdicts remain outside this qualitative review."
    return text.rstrip()


def network_concentration_text(network):
    if network.get("status") != "complete":
        return r"\pending{} Incident-flagged-edge concentration awaits the completed network."
    entries = sorted(network.get("conflict_concentration", []),
                     key=lambda row: (-row["incident_flagged_edges"], row["label"]))[:3]
    if not entries:
        return "No incident-flagged-edge concentration entries are available in the completed network."
    values = [tex(row["label"]) + " (" + number(row["incident_flagged_edges"]) + ")"
              for row in entries]
    return ("The largest incident-flagged-edge counts occur at " + ", ".join(values)
            + "; this describes query and candidate coverage, not biological importance.")


def qa_review_text(qa, review):
    """Quote original model wording with a separate source audit, never repair it."""
    if review.get("status") != "complete" or not review.get("report_examples"):
        return r"\pending{} Audited illustrative QA excerpts await source review; full outputs remain in the results."
    examples = {e.get("case"): e for e in qa.get("examples", [])}
    if set(review.get("citation_context_checks", {})) != set(examples):
        raise ValueError("QA source review must cover every current answer")
    for case, checks in review.get("citation_context_checks", {}).items():
        example = examples.get(case)
        if not example:
            raise ValueError("QA source review references a missing answer")
        raw_answer = example.get("raw_answer", example.get("answer", ""))
        evidence = json.dumps({k: example.get(k, []) for k in ["evidence_for", "evidence_against"]},
                              sort_keys=True, ensure_ascii=False)
        for value, key in [(raw_answer, "answer_sha256"), (evidence, "structured_evidence_sha256"),
                           (example.get("raw_limitations", example.get("limitations", "")), "limitations_sha256")]:
            if hashlib.sha256(value.encode()).hexdigest() != checks.get(key):
                raise ValueError(f"QA source review does not match preserved raw material: {case}/{key}")
    paragraphs = []
    for item in review["report_examples"][:3]:
        example = examples[item["case"]]
        if item["excerpt"] not in example.get("raw_answer", example.get("answer", "")):
            raise ValueError("QA report excerpt is not an exact substring of the raw answer")
        paragraphs.append(r"\textbf{" + tex(item["case"].title()) + " (model excerpt).} ``"
                          + tex(item["excerpt"]) + "'' " + r"\textit{Source audit:} "
                          + tex(item["review_note"]) + " (PMIDs "
                          + ", ".join(tex(p) for p in item["pmids"]) + ").")
    return ("These original-output excerpts have prediction-aware LLM-assisted source notes, "
            "not human validation. Citation identity alone does not establish factual correctness.\n\n"
            + "\n\n".join(paragraphs)
            + "\n\nFull answers, structured evidence and review are retained in the versioned results.")


def table(columns, headings, rows):
    return ("\\begin{tabular}{" + columns + "}\n\\toprule\n" +
            " & ".join(headings) + " \\\\\n\\midrule\n" +
            "\n".join(" & ".join(row) + " \\\\" for row in rows) +
            "\n\\bottomrule\n\\end{tabular}\n")


def build(root="."):
    root = Path(root)
    sources = {}

    def load(relative, default=None):
        path = root / relative
        if not path.exists():
            return {} if default is None else default
        contents = path.read_bytes()
        data = json.loads(contents)
        sources[str(relative)] = hashlib.sha256(contents).hexdigest()
        return data

    corpus = load("results/corpus_summary.json")
    pubtator = load("results/corpus_pubtator.json")
    retractions = load("results/corpus_retractions.json")
    lexical = load("results/mancon_lexical_test.json")
    nli = load("results/mancon_nli_test.json")
    literature = load("results/mancon_literature_reference.json")
    model_comparison = load("results/mancon_model_comparison.json")
    split = load("data/derived/mancon_split.json")
    selection = load("results/model_selection.json")
    legacy_metabolic = load("results/metabolic_evaluation.json")
    amendment_config = load("configs/evaluation_amendment04.json")
    amendment_result = load("results/metabolic_oracle_evaluation.json")
    amendment_active = bool(amendment_config or amendment_result)
    reference_view = amendment_reference_view(amendment_result) if amendment_active else None
    metabolic = reference_view["metrics"] if amendment_active else legacy_metabolic
    student_config = load("configs/evaluation_amendment05.json")
    student_plan = load("configs/human_validation_plan_pending.json")
    student_result = (load("results/metabolic_student_evaluation.json")
                      if student_config.get("amendment") == 5
                      and student_config.get("status") == "registered"
                      and student_config.get("configuration_confirmed") is True else {})
    student_notice = student_reporting_notice(
        student_config, student_result,
        config_sha256=sources.get("configs/evaluation_amendment05.json"),
        plan=student_plan)
    network = load("results/network.json")
    controls = load("results/controls.json")
    control_review = load("results/control_source_review.json")
    qa = load("results/qa_examples.json")
    qa_review = load("results/qa_source_review.json")
    index_result = load("results/index.json")
    classification = load("results/classification.json")
    pair_generation = load("results/normalization_pairs.json")
    metabolic_baselines = load("results/metabolic_baselines.json")
    retraction_flags = load("results/retraction_flags.json")
    retrieval = pair_generation.get("retrieval", {})
    retrieval_done = retrieval.get("status") == "complete" or "unique_retrieval_queries" in retrieval
    extraction_scope = load("results/extraction_scope.json")
    ledger = load("results/spend.json")
    spend_audit = load("results/spend_audit.json")
    ledger_path = root / "results/spend.json"
    current_ledger_hash = file_sha(ledger_path) if ledger_path.exists() else None
    audit_current = bool(
        spend_audit.get("status") == "complete"
        and current_ledger_hash
        and spend_audit.get("ledger_sha256") == current_ledger_hash
        and sources.get("results/spend.json") == current_ledger_hash
    )
    held_liability = sum(row["reserved"] for row in ledger.get("reconciled_reserves", {}).values())
    reconciled_incidents = len(ledger.get("resolved_unknown_cost_calls", []))
    observed_spend = ledger.get("total_cost")
    cost_upper_bound = observed_spend + held_liability if observed_spend is not None else None
    endpoints = load("results/model_endpoints.json")
    providers = endpoints.get("models", {}) or load("configs/models.json").get("models", {})
    prompts = load("configs/prompts_mancon.json")
    prompt_hash = hashlib.sha256(json.dumps(prompts, sort_keys=True).encode()).hexdigest()
    selected_model = selection.get("primary_model", selection.get("model"))
    if isinstance(selected_model, dict):
        selected_model = selected_model.get("model")
    extraction = {}
    if selected_model:
        extraction = load("results/extraction_" + selected_model.replace("/", "__") + "__full.json")
    human_complete = (reference_view["human_complete"] if amendment_active else
                      bool(metabolic.get("expected_items") and
                           metabolic.get("human_labeled") == metabolic["expected_items"]))
    metrics_complete = reference_view["ready"] if amendment_active else human_complete
    support_source = metabolic.get("extraction", {}).get("overall", {})
    support_known = bool(support_source.get("claims", 0) > 0 or
                         metrics_complete and support_source.get("claims") == 0)
    support_values = support_source.get("evidence_support", {})
    if amendment_active:
        support_values = support_values.get("counts", {})
    support_counts = ({key: support_values.get(key, 0)
                       for key in ["supported", "unsupported", "uncertain"]}
                      if support_known else {key: None for key in ["supported", "unsupported", "uncertain"]})
    support_n = sum(support_counts.values()) if support_known else None
    raw_claim_count = extraction.get("raw_claim_count")
    evidence_dropped, duplicates_removed, drop_count_source = extraction_drop_counts(extraction)
    drop_rate = (evidence_dropped / raw_claim_count
                 if raw_claim_count and evidence_dropped is not None else None)

    benchmark = {}
    for name, label in [("majority", "Majority"), ("polarity_negation", "Polarity / negation")]:
        benchmark[label] = {setting: lexical.get("models", {}).get(f"{name}/{setting}", {})
                            for setting in ["without_question", "with_question"]}
    benchmark["DeBERTa NLI"] = {setting: nli.get("settings", {}).get(setting, {})
                                for setting in ["without_question", "with_question"]}
    test_paths = selection.get("test_metrics", {})
    for model, label in MODELS.items():
        benchmark[label] = {}
        for setting in ["without_question", "with_question"]:
            path = test_paths.get(model, {}).get(setting)
            if isinstance(path, dict):
                path = path.get("metrics_path", path.get("path"))
            result = load(path) if path else {}
            if result:
                config = result.get("metadata", {}).get("config", {})
                if config.get("split") != "test" or config.get("smoke"):
                    raise ValueError(f"Refusing non-test or smoke metric file: {path}")
                if config.get("model") != model or config.get("setting") != setting:
                    raise ValueError(f"Model/setting mismatch in selected test file: {path}")
                if config.get("prompt_hash") != prompt_hash:
                    raise ValueError(f"Test metrics use an obsolete prompt: {path}")
            benchmark[label][setting] = result

    methods = {
        "publication_date_filter": "2016:2025[dp]", "seed": 42, "development_abstracts": 30,
        "planned_extraction_abstracts": 45, "planned_flagged_pairs": 100,
        "planned_per_arm_abstracts": 15, "pair_cap": 30, "concordant_cap": 5,
        "retrieval_top_k": 20, "rrf_constant": 60, "bootstrap_replicates": 2000,
        "selection_tolerance": 0.03, "selection_schema_validity": 0.98,
        "source": "PROTOCOL.md (signed pre-registration); methodological settings, not measured results",
    }
    if (root / "PROTOCOL.md").exists():
        sources["PROTOCOL.md"] = file_sha(root / "PROTOCOL.md")
    amendment = root / "PROTOCOL_AMENDMENT_02_ACCOUNTING.md"
    if amendment.exists():
        sources["PROTOCOL_AMENDMENT_02_ACCOUNTING.md"] = file_sha(amendment)
    for protocol in ("PROTOCOL_AMENDMENT_04.md", "PROTOCOL_ADDENDUM_04a.md"):
        if (root / protocol).exists():
            sources[protocol] = file_sha(root / protocol)
    if amendment_active:
        methods.update({"reference_amendment": 4, "human_validation_items": 46,
                        "human_validation_pairs": 40, "human_validation_abstracts": 6,
                        "human_validation_claims": 31, "binary_kappa_threshold": 0.40,
                        "human_selection_seed": 20261113, "agreement_bootstrap_seed": 20261115})
    claims_path = root / "data/derived/mancon_claims.jsonl"
    mancon_claims = sum(1 for _ in read_jsonl(claims_path)) if claims_path.exists() else None
    if claims_path.exists():
        sources["data/derived/mancon_claims.jsonl"] = file_sha(claims_path)

    pending = []
    if amendment_active:
        if not reference_view["oracle_finished"]:
            pending.append("complete isolated frontier-judge reference")
        if not human_complete:
            pending.append("human validation of six extraction abstracts and 40 pairs")
        elif reference_view["reliability"] == "inconclusive_undefined_kappa":
            pending.append("inconclusive human validation (undefined binary kappa)")
        secondary = amendment_result.get("reference_validity", {}).get("secondary", {})
        if not secondary.get("expected") or secondary.get("valid", 0) < secondary["expected"]:
            pending.append("preserved in-run xhigh secondary comparison")
        if student_notice:
            pending.append(student_notice["pending"])
    else:
        if not human_complete:
            pending.append("Antonio's human-reference metabolic evaluation")
        if not metabolic.get("expected_items") or metabolic.get("second_labeled", 0) < metabolic["expected_items"]:
            pending.append("independent second-annotator labels")
    if not selected_model:
        pending.append("final development-based model selection")
    if any(not benchmark[label][s] for label in MODELS.values()
           for s in ["without_question", "with_question"]):
        pending.append("complete selected-prompt LLM test comparisons")
    corpus_count = corpus.get("counts", {}).get("union")
    if not index_result.get("index_sha256") or index_result.get("count") != corpus_count:
        pending.append("complete corpus retrieval index")
    extraction_target = corpus.get("counts", {}).get("design_union")
    if extraction_scope.get("complete") and extraction_scope.get("reason") and extraction_scope.get("target_articles"):
        extraction_target = extraction_scope["target_articles"]
    if (not extraction or extraction.get("status") != "complete" or not extraction_target
            or extraction.get("articles", 0) < extraction_target):
        pending.append("extraction over the declared scope")
    if classification.get("status") != "complete" or classification.get("unprocessed_pairs", 0):
        pending.append("complete candidate classification")
    elif (pair_generation.get("final_pairs") is not None
          and classification.get("requested_pairs") != pair_generation["final_pairs"]):
        pending.append("classification of the entire frozen candidate frame")
    if not retrieval_done:
        pending.append("key-only versus hybrid retrieval audit")
    frame_counts = metabolic_baselines.get("frame_counts", {})
    conditional_baselines = metabolic_baselines.get(
        "amendment04_evaluation" if amendment_active else "sample_conditioned_evaluation", {})
    if amendment_active and (not reference_view["ready"] or
            conditional_baselines.get("reference_source") != reference_view["source"] or
            (conditional_baselines.get("evaluation_sha256") and
             conditional_baselines["evaluation_sha256"] != sources.get("results/metabolic_oracle_evaluation.json"))):
        conditional_baselines = {}
    if not frame_counts:
        pending.append("metabolic baseline counts and coverage audit")
    elif (classification.get("output_sha256") and metabolic_baselines.get("pair_source_sha256")
          and classification["output_sha256"] != metabolic_baselines["pair_source_sha256"]):
        pending.append("baseline audit of the current pair-verdict file")
    if conditional_baselines.get("status") not in {"complete", "no_sample_pairs"}:
        pending.append("reference-conditioned baseline comparison" if amendment_active else
                       "human-conditioned baseline comparison")
    if retraction_flags.get("status") != "observed":
        pending.append("retracted-source participation audit")
    for name, data in [("controls", controls), ("evidence network", network)]:
        if not data or isinstance(data, dict) and str(data.get("status", "")).startswith(("pending", "blocked")):
            pending.append(name)
    if (network.get("sources", {}).get("pairs_sha256") and classification.get("output_sha256")
            and network["sources"]["pairs_sha256"] != classification["output_sha256"]):
        pending.append("evidence network for the current pair-verdict file")
    if not (root / "report/figures/conflict_network.pdf").exists():
        pending.append("control-subnetwork figure")
    qa_examples = qa.get("examples", []) if isinstance(qa, dict) else qa
    if len(qa_examples) < 2:
        pending.append("evidence-grounded QA examples")
    if qa_review.get("status") != "complete" or not qa_review.get("report_examples"):
        pending.append("source review of illustrative QA")
    if ledger.get("unknown_cost_calls") or ledger.get("pending") or str(ledger.get("status", "")).startswith("blocked"):
        pending.append("settled API accounting")
    if not audit_current:
        pending.append("final provider-usage audit matching the current ledger")
    snapshot = {
        "generated_at": datetime.now(UTC).isoformat(), "draft": bool(pending),
        "pending": pending, "source_sha256": sources, "methods": methods,
        "corpus": corpus, "pubtator": pubtator, "retractions": retractions,
        "benchmark": benchmark, "benchmark_comparison": model_comparison,
        "literature_reference": literature, "mancon_split": split,
        "mancon_claims": mancon_claims, "selection": selection, "providers": providers,
        "extraction": extraction, "metabolic": metabolic, "network": network,
        "metabolic_oracle_evaluation": amendment_result,
        "metabolic_reference_display": reference_view,
        "student_reporting_notice": student_notice,
        "legacy_metabolic_evaluation": legacy_metabolic,
        "extraction_descriptive_rates": {"evidence_filter_drop_rate": drop_rate,
            "drop_denominator": raw_claim_count, "evidence_check_dropped": evidence_dropped,
            "duplicates_removed": duplicates_removed, "total_dropped": extraction.get("dropped"),
            "drop_count_source": drop_count_source, "support_counts": support_counts,
            "support_n": support_n,
            "supported_fraction": support_counts["supported"] / support_n if support_n else None,
            "reference_source": reference_view["source"] if amendment_active else "legacy_human"},
        "controls": controls, "control_source_review": control_review,
        "qa": qa, "qa_source_review": qa_review, "index": index_result,
        "classification": classification, "extraction_scope": extraction_scope,
        "pair_generation": pair_generation, "metabolic_baselines": metabolic_baselines,
        "retraction_flags": retraction_flags,
        "spend": {**{k: ledger.get(k) for k in ["total_cost", "cap", "currency", "status", "updated_at"]},
                  "held_liability": held_liability, "conservative_cost_upper_bound": cost_upper_bound,
                  "reconciled_incidents": reconciled_incidents},
        "spend_audit": spend_audit, "spend_audit_matches_current_ledger": audit_current,
    }
    write_json(root / "results/report_snapshot.json", snapshot)
    out = root / "report/generated"
    out.mkdir(parents=True, exist_ok=True)
    counts = corpus.get("counts", {})
    splits = split.get("counts", {})
    retracted_main = retraction_flags.get("pair_participation", {}).get("main_verdicts", {})
    retracted_controls = retraction_flags.get("pair_participation", {}).get("targeted_verdicts", {})
    baseline_frame = frame_counts.get("overall", {})
    macros = {
        "DraftMark": "DRAFT --- " if pending else "",
        "DraftNotice": tex("Pending: " + "; ".join(pending) + ".") if pending else "",
        "SlideDraftNotice": ("DRAFT: " + tex("; ".join(pending)) + ".") if pending else "",
        "SnapshotDate": tex(snapshot["generated_at"][:10]),
        "CorpusN": number(counts.get("union")), "DesignN": number(counts.get("design_union")),
        "PubTatorN": number(pubtator.get("available")),
        "PubTatorMissing": number(len(pubtator["missing_pmids"])) if "missing_pmids" in pubtator else r"\pending",
        "RetractedN": number(counts.get("retracted")),
        "RetractedDesignN": number(counts.get("retracted_design")),
        "SummaryOnlyN": number(len(corpus["missing_primary_abstract"])) if "missing_primary_abstract" in corpus else r"\pending",
        "RWRetractionsN": number(len(retractions["rw_retracted_pmids"])) if "rw_retracted_pmids" in retractions else r"\pending",
        "RWUnmatchedN": number(len(retractions["pubmed_retracted_missing_rw_retraction"])) if "pubmed_retracted_missing_rw_retraction" in retractions else r"\pending",
        "ManConClaims": number(mancon_claims),
        "ManConQuestions": number(len(split.get("question_assignment", {}))) if split else r"\pending",
        "DevQuestions": number(len(split.get("dev_question_ids", []))) if split else r"\pending",
        "TestQuestions": number(len(split.get("test_question_ids", []))) if split else r"\pending",
        "TestClusters": number(splits.get("test", {}).get("resampling_components")),
        "TestPairsNoQ": number(splits.get("test", {}).get("without_question", {}).get("pairs")),
        "TestPairsQ": number(splits.get("test", {}).get("with_question", {}).get("pairs")),
        "AllPairsNoQ": number(sum(splits.get(s, {}).get("without_question", {}).get("pairs", 0) for s in ["dev", "test"])) if split else r"\pending",
        "AllPairsQ": number(literature.get("n_pairs")),
        "GPTFourFOne": number(literature.get("ternary", {}).get("f1"), 3),
        "GPTFourRecall": number(literature.get("ternary", {}).get("recall"), 3),
        "SelectedModel": tex(MODELS.get(selected_model, selected_model)) if selected_model else r"\pending",
        "TotalCost": number(ledger.get("total_cost"), 3), "BudgetCap": number(ledger.get("cap"), 2),
        "HeldLiability": number(held_liability, 6),
        "ReconciledIncidentN": number(reconciled_incidents),
        "ConservativeCostUpperBound": number(cost_upper_bound, 6),
        "ExtractedArticles": number(extraction.get("successful_articles")),
        "ExtractedProcessed": number(extraction.get("articles")),
        "ExtractionTarget": number(extraction_target),
        "ClassifiedPairs": number(classification.get("successful_pairs")),
        "RequestedPairs": number(classification.get("requested_pairs")),
        "FailedPairs": number(classification.get("failed_pairs")),
        "ExtractedClaims": number(extraction.get("claims")), "DroppedClaims": number(extraction.get("dropped")),
        "EvidenceCheckDropped": number(evidence_dropped),
        "DuplicatesRemoved": number(duplicates_removed),
        "RawClaimN": number(raw_claim_count),
        "EvidenceDropRate": (number(100 * drop_rate, 4) + r"\%" if drop_rate is not None else
            fraction_text(evidence_dropped, raw_claim_count,
                          complete=extraction.get("status") == "complete", percent=True)),
        "HumanSupportN": number(support_n),
        "HumanSupportedN": number(support_counts["supported"]),
        "HumanUnsupportedN": number(support_counts["unsupported"]),
        "HumanUncertainSupportN": number(support_counts["uncertain"]),
        "HumanSupportRate": fraction_text(support_counts["supported"], support_n,
            complete=metrics_complete),
        "HumanLabeled": number(amendment_result.get("reference_validity", {}).get("human", {}).get("valid")
                               if amendment_active else metabolic.get("human_labeled")),
        "HumanExpected": number(amendment_result.get("reference_validity", {}).get("human", {}).get("expected")
                                if amendment_active else metabolic.get("expected_items")),
        "KeyCandidateN": number(pair_generation.get("key_pairs")),
        "HybridCandidateN": number(pair_generation.get("final_pairs")) if retrieval_done else r"\pending",
        "RetrievalAddedN": number(retrieval.get("added_over_key")) if retrieval_done else r"\pending",
        "RetrievalDisplacedN": number(retrieval.get("key_pairs_displaced_by_shared_cap")) if retrieval_done else r"\pending",
        "BaselineFrameN": number(baseline_frame.get("pairs")),
        "PubTatorBiomarkerCoverage": coverage_text(frame_counts.get("biomarker", {}).get("pubtator_relation", {})),
        "PubTatorDrugCoverage": coverage_text(frame_counts.get("drug", {}).get("pubtator_relation", {})),
        "RetractedMainPMIDs": number(observed_list_count(retracted_main, "retracted_pmids_in_flags")),
        "RetractedMainPairs": number(retracted_main.get("flagged_pairs_with_retracted_source")),
        "RetractedControlPMIDs": number(observed_list_count(retracted_controls, "retracted_pmids_in_flags")),
        "RetractedControlPairs": number(retracted_controls.get("flagged_pairs_with_retracted_source")),
        "NetworkNodes": number(network.get("nodes")),
        "NetworkEdges": number(network.get("edges")),
        "NetworkFlaggedEdges": number(network.get("flagged_edges")),
        "MedCPTMaxTokens": number(index_result.get("max_length")),
        "MedCPTAtLimit": number(index_result.get("at_token_limit")),
    }
    for name, key in [("DevAbstracts", "development_abstracts"), ("PlannedAbstracts", "planned_extraction_abstracts"),
                      ("PlannedPairs", "planned_flagged_pairs"), ("AbstractsPerArm", "planned_per_arm_abstracts"),
                      ("BootstrapN", "bootstrap_replicates"), ("PairCap", "pair_cap"),
                      ("ConcordantCap", "concordant_cap"), ("TopK", "retrieval_top_k"),
                      ("RRFConstant", "rrf_constant"), ("RandomSeed", "seed")]:
        macros[name] = number(methods[key])
    macros["LexicalFOne"] = number(benchmark["Polarity / negation"]["without_question"].get("f1"), 3)
    macros["LexicalFOneQ"] = number(benchmark["Polarity / negation"]["with_question"].get("f1"), 3)
    macros["NLIFOne"] = number(benchmark["DeBERTa NLI"]["without_question"].get("f1"), 3)
    primary_metrics = benchmark.get(MODELS.get(selected_model), {})
    macros["PrimaryFOneNoQ"] = number(primary_metrics.get("without_question", {}).get("f1"), 3)
    macros["PrimaryFOneQ"] = number(primary_metrics.get("with_question", {}).get("f1"), 3)
    macros["PrimaryPrecisionNoQ"] = number(primary_metrics.get("without_question", {}).get("precision"), 3)
    macros["PrimaryRecallNoQ"] = number(primary_metrics.get("without_question", {}).get("recall"), 3)
    for setting, name in [("without_question", "PrimaryVsHeuristicNoQ"), ("with_question", "PrimaryVsHeuristicQ")]:
        estimate, bounds = None, None
        for pair in model_comparison.get("settings", {}).get(setting, {}).get("paired_f1_differences", []):
            if (pair["first"], pair["second"]) == (selected_model, "polarity_negation"):
                estimate, bounds = pair["difference_first_minus_second"], pair["ci95"]
            elif (pair["second"], pair["first"]) == (selected_model, "polarity_negation"):
                estimate = -pair["difference_first_minus_second"]
                bounds = [-pair["ci95"][1], -pair["ci95"][0]]
        macros[name] = interval(estimate, bounds)
    macros["SelectionTolerance"] = number(100 * methods["selection_tolerance"], 0)
    macros["SchemaThreshold"] = number(100 * methods["selection_schema_validity"], 0)
    spend_label = "Provider-audited observed" if audit_current else "Recorded"
    macros["SpendStatement"] = (
        spend_label + " API spend is USD~" + number(observed_spend, 6)
        + "; USD~" + number(held_liability, 6) + " remains reserved for delayed billing"
        + " (conservative upper bound USD~" + number(cost_upper_bound, 6)
        + ", cap USD~" + number(ledger.get("cap"), 2) + ")."
        if held_liability > 0 else
        spend_label + " API spend is USD~" + number(observed_spend, 6)
        + " under a USD~" + number(ledger.get("cap"), 2) + " cap."
    )
    macros["ReconciliationStatement"] = (
        "For " + number(reconciled_incidents) + " response-loss "
        + ("incident" if reconciled_incidents == 1 else "incidents")
        + ", accounting amendment~02 allowed reconciliation against stable key-usage totals showing no extra charge; per-call usage stayed unavailable and worst-case costs remained reserved."
        if reconciled_incidents else ""
    )
    macros["HumanEvidenceStatus"] = amendment_reliability_text(reference_view) if amendment_active else (
        "Human-reference extraction and flag precision are reported separately from public-benchmark classification."
        if human_complete else
        "Metabolic accuracy remains unestablished until Antonio's planned reference annotations are complete."
    )
    macros["BenchmarkStatus"] = (
        "The table compares selected-development-prompt LLM tests with the fixed baselines."
        if all(benchmark[label][s] for label in MODELS.values()
               for s in ["without_question", "with_question"]) else
        "The table separates completed measurements from pending selected-prompt LLM tests."
    )
    agreement = (amendment_result.get("agreement", {}).get("oracle_human", {})
                 if amendment_active and reference_view["agreement_ready"] else {}
                 if amendment_active else metabolic.get("agreement", {}))
    category_agreement = agreement.get("categories") or {}
    binary_agreement = agreement.get("binary_true_flag") or {}
    paired_n = agreement.get("paired_items")
    agreement_complete = reference_view["agreement_ready"] if amendment_active else metabolic.get("status") == "complete"
    macros["CategoryKappa"] = kappa_text(category_agreement, complete=agreement_complete, paired_n=paired_n)
    macros["BinaryKappa"] = kappa_text(binary_agreement, complete=agreement_complete, paired_n=paired_n)
    weighted = metabolic.get("pairs", {}).get("overall", {}).get("design_weighted_precision", {})
    macros["WeightedPrecision"] = (r"\textit{not estimable}" if weighted.get("status") == "not_estimable"
                                    else interval(weighted.get("estimate"), weighted.get("bootstrap", {}).get("ci95", weighted.get("ci95"))))
    macros["ReferenceSource"] = tex(reference_view["label"]) if amendment_active else "Human reference"
    macros["ReferenceStatus"] = macros["HumanEvidenceStatus"]
    macros["HumanValidationAssignment"] = tex(student_notice["text"]) if student_notice else "The author annotates the subset blindly."
    macros["HumanValidationAssignmentShort"] = tex(student_notice["short"]) if student_notice else "Blind author validation: six abstracts and 40 pairs."
    macros["JudgeValidationPhrase"] = (
        "validated on a random human subset" if amendment_active and reference_view["reliability"] == "threshold_met" else
        "assessed against a random human subset" if amendment_active and reference_view["agreement_ready"] else
        "to be validated on a random human subset")
    effort = amendment_result.get("reference", {}).get("reasoning_effort", amendment_config.get("effort", "max"))
    macros["OracleEffort"] = "maximum" if effort == "max" else tex(effort) + " (declared fallback)"
    for suffix in ("SupportN", "SupportedN", "UnsupportedN", "UncertainSupportN", "SupportRate"):
        macros["Reference" + suffix] = macros["Human" + suffix]
    reference_validity = amendment_result.get("reference_validity", {})
    for role, name in (("oracle", "Oracle"), ("human", "Validation")):
        coverage = reference_validity.get(role, {})
        macros[name + "Coverage"] = number(coverage.get("valid")) + "/" + number(coverage.get("expected"))
    macros["OracleInvalidN"] = number(len(reference_validity["oracle"].get("invalid_ids", []))) if "oracle" in reference_validity else r"\pending"
    oracle_validity = reference_validity.get("oracle", {})
    macros["OracleValidN"] = number(oracle_validity.get("valid"))
    macros["OracleProcessed"] = (number(oracle_validity["valid"] + len(oracle_validity.get("invalid_ids", [])))
                                 if "valid" in oracle_validity else r"\pending")
    macros["OracleExpected"] = number(oracle_validity.get("expected"))
    (out / "macros.tex").write_text("% Generated; edit source results, never this file.\n" +
                                    "\n".join("\\newcommand{\\" + key + "}{" + value + "}" for key, value in macros.items()) + "\n")
    corpus_rows = [[label, number(counts.get("arms", {}).get(arm, {}).get("all")),
                   number(counts.get("arms", {}).get(arm, {}).get("design"))]
                  for arm, label in ARMS.items()]
    corpus_rows.append([r"\textbf{Unique union}", macros["CorpusN"], macros["DesignN"]])
    (out / "corpus.tex").write_text(table("lrr", ["Arm", "Retrieval", "Extraction eligible"], corpus_rows))
    benchmark_rows = []
    for model, settings in benchmark.items():
        a, b = settings["without_question"], settings["with_question"]
        ci_a = a.get("ci95", {}).get("intervals", {}).get("f1")
        ci_b = b.get("ci95", {}).get("intervals", {}).get("f1")
        validity = a.get("output_validity", 1.0 if a else None)
        benchmark_rows.append([tex(model), number(a.get("precision"), 3), number(a.get("recall"), 3),
                               interval(a.get("f1"), ci_a), interval(b.get("f1"), ci_b),
                               number(validity, 3)])
    (out / "benchmark.tex").write_text(table("lrrrrr", ["Method", "$P_{-Q}$", "$R_{-Q}$", "$F_{1,-Q}$ [95\\% CI]", "$F_{1,+Q}$ [95\\% CI]", "Valid$_{-Q}$"], benchmark_rows))
    provider_rows = [[tex(MODELS.get(m, m)), tex(p.get("provider_name", "pending")),
                      tex(p.get("quantization", "unknown"))] for m, p in providers.items()]
    (out / "providers.tex").write_text(table("lll", ["Model", "Provider", "Weights"], provider_rows))
    def precision_table(metrics, complete, *, include_weighted=amendment_active):
        rows = []
        for arm, label in {**ARMS, "overall": "Overall"}.items():
            record = metrics.get("pairs", {}).get(arm, {})
            result = record.get("sample_precision", {})
            row = [label, number(result.get("n")) if result.get("n") or complete else r"\pending",
                   interval(result.get("estimate"), result.get("wilson95"), n=result.get("n"), complete=complete)]
            if include_weighted:
                weighted = record.get("design_weighted_precision", {})
                row.append(r"\textit{not estimable}" if weighted.get("status") == "not_estimable" else
                           interval(weighted.get("estimate"), weighted.get("bootstrap", {}).get("ci95")))
            rows.append(row)
        headers = ["Arm", "$n$", "Observed [Wilson 95\\% CI]"]
        if include_weighted:
            headers.append("Weighted [95\\% CI]")
        return table("l" + "r" * (len(headers) - 1), headers, rows)

    def extraction_table(metrics, complete):
        rows = []
        for field in ["exposure", "outcome", "sign", "kind", "comparator", "context"]:
            result = metrics.get("extraction", {}).get("overall", {}).get("fields", {}).get(field, {})
            ci = (result.get("cluster_interval") or {}).get("ci95")
            rows.append([tex(field.title()), interval(result.get("estimate"), ci, n=result.get("n"), complete=complete)])
        return table("lr", ["Field", "Strict correctness [95\\% CI]"], rows)

    (out / "precision.tex").write_text(precision_table(metabolic, metrics_complete))
    (out / "precision_slide.tex").write_text(precision_table(metabolic, metrics_complete, include_weighted=False))
    (out / "extraction.tex").write_text(extraction_table(metabolic, metrics_complete))
    if amendment_active:
        human_metrics = amendment_result.get("human_subset", {}) if human_complete else {}
        oracle_metrics = amendment_result.get("oracle", {}) if reference_view["oracle_finished"] else {}
        (out / "oracle_precision.tex").write_text(precision_table(oracle_metrics, reference_view["oracle_finished"]))
        (out / "oracle_extraction.tex").write_text(extraction_table(oracle_metrics, reference_view["oracle_finished"]))
        (out / "human_subset_precision.tex").write_text(precision_table(human_metrics, human_complete))
        (out / "human_subset_extraction.tex").write_text(extraction_table(human_metrics, human_complete))
        for key, filename, ready in (
                ("oracle_human", "human_validation", reference_view["agreement_ready"]),
                ("oracle_secondary", "secondary_agreement", reference_view["oracle_finished"])):
            comparison = amendment_result.get("agreement", {}).get(key, {})
            rendered, notes = amendment_agreement_table(comparison, ready=ready)
            (out / (filename + ".tex")).write_text(rendered)
            (out / (filename + "_notes.tex")).write_text(notes + "\n")
        coverage = ("Judge processing: " + macros["OracleProcessed"] + "/" + macros["OracleExpected"]
                    + " items; valid references: " + macros["OracleCoverage"]
                    + "; terminal invalid items: " + macros["OracleInvalidN"]
                    + ". Human validation: " + macros["ValidationCoverage"] + ". ")
        if not reference_view["oracle_finished"]:
            coverage += "The oracle run is unfinished; pilot/in-progress accuracy is not reported."
            (out / "missing_reference_bounds.tex").write_text(r"\pending{} Full-run missing-reference bounds." + "\n")
        else:
            task_coverage = reference_validity.get("oracle", {}).get("by_task", {})
            coverage += "; ".join(task + ": " + number(row.get("valid")) + "/" + number(row.get("expected"))
                                   for task, row in sorted(task_coverage.items())) + ". "
            if reference_validity.get("oracle", {}).get("invalid_ids"):
                bound_rows = []
                for source, source_label in (("oracle", "Judge"), ("human_subset", "Human subset")):
                    metrics = amendment_result.get(source, {})
                    for arm, label in {**ARMS, "overall": "Overall"}.items():
                        record = metrics.get("pairs", {}).get(arm, {})
                        for key, short in (("sample_precision", "observed"), ("design_weighted_precision", "weighted")):
                            bounds = record.get(key, {}).get("missing_reference_bounds")
                            bound_rows.append([source_label, label + " " + short,
                                               "[" + ", ".join(number(x, 3) for x in bounds) + "]" if bounds else r"\pending"])
                    for field, row in metrics.get("extraction", {}).get("overall", {}).get("fields", {}).items():
                        bounds = row.get("missing_reference_bounds")
                        bound_rows.append([source_label, tex(field),
                                           "[" + ", ".join(number(x, 3) for x in bounds) + "]" if bounds else r"\pending"])
                (out / "missing_reference_bounds.tex").write_text(table("llr", ["Reference", "Measure", "Missing-reference bounds"], bound_rows))
                bounds = amendment_result.get("oracle", {}).get("pairs", {}).get("overall", {}).get("sample_precision", {}).get("missing_reference_bounds")
                coverage += ("Judge overall flag precision has missing-reference bounds "
                             + ("[" + ", ".join(number(x, 3) for x in bounds) + "]" if bounds else r"\pending")
                             + "; these are pessimistic/optimistic sensitivity bounds, not CIs. "
                             + "Per-arm, weighted and extraction bounds are in the companion generated table and JSON.")
            else:
                coverage += "No terminal missing judge references; missing-reference bounds equal the observed estimates."
                (out / "missing_reference_bounds.tex").write_text(coverage + "\n")
        (out / "reference_coverage.tex").write_text(coverage + "\n")
        secondary = amendment_result.get("agreement", {}).get("oracle_secondary", {})
        secondary_pair = agreement_cells(secondary.get("categories", {}), ready=reference_view["oracle_finished"])
        secondary_binary = agreement_cells(secondary.get("binary_true_flag", {}), ready=reference_view["oracle_finished"])
        (out / "secondary_agreement_summary.tex").write_text(
            "Preserved in-run xhigh comparison (instruction-only blinding): six-way raw agreement "
            + secondary_pair[1] + ", $\\kappa$ " + secondary_pair[2] + "; binary raw agreement "
            + secondary_binary[1] + ", $\\kappa$ " + secondary_binary[2]
            + ". Field agreement and resample diagnostics accompany the JSON and generated tables; "
            + "this is not independent human validation.\n")
    retrieval_rows = [["Key-only candidates", macros["KeyCandidateN"]],
                      ["Hybrid candidates", macros["HybridCandidateN"]],
                      ["Added through retrieval", macros["RetrievalAddedN"]],
                      ["Key pairs displaced by shared cap", macros["RetrievalDisplacedN"]]]
    (out / "retrieval_ablation.tex").write_text(table("lr", ["Candidate frame", "Pairs"], retrieval_rows))
    baseline_rows = []
    for name, label in [("sign_comparison", "Sign only"), ("pubtator_relation", "PubTator relations")]:
        frame = baseline_frame.get(name, {})
        evaluation = conditional_baselines.get("by_baseline", {}).get(name, {}).get("overall", {})
        baseline_rows.append([label, number(frame.get("applicable")), number(frame.get("covered")),
                              number(frame.get("flagged")),
                              number(evaluation.get("baseline_positive_reference_labeled_pairs" if amendment_active
                                                    else "baseline_positive_human_labeled_pairs")),
                              baseline_precision_text(evaluation)])
    (out / "metabolic_baselines.tex").write_text(table("lrrrrl", ["Baseline", "Applicable", "Covered", "Frame flags", "$n_+$ scored", "Overlap precision [Wilson 95\\% CI]"], baseline_rows))
    retraction_rows = [["Main system flags", macros["RetractedMainPMIDs"], macros["RetractedMainPairs"]],
                      ["Targeted control flags", macros["RetractedControlPMIDs"], macros["RetractedControlPairs"]]]
    (out / "retraction_participation.tex").write_text(table("lrr", ["Frame", "Retracted PMIDs", "Flagged pairs"], retraction_rows))
    (out / "ablation_summary.tex").write_text(
        "Key-only candidates: " + macros["KeyCandidateN"] + "; hybrid: " + macros["HybridCandidateN"]
        + ". Retrieval adds " + macros["RetrievalAddedN"] + " pairs and displaces "
        + macros["RetrievalDisplacedN"] + " key pairs under the shared cap.\n")
    (out / "retraction_summary.tex").write_text(
        "Main flags include " + macros["RetractedMainPMIDs"] + " distinct retracted PMIDs across "
        + macros["RetractedMainPairs"] + " flagged pairs; the separate targeted-control audit includes "
        + macros["RetractedControlPMIDs"] + " retracted PMIDs across " + macros["RetractedControlPairs"] + " flagged pairs.\n")
    control_rows = []
    control_list = controls.get("controls", []) if isinstance(controls, dict) else controls
    def stage(value):
        if value is None:
            return r"\pending"
        if isinstance(value, bool):
            return "yes" if value else "no"
        if isinstance(value, (int, float)):
            return number(value)
        return tex(value)
    class_names = {"contradictory": "direct", "apparent_contradiction": "apparent",
                   "not_replicated": "not replicated", "not_comparable": "not comparable",
                   "concordant": "concordant", "molecule_class": "molecule/class",
                   "disease_subtype": "subtype", "outcome_definition": "outcome"}
    case_names = {"irisin": "Irisin / diabetes", "eggs": "Eggs / T2D",
                  "amputation": "SGLT2i / amputation", "heart_failure": "SGLT2i / heart failure"}

    def frequencies(value):
        if isinstance(value, dict):
            return "; ".join(tex(class_names.get(k, k)) + " " + number(v)
                             for k, v in sorted(value.items())) if value else "---"
        return tex(value)

    for row in control_list:
        control_rows.append([tex(case_names.get(row.get("name"), row.get("name", "Control"))),
                             stage(row.get("documents_present")), stage(row.get("claims_extracted")),
                             stage(row.get("key_pair_recovered")), stage(row.get("hybrid_pair_recovered")),
                             frequencies(row.get("classification", row.get("status", "pending"))),
                             frequencies(row.get("factor", "pending"))])
    if not control_rows:
        control_rows = [[name, r"\pending", r"\pending", r"\pending", r"\pending", r"\pending", factor] for name, factor in [
            ("Irisin / diabetes", "population, subtype"), ("Eggs / T2D", "region"),
            ("SGLT2i / amputation", "molecule, design"), ("SGLT2i / heart failure", "negative control")]]
    (out / "controls.tex").write_text(table("lccccp{25mm}p{29mm}", ["Case", "Doc.", "Linked claims", "Key", "Hybrid", "Verdict", "Context factor"], control_rows))
    control_review_text, control_review_slide = control_review_snippets(control_review)
    (out / "control_review.tex").write_text(control_review_text + "\n")
    (out / "control_review_slide.tex").write_text(control_review_slide + "\n")
    (out / "fixed_control_review.tex").write_text(fixed_control_review_text(control_review) + "\n")
    network_stats = network.get("statistics", network)
    net_values = [("Nodes", network_stats.get("nodes", network_stats.get("n_nodes"))),
                  ("Edges", network_stats.get("edges", network_stats.get("n_edges"))),
                  ("Conflict-bearing edges", network_stats.get("flagged_edges", network_stats.get("conflict_edges", network_stats.get("n_conflict_edges"))))]
    (out / "network.tex").write_text(table("lr", ["Evidence network", "Count"], [[name, number(value) if isinstance(value, (int, float)) else r"\pending"] for name, value in net_values]))
    (out / "network_concentration.tex").write_text(network_concentration_text(network) + "\n")
    (out / "errors.tex").write_text(human_error_text(
        amendment_result.get("human_subset", {}) if amendment_active else metabolic,
        human_complete=human_complete) + "\n")
    (out / "qa.tex").write_text(qa_review_text(qa if isinstance(qa, dict) else {"examples": qa}, qa_review) + "\n")
    return {"draft": bool(pending), "pending": pending, "snapshot": "results/report_snapshot.json"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=".")
    print(json.dumps(build(parser.parse_args().root)))


if __name__ == "__main__":
    main()
