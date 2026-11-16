"""No-network, synthetic tests for independent v2 judging and cost settlement."""
import json
from copy import deepcopy
from pathlib import Path

import httpx
import pytest

from chl_rag import judges as judges
from chl_rag.io import digest, file_sha, write_json


@pytest.fixture
def article():
    return {"pmid": "synthetic-001", "title": "Synthetic only",
            "sentences": [{"id": 1, "text": "Treatment X reduced blood glucose in adults.",
                           "section": "RESULTS", "eligible": True}],
            "private_labels": "must not reach the model", "model": "hidden"}


@pytest.fixture
def inventory(article):
    evidence = [{"sentence_id": 1, "quote": article["sentences"][0]["text"]}]
    return {"schema_version": "2.0", "pmid": article["pmid"], "results": [{
        "result_id": "r001", "anchors": {"result_sentence_ids": [1],
            "exposure": "Treatment X", "outcome": "blood glucose", "comparison": None,
            "subgroup": "adults", "analysis": None, "time": None},
        "statement": "Treatment X reduced blood glucose in adults.",
        "multi_result_sentence": False, "evidence": evidence,
        "field_details": {**{key: [] for key in judges.GROUPS}, "population": [
            {"detail_id": "r001:population:001", "text": "adults", "evidence": evidence, "metadata_evidence": []}]}}]}


@pytest.fixture
def authorization(tmp_path):
    protocol = tmp_path / 'protocol.md'
    protocol.write_text('Synthetic registered fixture, not a study protocol.')
    config = tmp_path / 'development.json'
    write_json(config, {'development_judging_authorized': True})
    return {"partition": "P", "development_config_path": str(config), "development_amendment_path": str(protocol),
            "development_amendment_sha256": file_sha(protocol),
            "registered_at": "2026-11-13T18:00:00+01:00"}


def keyfile(tmp_path):
    path = tmp_path / 'anthropic.env'
    path.write_text('ANTHROPIC_API_KEY=synthetic-key-never-real\n')
    path.chmod(0o600)
    return path


class Ledger:
    def __init__(self):
        self.events = []
    def reserve(self, amount, request_hash, model, metadata=None):
        self.events.append(('reserve', amount, request_hash, model))
        return 'reservation-1'
    def settle(self, reservation, cost, **kwargs):
        self.events.append(('settle', reservation, cost, kwargs))
    def uncertain(self, reservation, reason):
        self.events.append(('uncertain', reservation, reason))
    def release(self, reservation, reason):
        self.events.append(('release', reservation, reason))


def stream(answer, *, stop='end_turn', complete=True, model=judges.FABLE_MODEL):
    events = [{'type': 'message_start', 'message': {'model': model,
        'usage': {'input_tokens': 20, 'output_tokens': 1, 'cache_creation_input_tokens': 10,
                  'cache_read_input_tokens': 40}}},
        {'type': 'content_block_delta', 'index': 0, 'delta': {'type':'thinking_delta','thinking':'Synthetic hidden thought.'}},
        {'type': 'content_block_delta', 'index': 1, 'delta': {'type':'text_delta','text':json.dumps(answer)}},
        {'type': 'message_delta', 'delta': {'stop_reason':stop}, 'usage': {'output_tokens':100}}]
    if complete:
        events.append({'type':'message_stop'})
    return ''.join('data: '+json.dumps(event)+'\n\n' for event in events)


def test_source_inventory_is_blind_to_any_candidate_or_private_metadata(article):
    job=judges.prepare_inventory(article)
    assert set(job.payload)=={'source'}
    text=json.dumps(job.payload)
    assert 'private_labels' not in text and 'hidden' not in text
    assert job.payload['source']['eligible_result_sentence_ids']==[1]
    assert judges.prepare_inventory(deepcopy(article)).request_hash==job.request_hash


def test_source_numbering_fails_closed_without_matching_eligibility():
    with pytest.raises((ValueError,KeyError)):
        judges.prepare_inventory({'pmid':'s','sentences':[{'id':1,'text':'Changed text.'}],
                                  'abstract':'A different source text.'})


def test_inventory_requires_literal_anchors_evidence_and_unique_details(article, inventory):
    source=judges.source_payload(article)
    assert judges.validate_inventory(inventory,source)==inventory
    for mutation in ['anchor','evidence','duplicate']:
        bad=deepcopy(inventory)
        if mutation=='anchor':bad['results'][0]['anchors']['exposure']='Invented therapy'
        if mutation=='evidence':bad['results'][0]['evidence'][0]['quote']='not source wording'
        if mutation=='duplicate':bad['results'].append(deepcopy(bad['results'][0]))
        with pytest.raises(ValueError):judges.validate_inventory(bad,source)


def test_request_uses_max_adaptive_structured_stream_and_two_cachebreakpoints(article):
    job=judges.prepare_inventory(article)
    request=judges.fable_request(job)
    assert request['model']==judges.FABLE_MODEL
    assert request['thinking']=={'type':'adaptive'}
    assert request['output_config']['effort']=='max'
    assert request['output_config']['format']['type']=='json_schema'
    assert request['stream'] and request['max_tokens']==128000
    assert not set(request)&{'temperature','top_p','top_k','tools','tool_choice'}
    assert request['system'][0]['cache_control']['ttl']=='5m'
    assert request['messages'][0]['content'][0]['cache_control']['ttl']=='5m'
    assert judges.worst_case_cost(request)>5.12


def test_cumulative_usage_is_not_added_twice():
    events=[{'type':'message_start','message':{'model':judges.FABLE_MODEL,
        'usage':{'input_tokens':20,'output_tokens':1}}},
        {'type':'message_delta','usage':{'output_tokens':40},'delta':{}},
        {'type':'message_delta','usage':{'output_tokens':100},'delta':{'stop_reason':'end_turn'}},
        {'type':'message_stop'}]
    result=judges.parse_stream(events)
    assert result['usage']=={'input_tokens':20,'output_tokens':100}
    assert judges.usage_cost(result['usage'])==pytest.approx(.00416)


def test_cache_cost_counts_write_read_and_thinking_output():
    usage={'input_tokens':100,'output_tokens':1000,'cache_creation_input_tokens':200,
           'cache_read_input_tokens':1000,'cache_creation':{'ephemeral_1h_input_tokens':50,
                                                          'ephemeral_5m_input_tokens':150}}
    assert judges.usage_cost(usage)==pytest.approx(.0433)
    with pytest.raises(ValueError):judges.usage_cost({'input_tokens':1})


@pytest.mark.parametrize('entrypoint', ['job', 'attempt', 'key'])
def test_fable_is_disabled_before_credentials_reservation_and_transport(tmp_path,article,authorization,monkeypatch,entrypoint):
    monkeypatch.setenv('ANTHROPIC_API_KEY','synthetic-disabled-key')
    ledger=Ledger()
    with pytest.raises(RuntimeError,match='permanently disabled'):
        if entrypoint == 'job':
            judges.run_job(judges.prepare_inventory(article),judge='fable',output_dir=tmp_path/'job',
                           authorization=authorization,ledger=ledger,allow_paid=True,key_path=tmp_path/'must-not-open')
        elif entrypoint == 'attempt':
            judges._fable_attempt(None,tmp_path/'job',ledger=ledger,key_path=tmp_path/'must-not-open',max_tokens=128000,timeout=1)
        else:
            judges._read_key(tmp_path/'must-not-open')
    assert 'ANTHROPIC_API_KEY' not in judges.os.environ
    assert ledger.events == [] and not (tmp_path/'job').exists()


@pytest.fixture
def astra_reply(monkeypatch):
    def configure(answer):
        def attempt(job,directory,*,timeout):
            text=json.dumps(answer)
            (directory/'raw_output.txt').write_text(text)
            return judges._validate_answer({'text':text,'cost_usd':None},job)
        monkeypatch.setattr(judges,'_astra_attempt',attempt)
    return configure


def test_astra_invalid_json_gets_one_identical_retry(tmp_path,article,inventory,authorization,monkeypatch):
    requests=[]
    def attempt(job,directory,*,timeout):
        requests.append(job.request_hash)
        return judges._validate_answer({'text':json.dumps({'invalid':True} if len(requests)==1 else inventory),'cost_usd':None},job)
    monkeypatch.setattr(judges,'_astra_attempt',attempt)
    result=judges.run_job(judges.prepare_inventory(article),judge='astra',output_dir=tmp_path/'job',authorization=authorization,allow_paid=True)
    assert result['status']=='valid' and result['attempts']==2 and requests[0]==requests[1]


def test_authorization_hash_and_h_selection_are_required(tmp_path,article,authorization):
    job=judges.prepare_inventory(article)
    with pytest.raises(RuntimeError,match='disabled'):
        judges.run_job(job,judge='astra',output_dir=tmp_path/'job',authorization=authorization)
    bad={**authorization,'development_amendment_sha256':'changed'}
    with pytest.raises(ValueError,match='hash'):
        judges.run_job(job,judge='astra',output_dir=tmp_path/'job',authorization=bad)
    bad={**authorization,'partition':'H'}
    with pytest.raises(ValueError,match='sealed'):
        judges.run_job(job,judge='astra',output_dir=tmp_path/'job',authorization=bad)


def test_candidate_fields_have_fixed_paths_and_nulls_receive_no_detail_credit(article,inventory):
    claim={'claim_id':'c001','exposure':'Treatment X','outcome':'blood glucose',
           'comparison':{'type':'within_group_change','comparator':None,'index_group':{'population':'adults'}},
           'analysis':{'method':None,'covariates':['age','sex']},
           'result':{'estimate_direction':'-','authors_significance':'not_reported','p_value':None}}
    job=judges.prepare_score(article,inventory,'candidate-a9', [claim])
    details=job.payload['candidate']['claims'][0]['emitted_details']
    assert details['population']==[{'detail_id':'comparison.index_group.population','text':'"adults"'}]
    assert not details['sample_size']
    assert not any(d['detail_id'].endswith('comparator') for rows in details.values() for d in rows)
    bad={**claim,'model':'hidden-model'}
    with pytest.raises(ValueError,match='provenance'):
        judges.prepare_score(article,inventory,'candidate-a9',[bad])


def test_complete_own_inventory_phase_is_required_for_candidate_scoring(tmp_path,article,inventory,authorization):
    path=tmp_path/'inventory-result.json'
    write_json(path,{'judge':'astra','kind':'inventory','status':'valid','pmid':article['pmid'],'answer':inventory,
                     'authorization':authorization,'source_sha256':digest(judges.prepare_inventory(article).payload['source']),
                     'raw_source_sha256':digest(judges.prepare_inventory(article).raw_source),
                     'source_provenance':judges.prepare_inventory(article).source_provenance})
    prepared=judges.prepare_inventory(article)
    write_json(path.parent/'payload.json',prepared.payload)
    write_json(path.parent/'schema.json',prepared.schema)
    write_json(path.parent/'raw_source.json',prepared.raw_source)
    (path.parent/'instructions.txt').write_text(prepared.instructions)
    seal=tmp_path/'seal.json'
    with pytest.raises(ValueError,match='complete'):
        judges.seal_inventories('astra','P',[path],[article['pmid'],'missing'],seal)
    judges.seal_inventories('astra','P',[path],[article['pmid']],seal)
    job=judges.prepare_score(article,inventory,'candidate-123',[])
    auth={**authorization,'inventory_seal_path':str(seal)}
    judges._authorize(job,'astra',auth)
    with pytest.raises(RuntimeError,match='permanently disabled'):
        judges._authorize(job,'fable',auth)
    altered=deepcopy(inventory);altered['results'][0]['statement']='Changed'
    altered_job=judges.prepare_score(article,altered,'candidate-123',[])
    with pytest.raises(ValueError,match='frozen'):
        judges._authorize(altered_job,'astra',auth)
    assert digest(inventory)==json.loads(seal.read_text())['inventories'][0]['answer_sha256']


def test_batch_preserves_aliases_and_each_claim_scope(article,inventory):
    candidates=[{'candidate_alias':'candidate-01','claims':[]},
                {'candidate_alias':'candidate-02','claims':[]}]
    job=judges.prepare_batch_score(article,inventory,candidates)
    answer={'schema_version':'2.0','pmid':article['pmid'],
            'candidates':[{'candidate_alias':row['candidate_alias'],'claims':[]} for row in candidates]}
    judges.validate_batch_judgment(answer,job)
    result={'kind':'batch_scoring','status':'valid','pmid':article['pmid'],'answer':answer}
    assert set(judges.split_batch_result(result))=={'candidate-01','candidate-02'}
    bad=deepcopy(answer);bad['candidates'].pop()
    with pytest.raises(ValueError,match='every candidate'):
        judges.validate_batch_judgment(bad,job)
    with pytest.raises(ValueError,match='one to ten'):
        judges.prepare_batch_score(article,inventory,[{'candidate_alias':f'candidate-{i:02x}','claims':[]} for i in range(11)])
    with pytest.raises(ValueError,match='unique'):
        judges.prepare_batch_score(article,inventory,[candidates[0],candidates[0]])


def test_judgment_cannot_change_or_drop_canonical_field_units(article,inventory):
    claim={'claim_id':'c001','comparison':{'type':'association','comparator':None}}
    job=judges.prepare_score(article,inventory,'candidate-01',[claim])
    fields={group:{'judgment':'supported','emitted_details':[
        {**detail,'judgment':'supported','matched_inventory_detail_ids':[]}
        for detail in job.payload['candidate']['claims'][0]['emitted_details'][group]]}
        for group in judges.GROUPS}
    answer={'schema_version':'2.0','pmid':article['pmid'],'candidate_alias':'candidate-01',
            'claims':[{'claim_id':'c001','core':'supported','critical_errors':[],
                       'matched_result_ids':['r001'],'field_groups':fields,'duplicate_of':None,'note':''}]}
    judges.validate_judgment(answer,job)
    bad=deepcopy(answer);bad['claims'][0]['field_groups']['comparison']['emitted_details'][0]['text']='changed'
    with pytest.raises(ValueError,match='detail text'):
        judges.validate_judgment(bad,job)
    bad=deepcopy(answer);bad['claims'][0]['field_groups']['comparison']['emitted_details']=[]
    with pytest.raises(ValueError,match='detail units'):
        judges.validate_judgment(bad,job)
    bad=deepcopy(answer);bad['claims'][0]['matched_result_ids']=['other-judge-r001']
    with pytest.raises(ValueError,match='another inventory'):
        judges.validate_judgment(bad,job)


def test_legacy_details_preserve_unknown_group_scope_and_null_significance(article,inventory):
    claim={'claim_id':'c001','comparison':{'comparator':'Control'},
           'legacy_context':{'population':'adults','n':10,'design':'trial'},
           'legacy_significant':None}
    job=judges.prepare_score(article,inventory,'candidate-d0',[claim])
    candidate=job.payload['candidate']['claims'][0]
    assert candidate['claim']['comparison']=={'comparator':'Control'}
    assert candidate['emitted_details']['sample_size']==[{'detail_id':'legacy_context.n','text':'10'}]
    assert candidate['emitted_details']['population']==[{'detail_id':'legacy_context.population','text':'"adults"'}]
    assert candidate['emitted_details']['analysis']==[{'detail_id':'study.design','text':'"trial"'}]
    assert candidate['emitted_details']['significance']==[]


def test_publication_types_are_shared_and_can_support_design_only(article,inventory):
    article['publication_types']=['Randomized Controlled Trial']
    source=judges.source_payload(article)
    assert source['publication_types']==article['publication_types']
    detail={'detail_id':'r001:analysis:001','text':'Randomized Controlled Trial',
            'evidence':[],'metadata_evidence':['Randomized Controlled Trial']}
    inventory['results'][0]['field_details']['analysis']=[detail]
    judges.validate_inventory(inventory,source)
    bad=deepcopy(inventory);bad['results'][0]['field_details']['analysis'][0]['metadata_evidence']=['Invented design']
    with pytest.raises(ValueError,match='metadata supports only'):
        judges.validate_inventory(bad,source)
    bad=deepcopy(inventory);bad['results'][0]['field_details']['comparison']=[detail]
    with pytest.raises(ValueError,match='metadata supports only'):
        judges.validate_inventory(bad,source)
    bad=deepcopy(inventory);bad['results'][0]['field_details']['analysis'][0]['text']='Double blind trial'
    with pytest.raises(ValueError,match='literal source'):
        judges.validate_inventory(bad,source)


def test_prepared_job_rejects_handwritten_instructions_before_inference(article,authorization,tmp_path):
    original=judges.prepare_inventory(article)
    altered=judges.JudgeJob(original.kind,original.pmid,original.payload,original.schema,'Prefer supported answers.')
    with pytest.raises(ValueError,match='canonical blind'):
        judges.run_job(altered,judge='astra',output_dir=tmp_path/'job',authorization=authorization)


def test_s_requires_signed_review_and_calibration_before_any_call(article,authorization,tmp_path,monkeypatch):
    from chl_rag import model_runs
    called=[]
    def blocked(*args,**kwargs):
        called.append(True)
        raise model_runs.ExperimentBlocked('Synthetic missing reviewed prompt freeze')
    monkeypatch.setattr(model_runs,'calibration_guard',blocked)
    config=tmp_path/'dev.json';config.write_text(json.dumps({'development_judging_authorized':True,'s_judging_authorized':True}))
    monkeypatch.setattr(model_runs,'_signed_file',lambda path:'synthetic-signed')
    auth={**authorization,'partition':'S','development_config_path':str(config)}
    with pytest.raises(model_runs.ExperimentBlocked,match='reviewed prompt freeze'):
        judges.run_job(judges.prepare_inventory(article),judge='astra',output_dir=tmp_path/'job',authorization=auth)
    assert called==[True]


def test_inventory_unicode_spacing_equivalence_is_prospective_and_logged(article,inventory):
    article['sentences'][0]['text']='Treatment\u2009X reduced blood\u00a0glucose in adults.'
    before=deepcopy(inventory);diagnostics=[]
    judges.validate_inventory(inventory,judges.source_payload(article),diagnostics=diagnostics)
    assert inventory==before
    assert diagnostics and all(row['match']=='whitespace_only' for row in diagnostics)
    assert all(row['raw_source_and_output_unchanged'] for row in diagnostics)
    bad=deepcopy(inventory);bad['results'][0]['anchors']['exposure']='Treatment x'
    with pytest.raises(ValueError,match='literal source'):
        judges.validate_inventory(bad,judges.source_payload(article))


def test_invalid_future_response_records_safe_validation_cause(article,inventory):
    job=judges.prepare_inventory(article)
    inventory['results'][0]['evidence'][0]['quote']='Invented words'
    result=judges._validate_answer({'text':json.dumps(inventory)},job)
    assert result['status']=='invalid'
    assert 'literal source span' in result['validation_error']
    assert 'Invented words' not in result['validation_error']


def test_historical_http_error_decoder_logs_only_sanitized_body_fields():
    secret='synthetic-key-never-real'
    body={'type':'error','request_id':'req_synthetic_001','error':{'type':'invalid_request_error',
          'message':f'Example rejected shape. x-api-key={secret}; token sk-ant-synthetic-long-token-value.'},
          'headers':{'x-api-key':secret}}
    result=judges._safe_http_error(httpx.Response(400,json=body),secret)
    assert result['error_type']=='invalid_request_error' and result['request_id']=='req_synthetic_001'
    assert 'headers' not in result
    assert secret not in result['error_message'] and 'sk-ant-synthetic-long-token-value' not in result['error_message']


def test_wire_refs_share_objects_without_changing_inventory_contract(article,inventory):
    from jsonschema import Draft202012Validator
    job=judges.prepare_inventory(article)
    wire=judges.wire_schema(job.schema)
    assert '$defs' in wire and len(wire['$defs'])==2
    assert len(json.dumps(wire))<len(json.dumps(job.schema))
    Draft202012Validator(job.schema).validate(inventory)
    Draft202012Validator(wire).validate(inventory)
    bad=deepcopy(inventory);bad['results'][0]['field_details']['population'][0]['metadata_evidence']=3
    assert list(Draft202012Validator(job.schema).iter_errors(bad))
    assert list(Draft202012Validator(wire).iter_errors(bad))
    assert judges.fable_request(job)['output_config']['format']['schema']==wire


def test_batch_wire_removes_unsupported_bounds_but_canonical_validation_retains_them(article,inventory):
    from jsonschema import Draft202012Validator
    job=judges.prepare_batch_score(article,inventory,[{'candidate_alias':'candidate-01','claims':[]}])
    wire=judges.wire_schema(job.schema)
    assert 'maxItems' not in json.dumps(wire)
    assert 'maxItems' in json.dumps(job.schema)
    invalid={'schema_version':'2.0','pmid':article['pmid'],
             'candidates':[{'candidate_alias':f'candidate-0{i}','claims':[]} for i in range(11)]}
    assert list(Draft202012Validator(job.schema).iter_errors(invalid))
    result=judges._validate_answer({'text':json.dumps(invalid)},job)
    assert result['status']=='invalid'


def test_request_identity_binds_wire_version(article,monkeypatch):
    job=judges.prepare_inventory(article)
    first=job.request_hash
    monkeypatch.setattr(judges,'WIRE_SCHEMA_VERSION','synthetic-other-wire')
    assert job.request_hash!=first


def test_model_source_view_preserves_raw_bytes_and_binds_both_hashes(article,inventory):
    article['sentences'][0]['text']='Treatment\u2009X reduced blood\u00a0glucose in adults.'
    before=deepcopy(article)
    job=judges.prepare_inventory(article)
    assert article==before
    assert job.raw_source['sentences'][0]['text']==article['sentences'][0]['text']
    assert job.payload['source']['sentences'][0]['text']=='Treatment X reduced blood glucose in adults.'
    assert job.source_provenance['changed_sentence_ids']==[1]
    assert job.source_provenance['raw_source_sha256']==digest(job.raw_source)
    assert job.source_provenance['display_source_sha256']==digest(job.payload['source'])
    assert job.source_provenance['raw_source_sha256']!=job.source_provenance['display_source_sha256']
    assert 'provenance' not in job.payload and 'raw_source' not in job.payload
    result=judges._validate_answer({'text':json.dumps(inventory)},job)
    assert result['status']=='valid' and result['quote_match_diagnostics']
    bad=deepcopy(inventory);bad['results'][0]['evidence'][0]['quote']='Treatment\u0000a0X reduced blood glucose in adults.'
    assert judges._validate_answer({'text':json.dumps(bad)},job)['status']=='invalid'


def test_legacy_raw_inventory_seal_cannot_be_rebound_to_display_view(tmp_path,article,inventory,authorization):
    path=tmp_path/'old-inventory.json'
    write_json(path,{'judge':'astra','kind':'inventory','status':'valid','pmid':article['pmid'],
                     'answer':inventory,'authorization':authorization,
                     'source_sha256':digest(judges.source_payload(article))})
    seal=tmp_path/'old-seal.json'
    prepared=judges.prepare_inventory(article)
    write_json(path.parent/'payload.json',prepared.payload)
    write_json(path.parent/'schema.json',prepared.schema)
    (path.parent/'instructions.txt').write_text(prepared.instructions)
    with pytest.raises(ValueError,match='provenance differs'):
        judges.seal_inventories('astra','P',[path],[article['pmid']],seal)


def test_valid_run_archives_raw_source_and_display_provenance(tmp_path,article,inventory,authorization,astra_reply):
    article['sentences'][0]['text']='Treatment\u2009X reduced blood\u00a0glucose in adults.'
    astra_reply(inventory)
    result=judges.run_job(judges.prepare_inventory(article),judge='astra',output_dir=tmp_path/'job',authorization=authorization,allow_paid=True)
    raw=json.loads((tmp_path/'job/raw_source.json').read_text())
    displayed=json.loads((tmp_path/'job/payload.json').read_text())['source']
    provenance=json.loads((tmp_path/'job/source_view.json').read_text())
    assert result['raw_source_sha256']==digest(raw)==provenance['raw_source_sha256']
    assert result['source_sha256']==digest(displayed)==provenance['display_source_sha256']
    assert raw['sentences'][0]['text']==article['sentences'][0]['text']


@pytest.mark.parametrize('judge',['astra'])
@pytest.mark.parametrize('kind',['inventory','scoring','batch_scoring'])
def test_s_judging_hold_blocks_direct_calls_independently_of_candidate_finality(
        tmp_path,article,inventory,authorization,monkeypatch,judge,kind):
    job=(judges.prepare_inventory(article) if kind=='inventory' else
         judges.prepare_score(article,inventory,'candidate-01',[]) if kind=='scoring' else
         judges.prepare_batch_score(article,inventory,[{'candidate_alias':'candidate-01','claims':[]}]))
    config=tmp_path/'held.json';config.write_text(json.dumps({'development_judging_authorized':True,'s_judging_authorized':False,'candidate_list_final':True}))
    def forbidden(*args,**kwargs):
        pytest.fail('The operational HOLD must be checked before source/inventory/credential work')
    monkeypatch.setattr(judges,'_validate_job_contract',forbidden)
    ledger=Ledger()
    with pytest.raises(RuntimeError,match='S judging is on HOLD'):
        judges.run_job(job,judge=judge,output_dir=tmp_path/'job',
            authorization={**authorization,'partition':'S','development_config_path':str(config)},
            ledger=ledger,allow_paid=True)
    assert ledger.events==[]
    assert not (tmp_path/'job').exists()


def test_ten_candidate_chunk_preserves_all_aliases_and_rejects_eleventh(article,inventory):
    rows=[{'candidate_alias':f'candidate-{index:02x}','claims':[]} for index in range(10)]
    job=judges.prepare_batch_score(article,inventory,rows)
    answer={'schema_version':'2.0','pmid':article['pmid'],
            'candidates':[{'candidate_alias':row['candidate_alias'],'claims':[]} for row in rows]}
    judges.validate_batch_judgment(answer,job)
    assert len(job.payload['candidates'])==10
    assert job.schema['properties']['candidates']['maxItems']==10
    with pytest.raises(ValueError,match='one to ten'):
        judges.prepare_batch_score(article,inventory,rows+[{'candidate_alias':'candidate-0a','claims':[]}])


def test_inventory_reuse_binds_its_own_rubric_not_batch_format(tmp_path,article,inventory,authorization,monkeypatch):
    job=judges.prepare_inventory(article)
    path=tmp_path/'result.json'
    write_json(path,{'judge':'astra','kind':'inventory','status':'valid','pmid':job.pmid,'answer':inventory,
        'authorization':authorization,'source_sha256':digest(job.payload['source']),
        'raw_source_sha256':digest(job.raw_source),'source_provenance':job.source_provenance})
    write_json(tmp_path/'payload.json',job.payload)
    write_json(tmp_path/'schema.json',job.schema)
    write_json(tmp_path/'raw_source.json',job.raw_source)
    (tmp_path/'instructions.txt').write_text(job.instructions)
    binding=judges.inventory_contract(path)
    monkeypatch.setattr(judges,'BATCH_SCORING_INSTRUCTIONS',judges.BATCH_SCORING_INSTRUCTIONS+'\nSynthetic format-only change.')
    assert judges.inventory_contract(path)==binding
    monkeypatch.setattr(judges,'INVENTORY_INSTRUCTIONS',judges.INVENTORY_INSTRUCTIONS+'\nSynthetic changed inventory rubric.')
    with pytest.raises(ValueError,match='source/instructions/schema differs'):
        judges.inventory_contract(path)


def test_compact_p_transport_archives_map_and_validates_decoded_canonical_answer(tmp_path,article,inventory,authorization,astra_reply):
    inventory_job=judges.prepare_inventory(article)
    inv=tmp_path/'inventory';inv.mkdir();inv_result=inv/'result.json'
    write_json(inv_result,{'judge':'astra','kind':'inventory','status':'valid','pmid':inventory_job.pmid,'answer':inventory,
        'authorization':authorization,'source_sha256':digest(inventory_job.payload['source']),
        'raw_source_sha256':digest(inventory_job.raw_source),'source_provenance':inventory_job.source_provenance})
    write_json(inv/'payload.json',inventory_job.payload);write_json(inv/'schema.json',inventory_job.schema)
    write_json(inv/'raw_source.json',inventory_job.raw_source);(inv/'instructions.txt').write_text(inventory_job.instructions)
    seal=tmp_path/'seal.json';judges.seal_inventories('astra','P',[inv_result],[article['pmid']],seal)
    candidates=[{'candidate_alias':'candidate-01','claims':[{'claim_id':'c001','comparison':{'type':'association'}}]}]
    job=judges.prepare_batch_score(article,inventory,candidates,scoring_wire_format='compact')
    canonical_job=judges.prepare_batch_score(article,inventory,candidates)
    assert job.canonical_request_hash==canonical_job.request_hash
    assert job.request_hash!=canonical_job.request_hash
    contract=judges.model_wire(job);mapping=contract['mapping']
    answer={'c':[]}
    for alias,c in mapping['candidates'].items():
        rows=[]
        for cid in c['claims']:
            groups={g:{'j':'unclear','d':[{'i':did,'j':'unclear','m':[]} for did in d]}
                    for g,d in mapping['claims'][cid]['groups'].items()}
            rows.append({'i':cid,'j':'unclear','e':[],'r':[],'g':groups,'o':None,'n':''})
        answer['c'].append({'i':alias,'c':rows})
    astra_reply(answer)
    result=judges.run_job(job,judge='astra',output_dir=tmp_path/'score',
        authorization={**authorization,'inventory_seal_path':str(seal)},allow_paid=True)
    with pytest.raises(ValueError,match='different existing'):
        judges.run_job(canonical_job,judge='astra',output_dir=tmp_path/'score',
            authorization={**authorization,'inventory_seal_path':str(seal)},allow_paid=True)
    assert result['status']=='valid'
    assert result['wire_answer']==answer
    assert result['answer']['candidates'][0]['candidate_alias']=='candidate-01'
    assert result['answer']['candidates'][0]['claims'][0]['claim_id']=='c001'
    assert json.loads((tmp_path/'score/wire_map.json').read_text())==mapping
    assert json.loads((tmp_path/'score/attempt-1/raw_output.txt').read_text())==answer
    assert result['wire_map_sha256']==mapping['map_sha256']


def test_old_canonical_astra_cache_identity_is_preserved_without_rewriting(tmp_path,article,inventory,authorization,astra_reply):
    astra_reply(inventory);job=judges.prepare_inventory(article)
    result=judges.run_job(job,judge='astra',output_dir=tmp_path/'job',authorization=authorization,allow_paid=True)
    legacy={key:value for key,value in result.items() if key not in
            {'canonical_request_hash','scoring_wire_format','wire_payload_sha256','wire_instructions_sha256','wire_map_sha256'}}
    path=tmp_path/'job/result.json';write_json(path,legacy);before=path.read_bytes()
    repeated=judges.run_job(job,judge='astra',output_dir=tmp_path/'job',authorization=authorization,allow_paid=True)
    assert repeated==legacy and path.read_bytes()==before


def test_final_plan_stops_development_before_first_call_and_between_attempts(tmp_path,article,inventory,authorization,monkeypatch):
    config=Path(authorization['development_config_path'])
    write_json(config, {'development_judging_authorized': False})
    calls=[]
    def attempt(job,directory,*,timeout):
        calls.append(True)
        write_json(config, {'development_judging_authorized': False})
        return {'status':'invalid','retryable':True,'text':'invalid'}
    monkeypatch.setattr(judges,'_astra_attempt',attempt)
    job=judges.prepare_inventory(article)
    with pytest.raises(RuntimeError,match='stopped'):
        judges.run_job(job,judge='astra',output_dir=tmp_path/'first',authorization=authorization,allow_paid=True)
    assert calls==[] and not (tmp_path/'first').exists()
    write_json(config, {'development_judging_authorized': True})
    with pytest.raises(RuntimeError,match='stopped'):
        judges.run_job(job,judge='astra',output_dir=tmp_path/'retry',authorization=authorization,allow_paid=True)
    assert len(calls)==1 and (tmp_path/'retry/attempt-1/metadata.json').exists()
    assert not (tmp_path/'retry/attempt-2').exists()
