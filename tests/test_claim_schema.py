"""Only invented development sources; no study or judge labels are fixtures."""
import json
from copy import deepcopy
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, ValidationError

from chl_rag.extraction import (
    apply_verification,
    extraction_item,
    extraction_request,
    pair_item,
    render_claim_en,
    validate_canonical,
    validate_extraction,
    verify_request,
)
from chl_rag.claim_schema import EXTRACTION, MODEL_EXTRACTION, PACKAGE_ITEM

FIXTURES = json.loads(Path("configs/synthetic.json").read_text())["cases"]


@pytest.mark.parametrize("case", FIXTURES, ids=lambda c: c["case_id"])
def test_twelve_atomic_microcases_have_grounded_strict_schemas(case):
    result = validate_extraction(case["article"], case["expected_model_output"])
    assert result["raw_claim_count"] == result["retained_claim_count"] == 2
    assert not result["dropped"]
    assert result["semantic_support_validated"] is False
    validate_canonical(case["article"], result["extraction"])
    item = extraction_item("v2-extraction-001", case["article"]["arms"][0], case["article"], result["extraction"])
    Draft202012Validator(PACKAGE_ITEM).validate(item)
    rows = render_claim_en(item["claims"][0], item["article"]["study"])["rows"]
    assert len(rows) == 7
    assert rows[2]["field"] == "sample_size"


def test_schemas_are_strict_and_written_artifacts_match_python():
    for schema in [MODEL_EXTRACTION, EXTRACTION, PACKAGE_ITEM]:
        Draft202012Validator.check_schema(schema)
    assert json.loads(Path("configs/extraction_schema.json").read_text()) == MODEL_EXTRACTION
    assert json.loads(Path("configs/package_item_schema.json").read_text()) == PACKAGE_ITEM
    case = deepcopy(FIXTURES[0])
    case["expected_model_output"]["claims"][0]["predicted_label"] = "concordant"
    with pytest.raises(ValidationError):
        validate_extraction(case["article"], case["expected_model_output"])


def test_context_methods_support_is_allowed_but_results_from_methods_are_dropped():
    case = deepcopy(FIXTURES[0])
    raw = case["expected_model_output"]
    raw["claims"][0]["result"]["evidence"] = [{"sentence_id": 1, "quote": "Twenty adults"}]
    result = validate_extraction(case["article"], raw)
    assert result["retained_claim_count"] == 1
    assert result["dropped"][0]["errors"][0]["reason"] == "ineligible_result_evidence"
    assert len(result["emitted_claims"]) == 2
    assert not result["emitted_claims"][0]["retained"]


def test_nonliteral_quote_and_missing_sentence_are_not_repaired():
    case = deepcopy(FIXTURES[0]); raw = case["expected_model_output"]
    raw["claims"][0]["result"]["evidence"][0]["quote"] = "a fabricated quotation"
    raw["claims"][1]["result"]["evidence"][0]["sentence_id"] = 99
    before = deepcopy(raw)
    result = validate_extraction(case["article"], raw)
    assert raw == before
    assert not result["extraction"]["claims"]
    assert len(result["emitted_extraction"]["claims"]) == 2
    assert len(result["dropped"]) == 2


def test_duplicate_claims_remain_emitted_for_precision_but_not_production():
    case = deepcopy(FIXTURES[0]); raw = case["expected_model_output"]
    raw["claims"].append(deepcopy(raw["claims"][0]))
    result = validate_extraction(case["article"], raw)
    assert len(result["emitted_claims"]) == 3
    assert len({x["emission_id"] for x in result["emitted_claims"]}) == 3
    assert result["emitted_claims"][0]["claim"]["claim_id"] == result["emitted_claims"][2]["claim"]["claim_id"]
    assert result["retained_claim_count"] == 2
    assert result["dropped"][0]["errors"][0]["reason"] == "duplicate"


def test_claim_ids_depend_on_source_and_all_rich_fields():
    case = deepcopy(FIXTURES[0]); article, raw = case["article"], case["expected_model_output"]
    first = validate_extraction(article, raw)["extraction"]["claims"][0]["claim_id"]
    article["pmid"] = "899999999"
    assert validate_extraction(article, raw)["extraction"]["claims"][0]["claim_id"] != first
    article["pmid"] = case["article"]["pmid"]
    raw["claims"][0]["analysis"]["adjustment"] = "adjusted"
    assert validate_extraction(article, raw)["extraction"]["claims"][0]["claim_id"] != first


def test_no_claim_limit_or_visible_output_truncation():
    case = deepcopy(FIXTURES[0]); raw = case["expected_model_output"]
    raw["claims"] = []
    for i in range(40):
        c = deepcopy(FIXTURES[0]["expected_model_output"]["claims"][0])
        c["outcome"] = f"Synthetic unique outcome {i}"
        raw["claims"].append(c)
    result = validate_extraction(case["article"], raw)
    assert result["retained_claim_count"] == 40


def test_interval_pvalue_and_comparison_structure_fail_explicitly():
    case = deepcopy(FIXTURES[4]); raw = case["expected_model_output"]
    raw["claims"][0]["result"]["confidence_interval"].update(lower=5, upper=-1)
    result = validate_extraction(case["article"], raw)
    assert result["dropped"][0]["errors"][0]["reason"] == "reversed_interval_bounds"
    raw["claims"][1]["result"]["p_value"]["value"] = 1.5
    with pytest.raises(ValidationError):
        validate_extraction(case["article"], raw)
    case = deepcopy(FIXTURES[0]); case["expected_model_output"]["claims"][0]["comparison"]["comparator"] = "invented placebo"
    result = validate_extraction(case["article"], case["expected_model_output"])
    assert result["dropped"][0]["errors"][0]["reason"] == "within_group_has_between_group_reference"


def test_nonfinite_and_broken_study_evidence_reject_response():
    case = deepcopy(FIXTURES[2]); raw = case["expected_model_output"]
    raw["study"]["evidence"][0]["quote"] = "invented"
    with pytest.raises(ValueError, match="Invalid study evidence"):
        validate_extraction(case["article"], raw)
    case = deepcopy(FIXTURES[4]); raw = case["expected_model_output"]
    raw["claims"][0]["result"]["estimate"]["value"] = float("nan")
    with pytest.raises(ValueError, match="Nonfinite"):
        validate_extraction(case["article"], raw)


def test_prompt_identity_binds_reasoning_limit_provider_and_source():
    article = FIXTURES[0]["article"]
    first = extraction_request(article, model="synthetic", reasoning="off", max_output_tokens=1000, provider="x")
    for kwargs in [{"reasoning": "max"}, {"max_output_tokens": 2000}, {"provider": "y"}]:
        options = {"model": "synthetic", "reasoning": "off", "max_output_tokens": 1000, "provider": "x", **kwargs}
        assert extraction_request(article, **options)["identity"] != first["identity"]


def test_pair_package_allowlist_preserves_full_source_and_study():
    a = deepcopy(FIXTURES[0]); b = deepcopy(a); b["article"]["pmid"] = "899999998"
    ae = validate_extraction(a["article"], a["expected_model_output"])["extraction"]
    be = validate_extraction(b["article"], b["expected_model_output"])["extraction"]
    a["article"]["predicted_label"] = "secret"
    item = pair_item("v2-pair-001", "diet", a["article"], ae, ae["claims"][0]["claim_id"],
                     b["article"], be, be["claims"][0]["claim_id"])
    assert set(item) == {"item_id", "task", "arm", "a", "b"}
    assert "study" in item["a"]["article"]
    assert "predicted_label" not in json.dumps(item)
    assert "fields" not in item["a"]["claim"]
    Draft202012Validator(PACKAGE_ITEM).validate(item)


def test_same_model_verification_drops_only_unsupported_and_never_rewrites():
    case = FIXTURES[0]
    extraction = validate_extraction(case["article"], case["expected_model_output"])["extraction"]
    verdict = {"claims": [{"claim_id": c["claim_id"], "support": support,
                           "contested_fields": ["comparison"], "evidence": [2]}
                          for c, support in zip(extraction["claims"], ["unsupported", "unclear"], strict=True)]}
    result = apply_verification(case["article"], extraction, verdict)
    assert result["original_extraction"] == extraction
    assert result["extraction"]["claims"] == extraction["claims"][1:]
    assert len(result["unclear_claim_ids"]) == 1
    verdict["claims"].append(deepcopy(verdict["claims"][0]))
    with pytest.raises(ValueError, match="every original claim once"):
        apply_verification(case["article"], extraction, verdict)


def test_renderer_keeps_n_scope_uncertainty_and_statistical_targets():
    for index in [2, 4, 5]:
        case = FIXTURES[index]
        extraction = validate_extraction(case["article"], case["expected_model_output"])["extraction"]
        display = json.dumps(render_claim_en(extraction["claims"][0], extraction["study"]))
        if index == 2:
            assert "120 participants; scope: enrolled" in display
            assert "42 participants; scope: analysed" in display
        if index == 4:
            assert "95% confidence interval: [-5, 1]" in display
            assert "p-value: Not extracted" in display
        if index == 5:
            assert "target: interaction with age" in display
            assert "Authors' significance: not_reported" in display


def test_zero_claim_verification_schema_is_valid_and_empty():
    case = deepcopy(FIXTURES[0])
    case["expected_model_output"]["claims"] = []
    extraction = validate_extraction(case["article"], case["expected_model_output"])["extraction"]
    schema = verify_request(case["article"], extraction)["schema"]
    Draft202012Validator.check_schema(schema)
    assert apply_verification(case["article"], extraction, {"claims": []})["extraction"] == extraction


def test_wire_schema_removes_only_unique_items_and_keeps_local_constraint():
    from chl_rag.claim_schema import WIRE_MODEL_EXTRACTION, wire_schema
    assert WIRE_MODEL_EXTRACTION == wire_schema(MODEL_EXTRACTION)
    assert 'uniqueItems' not in json.dumps(WIRE_MODEL_EXTRACTION)
    assert 'uniqueItems' in json.dumps(MODEL_EXTRACTION)
    case = deepcopy(FIXTURES[4])
    raw = case['expected_model_output']
    raw['claims'][0]['result']['significance_basis'] = ['confidence_interval', 'confidence_interval']
    Draft202012Validator(WIRE_MODEL_EXTRACTION).validate(raw)
    with pytest.raises(ValidationError, match='non-unique'):
        validate_extraction(case['article'], raw)
    assert json.loads(Path('configs/extraction_wire_schema.json').read_text()) == WIRE_MODEL_EXTRACTION


def test_renderer_preserves_every_semantic_value_in_existing_seven_rows():
    case = deepcopy(FIXTURES[0])
    case['expected_model_output']['study']['aims_reported'] = ['Measure two fictional outcomes']
    case['expected_model_output']['claims'][0]['exposure_class'] = 'Fictional food class'
    case['expected_model_output']['claims'][0]['result']['hedged'] = True
    extraction = validate_extraction(case['article'], case['expected_model_output'])['extraction']
    display = render_claim_en(extraction['claims'][0], extraction['study'])
    assert display['renderer_version'] == 'v2-en-3'
    assert len(display['rows']) == 7
    assert [r['field'] for r in display['rows']][-2:] == ['significance', 'context']
    text = '\n'.join(row['text'] for row in display['rows'])
    for fragment in ['Finding type: effect', 'Exposure class: Fictional food class', 'Hedged wording: Yes',
                     'Stated study aims: Measure two fictional outcomes', 'Reported statistical basis: Not extracted',
                     'Estimate: Not extracted']:
        assert fragment in text


def test_whitespace_equivalent_quotes_are_audited_without_repairing_raw_output():
    case = deepcopy(FIXTURES[0])
    original = case['article']['sections'][1]['text']
    case['article']['sections'][1]['text'] = original.replace('marker Alben', 'marker\u2009Alben')
    case['article']['abstract'] = case['article']['abstract'].replace('marker Alben', 'marker\u2009Alben')
    before = deepcopy(case['expected_model_output'])
    result = validate_extraction(case['article'], case['expected_model_output'])
    assert result['retained_claim_count'] == 2
    assert result['quote_match_diagnostics']
    assert all(row['match_kind'] == 'whitespace_only' for row in result['quote_match_diagnostics'])
    assert case['expected_model_output'] == before
    assert result['emitted_extraction']['claims'][0]['result']['evidence'][0]['quote'] == original


def test_extraction_request_uses_display_view_and_keeps_raw_validation():
    case = deepcopy(FIXTURES[0])
    case['article']['sections'][1]['text'] = case['article']['sections'][1]['text'].replace('marker Alben','marker\u2009Alben')
    request = extraction_request(case['article'])
    assert request['source_provenance']['changed_sentence_ids'] == [2]
    assert '\u2009' not in request['payload']['sentences'][1]['text']
    result = validate_extraction(case['article'], case['expected_model_output'])
    assert result['quote_match_diagnostics']
    assert result['source_provenance']['raw_source_sha256'] == request['source_provenance']['raw_source_sha256']
