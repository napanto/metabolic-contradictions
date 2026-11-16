"""Faithful, deterministic V2 presentation; no model or statistical inference.

Null means the extractor supplied no value. Only an explicit ``not_reported``
state is described as not reported by the source. Text supplied by the package is
always escaped; connective wording is the only text translated by the renderer.
"""

import json
import re

from markupsafe import Markup

TEXT = {
    "en": {
        "missing": "Not extracted",
        "not_reported": "Not reported",
        "yes": "Yes",
        "no": "No",
        "result": "Finding",
        "groups": "People and groups",
        "sample_size": "Numbers",
        "comparison": "What was compared",
        "analysis": "Study and analysis",
        "significance": "Statistical evidence",
        "context": "Context",
        "exposure": "What was studied",
        "outcome": "What was measured",
        "estimate": "Reported estimate",
        "index": "Study population",
        "reference": "Comparison group",
        "population": "People in this group",
        "subgroup": "Subgroup for this finding",
        "total_n": "Whole study",
        "group_n": "Group size",
        "design": "Study design",
        "aims": "Stated aims",
        "method": "Analysis method",
        "adjustment": "Statistical adjustment",
        "covariates": "Variables adjusted for",
        "analysis_population": "People included in the analysis",
        "comparator": "Compared with",
        "reference_time": "Starting time",
        "outcome_time": "Time of measurement",
        "orientation": "How the comparison is described",
        "p_value": "Reported p-value",
        "interval": "Reported interval",
        "target": "What they refer to",
        "significance_wording": "Authors’ wording",
        "authors_significance": "Statistical significance reported by the authors",
        "trend": "Authors’ wording about a trend",
        "hedged": "The authors state this tentatively",
        "region": "Where people were recruited",
        "duration": "Duration",
        "dose": "Dose",
        "sample": "Biological material",
        "comparison_type": "Comparison",
        "evidence": "Source sentence",
        "technical": "Technical details",
        "note": "Check this finding against the abstract, including any missing information. It does not cover every result in the study.",
        "unclear": "The extraction does not establish the comparison or its direction.",
        "direction_unknown": "The direction is not extracted.",
        "not_confirmed": "The authors did not confirm the finding for {outcome}.",
        "null": "The authors reported no difference in {outcome}.",
        "no_assoc": "The authors reported no association between {exposure} and {outcome}.",
        "not_confirmed_assoc": "The authors did not confirm an association between {exposure} and {outcome}.",
        "between_pos": "{outcome} was higher in {index} than in {reference}.",
        "between_neg": "{outcome} was lower in {index} than in {reference}.",
        "between_null": "The authors reported no difference in {outcome} between {index} and {reference}.",
        "between_unconfirmed": "The authors did not confirm a difference in {outcome} between {index} and {reference}.",
        "within_pos": "In {index}, {outcome} increased{times}.",
        "within_neg": "In {index}, {outcome} decreased{times}.",
        "within_null": "In {index}, the authors reported no change in {outcome}{times}.",
        "within_unconfirmed": "In {index}, the authors did not confirm a change in {outcome}{times}.",
        "within_pos_nogroup": "{outcome} increased{times}.",
        "within_neg_nogroup": "{outcome} decreased{times}.",
        "within_null_nogroup": "The authors reported no change in {outcome}{times}.",
        "within_unconfirmed_nogroup": "The authors did not confirm a change in {outcome}{times}.",
        "association_pos": "{exposure} was associated with higher {outcome}.",
        "association_neg": "{exposure} was associated with lower {outcome}.",
        "from": " from {value}",
        "to": " to {value}",
        "at": " at {value}",
        "missing_groups": "The extraction does not identify both groups.",
        "no_reference_group": "This kind of finding has no comparison group",
        "none_association": "None: the extraction gives an association",
        "none_within": "None: the extraction gives a change within one group",
    },
    "it": {
        "missing": "Non estratto",
        "not_reported": "Non riportato",
        "yes": "Sì",
        "no": "No",
        "result": "Risultato",
        "groups": "Persone e gruppi",
        "sample_size": "Numerosità",
        "comparison": "Che cosa viene confrontato",
        "analysis": "Studio e analisi",
        "significance": "Evidenza statistica",
        "context": "Contesto",
        "exposure": "Che cosa è stato studiato",
        "outcome": "Che cosa è stato misurato",
        "estimate": "Stima riportata",
        "index": "Popolazione in studio",
        "reference": "Gruppo di confronto",
        "population": "Persone di questo gruppo",
        "subgroup": "Sottogruppo a cui si riferisce il risultato",
        "total_n": "Intero studio",
        "group_n": "Numerosità del gruppo",
        "design": "Disegno dello studio",
        "aims": "Obiettivi dichiarati",
        "method": "Metodo di analisi",
        "adjustment": "Aggiustamento statistico",
        "covariates": "Variabili considerate nell’aggiustamento",
        "analysis_population": "Persone incluse nell’analisi",
        "comparator": "Confrontato con",
        "reference_time": "Momento di partenza",
        "outcome_time": "Momento della misura",
        "orientation": "Descrizione del confronto",
        "p_value": "p-value riportato",
        "interval": "Intervallo riportato",
        "target": "A cosa si riferiscono",
        "significance_wording": "Parole degli autori",
        "authors_significance": "Significatività statistica riportata dagli autori",
        "trend": "Parole degli autori sulla tendenza",
        "hedged": "Gli autori esprimono il risultato in forma ipotetica",
        "region": "Luogo di reclutamento",
        "duration": "Durata",
        "dose": "Dose",
        "sample": "Materiale biologico",
        "comparison_type": "Confronto",
        "evidence": "Frase della fonte",
        "technical": "Dettagli tecnici",
        "note": "Controlla questo risultato nell’abstract, anche per le informazioni mancanti. Non copre tutti i risultati dello studio.",
        "unclear": "L’estrazione non chiarisce il confronto o la sua direzione.",
        "direction_unknown": "La direzione non è stata estratta.",
        "not_confirmed": "Gli autori non hanno confermato il risultato per {outcome}.",
        "null": "Gli autori non hanno rilevato differenze in {outcome}.",
        "no_assoc": "Gli autori non hanno rilevato un’associazione tra {exposure} e {outcome}.",
        "not_confirmed_assoc": "Gli autori non hanno confermato un’associazione tra {exposure} e {outcome}.",
        "between_pos": "{outcome} è risultato più alto in {index} rispetto a {reference}.",
        "between_neg": "{outcome} è risultato più basso in {index} rispetto a {reference}.",
        "between_null": "Gli autori non hanno rilevato differenze in {outcome} tra {index} e {reference}.",
        "between_unconfirmed": "Gli autori non hanno confermato una differenza in {outcome} tra {index} e {reference}.",
        "within_pos": "Nel gruppo {index}, {outcome} è aumentato{times}.",
        "within_neg": "Nel gruppo {index}, {outcome} è diminuito{times}.",
        "within_null": "Nel gruppo {index}, gli autori non hanno rilevato cambiamenti in {outcome}{times}.",
        "within_unconfirmed": "Nel gruppo {index}, gli autori non hanno confermato un cambiamento in {outcome}{times}.",
        "within_pos_nogroup": "{outcome} è aumentato{times}.",
        "within_neg_nogroup": "{outcome} è diminuito{times}.",
        "within_null_nogroup": "Gli autori non hanno rilevato cambiamenti in {outcome}{times}.",
        "within_unconfirmed_nogroup": "Gli autori non hanno confermato un cambiamento in {outcome}{times}.",
        "association_pos": "{exposure} è risultato associato a valori più alti di {outcome}.",
        "association_neg": "{exposure} è risultato associato a valori più bassi di {outcome}.",
        "from": " da {value}",
        "to": " a {value}",
        "at": " a {value}",
        "missing_groups": "L’estrazione non identifica entrambi i gruppi.",
        "no_reference_group": "Questo tipo di risultato non ha un gruppo di confronto",
        "none_association": "Nessuno: l’estrazione indica un’associazione",
        "none_within": "Nessuno: l’estrazione indica una variazione nello stesso gruppo",
    },
}

CODES = {
    "en": {
        "between_group": "Between groups",
        "within_group_change": "Change within the same group",
        "association": "Association",
        "unclear": "Unclear",
        "level": "Measured level",
        "effect": "Effect",
        "risk": "Risk",
        "+": "↑ higher outcome",
        "-": "↓ lower outcome",
        "0": "↔ no relationship reported",
        "participants": "participants",
        "studies": "studies",
        "events": "events",
        "other": "other units",
        "enrolled": "enrolled",
        "randomized": "randomised",
        "analysed": "analysed",
        "not_reported": "Not reported",
        "adjusted": "Adjusted",
        "unadjusted": "Unadjusted",
        "significant": "Significant",
        "not_significant": "Not statistically significant",
        "not_tested": "Not tested",
        "case": "Cases",
        "control": "Controls",
        "intervention": "Intervention",
        "reference": "Reference",
        "cohort": "Cohort",
        "unknown": "Not identified",
        "directional": "Direction reported",
        "explicit_no_difference": "Explicitly no difference",
        "not_confirmed": "Not confirmed",
    },
    "it": {
        "between_group": "Tra gruppi",
        "within_group_change": "Cambiamento nello stesso gruppo",
        "association": "Associazione",
        "unclear": "Non chiaro",
        "level": "Livello misurato",
        "effect": "Effetto",
        "risk": "Rischio",
        "+": "↑ esito più alto",
        "-": "↓ esito più basso",
        "0": "↔ nessuna relazione riportata",
        "participants": "partecipanti",
        "studies": "studi",
        "events": "eventi",
        "other": "altre unità",
        "enrolled": "arruolati",
        "randomized": "randomizzati",
        "analysed": "analizzati",
        "not_reported": "Non riportato",
        "adjusted": "Con aggiustamento",
        "unadjusted": "Senza aggiustamento",
        "significant": "Significativo",
        "not_significant": "Non statisticamente significativo",
        "not_tested": "Non verificato",
        "case": "Casi",
        "control": "Controlli",
        "intervention": "Intervento",
        "reference": "Riferimento",
        "cohort": "Coorte",
        "unknown": "Non identificato",
        "directional": "Direzione riportata",
        "explicit_no_difference": "Assenza esplicita di differenza",
        "not_confirmed": "Non confermato",
    },
}

# Kept as module-level names for the app's shared template context.
CONTEXT_LABELS = {
    lang: {k: text[k] for k in ("region", "duration", "dose", "sample")}
    for lang, text in TEXT.items()
}
CHIP_HELP = {"it": {}, "en": {}}


def readable(value, lang):
    if value is None or value == "" or value == []:
        return TEXT[lang]["missing"]
    if value is True:
        return TEXT[lang]["yes"]
    if value is False:
        return TEXT[lang]["no"]
    if isinstance(value, list):
        return "; ".join(str(v) for v in value)
    return str(value)


def code_label(value, lang):
    """Only use for enum fields: never translate a term taken from an abstract."""
    return CODES[lang].get(value, readable(value, lang))


def bold(value):
    return Markup("<strong>{}</strong>").format(value)


def claim_sentence(claim, lang):
    words = TEXT[lang]
    comparison = claim.get("comparison") or {}
    result = claim.get("result") or {}
    kind = comparison.get("type")
    assertion = result.get("assertion")
    direction = result.get("estimate_direction")
    outcome = bold(readable(claim.get("outcome"), lang))
    if claim.get("kind") == "risk":
        outcome = Markup("rischio di {}" if lang == "it" else "risk of {}").format(outcome)
    exposure = bold(readable(claim.get("exposure"), lang))
    index = (comparison.get("index_group") or {}).get("label")
    reference = (comparison.get("reference_group") or {}).get("label")
    # A comparator phrase is not automatically a group and is never promoted to one.
    values = {
        "outcome": outcome,
        "exposure": exposure,
        "index": bold(index or words["missing"]),
        "reference": bold(reference or words["missing"]),
    }
    if (
        kind == "unclear"
        or assertion == "unclear"
        or kind not in {"between_group", "within_group_change", "association"}
    ):
        return Markup("{} {} → {}.").format(words["unclear"], exposure, outcome)
    if kind == "association":
        if assertion == "explicit_no_difference":
            key = "no_assoc"
        elif assertion == "not_confirmed":
            key = "not_confirmed_assoc"
        elif direction in ("+", "-"):
            key = "association_pos" if direction == "+" else "association_neg"
        else:
            return Markup("{} {} → {}.").format(words["direction_unknown"], exposure, outcome)
    elif kind == "between_group":
        if not index or not reference:
            return Markup("{} {} {}.").format(
                words["missing_groups"], words["outcome"] + ":", outcome
            )
        key = {
            "explicit_no_difference": "between_null",
            "not_confirmed": "between_unconfirmed",
        }.get(assertion)
        if not key:
            key = {"+": "between_pos", "-": "between_neg"}.get(direction)
    else:
        times = Markup("")
        if comparison.get("reference_time"):
            times += Markup(words["from"]).format(value=bold(comparison["reference_time"]))
        if comparison.get("outcome_time"):
            times += Markup(words["to"] if times else words["at"]).format(
                value=bold(comparison["outcome_time"])
            )
        values["times"] = times
        key = {"explicit_no_difference": "within_null", "not_confirmed": "within_unconfirmed"}.get(
            assertion
        )
        if not key:
            key = {"+": "within_pos", "-": "within_neg"}.get(direction)
        if not index:
            # Without the group name, the extracted times still describe the change
            # (Antonio, 15 November); without them the comparison stays unclear.
            if not key or not times:
                return Markup("{} {}: {}.").format(words["unclear"], words["outcome"], outcome)
            key += "_nogroup"
    if key is None:
        return Markup("{} {}: {}.").format(words["direction_unknown"], words["outcome"], outcome)
    return Markup(words[key]).format(**values)


def count_text(count, lang):
    if not count:
        return TEXT[lang]["missing"]
    pieces = [str(count["value"]) if count.get("value") is not None else TEXT[lang]["missing"]]
    unit = code_label(count.get("unit"), lang)
    scope = code_label(count.get("scope"), lang)
    if count.get("value") == 1:
        units = {
            "en": {
                "participants": "participant",
                "studies": "study",
                "events": "event",
                "other": "other unit",
            },
            "it": {
                "participants": "partecipante",
                "studies": "studio",
                "events": "evento",
                "other": "altra unità",
            },
        }
        unit = units[lang].get(count.get("unit"), unit)
        if lang == "it":
            scope = {
                "enrolled": "arruolato",
                "randomized": "randomizzato",
                "analysed": "analizzato",
            }.get(count.get("scope"), scope)
    pieces.append(unit)
    pieces.append(scope)
    text = " ".join(pieces)
    if count.get("reported_text"):
        text += " — “" + count["reported_text"] + "”"
    return text


def evidence_ids(claim, study):
    result = set()

    def visit(value):
        if isinstance(value, dict):
            for entry in value.get("evidence", []):
                if isinstance(entry.get("sentence_id"), int):
                    result.add(entry["sentence_id"])
            for key, child in value.items():
                if key != "evidence":
                    visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(claim)
    visit(study)
    return sorted(result)


FIELD_LABELS = {
    "it": {
        "sample_size": "Numerosità",
        "exposure": "Esposizione o intervento",
        "outcome": "Esito misurato",
        "direction": "Direzione della stima",
        "group_name": "Nome usato nello studio",
        "population": "Popolazione",
        "subgroup": "Sottogruppo del risultato",
        "design": "Disegno",
        "adjustment": "Aggiustamento",
        "covariates": "Variabili di aggiustamento",
        "estimate": "Stima",
        "p_value": "p-value",
        "interval": "Intervallo di confidenza",
        "authors_significance": "Significatività secondo gli autori",
        "hedged": "Formulato come ipotesi",
        "region": "Luogo",
        "no_stats": "Il sistema non ha trovato p-value, intervallo né parole degli autori sulla significatività: controlla l’abstract.",
    },
    "en": {
        "sample_size": "Sample size",
        "exposure": "Exposure or intervention",
        "outcome": "Outcome",
        "direction": "Direction of the estimate",
        "group_name": "Name used in the study",
        "population": "Population",
        "subgroup": "Subgroup for this finding",
        "design": "Design",
        "adjustment": "Adjustment",
        "covariates": "Adjustment variables",
        "estimate": "Estimate",
        "p_value": "p-value",
        "interval": "Confidence interval",
        "authors_significance": "Significance according to the authors",
        "hedged": "Stated as a hypothesis",
        "region": "Location",
        "no_stats": "The system found no p-value, interval or authors’ wording about significance: check the abstract.",
    },
}


def direction_text(claim, lang):
    """Describe direction in its extracted comparison frame, without inferring significance."""
    result = claim.get("result") or {}
    kind = (claim.get("comparison") or {}).get("type")
    direction = result.get("estimate_direction")
    if direction is None:
        return TEXT[lang]["missing"]
    if direction == "0":
        return "nessuna relazione riportata" if lang == "it" else "no relationship reported"
    if direction not in ("+", "-"):
        return code_label(direction, lang)
    if kind == "between_group":
        return (
            ("più alto" if direction == "+" else "più basso")
            + " nella popolazione in studio che nel gruppo di confronto"
            if lang == "it"
            else ("higher" if direction == "+" else "lower")
            + " in the study population than in the comparison group"
        )
    if kind == "within_group_change":
        return (
            ("aumenta" if direction == "+" else "diminuisce") + " nello stesso gruppo"
            if lang == "it"
            else ("increases" if direction == "+" else "decreases") + " within the same group"
        )
    if kind == "association":
        return (
            (
                "maggiore esposizione associata a un esito più alto"
                if direction == "+"
                else "maggiore esposizione associata a un esito più basso"
            )
            if lang == "it"
            else (
                "higher exposure associated with a higher outcome"
                if direction == "+"
                else "higher exposure associated with a lower outcome"
            )
        )
    return code_label(direction, lang)


def comparison_text(comp, lang):
    kind = code_label(comp.get("type"), lang)
    index = (comp.get("index_group") or {}).get("label")
    reference = (comp.get("reference_group") or {}).get("label")
    if comp.get("type") == "between_group" and index and reference:
        return (
            kind
            + ": "
            + index
            + (" rispetto a " if lang == "it" else " compared with ")
            + reference
            + "."
        )
    if comp.get("type") == "within_group_change" and index:
        return kind + ": " + index + "."
    return kind + "."


def claim_view(claim, study, lang):
    """Present the same extracted values next to the check that needs them."""
    words = {**TEXT[lang], **FIELD_LABELS[lang]}
    study = study or {}
    comp = claim.get("comparison") or {}
    analysis = claim.get("analysis") or {}
    result = claim.get("result") or {}
    context = claim.get("context") or {}
    sections = []
    evidence_sources = {
        "result": [result],
        # The analysed population is shown with the groups, where readers look for people
        # (Antonio, 14 November); its evidence comes with the analysis object.
        "groups": [
            analysis,
            comp.get("index_group"),
            comp.get("reference_group"),
            claim.get("subgroup"),
        ],
        "sample_size": [
            study.get("total_n"),
            (comp.get("index_group") or {}).get("n"),
            (comp.get("reference_group") or {}).get("n"),
        ],
        "comparison": [comp.get("index_group"), comp.get("reference_group"), result],
        "analysis": [study, analysis],
        "significance": [result],
        "context": [context],
    }

    def ids(sources):
        return sorted(
            {
                entry["sentence_id"]
                for source in sources
                if source
                for entry in source.get("evidence", [])
            }
        )

    def row(key, value, code=False, **extra):
        missing = value is None or value == "" or value == []
        item = {
            "key": key,
            "label": words.get(key, key),
            "value": code_label(value, lang) if code else readable(value, lang),
            "raw": str(value) if code and value is not None else "",
            "missing": missing or value == "not_reported",
            **extra,
        }
        # Empty: the row shows only Not extracted or Not reported, nothing extracted beside it.
        item["empty"] = item["missing"] and not item.get("reported") and not item.get("target")
        return item

    def add(key, rows, **extra):
        section = {
            "key": key,
            "title": words[key],
            "rows": rows,
            "evidence": ids(evidence_sources[key]),
            # Every value of this check is empty; the finding sentence is never empty.
            "empty": key != "result" and all(item["empty"] for item in rows),
            **extra,
        }
        sections.append(section)
        return section

    direction = direction_text(claim, lang)
    finding = add(
        "result",
        [
            row("exposure", claim.get("exposure")),
            row("outcome", claim.get("outcome")),
            row(
                "direction",
                direction,
                raw=result.get("estimate_direction") or "",
                missing=result.get("estimate_direction") is None,
            ),
        ],
        sentence=claim_sentence(claim, lang),
    )
    groups = []
    numbers = []
    # A missing reference is expected only in these comparison frames. Preserve
    # any supplied reference, even if it looks inappropriate: readers check it.
    no_reference_group = (
        comp.get("type") in ("association", "within_group_change")
        and comp.get("reference_group") is None
    )
    # Both group slots are always shown under fixed labels (Antonio, 15 November): the name
    # the extraction gives a group is a value beside them, never a label. When the extracted
    # comparison has no reference group, its slot says so; readers can still mark it wrong.
    none_text = (
        words["none_association"] if comp.get("type") == "association" else words["none_within"]
    )

    def count_row(key, label, count):
        item = row(
            key,
            count_text({**count, "reported_text": None}, lang) if count else None,
            reported=(count or {}).get("reported_text"),
            label=label,
        )
        # A missing structured number remains explicit beside any reported wording.
        item["missing"] = not count or count.get("value") is None
        if count and count.get("value") is None:
            item["suffix"] = item["value"].removeprefix(words["missing"]).strip()
            item["value"] = words["missing"]
        # A unit, scope or reported wording beside a missing number is still a value shown.
        item["empty"] = not count or (
            count.get("value") is None
            and not count.get("reported_text")
            and count.get("unit") in (None, "not_reported")
            and count.get("scope") in (None, "not_reported")
        )
        return item

    numbers.append(count_row("total_n", words["total_n"], study.get("total_n")))
    for key, label in (("index_group", "index"), ("reference_group", "reference")):
        heading = words[label]
        if key == "reference_group" and no_reference_group:
            groups.append(
                row(key + "_population", none_text, label=words["population"], group=heading, member=key)
            )
            numbers.append(row(key + "_n", none_text, label=heading))
            continue
        group = comp.get(key) or {}
        role = group.get("role")
        name = row(
            key + "_name",
            group.get("label"),
            label=words["group_name"],
            group=heading,
            member=key,
        )
        # A case, control or cohort role stays visible beside the name.
        if role in ("case", "control", "cohort"):
            name["suffix"] = "(" + code_label(role, lang).lower() + ")"
            name["empty"] = False
        groups.append(name)
        groups.append(
            row(key + "_population", group.get("population"), label=words["population"], member=key)
        )
        numbers.append(count_row(key + "_n", heading, group.get("n")))
    groups.append(row("subgroup", (claim.get("subgroup") or {}).get("description")))
    groups.append(row("analysis_population", analysis.get("analysis_population")))
    add("groups", groups)
    add("sample_size", numbers)
    comparison = add(
        "comparison",
        [
            row("comparator", comp.get("comparator")),
            row("reference_time", comp.get("reference_time")),
            row("outcome_time", comp.get("outcome_time")),
            row("orientation", comp.get("orientation_text")),
        ],
        intro=comparison_text(comp, lang),
    )
    # The intro shows the extracted comparison type unless it is Not extracted.
    comparison["empty"] &= comp.get("type") is None
    add(
        "analysis",
        [
            row("aims", study.get("aims_reported")),
            row("design", study.get("design")),
            row("method", analysis.get("method")),
            row("adjustment", analysis.get("adjustment"), True),
            row(
                "covariates",
                ", ".join(analysis["covariates"]) if analysis.get("covariates") else None,
            ),
        ],
    )
    estimate = result.get("estimate")
    pvalue = result.get("p_value")
    interval = result.get("confidence_interval")
    targets = []
    for source, label in ((pvalue, words["p_value"]), (interval, words["interval"])):
        if source:
            targets.append(label + ": " + readable(source.get("target"), lang))
    shared_target = bool(pvalue and interval and pvalue.get("target") == interval.get("target"))
    if shared_target:
        targets = [readable(pvalue.get("target"), lang)]
    stats = [
        row(
            "estimate",
            f"{estimate['measure']}: {estimate['value']} {estimate.get('unit') or ''}".strip()
            if estimate
            else None,
        ),
        row(
            "p_value",
            f"p {pvalue['operator']} {pvalue['value']}" if pvalue else None,
            reported=(pvalue or {}).get("reported_text"),
            target=readable(pvalue.get("target"), lang) if pvalue and not shared_target else None,
        ),
        row(
            "interval",
            (
                f"{interval['level']}%: "
                + (
                    f"da {interval['lower']} a {interval['upper']}"
                    if lang == "it"
                    else f"from {interval['lower']} to {interval['upper']}"
                )
            )
            if interval
            else None,
            reported=(interval or {}).get("reported_text"),
            target=readable(interval.get("target"), lang)
            if interval and not shared_target
            else None,
        ),
        row("target", targets or None),
        row("significance_wording", result.get("significance_wording"), quoted=True),
        row("authors_significance", result.get("authors_significance"), True),
        row("hedged", result.get("hedged")),
        row("trend", result.get("authors_trend_wording"), quoted=True),
    ]
    if not shared_target and (pvalue or interval):
        stats = [item for item in stats if item["key"] != "target"]
    add(
        "significance",
        stats,
        note=(
            words["no_stats"]
            if not pvalue and not interval and not result.get("significance_wording")
            else ""
        ),
    )
    add("context", [row(k, context.get(k)) for k in ("region", "duration", "dose", "sample")])
    by_key = {section["key"]: section for section in sections}
    comparisons = {
        "exposure": {
            "rows": [
                row("exposure", claim.get("exposure")),
                row("comparator", comp.get("comparator")),
            ],
            "evidence": ids([result, comp.get("index_group"), comp.get("reference_group")]),
        },
        "outcome": {
            "rows": [
                row("outcome", claim.get("outcome")),
                row("sample", context.get("sample")),
                row("outcome_time", comp.get("outcome_time")),
            ],
            "evidence": ids([result, context]),
        },
        "comparison": {
            "rows": [row("comparison_type", comp.get("type"), True), *comparison["rows"]],
            "evidence": comparison["evidence"],
        },
        "result": {
            "sentence": finding["sentence"],
            "rows": [item for item in finding["rows"] if item["key"] == "direction"],
            "evidence": finding["evidence"],
        },
        "context": {
            "rows": [
                # The Q2 table keeps its earlier shape: no empty reference slot.
                *(r for r in groups if not (no_reference_group and r.get("member") == "reference_group")),
                row("dose", context.get("dose")),
                row("duration", context.get("duration")),
                *by_key["analysis"]["rows"],
                row("region", context.get("region")),
                row("sample", context.get("sample")),
            ],
            "evidence": ids([*evidence_sources["groups"], study, analysis, context]),
        },
    }
    technical = [
        {"label": "kind", "value": code_label(claim.get("kind"), lang), "raw": claim.get("kind")},
        {
            "label": "estimate_direction",
            "value": code_label(result.get("estimate_direction"), lang),
            "raw": result.get("estimate_direction"),
        },
        {
            "label": words["orientation"],
            "value": readable(comp.get("orientation_text"), lang),
            "raw": comp.get("orientation_text"),
        },
        {
            "label": "assertion",
            "value": code_label(result.get("assertion"), lang),
            "raw": result.get("assertion"),
        },
        {
            "label": "hedged",
            "value": readable(result.get("hedged"), lang),
            "raw": json.dumps(result.get("hedged")),
        },
    ]
    return {
        "sentence": finding["sentence"],
        "sections": sections,
        "comparisons": comparisons,
        "no_reference_group": no_reference_group,
        "technical": technical,
        "raw": json.dumps({"study": study, "claim": claim}, ensure_ascii=False, indent=2),
        "evidence": evidence_ids(claim, study),
        "words": words,
    }


def feedback_view(actual, expected, lang):
    """Stage-aligned practice feedback from saved paths, without participant jargon."""
    from .instrument import FIELDS, instrument_copy

    ui = instrument_copy(lang)
    keywords = ui["keywords"]
    answers = {key: ui[key] for key in ("yes", "no", "cannot_tell")}
    absent = "Non richiesto in questo percorso" if lang == "it" else "Not needed on this path"
    problem_labels = dict(ui["problems"])
    factor_labels = dict(ui["factors"])
    exposure_labels = dict(ui["exposure_reasons"])
    comparison_labels = dict(ui["comparison_reasons"])

    def describe(path):
        if isinstance(path, str):
            path = json.loads(path)
        stages = {"a": [], "b": [], "comparison": []}
        for side in ("a", "b"):
            for field in FIELDS:
                item = path["q1"][side][field]
                text = answers[item["answer"]]
                if item.get("problems"):
                    text += ": " + ", ".join(problem_labels[p] for p in item["problems"])
                stages[side].append((field, keywords[field], text))
        if path.get("q1_relevance"):
            stages["b"].append(
                (
                    "relevance",
                    keywords["relevance"],
                    dict(ui["relevance_options"])[path["q1_relevance"]],
                )
            )
        comparison = stages["comparison"]
        for field in ("exposure", "outcome", "comparison"):
            if field not in path.get("q2", {}):
                continue
            item = path["q2"][field]
            text = answers[item["answer"]]
            if item.get("reason"):
                reason = item["reason"]
                labels = comparison_labels if field == "comparison" else exposure_labels
                text += ": " + (
                    labels.get(reason) or factor_labels.get(reason) or ui["outcome_reason"]
                )
                if item.get("detail"):
                    text += " — " + item["detail"]
            comparison.append(
                ("q2." + field, keywords["measures" if field == "comparison" else field], text)
            )
        if "q2_primary" in path:
            key = path["q2_primary"]
            comparison.append(
                (
                    "primary",
                    keywords["primary"],
                    keywords["measures" if key == "comparison" else key],
                )
            )
        for key in ("q3", "q4"):
            if key in path:
                comparison.append(
                    (
                        key,
                        keywords[key],
                        answers[path[key]] if key == "q3" else dict(ui["q4_options"])[path[key]],
                    )
                )
        if "q5" in path:
            comparison.append(("q5", keywords["q5"], answers[path["q5"]["answer"]]))
            if path["q5"].get("factor"):
                factor = factor_labels[path["q5"]["factor"]]
                if path["q5"].get("detail"):
                    factor += " — " + path["q5"]["detail"]
                comparison.append(("factor", keywords["factor"], factor))
        return stages

    chosen, target = describe(actual), describe(expected)
    result = []
    for stage in ("a", "b", "comparison"):
        left = {key: (label, answer) for key, label, answer in chosen[stage]}
        right = {key: (label, answer) for key, label, answer in target[stage]}
        order = list(
            dict.fromkeys(
                [*(key for key, _, _ in target[stage]), *(key for key, _, _ in chosen[stage])]
            )
        )
        result.append(
            {
                "stage": stage,
                "title": "Abstract " + stage.upper()
                if stage != "comparison"
                else "Confronto"
                if lang == "it"
                else "Comparison",
                "rows": [
                    {
                        "label": (left.get(key) or right[key])[0],
                        "actual": left.get(key, ("", absent))[1],
                        "expected": right.get(key, ("", absent))[1],
                        "different": left.get(key) != right.get(key),
                    }
                    for key in order
                ],
            }
        )
    return result


def feedback_explanation(text):
    """The adjacent worked path replaces redundant protocol-number instructions."""
    return " ".join(
        sentence
        for sentence in re.split(r"(?<=\.)\s+", text)
        if not re.search(r"\bQ[1-5]\b", sentence)
    )
