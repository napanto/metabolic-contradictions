"""Reference-only naming parity, using invented items and no real oracle/human data."""
import ast
import itertools
import json
from copy import deepcopy
from pathlib import Path

import pytest
from jsonschema import ValidationError

from chl_rag import annotation_names_study_start as names
from chl_rag import extraction_comparison_study_start as comparison
from chl_rag import extraction_comparison_first_site as old_comparison
from chl_rag import instrument_study_start as instrument
from chl_rag import instrument_first_site as old_instrument
from chl_rag import pair_instrument_study_start as pair
from chl_rag import pair_instrument_first_site as old_pair
from chl_rag.io import digest, file_sha
from chl_rag.prototype_baseline import final_d0_from_checkpoint
from chl_rag.extraction import extraction_item, pair_item, validate_extraction


@pytest.fixture
def examples():
    case=json.loads(Path('configs/synthetic.json').read_text())['cases'][0]
    a=deepcopy(case['article']);b=deepcopy(a);b['pmid']='899999997'
    ea=validate_extraction(a,case['expected_model_output'])['extraction']
    eb=validate_extraction(b,case['expected_model_output'])['extraction']
    arm=ea['claims'][0]['arm']
    extracted=extraction_item('v2-extraction-001',arm,a,ea)
    paired=pair_item('v2-pair-001',arm,a,ea,ea['claims'][0]['claim_id'],b,eb,eb['claims'][0]['claim_id'])
    claim=ea['claims'][0]
    raw={'arm':arm,'exposure':claim['exposure'],'exposure_class':claim['exposure_class'],
         'outcome':claim['outcome'],'kind':claim['kind'],'sign':'-','significant':None,'hedged':False,
         'comparator':None,'context':dict.fromkeys(('population','n','design','region','duration','dose','sample')),
         'evidence':[extracted['article']['sentences'][-1]['id']]}
    baseline=final_d0_from_checkpoint({'pmid':a['pmid'],'status':'ok','claims':[raw],
                                      'raw_claim_count':1,'dropped':[]},a['pmid'])
    return extracted,paired,baseline


def canonical_answer(item):
    return {'instrument_version':'2.0','pipeline_version':'2.0','task':'extraction','item_id':item['item_id'],
      'claims':[{'claim_id':c['claim_id'],'fields':dict.fromkeys(instrument.COMMON_FIELDS,'correct'),
        'support':'supported','diagnostics':dict.fromkeys(instrument.DIAGNOSTICS,'correct'),
        'diagnostic_problems':{k:[] for k in instrument.DIAGNOSTICS},'note':'unsupported'} for c in item['claims']]}


def new_path(path):
    out=deepcopy(path)
    for rows in out['q1'].values():
        for row in rows.values():row['problems']=[names.PAIR_RENAMES.get(x,x) for x in row['problems']]
    if 'q1_relevance' in out:out['q1_relevance']=names.PAIR_RENAMES.get(out['q1_relevance'],out['q1_relevance'])
    if 'q4' in out:out['q4']=names.PAIR_RENAMES.get(out['q4'],out['q4'])
    for field,row in out.get('q2',{}).items():
        if field=='comparison' and row.get('reason')=='sample':row['reason']='biological_sample'
    if out.get('q5',{}).get('factor')=='sample':out['q5']['factor']='biological_sample'
    return out


def base_path():
    return {'q1':{side:{field:{'answer':'yes','problems':[]} for field in old_pair.FIELDS} for side in ('a','b')}}


def route_cases():
    """Every downstream branch/reason and every legal single-field problem subset."""
    base=base_path()
    options={
      'exposure':[{'answer':'yes'},{'answer':'cannot_tell'}, *({'answer':'no','reason':v} for v in ('different_exposure','molecule_class'))],
      'outcome':[{'answer':'yes'},{'answer':'cannot_tell'},{'answer':'no','reason':'outcome_definition'}],
      'comparison':[{'answer':'yes'},{'answer':'cannot_tell'}, *({'answer':'no','reason':v,**({'detail':'Synthetic difference'} if v=='other' else {})} for v in old_pair.FACTORS)]}
    for values in itertools.product(*(options[k] for k in ('exposure','outcome','comparison'))):
        q2=dict(zip(('exposure','outcome','comparison'),values,strict=True))
        failures=[k for k,v in q2.items() if v['answer']!='yes']
        if failures:
            for primary in failures:yield {**deepcopy(base),'q2':deepcopy(q2),'q2_primary':primary}
    q2={k:{'answer':'yes'} for k in ('exposure','outcome','comparison')}
    yield {**deepcopy(base),'q2':deepcopy(q2),'q3':'yes'}
    yield {**deepcopy(base),'q2':deepcopy(q2),'q3':'cannot_tell'}
    for q4 in ('no_effect','size_only','insufficient'):
        yield {**deepcopy(base),'q2':deepcopy(q2),'q3':'no','q4':q4}
    for q5 in [{'answer':'no'},{'answer':'cannot_tell'}, *({'answer':'yes','factor':v,**({'detail':'Synthetic difference'} if v=='other' else {})} for v in old_pair.FACTORS)]:
        yield {**deepcopy(base),'q2':deepcopy(q2),'q3':'no','q4':'opposite','q5':q5}
    for side in ('a','b'):
        for field in old_pair.FIELDS:
            allowed=old_pair.RESULT_PROBLEMS if field=='result' else old_pair.COMMON_PROBLEMS
            for size in range(1,len(allowed)+1):
                for problems in itertools.combinations(allowed,size):
                    value=deepcopy(base);value['q1'][side][field]={'answer':'no','problems':list(problems)}
                    yield {**deepcopy(value),'q1_relevance':'material'}
                    yield {**deepcopy(value),'q1_relevance':'cannot_establish'}
                    yield {**deepcopy(value),'q1_relevance':'supporting','q2':deepcopy(q2),'q3':'yes'}
            uncertain=deepcopy(base);uncertain['q1'][side][field]={'answer':'cannot_tell','problems':[]}
            yield {**uncertain,'q1_relevance':'cannot_establish'}
    for a,b in itertools.product(old_pair.FIELDS,repeat=2):
        mixed=deepcopy(base)
        mixed['q1']['a'][a]={'answer':'no','problems':['wrong_value']}
        mixed['q1']['b'][b]={'answer':'cannot_tell','problems':[]}
        yield {**mixed,'q1_relevance':'material'}


def test_every_reachable_branch_and_problem_subset_preserves_category_factor():
    count=0
    for path in route_cases():
        old=old_pair.validate_path(path);new=pair.validate_path(new_path(path))
        for field in ('label','factor','binary_inconsistent','other_detail'):
            assert old[field]==new[field],(field,path)
        assert new['path']==new_path(old['path'])
        assert new['relevance']==names.PAIR_RENAMES.get(old['relevance'],old['relevance'])
        count+=1
    assert count>1500


def test_old_pair_answer_codes_are_rejected():
    cases=list(route_cases())
    def contains(value,code):
        if isinstance(value,dict):return any(contains(x,code) for x in value.values())
        if isinstance(value,list):return any(contains(x,code) for x in value)
        return value==code
    for old in names.PAIR_RENAMES:
        path=next(p for p in cases if contains(p,old))
        # An unconverted legacy path cannot remain valid merely because its meaning is familiar.
        with pytest.raises(ValueError):pair.validate_path(path)
        assert pair.validate_path(new_path(path))['label']==old_pair.validate_path(path)['label']


def test_exact_site_export_and_substantive_english_questions_are_in_prompt():
    assert file_sha('configs/site_instrument_study_start.json')=='9ad34d2a75ebae868af7532122427982edd964166eecf41c4fe47a84eca44873'
    site=json.loads(Path('configs/site_instrument_study_start.json').read_text())
    spec=instrument.load_instrument();text=instrument.oracle_instructions(spec)
    assert spec['pair_wording']==site['wording']['en']
    substantive=json.JSONDecoder().raw_decode(text[text.index('\n\n{')+2:])[0]
    for key in instrument.SUBSTANTIVE_WORDING:
        assert substantive['pair'][key]==site['wording']['en'][key]
    for row in site['wording']['en']['checks'].values():
        for key in ('question','how','help','example'):
            assert row[key] in text
    assert 'example_paths' not in text and 'expected_model_output' not in text
    assert 'Stated as a hypothesis' in text


def test_new_site_export_row_keeps_legacy_biological_factor(examples):
    _,item,_=examples
    path=next(new_path(p) for p in route_cases() if p.get('q5',{}).get('factor')=='sample')
    derived=pair.validate_path(path)
    row={**derived,'item_id':item['item_id'],'pipeline_version':'2.0','preview':0}
    assert row['path']['q5']['factor']=='biological_sample' and row['factor']=='sample'
    assert instrument.validate_pair_export(row,item)['factor']=='sample'


def test_model_extraction_names_roundtrip_and_legacy_wire_rejected(examples):
    item,_,_=examples;canonical=canonical_answer(item)
    canonical['claims'][0]['diagnostics']['analysis']='incorrect'
    canonical['claims'][0]['diagnostic_problems']['analysis']=['unsupported','missing_detail']
    wire=names.extraction_to_wire(canonical)
    assert instrument.annotation_from_response(wire,item)==canonical
    assert wire['claim_assessments'][0]['source_note']=='unsupported'  # free text is never rewritten
    with pytest.raises(ValidationError):instrument.validate_response(canonical,item)
    wrong=deepcopy(wire);wrong['claim_assessments'][0]['source_support']='unsupported'
    with pytest.raises(ValidationError):instrument.validate_response(wrong,item)
    assert instrument.COMMON_FIELDS==old_instrument.COMMON_FIELDS
    assert instrument.DIAGNOSTICS==old_instrument.DIAGNOSTICS


def test_human_author_keys_are_canonical_but_problem_codes_are_new(examples):
    item,_,_=examples;raw=canonical_answer(item)
    raw['claims'][0]['diagnostics']['analysis']='incorrect'
    raw['claims'][0]['diagnostic_problems']['analysis']=['not_supported_by_abstract','missing_from_extraction']
    canonical=instrument.annotation_from_human_response(raw,item)
    assert canonical['claims'][0]['fields'].keys()==raw['claims'][0]['fields'].keys()
    assert canonical['claims'][0]['diagnostic_problems']['analysis']==['unsupported','missing_detail']
    assert instrument.validate_human_response(raw,item)==raw
    with pytest.raises(ValidationError):instrument.validate_human_response(canonical,item)


def test_input_views_are_reversible_and_latest_visible_labels_are_used(examples):
    for item in examples[:2]:
        before=deepcopy(item);request=instrument.oracle_request(item);payload=json.loads(request['prompt'])
        assert names.restore_model_input(payload['item'])==item==before
        assert request['raw_public_item_sha256']==digest(item)
        assert request['input_projection']['values_unchanged']
        assert 'Hedged wording:' not in request['prompt']
        assert 'Stated as a hypothesis:' in request['prompt']
        assert '"summary"' not in request['prompt']
        assert '"kind"' not in request['prompt'] and '"sign"' not in request['prompt']


def test_identifier_enums_are_not_reinterpreted_as_quality_codes():
    schema={'type':'object','properties':{'claim_id':{'type':'string','enum':['correct']}}}
    assert names.judgment_schema(schema)['properties']['claim_id']['enum']==['correct']


def test_same_call_comparison_preserves_canonical_answers(examples):
    item,_,baseline=examples
    original=old_comparison.prepare(item,baseline)
    raw={'instrument_version':'2.0','pipeline_version':'2.0','item_id':item['item_id'],
         'task':'extraction_comparison','inventory':{'results':[]},'variants':{}}
    canonical=canonical_answer(item)
    for alias,mapping in original['comparison_map'].items():
        rows=[]
        for opaque in mapping['claims']:
            row=deepcopy(canonical['claims'][0]);row['claim_id']=opaque;row['matched_result_ids']=[]
            if mapping['role']=='D0':row.pop('diagnostics');row.pop('diagnostic_problems')
            rows.append(row)
        raw['variants'][alias]={'claims':rows}
    wire=comparison.response_from_canonical(raw)
    assert comparison.response_to_canonical(wire)==raw
    old=old_comparison.normalize(raw,item,baseline);new=comparison.normalize(wire,item,baseline)
    assert old['claims']==new['claims'] and old['comparison']['variants']==new['comparison']['variants']
    request=comparison.prepare(item,baseline)
    assert request['input_projection']['values_unchanged']
    assert '"kind"' not in request['prompt'] and '"sample"' not in request['prompt']
    with pytest.raises(ValidationError):comparison.normalize(raw,item,baseline)


def test_vendored_validator_has_no_runtime_dependencies_beyond_json():
    tree=ast.parse(Path('src/chl_rag/pair_instrument_study_start.py').read_text())
    assert [x.names[0].name for x in tree.body if isinstance(x,ast.Import)]==['json']
    assert not any(isinstance(x,ast.ImportFrom) for x in tree.body)


def test_human_render_keeps_public_data_keys_and_latest_labels(examples):
    item=examples[1]
    payload=instrument.render_human_item(item)
    assert payload['item']==item
    assert 'kind' in payload['item']['a']['claim']
    assert payload['presentation']['a']['rows'][0]['label']=='Finding'
    assert 'Stated as a hypothesis:' in payload['presentation']['a']['rows'][0]['text']


def test_normalized_validator_ast_matches_except_explicit_factor_alias():
    class Normalize(ast.NodeTransformer):
        def visit_Constant(self,node):
            if isinstance(node.value,str):node.value=names.PAIR_RENAMES.get(node.value,node.value)
            return node
    def definitions(path,normalize=False):
        tree=ast.parse(Path(path).read_text())
        if normalize:tree=Normalize().visit(tree)
        result={}
        for node in tree.body:
            if isinstance(node,ast.FunctionDef) and node.name!='_factor':
                if node.body and isinstance(node.body[0],ast.Expr) and isinstance(node.body[0].value,ast.Constant):node.body=node.body[1:]
                result[node.name]=ast.dump(node,include_attributes=False)
        return result
    assert definitions('src/chl_rag/pair_instrument_first_site.py',True)==definitions('src/chl_rag/pair_instrument_study_start.py')
    for factor in old_pair.FACTORS:
        detail='Synthetic difference' if factor=='other' else ''
        assert old_pair._factor(factor,detail)==pair._factor(names.PAIR_RENAMES.get(factor,factor),detail)


def test_generated_label_replacement_never_rewrites_extracted_strings(examples):
    item=deepcopy(examples[0]);claim=item['claims'][0]
    claim['comparison']['orientation_text']='Region: north; Hedged wording: quoted phrase; Sample: plasma'
    claim['exposure_class']='Hedged wording: an extracted class name'
    claim['context']['region']='Region: the study location'
    claim['context']['sample']='Sample: plasma; Hedged wording: a source phrase'
    claim['context']['dose']='Region: text inside a dose field'
    shown=instrument.render_claim_en(claim,item['article']['study'])
    assert claim['comparison']['orientation_text'] in shown['summary']
    assert claim['exposure_class'] in shown['summary']
    assert 'Stated as a hypothesis: No.' in shown['summary']
    context=next(row['text'] for row in shown['rows'] if row['field']=='context')
    for key in ('region','sample','dose'):assert claim['context'][key] in context
    assert 'Location: Region: the study location' in context
    assert 'Biological material: Sample: plasma' in context
