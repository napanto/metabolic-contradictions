"""Final-oracle pair binding to site 61c08a1, using invented items only; no model or human data."""
import ast
import itertools
import json
from copy import deepcopy
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, ValidationError

from chl_rag import annotation_names_study_start as names
from chl_rag import site_instrument as instrument
from chl_rag import instrument_study_start as old_instrument
from chl_rag import pair_instrument as pair
from chl_rag import pair_instrument_study_start as old_pair
from chl_rag import reference_oracles as reference
from chl_rag.io import file_sha
from chl_rag.extraction import pair_item, validate_extraction

SITE = json.loads(Path('configs/site_instrument.json').read_text())
WORDING = SITE['wording']['en']


def _pair(mutate_b=None):
    case = json.loads(Path('configs/synthetic.json').read_text())['cases'][0]
    a = deepcopy(case['article']); b = deepcopy(a); b['pmid'] = '899999997'
    ea = validate_extraction(a, case['expected_model_output'])['extraction']
    eb = validate_extraction(b, case['expected_model_output'])['extraction']
    item = pair_item('v2-pair-001', ea['claims'][0]['arm'], a, ea, ea['claims'][0]['claim_id'],
                     b, eb, eb['claims'][0]['claim_id'])
    if mutate_b:
        mutate_b(item['b']['claim'])
    return item


@pytest.fixture
def item():
    return _pair()


def _between(claim):
    """Side B becomes a between-group comparison with a cohort-free case/control pair."""
    comp = claim['comparison']
    comp['type'] = 'between_group'
    comp['index_group']['role'] = 'case'
    comp['reference_group'] = {'label': 'healthy controls', 'population': 'adults without diabetes',
                               'role': 'control', 'n': None, 'evidence': []}


def yes_rows():
    return {side: {field: {'answer': 'yes', 'problems': []} for field in pair.FIELDS} for side in ('a', 'b')}


def response(path, item_id='v2-pair-001', rationale=''):
    return {'instrument_version': '2.0', 'pipeline_version': '2.0', 'item_id': item_id, 'task': 'pair',
            'path': path, 'rationale': rationale}


def routes():
    """Every terminal route of the final instrument, with options the page offers on the fixture."""
    q2_yes = {field: {'answer': 'yes'} for field in instrument.Q2_KEYS}
    out = []
    # Q1 problems and relevance.
    for side, field, problem in [('a', 'result', 'wrong_direction'), ('b', 'result', 'planned_as_observed'),
                                 ('a', 'comparison', 'wrong_value'), ('b', 'groups', 'wrong_group_time'),
                                 ('a', 'sample_size', 'missing_from_extraction'),
                                 ('b', 'analysis', 'missing_from_extraction'),
                                 ('a', 'significance', 'not_supported_by_abstract'),
                                 ('b', 'context', 'missing_from_extraction')]:
        rows = yes_rows(); rows[side][field] = {'answer': 'no', 'problems': [problem]}
        out.append({'q1': rows, 'q1_relevance': 'material'})
        if field in instrument.CORE_FIELDS:
            out.append({'q1': deepcopy(rows), 'q1_relevance': 'cannot_establish_comparison'})
        out.append({'q1': deepcopy(rows), 'q1_relevance': 'not_material', 'q2': deepcopy(q2_yes), 'q3': 'yes'})
    rows = yes_rows(); rows['b']['comparison'] = {'answer': 'cannot_tell', 'problems': []}
    out.append({'q1': rows, 'q1_relevance': 'cannot_establish_comparison'})
    rows = yes_rows(); rows['a']['groups'] = {'answer': 'cannot_tell', 'problems': []}
    out.append({'q1': rows, 'q1_relevance': 'not_material', 'q2': deepcopy(q2_yes), 'q3': 'cannot_tell'})
    # Q2 failures with every offered reason, single and multiple.
    failures = {'exposure': [{'answer': 'no', 'reason': 'different_exposure'},
                             {'answer': 'no', 'reason': 'molecule_class'}, {'answer': 'cannot_tell'}],
                'outcome': [{'answer': 'no', 'reason': 'outcome_definition'}, {'answer': 'cannot_tell'}],
                'comparison': [{'answer': 'no', 'reason': 'comparator'},
                               {'answer': 'no', 'reason': 'other', 'detail': 'baseline versus week 12'},
                               {'answer': 'no', 'reason': 'outcome_definition'}, {'answer': 'cannot_tell'}]}
    for field, options in failures.items():
        for option in options:
            q2 = deepcopy(q2_yes); q2[field] = option
            out.append({'q1': yes_rows(), 'q2': q2})
    q2 = deepcopy(q2_yes); q2['exposure'] = failures['exposure'][0]; q2['comparison'] = failures['comparison'][0]
    for primary in ('exposure', 'comparison'):
        out.append({'q1': yes_rows(), 'q2': deepcopy(q2), 'q2_primary': primary})
    # Q3, Q4, Q5.
    out.append({'q1': yes_rows(), 'q2': deepcopy(q2_yes), 'q3': 'yes'})
    out.append({'q1': yes_rows(), 'q2': deepcopy(q2_yes), 'q3': 'cannot_tell'})
    for q4 in ('effect_vs_no_effect', 'size_only', 'insufficient'):
        out.append({'q1': yes_rows(), 'q2': deepcopy(q2_yes), 'q3': 'no', 'q4': q4})
    for answer in ('no', 'cannot_tell'):
        out.append({'q1': yes_rows(), 'q2': deepcopy(q2_yes), 'q3': 'no', 'q4': 'opposite', 'q5': {'answer': answer}})
    for factor in pair.FACTORS:
        q5 = {'answer': 'yes', 'factor': factor}
        if factor == 'other':
            q5['detail'] = 'different follow-up'
        out.append({'q1': yes_rows(), 'q2': deepcopy(q2_yes), 'q3': 'no', 'q4': 'opposite', 'q5': q5})
    return out


# --- binding and provenance ------------------------------------------------------------------------


def test_spec_binds_final_site_export_validator_presentation_and_guidelines():
    spec = instrument.load_instrument()
    source = spec['pair_source']
    assert source['commit'] == '61c08a1e77824b39cd7d4f3f1f27ec7d5682fa20'
    assert source['tag'] == 'v2.1-layout-20261115'
    assert file_sha('configs/site_instrument.json') == \
        'dfe916b7cc795efedfbc60ea0f2e8be44ff91c41f74f56777bb9edde840b1eb3'
    assert file_sha(source['presentation']) == source['site_presentation_sha256']
    assert [row[0] for row in spec['pair_wording']['rows']] == list(pair.DISPLAY_ORDER)
    assert spec == instrument.build_spec()


def test_vendored_validator_logic_is_identical_to_5208dfa():
    def definitions(path):
        tree = ast.parse(Path(path).read_text())
        out = {}
        for node in tree.body:
            if isinstance(node, ast.FunctionDef):
                out[node.name] = ast.dump(node)
            elif isinstance(node, ast.Assign):
                out[node.targets[0].id] = ast.dump(node.value)
        return out
    new = definitions('src/chl_rag/pair_instrument.py')
    old = definitions('src/chl_rag/pair_instrument_study_start.py')
    assert new.pop('DISPLAY_ORDER')
    assert new == old
    tree = ast.parse(Path('src/chl_rag/pair_instrument.py').read_text())
    assert [x.names[0].name for x in tree.body if isinstance(x, ast.Import)] == ['json']
    assert not any(isinstance(x, ast.ImportFrom) for x in tree.body)


def test_guidelines_drop_only_the_participant_only_paragraph():
    text = Path('configs/site_guidelines_en.md').read_text()
    kept = instrument.load_instrument()['pair_scientific_guidelines']
    assert 'without AI tools' not in kept and '## Saving and taking a break' not in kept
    assert kept == text.split('## Saving and taking a break')[0].replace(instrument.GUIDELINE_PARTICIPANT_ONLY, '')
    assert '**General rule.**' in kept and '“Study population” and “Comparison group” always appear' in kept


# --- prompt -----------------------------------------------------------------------------------------


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for child in value.values():
            yield from _strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from _strings(child)


def test_prompt_contains_every_site_question_option_and_rule():
    system = json.loads(instrument.PROMPT_PATH.read_text())['system']
    assert system == instrument.oracle_instructions()
    for key in instrument.SUBSTANTIVE_WORDING:
        for text in _strings(WORDING[key]):
            assert json.dumps(text, ensure_ascii=False)[1:-1] in system, (key, text[:60])
    for check in WORDING['checks'].values():
        assert json.dumps(check['question'], ensure_ascii=False)[1:-1] in system
    for options in ('relevance_options', 'q4_options', 'factors', 'problems', 'comparison_reasons', 'exposure_reasons'):
        for code, label in WORDING[options]:
            assert json.dumps(label, ensure_ascii=False)[1:-1] in system
    for rule in instrument.PAIR_PATH_RULES:
        assert json.dumps(rule, ensure_ascii=False)[1:-1] in system


def test_prompt_applies_only_the_three_approved_reductions():
    system = instrument.oracle_instructions()
    body = json.loads(system[system.index('{'):system.rindex('}') + 1])
    old = json.loads(Path('configs/oracle_prompt_study_start.json').read_text())['system']
    old_body = json.loads(old[old.index('{'):old.rindex('}') + 1])
    assert 'extraction' not in body and 'extraction' in old_body
    assert '\n  ' not in system[system.index('{'):system.rindex('}')]
    assert 'claim_assessments' not in body['oracle_field_descriptions']
    assert set(body['oracle_field_descriptions']) == {'instrument_version', 'pipeline_version', 'item_id', 'task'}
    assert 'When the supplied item.task is extraction' not in system
    assert body['pair_guidelines'] == instrument.load_instrument()['pair_scientific_guidelines']
    assert body['extraction_general_rules'] == old_body['extraction_general_rules']
    assert body['missing_detail_display'] == old_body['missing_detail_display']
    for key, text in old_body['model_input_name_descriptions'].items():
        if key != 'extraction':
            assert body['model_input_name_descriptions'][key] == text


# --- presentation -------------------------------------------------------------------------------------


def test_presentation_follows_display_order_fixed_slots_and_site_labels(item):
    view = instrument.render_presentation(item)
    for side in ('a', 'b'):
        assert [c['check'] for c in view['abstract_' + side]['checks']] == list(pair.DISPLAY_ORDER)
    checks = {c['check']: c for c in view['abstract_a']['checks']}
    groups = checks['groups']['values']
    assert [p.get('heading') for p in groups] == ['Study population', 'Comparison group', None]
    assert 'Name used in the study: Nuvora cohort (cohort)' in groups[0]['lines']
    assert groups[1]['lines'] == ['Population: None: the extraction gives a change within one group']
    assert checks['sample_size']['values'][0]['lines'][2] == \
        'Comparison group: None: the extraction gives a change within one group'
    assert checks['result']['finding_sentence'].startswith('In Nuvora cohort, marker Alben decreased')
    assert checks['analysis']['all_values_missing'] is True
    assert checks['analysis']['problem_codes_offered'] == ['missing_from_extraction']
    assert checks['result']['problem_codes_offered'] == list(pair.RESULT_PROBLEMS)
    assert checks['context']['problem_codes_offered'] == list(pair.COMMON_PROBLEMS)


def test_presentation_shows_every_site_value_and_role(item):
    item = _pair(_between)
    views = instrument.site_views(item)
    view = instrument.render_presentation(item)
    for side in ('a', 'b'):
        rendered = json.dumps(view['abstract_' + side], ensure_ascii=False)
        for section in views[side]['sections']:
            for row in section['rows']:
                assert json.dumps(instrument._plain(row['label']) + ': ' + instrument._value(row),
                                  ensure_ascii=False)[1:-1] in rendered
    groups = {c['check']: c for c in view['abstract_b']['checks']}['groups']['values']
    assert 'Name used in the study: Nuvora cohort (cases)' in groups[0]['lines']
    assert 'Name used in the study: healthy controls (controls)' in groups[1]['lines']
    direction = {c['check']: c for c in view['abstract_b']['checks']}['result']['values'][0]['lines'][2]
    assert direction == 'Direction of the estimate: lower in the study population than in the comparison group'
    names_row = next(line for line in view['comparison_tables']['context']['lines']
                     if line['label'] == 'Name used in the study' and line['a'] == 'This kind of finding has no comparison group')
    assert names_row['b'] == '[Comparison group] healthy controls (controls)'


def test_model_payload_keeps_public_item_and_adds_no_private_or_predicted_data(item):
    request = instrument.oracle_request(item)
    payload = json.loads(request['prompt'])
    assert names.restore_model_input(payload['item']) == item
    keys = set()

    def visit(value):
        if isinstance(value, dict):
            keys.update(value); [visit(v) for v in value.values()]
        elif isinstance(value, list):
            [visit(v) for v in value]
    visit(payload)
    assert not keys & reference.PRIVATE_KEYS
    text = request['prompt']
    for forbidden in ('example_paths', 'binary_inconsistent', '"label":"contradictory"', 'student', 'human_labels'):
        assert forbidden not in text
    leaked = deepcopy(item); leaked['a']['claim']['student_answers'] = []
    with pytest.raises(ValueError):
        instrument.prepare_reference(leaked)
    prepared = instrument.prepare_reference(item)
    assert prepared['human_answers_in_prompt'] is False and prepared['wire_schema'] == request['wire_schema']


# --- strict wire and golden routes ------------------------------------------------------------------


def test_wire_schema_is_strict_and_canonical_schema_unchanged_in_shape(item):
    wire = instrument.pair_wire_schema(item['item_id'])
    assert instrument.strict_schema_issues(wire) == []
    Draft202012Validator.check_schema(wire)
    old = old_instrument.pair_response_schema(item['item_id'])
    new = instrument.pair_response_schema(item['item_id'])
    old['properties']['path']['properties']['q2']['properties']['comparison']['properties']['reason']['enum'] = \
        list(instrument.COMPARISON_REASONS)
    assert new == old


def test_every_route_round_trips_through_the_wire_with_the_site_derivation(item):
    seen = set()
    for path in routes():
        canonical = response(path, rationale='short note')
        expected = pair.validate_path(deepcopy(path), 'short note')
        assert expected == old_pair.validate_path(deepcopy(path), 'short note')
        wire = instrument.encode_response(canonical, item)
        assert set(wire['path']) == {'q1', 'q1_relevance', 'q2', 'q2_primary', 'q3', 'q4', 'q5'}
        assert json.loads(json.dumps(wire)) == wire
        record = instrument.annotation_from_response(wire, item)
        for key in ('label', 'factor', 'binary_inconsistent', 'field_problems', 'relevance', 'other_detail', 'path'):
            assert record[key] == expected[key]
        seen.add(record['label'])
    assert seen == set(instrument.CATEGORIES)


def test_site_example_paths_round_trip(item):
    for label, path in SITE['example_paths'].items():
        record = instrument.annotation_from_response(instrument.encode_response(response(path), item), item)
        assert record['label'] == label


def test_page_offer_rules_reject_choices_the_page_hides(item):
    rows = yes_rows(); rows['a']['groups'] = {'answer': 'no', 'problems': ['wrong_value']}
    with pytest.raises(ValueError, match='offered only after a Finding or Comparison'):
        instrument.validate_response(response({'q1': rows, 'q1_relevance': 'cannot_establish_comparison'}), item)
    pair.validate_path({'q1': rows, 'q1_relevance': 'cannot_establish_comparison'})  # the server accepts it
    rows = yes_rows(); rows['b']['analysis'] = {'answer': 'no', 'problems': ['wrong_value']}
    with pytest.raises(ValueError, match='problem codes not offered'):
        instrument.validate_response(response({'q1': rows, 'q1_relevance': 'material'}), item)
    q2 = {field: {'answer': 'yes'} for field in instrument.Q2_KEYS}
    q2['comparison'] = {'answer': 'no', 'reason': 'population'}
    pair.validate_path({'q1': yes_rows(), 'q2': q2})  # the server accepts it
    with pytest.raises(ValidationError):
        instrument.validate_response(response({'q1': yes_rows(), 'q2': q2}), item)


def test_wire_rejects_filled_unvisited_branches_and_missing_keys(item):
    wire = instrument.encode_response(response({'q1': yes_rows(), 'q2': {f: {'answer': 'yes'} for f in instrument.Q2_KEYS},
                                                'q3': 'yes'}), item)
    stale = deepcopy(wire); stale['path']['q4'] = 'opposite'
    with pytest.raises(ValueError):
        instrument.annotation_from_response(stale, item)
    missing = deepcopy(wire); del missing['path']['q5']
    with pytest.raises(ValidationError):
        instrument.annotation_from_response(missing, item)
    category = deepcopy(wire); category['label'] = 'contradictory'
    with pytest.raises(ValidationError):
        instrument.annotation_from_response(category, item)


# --- the earlier binding is untouched ------------------------------------------------------------


def test_5208dfa_artifacts_still_validate(item):
    old_instrument.load_instrument()
    request = old_instrument.oracle_request(item)
    assert request['instructions'] == json.loads(Path('configs/oracle_prompt_study_start.json').read_text())['system']
    path = SITE['example_paths']['contradictory']
    assert old_instrument.annotation_from_response(response(path), item)['label'] == 'contradictory'


def test_route_enumeration_covers_all_offered_options():
    paths = routes()
    reasons = {p['q2']['comparison'].get('reason') for p in paths if 'q2' in p}
    assert set(instrument.COMPARISON_REASONS) <= reasons
    factors = {p['q5'].get('factor') for p in paths if 'q5' in p}
    assert set(pair.FACTORS) <= factors
    relevance = {p.get('q1_relevance') for p in paths}
    assert {'material', 'not_material', 'cannot_establish_comparison'} <= relevance
    assert len(list(itertools.chain(paths))) >= 50


def test_every_frozen_handoff_pair_renders_and_prepares():
    """Public v2.1 package items only (no answers): every site row reaches the model view."""
    items = [json.loads(line) for line in Path('annotation/study_pairs/items.jsonl').read_text().splitlines()]
    assert len(items) == 36
    for frozen in items:
        prepared = instrument.prepare_reference(frozen)
        view = json.loads(prepared['prompt'])['presentation']
        views = instrument.site_views(frozen)
        for side in ('a', 'b'):
            assert [c['check'] for c in view['abstract_' + side]['checks']] == list(pair.DISPLAY_ORDER)
            rendered = json.dumps(view['abstract_' + side], ensure_ascii=False)
            for section in views[side]['sections']:
                for row in section['rows']:
                    assert json.dumps(instrument._plain(row['label']) + ': ' + instrument._value(row),
                                      ensure_ascii=False)[1:-1] in rendered
        assert instrument.strict_schema_issues(prepared['wire_schema']) == []
