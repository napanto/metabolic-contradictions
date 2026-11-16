import copy

import pytest

from chl_rag.entity_normalize import Normalizer


def claim(**overrides):
    row = {"claim_id": "a", "pmid": "1", "arm": "drug", "exposure": "canagliflozin",
           "exposure_class": "SGLT2 inhibitors", "outcome": "amputation", "comparator": "placebo",
           "kind": "risk", "sign": "+", "significant": False, "hedged": False,
           "context": {"population": "type 2 diabetes", "region": None, "design": "RCT",
                       "n": 100, "duration": None, "dose": None, "sample": None},
           "evidence": [3], "evidence_text": ["No statistically significant difference was found."]}
    return {**row, **overrides}


def entity(text, identifier, kind="Chemical", name=None):
    return {"text": text, "identifier": identifier, "type": kind, "name": name or text, "valid": True}


def test_same_abstract_linking_and_raw_provenance():
    original = claim()
    frozen = copy.deepcopy(original)
    docs = {"1": {"entities": [entity("canagliflozin", "MESH:C1"), entity("amputation", "MESH:D1", "Disease")],
                  "relations": [{"type": "Positive_Correlation", "score": .99,
                                 "entity1": {"identifier": "MESH:C1"}, "entity2": {"identifier": "MESH:D1"}}]},
            "2": {"entities": [entity("canagliflozin", "MESH:WRONG")], "relations": []}}
    result = Normalizer(pubtator=docs).normalize(original)
    assert original == frozen
    assert all(result[k] == v for k, v in original.items())
    assert result["normalization"]["exposure"]["pubtator_ids"] == ["MESH:C1"]
    assert result["pubtator_relation"]["signs"] == ["+"]
    # A nonsignificant directional estimate is not rewritten as a null effect.
    assert result["sign"] == "+" and result["significant"] is False


def test_different_measurements_and_thresholds_stay_distinct():
    n = Normalizer()
    values = [n.normalize(claim(outcome=o)) for o in ["HOMA-IR", "insulin resistance", "clamp insulin sensitivity"]]
    assert len({r["outcome_key"] for r in values}) == 3
    assert {r["normalization"]["outcome_family"] for r in values} == {"insulin_homeostasis"}
    threshold = n.normalize(claim(outcome="HbA1c < 7%"))
    continuous = n.normalize(claim(outcome="glycated hemoglobin"))
    assert threshold["outcome_key"].startswith("OUTCOME:hba1c|threshold:")
    assert threshold["outcome_key"] != continuous["outcome_key"]


def test_drug_classes_do_not_merge_molecules_or_dual_agonists():
    n = Normalizer()
    a = n.normalize(claim(exposure="canagliflozin"))
    b = n.normalize(claim(exposure="dapagliflozin"))
    assert a["exposure_key"] != b["exposure_key"]
    assert a["exposure_class_key"] == b["exposure_class_key"]
    tirzepatide = n.normalize(claim(exposure="tirzepatide", exposure_class="GLP-1 receptor agonists"))
    assert tirzepatide["exposure_class_key"] == "DRUGCLASS:dual_gip_glp1_agonists"
    assert tirzepatide["normalization"]["notes"]


def test_level_reorientation_keeps_original_evidence_and_sign():
    row = claim(arm="biomarker", kind="level", exposure="serum irisin levels", outcome="T2DM", sign="-", exposure_class=None)
    result = Normalizer().normalize(row)
    assert result["exposure"] == "serum irisin levels"
    assert result["exposure_key"] == "DISEASE:type_2_diabetes"
    assert result["outcome_key"] == "BIOMARKER:irisin"
    assert result["sign"] == "-"
    assert result["normalization"]["level_orientation"] == "level_disease_biomarker_reoriented"


def test_ambiguous_pubtator_ids_do_not_fabricate_external_link():
    doc = {"entities": [entity("irisin", "252995", "Gene"), entity("irisin", "999", "Gene")], "relations": []}
    row = Normalizer(pubtator={"1": doc}).normalize(claim(exposure="irisin", exposure_class=None, arm="biomarker"))
    linked = row["normalization"]["exposure"]
    assert linked["key"] == "BIOMARKER:irisin"
    assert linked["pubtator_ids"] == [] and len(linked["pubtator_candidates"]) == 2


def test_diet_vocabulary_and_missing_baseline_coverage():
    result = Normalizer().normalize(claim(arm="diet", exposure="egg intake", exposure_class=None, outcome="risk of T2D"))
    assert result["exposure_key"] == "DIET:eggs"
    assert result["outcome_key"] == "DISEASE:type_2_diabetes"
    assert result["pubtator_relation"]["applicable"] is False
    assert result["pubtator_relation"]["document_available"] is False


@pytest.mark.parametrize("text,key", [
    ("GLP-1RA", "DRUGCLASS:glp1_receptor_agonists"), ("GLP-1RAs", "DRUGCLASS:glp1_receptor_agonists"),
    ("GLP1-RA", "DRUGCLASS:glp1_receptor_agonists"), ("GLP-1 agonists", "DRUGCLASS:glp1_receptor_agonists"),
    ("SGLT-2i", "DRUGCLASS:sglt2_inhibitors"), ("SGLT2is", "DRUGCLASS:sglt2_inhibitors"),
    ("DPP-4i", "DRUGCLASS:dpp4_inhibitors"), ("VLCD", "DIET:very_low_calorie"),
    ("early TRE", "DIET:early_time_restricted_eating"), ("time-restricted eating (TRE)", "DIET:time_restricted_eating"),
])
def test_refined_unambiguous_aliases(text, key):
    assert Normalizer().resolve(text, [], role="exposure")["key"] == key


def test_dose_route_frequency_variants_share_molecule_and_preserve_literal_facets():
    original = claim(exposure="oral semaglutide 14 mg once daily", context={"dose": None}, sign="+")
    result = Normalizer().normalize(original)
    assert result["exposure_key"] == "DRUG:semaglutide"
    assert result["exposure"] == original["exposure"] and result["context"] == original["context"]
    assert result["sign"] == "+"
    facets = result["normalization"]["exposure"]["facets"]
    assert facets["routes"] == [{"value": "oral", "text": "oral"}]
    assert facets["doses"] == ["14 mg"] and facets["frequencies"] == ["once daily"]
    assert Normalizer().normalize(claim(exposure="semaglutide SC 0.5 mg once-weekly"))["exposure_key"] == "DRUG:semaglutide"
    assert Normalizer().normalize(claim(exposure="metformin (1,500 mg/day)"))["exposure_key"] == "DRUG:metformin"
    assert Normalizer().normalize(claim(exposure="tirzepatide 15 mg"))["exposure_key"] == "DRUG:tirzepatide"
    rate = Normalizer().normalize(claim(exposure="metformin daily dose ≥1500 mg/day"))
    assert rate["exposure_key"] == "DRUG:metformin"
    assert rate["normalization"]["exposure"]["facets"]["frequencies"] == []
    assert rate["normalization"]["exposure"]["facets"]["doses"] == ["≥1500 mg/day"]


@pytest.mark.parametrize("text", ["IDegLira", "iGlarLixi", "semaglutide plus metformin", "metformin and semaglutide",
                                   "canagliflozin 100 mg/metformin", "semaglutide 2 mg + unknown drug"])
def test_combinations_never_collapse_to_a_pubtator_component_or_its_class(text):
    doc = {"entities": [entity(text, "MESH:C1", name="semaglutide")], "relations": []}
    result = Normalizer(pubtator={"1": doc}).normalize(claim(exposure=text, exposure_class="GLP-1RA"))
    assert result["exposure_key"].startswith("TEXT:")
    assert result["exposure_class_key"] is None
    assert result["normalization"]["exposure"]["pubtator_ids"] == []
    assert result["normalization"]["exposure"]["facets"]["composite_expression"]


def test_ambiguous_diet_acronyms_stay_unresolved_even_with_suggestive_entities():
    n = Normalizer()
    for text in ["LCD", "VLCKD"]:
        entities = [entity(text, "MESH:wrong", name="low-carbohydrate diet")]
        resolved = n.resolve(text, entities, role="exposure")
        assert resolved["key"].startswith("TEXT:") and not resolved["pubtator_ids"]
        assert resolved["facets"]["ambiguous_alias"]


def test_renal_egfr_is_case_sensitive_and_cannot_link_to_egfr_gene():
    entities = [entity("EGFR", "1956", "Gene", "EGFR")]
    n = Normalizer()
    renal = n.resolve("eGFR", entities, role="outcome")
    gene = n.resolve("EGFR", entities, role="outcome")
    assert renal["key"] == "OUTCOME:egfr" and renal["pubtator_ids"] == []
    assert gene["key"] == "PUBTATOR:NCBIGene:1956"
    assert n.resolve("egfr", [], role="outcome")["key"] != renal["key"]
    assert n.resolve("ESTIMATED GLOMERULAR FILTRATION RATE", entities, role="outcome")["key"] == renal["key"]
    # Even a bad preferred gene name must not promote a gene to the renal concept.
    bad = [entity("EGFR", "1956", "Gene", "estimated glomerular filtration rate")]
    assert n.resolve("EGFR", bad, role="outcome")["key"] != renal["key"]


def test_outcome_changes_severity_and_survival_remain_distinct_without_sign_inversion():
    n = Normalizer()
    for expressions in [["HbA1c", "HbA1c change", "HbA1c reduction"],
                        ["body weight", "weight change", "weight loss"],
                        ["hypoglycaemia", "severe hypoglycaemia"],
                        ["mortality", "all-cause death", "overall survival"]]:
        results = [n.normalize(claim(outcome=text, sign="-")) for text in expressions]
        assert len({r["outcome_key"] for r in results}) == len(expressions)
        assert all(r["sign"] == "-" for r in results)
    assert n.resolve("hypoglycemia", [], role="outcome")["key"] == n.resolve("hypoglycaemia", [], role="outcome")["key"]
    for exposure in ["higher metformin dose", "lower semaglutide", "exercise", "bariatric surgery"]:
        assert not n.resolve(exposure, [], role="exposure")["key"].startswith(("DRUG:", "DIET:"))
