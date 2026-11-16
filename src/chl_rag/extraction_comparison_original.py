"""Clear model-facing reference names; canonical comparison science stays unchanged."""
from __future__ import annotations

import json
import re
from copy import deepcopy
from pathlib import Path

from jsonschema import Draft202012Validator

from . import annotation_names_study_start as names
from . import extraction_comparison_first_site as canonical_comparison
from .io import canonical, digest, file_sha
from .instrument_study_start import SPEC_PATH, extraction_rules, load_instrument, render_claim_en

ROOT = Path(__file__).resolve().parents[2]
PROMPT_PATH = ROOT / 'configs/extraction_comparison_prompt_study_start.json'
VERSION = 'v2-final-extraction-comparison-5208dfa'
INVENTORY_DESCRIPTIONS = {
    'source_result_inventory':'Atomic reported source findings, derived from the full abstract rather than candidate coverage.',
    'atomic_results':'One entry per distinct source contrast/outcome/population/time result; no result/conclusion duplication.',
    'source_result_id':'Unique local ID for one source result, such as r001.',
    'source_text_anchors':'Literal source spans identifying the atomic contrast, with null for unstated optional anchors.',
    'source_text_anchors.exposure_or_condition':'Literal source wording identifying this atomic result\'s exposure or condition, not a correctness judgment.',
    'source_text_anchors.measured_outcome':'Literal source wording identifying this atomic result\'s measured outcome, not a correctness judgment.',
    'source_result_sentence_ids':'Sorted unique original eligible result sentence IDs supporting this atomic result.',
    'source_finding':'Short source-grounded statement of this atomic result.',
    'source_evidence':'Literal quotations from eligible result sentences, with their original IDs.',
    'source_sentence_id':'The original numbered abstract sentence, not a claim ID.',
    'source_quote':'An unchanged literal source span; only whitespace-format equivalence is permitted.',
    'shares_sentence_with_other_atomic_results':'True when a cited sentence states more than one distinct atomic result.',
    'comparison_contrast':'Literal reference/group/orientation wording for this result, or null if unstated.',
    'population_subgroup':'Literal subgroup/population scope of this result, or null if unstated.',
    'analysis_method':'Literal analysis wording for this result, or null if unstated.',
    'time_point':'Literal time or follow-up wording for this result, or null if unstated.',
    'candidate_extractions':'The two supplied extracted outputs; their agreement does not establish source truth.',
    'extraction_assessments':'Separate assessments of both opaque candidates against the same source inventory.',
    'paired_extraction_assessment':'Assess both supplied extractions in one call, without ranking or merging them.',
}


def instructions():
    original = canonical_comparison.instructions()
    prefix = original.split('{\n  "common_annotation_rules"',1)[0]
    replacements = {'matched_result_ids':'matched_source_result_ids', 'result_id':'source_result_id',
                    'multi_result_sentence':'shares_sentence_with_other_atomic_results',
                    'sign':'direction', 'significant':'authors_reported_statistical_significance',
                    'hedged':'stated_as_hypothesis', 'n':'sample_size'}
    for old,new in replacements.items():
        prefix = re.sub(r'\b'+re.escape(old)+r'\b',new,prefix)
    prefix = prefix.replace('comparison/result/study fields','comparison/finding_result/study fields')
    prefix = prefix.replace('additional diagnostics','additional structured-field checks').replace('Rich diagnostics','Rich structured-field checks')
    spec = load_instrument()
    common = extraction_rules(spec)
    common.pop('help')  # The human per-claim no-inventory help does not apply to this paired inventory task.
    common.pop('heading')
    return prefix + json.dumps({'common_annotation_rules':common,
        'oracle_field_descriptions':names.FIELD_DESCRIPTIONS,
        'inventory_and_comparison_field_descriptions':INVENTORY_DESCRIPTIONS,
        'model_input_name_descriptions':names.INPUT_DESCRIPTIONS,
        'general_rules':spec['general_rules'][:4]},ensure_ascii=False,indent=2) + (
        '\nUse only the model-facing names in the supplied schema. Canonical evaluator names are not output codes. '
        'Return only JSON. source_note is a short source-grounded comment, not step-by-step reasoning.\n')


def write_prompt(path=PROMPT_PATH):
    value = {'version':VERSION, 'reference_wording_revision':'site-5208dfa-2026-11-14-0415',
             'instrument_sha256':file_sha(SPEC_PATH),
             'legacy_schema_sha256':file_sha(canonical_comparison.LEGACY_SCHEMA_PATH), 'system':instructions()}
    Path(path).write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n')
    return value


def _wire_schema(schema):
    value = deepcopy(schema)
    value['properties']['task']['const'] = 'paired_extraction_assessment'
    value['properties']['source_result_inventory'] = names.rename_schema(value['properties'].pop('inventory'), names.INVENTORY_NAMES)
    variants = value['properties'].pop('variants')
    variants['properties'] = {alias:names.judgment_schema(v) for alias,v in variants['properties'].items()}
    value['properties']['extraction_assessments'] = variants
    value['required'] = [{'inventory':'source_result_inventory','variants':'extraction_assessments'}.get(k,k) for k in value['required']]
    return value


def prepare(item, baseline):
    original = canonical_comparison.prepare(item,baseline)
    stored = json.loads(PROMPT_PATH.read_text())
    expected = {'version':VERSION, 'reference_wording_revision':'site-5208dfa-2026-11-14-0415',
                'instrument_sha256':file_sha(SPEC_PATH),
                'legacy_schema_sha256':file_sha(canonical_comparison.LEGACY_SCHEMA_PATH), 'system':instructions()}
    if stored != expected:raise ValueError('Staged comparison prompt differs from reviewed names/instructions')
    payload = json.loads(original['prompt'])
    for alias,binding in original['comparison_map'].items():
        if binding['role']=='v2':
            originals = {c['claim_id']:c for c in item['claims']}
            payload['variants'][alias]['presentation'] = [{'claim_id':opaque,
                **render_claim_en(originals[canonical_id],item['article']['study'])}
                for opaque,canonical_id in binding['claims'].items()]
    payload['candidate_extractions'] = payload.pop('variants')
    payload['task'] = 'paired_extraction_assessment'
    model_view = names.model_input_view(payload)
    projection = names.projection_record(payload,model_view)
    return {**original, 'instructions':stored['system'], 'prompt':canonical(model_view).decode(),
            'schema':_wire_schema(original['schema']), 'instrument_sha256':stored['instrument_sha256'],
            'pair_instrument_sha256':load_instrument()['pair_source']['artifact_sha256'],
            'prompt_sha256':file_sha(PROMPT_PATH), 'comparison_wire_version':VERSION,
            'input_projection':projection,'input_projection_sha256':digest(projection),
            'model_view_sha256':digest(model_view)}


def response_to_canonical(response):
    return {**{k:deepcopy(v) for k,v in response.items() if k not in {'task','source_result_inventory','extraction_assessments'}},
            'task':'extraction_comparison',
            'inventory':names.rename_keys(response['source_result_inventory'], names.reverse(names.INVENTORY_NAMES)),
            'variants':{alias:{'claims':[names.claim_from_wire(c) for c in value['claim_assessments']]}
                        for alias,value in response['extraction_assessments'].items()}}


def response_from_canonical(response):
    """Offline fixture/compatibility helper, never repairs an actual oracle output."""
    return {**{k:deepcopy(v) for k,v in response.items() if k not in {'task','inventory','variants'}},
            'task':'paired_extraction_assessment',
            'source_result_inventory':names.rename_keys(response['inventory'],names.INVENTORY_NAMES),
            'extraction_assessments':{alias:{'claim_assessments':[names.claim_to_wire(c) for c in value['claims']]}
                                      for alias,value in response['variants'].items()}}


def normalize(response,item,baseline):
    request = prepare(item,baseline)
    Draft202012Validator(request['schema']).validate(response)
    answer = canonical_comparison.normalize(response_to_canonical(response),item,baseline)
    answer['comparison']['reference_wording_revision'] = 'site-5208dfa-2026-11-14-0415'
    answer['comparison']['wire_version'] = VERSION
    return answer
