"""Conservative, auditable claim normalization using same-abstract PubTator links.

Original extracted fields never change. Local curated concept keys are deliberately
distinct from external ontology IDs; HOMA-IR, clamp measurements, generic insulin
resistance and individual drugs retain separate keys.
"""
from __future__ import annotations

import argparse
import json
import re
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path

from .io import file_sha, read_jsonl, utcnow, write_json, write_jsonl


def normal_form(text):
    text = unicodedata.normalize("NFKC", text or "").casefold()
    text = text.replace("α", "alpha").replace("β", "beta")
    return " ".join(re.findall(r"[^\W_]+", text, flags=re.UNICODE))


def forms(text):
    """Only remove explicit measurement/reporting wrappers, not arbitrary words."""
    value = normal_form(text)
    result = [value]
    stripped = re.sub(r"^(?:risk of|incidence of|levels? of|concentrations? of|treatment with|use of|circulating|serum|plasma)\s+", "", value)
    stripped = re.sub(r"^(?:circulating|serum|plasma)\s+", "", stripped)
    stripped = re.sub(r"\s+(?:levels?|concentrations?|risk|incidence|use)$", "", stripped)
    if stripped != value:
        result.append(stripped)
    return result


def external_id(value):
    if not value or value in ["-", "None", "null"]:
        return None
    # Preserve source namespace: gene identifiers are numeric, MeSH identifiers
    # carry MESH:, and chemical identifiers can contain multiple IDs.
    value = str(value)
    return "NCBIGene:" + value if value.isdecimal() else value


DOSE = re.compile(r"(?:[<>]=?|[≤≥])?\s*\d+(?:,\d{3})*(?:\.\d+)?(?:\s*[-–]\s*\d+(?:\.\d+)?)?\s*-?\s*(?:mg|mcg|μg|ug|g|IU|units?)(?:\s*(?:/|per\s+)(?:kg|day|d|week|wk|weekly)){0,2}\b", re.IGNORECASE)
ROUTES = [(r"\b(?:orally administered|oral formulation|oral|orally)\b", "oral"),
          (r"\b(?:subcutaneously administered|subcutaneous|subcutaneously|s\.?c\.?)\b", "subcutaneous"),
          (r"\b(?:intravenous|intravenously)\b", "intravenous"),
          (r"\b(?:injectable|injected|injections?)\b", "injection_unspecified")]
FREQUENCY = re.compile(r"\b(?:(?:once|twice|thrice)[ -](?:daily|weekly)|once every \d+ weeks?|every \d+ weeks?|every day|daily|weekly|q\.?d\.?|q\.?w\.?|b\.?i\.?d\.?|t\.?i\.?d\.?)\b", re.IGNORECASE)
FORMULATION = re.compile(r"\b(?:modified|extended|delayed|immediate)[ -]release\b", re.IGNORECASE)


def single_drug_variant(text, concepts):
    """Accept one known molecule only if all residual text is an explicit qualifier."""
    value = unicodedata.normalize("NFKC", text)
    value = re.sub(r"^(?:treatment with|use of)\s+", "", value, flags=re.IGNORECASE)
    if re.search(r"\b(?:higher|lower|increased|decreased|reduced|greater|less)\b", value, re.IGNORECASE):
        return None, {}
    matches = []
    for concept in concepts.values():
        if concept["category"] != "drug":
            continue
        for alias in concept["aliases"]:
            for match in re.finditer(r"(?<![A-Za-z])" + re.escape(alias) + r"(?![A-Za-z])", value, re.IGNORECASE):
                matches.append((match.start(), match.end(), concept))
    unique = {(a, b, c["key"]): c for a, b, c in matches}
    if len(unique) != 1:
        return None, {}
    (start, end, _), concept = next(iter(unique.items()))
    residual = value[:start] + " " + value[end:]
    facets = {"resolution_rule": "single_drug_with_explicit_qualifiers", "routes": [],
              "doses": [], "frequencies": [], "formulations": []}
    facets["doses"] = [m.group(0).strip() for m in DOSE.finditer(residual)]
    residual = DOSE.sub(" ", residual)
    for pattern, canonical in ROUTES:
        facets["routes"].extend({"value": canonical, "text": m.group(0)} for m in re.finditer(pattern, residual, re.IGNORECASE))
        residual = re.sub(pattern, " ", residual, flags=re.IGNORECASE)
    facets["dose_qualifiers"] = [m.group(0) for m in re.finditer(r"\b(?:total daily dose|average daily dose|daily dose)\b", residual, re.IGNORECASE)]
    residual = re.sub(r"\b(?:total daily dose|average daily dose|daily dose)\b", " ", residual, flags=re.IGNORECASE)
    facets["frequencies"] = [m.group(0) for m in FREQUENCY.finditer(residual)]
    residual = FREQUENCY.sub(" ", residual)
    facets["formulations"] = [m.group(0) for m in FORMULATION.finditer(residual)]
    residual = FORMULATION.sub(" ", residual)
    residual = re.sub(r"\b(?:treatment|therapy|administration|dosing|dose|daily dose|total daily dose)\b", " ", residual, flags=re.IGNORECASE)
    residual = re.sub(r"[\s(),.\-–]+", "", residual)
    if residual or not any(facets[k] for k in ["routes", "doses", "frequencies", "formulations"]):
        return None, {}
    return concept, facets


class Normalizer:
    def __init__(self, vocabulary="configs/vocabularies.json", pubtator=None):
        self.vocabulary_path = Path(vocabulary)
        self.vocabulary = json.loads(self.vocabulary_path.read_text())
        self.concepts = {c["key"]: c for c in self.vocabulary["concepts"]}
        self.aliases = defaultdict(set)
        self.case_sensitive_aliases = defaultdict(set)
        for concept in self.concepts.values():
            for alias in concept["aliases"]:
                self.aliases[normal_form(alias)].add(concept["key"])
            for alias in concept.get("case_sensitive_aliases", []):
                self.case_sensitive_aliases[alias].add(concept["key"])
        self.ambiguous = {normal_form(k): v for k, v in self.vocabulary.get("ambiguous_aliases", {}).items()}
        self.protected_combinations = {normal_form(k) for k in self.vocabulary.get("protected_combinations", [])}
        self.pubtator = pubtator or {}

    def resolve(self, text, entities, *, role):
        if text is None or not str(text).strip():
            return {"key": None, "label": None, "source": "missing", "pubtator_ids": [],
                    "pubtator_candidates": [], "family": None, "measurement": None,
                    "category": None, "class_key": None}
        facets = {}
        candidates = forms(text)
        threshold_free = re.sub(r"(?:<=|>=|<|>|≤|≥)\s*\d+(?:\.\d+)?\s*%?", "", str(text)).strip()
        if threshold_free != str(text).strip():
            candidates.extend(forms(threshold_free))
        if role in {"exposure", "comparator"}:
            candidates.extend(re.sub(r"\s+(?:treatment|therapy|administration)$", "", value) for value in list(candidates))
        allowed = {"comparator"} if role == "comparator" else set()
        concept_keys = set()
        for value in [str(text).strip(), threshold_free]:
            concept_keys.update(self.case_sensitive_aliases.get(value, set()))
        for value in candidates:
            matches = self.aliases.get(value, set())
            # Comparator aliases must not normalize an exposure/outcome to a
            # control group. Drug/diet comparators remain valid concepts.
            concept_keys.update(k for k in matches if self.concepts[k]["category"] != "comparator" or allowed)
        concept = self.concepts[next(iter(concept_keys))] if len(concept_keys) == 1 else None
        ambiguous = self.ambiguous.get(normal_form(text))
        composite = (normal_form(text) in self.protected_combinations
                     or bool(re.search(r"\+|\b(?:plus|combination|combined)\b", str(text), re.IGNORECASE)))
        if concept is None and role in {"exposure", "comparator"}:
            residue = DOSE.sub(" ", str(text))
            residue = re.sub(r"^(?:treatment with|use of)\s+", "", residue, flags=re.IGNORECASE)
            composite = composite or bool(re.search(r"\b(?:and|with)\b|/", residue, re.IGNORECASE))
        if concept is None and role in {"exposure", "comparator"} and not ambiguous and not composite:
            concept, facets = single_drug_variant(str(text), self.concepts)
        if ambiguous:
            concept = None
            facets = {"ambiguous_alias": ambiguous, "resolution_rule": "leave_ambiguous_alias_unresolved"}
        if composite:
            concept = None
            facets = {"composite_expression": True, "resolution_rule": "do_not_collapse_combination"}
        if concept:
            facets = {**concept.get("facets", {}), **facets}
        match_forms = set(candidates)
        if concept:
            match_forms.update(normal_form(x) for x in concept["aliases"])
            match_forms.update(normal_form(x) for x in concept.get("link_aliases", []))
            match_forms.update(normal_form(x) for x in concept.get("case_sensitive_aliases", []))
        linked = []
        for entity in entities:
            if entity.get("valid") is False or entity.get("type") not in {"Gene", "Disease", "Chemical"}:
                continue
            if ambiguous or composite or (concept is not None and entity.get("type") not in concept.get("pubtator_types", ["Gene", "Disease", "Chemical"])):
                continue
            surfaces = {normal_form(entity.get("text")), normal_form(entity.get("name"))}
            if surfaces & match_forms:
                identifier = external_id(entity.get("identifier"))
                if identifier:
                    linked.append((identifier, entity))
        ids = sorted({x[0] for x in linked})
        unambiguous_ids = ids if len(ids) == 1 else []
        # A source's normalized preferred name can identify a curated synonym
        # (e.g. brand name -> drug). It never imports another abstract's link.
        if concept is None and len(ids) == 1:
            suggested = set()
            for _, entity in linked:
                suggested.update(k for k in self.aliases.get(normal_form(entity.get("name")), set())
                                 if entity.get("type") in self.concepts[k].get("pubtator_types", ["Gene", "Disease", "Chemical"]))
            if len(suggested) == 1:
                concept = self.concepts[next(iter(suggested))]
        if concept:
            key, label = concept["key"], concept["label"]
            source = "curated+pubtator" if unambiguous_ids else "curated"
            family, measurement = concept["family"], concept.get("measurement")
            category, class_key = concept["category"], concept.get("class_key")
        elif unambiguous_ids:
            key, label = "PUBTATOR:" + ids[0], linked[0][1].get("name") or str(text)
            source, family, measurement, category, class_key = "pubtator", key, None, "entity", None
        else:
            key, label = "TEXT:" + normal_form(text), str(text)
            source, family, measurement, category, class_key = "text", key, None, "unknown", None
        # Threshold endpoints are different from continuous measurements. Preserve
        # the operator before token normalization can erase it.
        threshold = re.findall(r"(?:<=|>=|<|>|≤|≥)\s*\d+(?:\.\d+)?\s*%?", str(text))
        if threshold and not (concept and concept["category"] == "drug" and facets.get("doses")):
            threshold = [re.sub(r"\s+", "", t) for t in threshold]
            key += "|threshold:" + ",".join(threshold)
            measurement = (measurement or "unspecified") + "|threshold:" + ",".join(threshold)
        return {"key": key, "label": label, "source": source,
                "pubtator_ids": unambiguous_ids, "pubtator_candidates": ids,
                "family": family, "measurement": measurement, "category": category,
                "class_key": class_key, "facets": facets}

    def normalize(self, claim):
        doc = self.pubtator.get(str(claim["pmid"]))
        entities = doc.get("entities", []) if doc else []
        exposure = self.resolve(claim["exposure"], entities, role="exposure")
        outcome = self.resolve(claim["outcome"], entities, role="outcome")
        comparator = self.resolve(claim.get("comparator"), entities, role="comparator")
        orientation, notes = "as_extracted", []
        if claim["kind"] == "level":
            if exposure["category"] == "biomarker" and outcome["category"] == "disease":
                exposure, outcome = outcome, exposure
                orientation = "level_disease_biomarker_reoriented"
                notes.append("Normalized level axis is disease -> biomarker; raw fields and raw sign retained.")
            elif outcome["category"] != "biomarker":
                notes.append("Level outcome not in curated biomarker vocabulary; orientation unverified.")
        explicit_class = self.resolve(claim.get("exposure_class"), entities, role="exposure")
        class_key = exposure.get("class_key")
        if explicit_class["category"] == "drug_class":
            if class_key and explicit_class["key"] != class_key:
                notes.append("Extracted drug class conflicts with curated map; curated map retained and conflict recorded.")
            elif (not class_key and exposure["category"] in {"drug", "entity", "unknown"}
                  and not exposure.get("facets", {}).get("composite_expression")):
                class_key = explicit_class["key"]
        if claim["kind"] == "level" and orientation != "as_extracted":
            class_key = exposure.get("class_key")
        for field, value in [("exposure", exposure), ("outcome", outcome)]:
            if len(value["pubtator_candidates"]) > 1:
                notes.append(field + " has ambiguous same-abstract PubTator IDs; no external link assigned.")
        relations = []
        wanted = set(exposure["pubtator_ids"] + outcome["pubtator_ids"])
        pair_linked = bool(exposure["pubtator_ids"] and outcome["pubtator_ids"] and len(wanted) == 2)
        if doc and pair_linked:
            for relation in doc.get("relations", []):
                got = {external_id(relation.get("entity1", {}).get("identifier")),
                       external_id(relation.get("entity2", {}).get("identifier"))}
                if got == wanted and relation["type"] in {"Positive_Correlation", "Negative_Correlation"}:
                    relations.append({"type": relation["type"], "score": relation.get("score")})
        signs = sorted({"+" if r["type"] == "Positive_Correlation" else "-" for r in relations})
        normalization = {"exposure": exposure, "outcome": outcome, "comparator": comparator,
                         "outcome_measurement": outcome["measurement"], "outcome_family": outcome["family"],
                         "level_orientation": orientation, "notes": notes}
        return {**claim, "exposure_key": exposure["key"], "outcome_key": outcome["key"],
                "exposure_class_key": class_key,
                "pair_key": "|".join([claim["arm"], exposure["key"], outcome["key"]]),
                "normalization": normalization,
                "pubtator_relation": {"applicable": claim["arm"] in {"biomarker", "drug"},
                                      "document_available": doc is not None,
                                      "entity_pair_linked": pair_linked,
                                      "relation_available": bool(relations), "signs": signs,
                                      "relations": relations}}


def run(claims="data/derived/claims.jsonl", output="data/derived/claims_normalized.jsonl",
        pubtator="data/derived/pubtator.jsonl.gz", vocabulary="configs/vocabularies.json",
        summary_path="results/normalization.json"):
    rows = list(read_jsonl(claims))
    wanted_pmids = {str(c["pmid"]) for c in rows}
    annotations = {r["pmid"]: r for r in read_jsonl(pubtator) if r["pmid"] in wanted_pmids}
    normalizer = Normalizer(vocabulary, annotations)
    normalized = [normalizer.normalize(row) for row in rows]
    write_jsonl(output, normalized)
    coverage = {}
    for arm in ["biomarker", "drug", "diet", "overall"]:
        subset = [r for r in normalized if arm == "overall" or r["arm"] == arm]
        coverage[arm] = {"claims": len(subset), "pmids": len({r["pmid"] for r in subset}),
                         "exposure_sources": dict(Counter(r["normalization"]["exposure"]["source"] for r in subset)),
                         "outcome_sources": dict(Counter(r["normalization"]["outcome"]["source"] for r in subset)),
                         "both_externally_linked": sum(r["pubtator_relation"]["entity_pair_linked"] for r in subset),
                         "pubtator_documents_available": sum(r["pubtator_relation"]["document_available"] for r in subset),
                         "pubtator_correlations_available": sum(r["pubtator_relation"]["relation_available"] for r in subset),
                         "level_reorientations": sum(r["normalization"]["level_orientation"] != "as_extracted" for r in subset)}
    summary = {"created_at": utcnow(), "claims_input": claims, "claims_sha256": file_sha(claims),
               "vocabulary_sha256": file_sha(vocabulary), "pubtator_sha256": file_sha(pubtator),
               "normalized_sha256": file_sha(output), "output": output, "coverage": coverage,
               "unique_keys": len({r["pair_key"] for r in normalized}),
               "automatic_embedding_merges": 0,
               "embedding_policy": "No embedding-based equivalence merges. Hybrid retrieval proposes related but distinct measurements for pair assessment, preserving their original keys."}
    write_json(summary_path, summary)
    print(json.dumps(summary), flush=True)
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--claims", default="data/derived/claims.jsonl")
    parser.add_argument("--output", default="data/derived/claims_normalized.jsonl")
    parser.add_argument("--pubtator", default="data/derived/pubtator.jsonl.gz")
    parser.add_argument("--summary", default="results/normalization.json")
    args = parser.parse_args()
    run(args.claims, args.output, args.pubtator, summary_path=args.summary)
