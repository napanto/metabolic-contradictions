"""Synthetic reference packages and fake transports; no actual draws/inference."""
import json
import sys
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from jsonschema import ValidationError

from chl_rag import annotation_names_study_start as names
from chl_rag import frozen_compat as compatibility
from chl_rag import reference_oracles as refs
from chl_rag import reference_revision as revision
from chl_rag.io import digest, file_sha, read_jsonl, write_json, write_jsonl
from chl_rag.extraction import extraction_item, pair_item, validate_extraction

PROJECT_ROOT=Path(__file__).resolve().parents[1]
_fixture_factory=None


def reference_environment(root,monkeypatch,overrides=None):
    """Real companion/hash validation; only cryptographic signing and FS root are synthetic.

    A signed-byte registry rejects unsigned or changed fixture files. This does
    not stub effective_reference_config, runtime checks or reference provenance.
    No production evaluation config, source sample, participant record or credential is copied.
    """
    root=Path(root).resolve();root.mkdir(parents=True,exist_ok=True)
    signed={}
    def sign(path):
        resolved=Path(path).resolve()
        assert resolved.is_relative_to(root)
        signed[str(resolved)]=file_sha(resolved)
    def verify(path,*_args,**_kwargs):
        resolved=Path(path).resolve()
        if not resolved.is_relative_to(root) or signed.get(str(resolved))!=file_sha(resolved):
            raise refs.ExperimentBlocked('Synthetic unsigned or changed input: '+resolved.name)
        return 'synthetic-signed-'+signed[str(resolved)][:12]
    for relative in refs.RUNTIME:
        destination=root/relative;destination.parent.mkdir(parents=True,exist_ok=True)
        destination.write_bytes((PROJECT_ROOT/relative).read_bytes());sign(destination)
    target='src/chl_rag/instrument_original.py';archive=root/'fixtures/original_instrument.py'
    archive.parent.mkdir(parents=True,exist_ok=True);archive.write_text('"""Synthetic archived reference source."""\n');sign(archive)
    runtime={relative:file_sha(root/relative) for relative in refs.RUNTIME}
    runtime[target]=file_sha(archive)
    config={'status':'registered','amendment':6,'pipeline_version':'2.0','synthetic_fixture':True,
        'active_judges':['astra'],'judge_policy':'astra_only','reference_design':'single_astra_max',
        'development_references_authorized':False,'final_references_authorized':True,
        'oracle_release_lock_path':str(root/'release.json'),'references':settings(),
        'reference_frame_manifest_path':str(root/'frame.json'),
        'extraction_comparison':{'baseline':'saved_v1_retained','bundle_path':str(root/'baselines.json')},
        'evaluation':{},'source_sha256':{target:file_sha(archive)},'reference_runtime_sha256':runtime}
    config.update(overrides or {})
    config_path=root/'configs/evaluation_system.json';write_json(config_path,config);sign(config_path)
    base_sha=file_sha(config_path)
    monkeypatch.setattr(compatibility,'BASE_SHA256',base_sha)
    monkeypatch.setattr(revision,'BASE_SHA256',base_sha)
    paths={
        'instrument':'configs/instrument_study_start.json','oracle_prompt':'configs/oracle_prompt_study_start.json',
        'site_instrument':'configs/site_instrument_study_start.json','site_guidelines':'configs/site_guidelines_en_study_start.md',
        'pair_schema':'configs/pair_annotation_schema_study_start.json','extraction_schema':'configs/extraction_annotation_schema_study_start.json',
        'pair_runtime':'src/chl_rag/pair_instrument_study_start.py','instrument_runtime':'src/chl_rag/instrument_study_start.py',
        'comparison_runtime':'src/chl_rag/extraction_comparison_study_start.py','comparison_prompt':'configs/extraction_comparison_prompt_study_start.json',
        'annotation_names_runtime':'src/chl_rag/annotation_names_study_start.py'}
    protocol=root/'FIXTURE_REFERENCE.md';protocol.write_text('Synthetic prospective authority only.');sign(protocol)
    review=root/'fixtures/review.json';write_json(review,{'status':'passed','synthetic':True});sign(review)
    document={'schema_version':1,'status':'registered','kind':'reference','base_config_path':compatibility.BASE_CONFIG,
        'base_config_sha256':base_sha,'production_binding_changed':False,'glm_prompts_changed':False,
        'compatibility_module_sha256':file_sha(root/'src/chl_rag/frozen_compat.py'),
        'protocol_path':str(protocol.relative_to(root)),'protocol_sha256':file_sha(protocol),
        'review_path':str(review.relative_to(root)),'review_sha256':file_sha(review),'site_commit':revision.SITE_COMMIT,
        'artifact_paths':paths,'new_files':{path:file_sha(root/path) for path in paths.values()},
        'replacements':{target:{'original_sha256':file_sha(archive),'approved_sha256':file_sha(root/target),
                               'archive_path':str(archive.relative_to(root))}}}
    revision_path=root/revision.REVISION_PATH;write_json(revision_path,document);sign(revision_path)
    real_validate=compatibility.validate_companion
    def validate(document,kind,**kwargs):
        return real_validate(document,kind,verify_signature=verify,**kwargs)
    monkeypatch.setattr(revision,'validate_companion',validate)
    monkeypatch.setattr(revision,'_signed',verify)
    monkeypatch.setattr(refs,'_signed_file',verify)
    monkeypatch.setattr(refs,'ROOT',root)
    real_effective=revision.effective_reference_config
    real_files=revision.reference_revision_files
    def effective(path):return real_effective(path,root=root)
    monkeypatch.setattr(refs,'effective_reference_config',effective)
    monkeypatch.setattr(refs,'reference_revision_files',lambda cfg:real_files(cfg,root=root))
    monkeypatch.setattr(refs,'_isolation_module',lambda:pytest.fail('Synthetic fixture attempted real model/inspection transport'))
    monkeypatch.chdir(root)
    view=effective(config_path)
    provenance=revision.reference_provenance(view)
    def release():
        value={'status':'released','scope':'full_run','evaluation_config_sha256':base_sha,
            'reference_revision_sha256':provenance['reference_revision_sha256'],
            'released_at':'2026-11-14T09:00:00+01:00',
            'authority':{'actor':'Antonio','kind':'explicit_go','verbatim':'Synthetic fixture go','source':'synthetic test'}}
        path=Path(config['oracle_release_lock_path']);write_json(path,value);sign(path)
        return path
    return SimpleNamespace(root=root,config_path=config_path,config=config,revision_path=revision_path,
        document=document,view=view,provenance=provenance,sign=sign,verify=verify,signed=signed,
        release=release,effective=effective)


@pytest.fixture(autouse=True)
def synthetic_reference_factory(monkeypatch):
    # Each fixture registers real immutable hashes. There is no blanket signature bypass.
    monkeypatch.setattr(sys.modules[__name__],'_fixture_factory',
                        lambda root:reference_environment(root,monkeypatch),raising=False)


@pytest.fixture
def items():
    case=json.loads((PROJECT_ROOT/'configs/synthetic.json').read_text())['cases'][0]
    a=deepcopy(case['article']);b=deepcopy(a);b['pmid']='899999997'
    ext_a=validate_extraction(a,case['expected_model_output'])['extraction']
    ext_b=validate_extraction(b,case['expected_model_output'])['extraction']
    arm=ext_a['claims'][0]['arm']
    pair=pair_item('v2-pair-700001',arm,a,ext_a,ext_a['claims'][0]['claim_id'],b,ext_b,ext_b['claims'][0]['claim_id'])
    extraction=extraction_item('v2-extraction-001',arm,a,ext_a)
    return pair,extraction


def answer(item):
    if item['task']=='pair':
        site=json.loads((PROJECT_ROOT/'configs/site_instrument_study_start.json').read_text())
        return {'item_id':item['item_id'],'task':'pair','instrument_version':'2.0','pipeline_version':'2.0',
                'path':deepcopy(site['example_paths']['concordant']),'rationale':''}
    return names.extraction_to_wire({'item_id':item['item_id'],'task':'extraction','instrument_version':'2.0','pipeline_version':'2.0',
        'claims':[{'claim_id':c['claim_id'],'fields':{k:'correct' for k in ('exposure','outcome','sign','kind','comparator','context')},
            'support':'supported','diagnostics':{k:'correct' for k in ('comparison','population','sample_size','analysis','significance')},
            'diagnostic_problems':{k:[] for k in ('comparison','population','sample_size','analysis','significance')},'note':''}
            for c in item['claims']]})


def plan_fixture(tmp_path,items,*,judge='astra',stage=None,effort='max'):
    assert _fixture_factory is not None, 'Synthetic reference fixture was not installed'
    environment=_fixture_factory(tmp_path)
    environment.release()
    if stage=='final_evaluation':
        pairs=[{**deepcopy(items[0]),'item_id':f'v2-pair-{i:03d}'} for i in range(1,5)]
        abstracts=[{**deepcopy(items[1]),'item_id':f'v2-extraction-{i:03d}'} for i in range(1,4)]
        data=pairs+abstracts;pilot,order=refs.final_order(data)
    else:data=[items[0]];pilot=[];order=[items[0]['item_id']]
    settings_value={'concurrency':4,'max_tokens':128000,'max_attempts':2,'attempt_timeout_seconds':1800}
    files={str(environment.config_path):file_sha(environment.config_path),
           **refs._runtime_binding(environment.view),**refs.reference_revision_files(environment.view)}
    return refs.ReferencePlan('final_evaluation',judge,effort,str(environment.config_path),tuple(data),
        tuple(i['item_id'] for i in data),tuple(order),tuple(pilot),str(environment.root/'output'),
        digest(data),'synthetic-frame',{'reference_revision':environment.provenance},files,
        settings_value,environment.config['references']['pilot'],refs._AUTHORIZATION_TOKEN)


class Ledger:
    vendor='anthropic'
    phase='development_judging'


def fake_transport(calls,outputs=None):
    def run(plan,request,directory,ledger):
        calls.append({'id':request['item']['item_id'],'prompt':request['prompt'],'instructions':request['instructions']})
        value=outputs.pop(0) if outputs else answer(request['item'])
        return {'status':'completed','model':refs.MODELS[plan.judge],
                'text':json.dumps(value),'cost_usd':.01 if plan.judge=='fable' else None}
    return run


def test_prepared_input_matches_public_item_and_contains_no_other_answers(items):
    for item in items:
        request=refs.prepare_reference(item)
        assert names.restore_model_input(json.loads(request['prompt'])['item'])==item
        assert request['raw_public_item_sha256']==digest(item)
        assert request['displayed_public_item_sha256']==digest(names.model_input_view(item))
        assert request['input_projection']['values_unchanged']
        assert not request['classifier_outputs_in_prompt']
        assert not request['peer_outputs_in_prompt'] and not request['human_answers_in_prompt']


@pytest.mark.parametrize('field',['session_pseudonym','human_labels','student_answers','rationale','path'])
def test_participant_data_is_rejected_not_silently_forwarded(items,field):
    bad=deepcopy(items[0]);bad[field]='private fixture'
    with pytest.raises(ValueError,match='Participant'):refs.prepare_reference(bad)


def test_classifier_metadata_cannot_enter_public_schema(items):
    bad=deepcopy(items[0]);bad['predicted_label']='contradictory'
    with pytest.raises(ValidationError):refs.prepare_reference(bad)
    bad=deepcopy(items[0]);bad['a']['claim']['provider']='private model identity'
    with pytest.raises(ValidationError):refs.prepare_reference(bad)


def test_pair_requires_distinct_sources_and_finite_values(items):
    bad=deepcopy(items[0]);bad['b']['article']['pmid']=bad['a']['article']['pmid']
    with pytest.raises(ValueError,match='distinct'):refs.prepare_reference(bad)
    bad=deepcopy(items[0]);bad['a']['claim']['result']['estimate']={'measure':'toy','value':float('nan'),'unit':None}
    with pytest.raises(ValueError,match='Nonfinite'):refs.prepare_reference(bad)


def test_no_unsigned_manual_plan_or_implicit_inference(tmp_path,items):
    plan=plan_fixture(tmp_path,items)
    with pytest.raises(refs.ExperimentBlocked,match='signed authorization loader'):
        refs.revalidate(replace(plan,_authorization_token=None))
    with pytest.raises(refs.ExperimentBlocked,match='disabled'):
        refs.run_item(plan,plan.expected_ids[0])
    assert not Path(plan.output_dir).exists()


def test_once_cache_and_raw_derived_answer_identity(tmp_path,items):
    plan=plan_fixture(tmp_path,items);calls=[]
    row=refs.run_item(plan,plan.expected_ids[0],allow_inference=True,transport=fake_transport(calls))
    assert row['status']=='valid' and row['answer']['label']=='concordant'
    assert 'label' not in row['raw_response']
    again=refs.run_item(plan,plan.expected_ids[0],allow_inference=True,transport=lambda *a:pytest.fail('duplicate inference'))
    assert again==row and len(calls)==1
    result_path=Path(plan.output_dir)/'items'/plan.expected_ids[0]/'result.json'
    changed=deepcopy(row);changed['answer']['label']='contradictory';write_json(result_path,changed)
    with pytest.raises(refs.ExperimentBlocked,match='Cached annotation was changed'):
        refs.run_item(plan,plan.expected_ids[0],allow_inference=True,transport=lambda *a:pytest.fail('no repair'))


def test_wrong_item_invalid_twice_no_repair_or_third_attempt(tmp_path,items):
    plan=plan_fixture(tmp_path,items);bad=answer(items[0]);bad['item_id']='v2-pair-799999';calls=[]
    row=refs.run_item(plan,plan.expected_ids[0],allow_inference=True,transport=fake_transport(calls,[bad,bad]))
    assert row['status']=='invalid' and row['answer'] is None and row['attempt_count']==2
    assert calls[0]['prompt']==calls[1]['prompt'] and calls[0]['instructions']==calls[1]['instructions']
    refs.run_item(plan,plan.expected_ids[0],allow_inference=True,transport=lambda *a:pytest.fail('third attempt'))
    assert len(calls)==2


def test_refusal_is_terminal_and_wrong_model_never_accepted(tmp_path,items):
    plan=plan_fixture(tmp_path,items);calls=[]
    def refuse(*args):calls.append(True);return {'status':'refusal','text':'Refused','retryable':False,'schema_valid':False,'cost_usd':.02}
    row=refs.run_item(plan,plan.expected_ids[0],allow_inference=True,ledger=Ledger(),transport=refuse)
    assert row['status']=='refusal' and row['attempt_count']==1 and len(calls)==1
    other=plan_fixture(tmp_path/'second',items)
    wrong=lambda p,r,d,l:{'status':'completed','model':'other-model','text':json.dumps(answer(r['item']))}
    row=refs.run_item(other,other.expected_ids[0],allow_inference=True,transport=wrong)
    assert row['status']=='model_mismatch' and row['answer'] is None


def test_changed_frozen_input_blocks_before_transport(tmp_path,items):
    plan=plan_fixture(tmp_path,items)
    Path(plan.config_path).write_text('{}')
    with pytest.raises(refs.ExperimentBlocked,match='changed'):
        refs.run_item(plan,plan.expected_ids[0],allow_inference=True,transport=lambda *a:pytest.fail('blocked'))


def test_interrupted_attempt_is_not_silently_resubmitted(tmp_path,items):
    plan=plan_fixture(tmp_path,items)
    (Path(plan.output_dir)/'items'/plan.expected_ids[0]/'attempt-1').mkdir(parents=True)
    with pytest.raises(refs.ExperimentBlocked,match='Interrupted'):
        refs.run_item(plan,plan.expected_ids[0],allow_inference=True,transport=lambda *a:pytest.fail('resubmitted'))


def test_final_reference_manifest_matches_evaluator_contract(tmp_path,items):
    plan=plan_fixture(tmp_path,items,stage='final_evaluation');calls=[]
    refs.run_plan(plan,phase='pilot',allow_inference=True,transport=fake_transport(calls))
    result=refs.run_plan(plan,allow_inference=True,transport=fake_transport(calls))
    assert result['status']=='complete' and result['effort']=='max'
    assert result['expected_item_ids']==list(plan.expected_ids)
    assert result['frame_manifest_sha256']==plan.frame_manifest_sha256
    rows=list(read_jsonl(result['answers']['path']))
    assert len(rows)==len(plan.items) and rows[0]['answer']['label']=='concordant'
    assert result['reference_revision']==plan.binding['reference_revision']
    assert result['classifier_outputs_in_prompt'] is False and result['peer_outputs_in_prompt'] is False


def test_final_pilot_is_three_pairs_two_abstracts_fixed_then_full_reuses_it(tmp_path,items):
    plan=plan_fixture(tmp_path,items,stage='final_evaluation');calls=[]
    index={row['item_id']:row for row in plan.items}
    assert CounterTasks(plan.pilot_ids,index)=={'pair':3,'extraction':2}
    with pytest.raises(FileNotFoundError):refs.run_plan(plan,allow_inference=True,transport=fake_transport(calls))
    pilot=refs.run_plan(plan,phase='pilot',allow_inference=True,transport=fake_transport(calls))
    assert pilot['valid']==5 and len(calls)==5
    decision=json.loads((Path(plan.output_dir)/'pilot_decision.json').read_text())
    assert decision['decision']=='continue_max'
    full=refs.run_plan(plan,allow_inference=True,transport=fake_transport(calls))
    assert full['status']=='complete' and len(calls)==len(plan.items)
    assert full['expected_item_ids']==list(plan.expected_ids)


def CounterTasks(ids,index):
    from collections import Counter
    return dict(Counter(index[i]['task'] for i in ids))


def test_pilot_uses_all_terminal_timings_and_separate_effort_policy(tmp_path,items):
    plan=plan_fixture(tmp_path,items,stage='final_evaluation')
    plan=replace(plan,pilot_policy={**plan.pilot_policy,'deadline':'2026-11-14T18:00:00+01:00'})
    index={row['item_id']:row for row in plan.items}
    rows=[{'item_id':i,'task':index[i]['task'],'status':'valid' if n<3 else 'invalid',
           'elapsed_seconds':10 if n<3 else 3600} for n,i in enumerate(plan.pilot_ids)]
    decision=refs.pilot_decision(plan,rows,as_of='2026-11-14T10:00:00+01:00')
    assert decision['decision']=='human_priority_oracle_incomplete'
    assert decision['task_median_seconds']['extraction']==3600
    late=refs.pilot_decision(plan,rows,as_of='2026-11-14T19:00:00+01:00')
    assert late['decision']=='human_priority_oracle_incomplete'
    with pytest.raises(ValueError,match='registered final-reference'):
        refs.pilot_decision(replace(plan,effort='xhigh'),rows,as_of='2026-11-14T10:00:00+01:00')


def test_pilot_decision_cannot_be_forged_or_mixed(tmp_path,items):
    plan=plan_fixture(tmp_path,items,stage='final_evaluation')
    refs.run_plan(plan,phase='pilot',allow_inference=True,transport=fake_transport([]))
    path=Path(plan.output_dir)/'pilot_decision.json';value=json.loads(path.read_text());value['valid']=0;write_json(path,value)
    with pytest.raises(refs.ExperimentBlocked,match='differs from its frozen inputs'):
        refs.run_plan(plan,allow_inference=True,transport=lambda *a:pytest.fail('forged pilot'))


def test_final_fable_gate_is_separate_from_dev_and_precedes_package_read(tmp_path,monkeypatch):
    funding=tmp_path/'funding.json';write_json(funding,{'status':'confirmed','authority':'synthetic','final_fable_topup_confirmed':False})
    monkeypatch.setattr(refs,'FUNDING',funding)
    cfg=tmp_path/'final.json';write_json(cfg,{'active_judges':['astra'],'judge_policy':'astra_only','reference_design':'single_astra_max','status':'registered','final_references_authorized':True,
        'references':{'transport':'isolated_codex','max_attempts':2,'attempt_timeout_seconds':1800,
                      'fable':{'model':'claude-fable-5-2','effort':'max','max_tokens':128000}}})
    with pytest.raises(RuntimeError,match='permanently disabled'):
        refs.load_plan('final_evaluation','fable',config_path=cfg)


def test_final_fable_cannot_spend_from_dev_ledger(tmp_path,items,monkeypatch):
    plan=plan_fixture(tmp_path,items,stage='final_evaluation',judge='fable')
    monkeypatch.setattr(refs,'_final_fable_gate',lambda config:'synthetic-topup')
    with pytest.raises(RuntimeError,match='permanently disabled'):
        refs.run_item(plan,plan.expected_ids[0],allow_inference=True,ledger=Ledger(),transport=lambda *a:pytest.fail('wrong funds'))


def test_s_cannot_masquerade_as_development_reference_and_no_batch_api(tmp_path):
    with pytest.raises(refs.ExperimentBlocked,match='retired'):refs.load_plan('S','astra')
    cfg={'active_judges':['astra'],'judge_policy':'astra_only','reference_design':'single_astra_max','references':{'transport':'message_batches'}}
    with pytest.raises(refs.ExperimentBlocked,match='Message Batches'):
        refs._reference_settings(cfg,'astra','classifier_development','max')


def test_registered_astra_concurrency_limits_are_phase_specific():
    cfg={'active_judges':['astra'],'judge_policy':'astra_only','reference_design':'single_astra_max','references':{'max_attempts':2,'attempt_timeout_seconds':1800,
        'astra':{'model':'gpt-6-astra','effort':'max','concurrency':4}}}
    assert refs._reference_settings(cfg,'astra','classifier_development','max')['concurrency']==4
    assert refs._reference_settings(cfg,'astra','final_evaluation','max')['concurrency']==4
    cfg['references']['astra']['concurrency']=5
    with pytest.raises(refs.ExperimentBlocked,match='concurrency'):
        refs._reference_settings(cfg,'astra','final_evaluation','max')


def test_stage_authorization_checked_before_frame_or_package_access(tmp_path):
    cfg=tmp_path/'no.json';write_json(cfg,{'active_judges':['astra'],'judge_policy':'astra_only','reference_design':'single_astra_max','status':'registered','stage':'classifier_development',
                                        'development_references_authorized':False})
    with pytest.raises(refs.ExperimentBlocked,match='retired'):
        refs.load_plan('classifier_development','astra',config_path=cfg)


def settings():
    return {'transport':'isolated_codex','max_attempts':2,'attempt_timeout_seconds':1800,
        'astra':{'model':'gpt-6-astra','effort':'max','concurrency':4},
        'fable':{'model':'claude-fable-5-2','effort':'max','max_tokens':128000,'concurrency':4},
        'pilot':{'seed':20261123,'pairs':3,'abstracts':2,'minimum_valid':4,'projection_margin':1.5,
                 'deadline':'2099-01-01T18:00:00+01:00','allow_whole_reference_xhigh_fallback':False}}


def test_development_experiment_is_retired_before_config_or_source_access(tmp_path,monkeypatch):
    monkeypatch.setattr(refs,'_json',lambda path:pytest.fail('Retired stage must not read data'))
    with pytest.raises(refs.ExperimentBlocked,match='retired'):
        refs.load_plan('classifier_development','astra',config_path=tmp_path/'not-read')


def test_final_loader_binds_subset_order_frame_classifier_and_whitelist(tmp_path,items,monkeypatch):
    from chl_rag import production as production
    environment=reference_environment(tmp_path,monkeypatch)
    cfg=environment.config_path
    data=[{**deepcopy(items[0]),'item_id':f'v2-pair-{i:03d}'} for i in range(1,4)]
    data += [{**deepcopy(items[1]),'item_id':f'v2-extraction-{i:03d}'} for i in range(1,3)]
    public=tmp_path/'items.jsonl';human=tmp_path/'human.jsonl';manifest_path=tmp_path/'manifest.json';frame_path=tmp_path/'frame.json'
    write_jsonl(public,data);write_jsonl(human,data)
    pipeline={'config_sha256':file_sha(cfg)};classifier={'classifier_lock_sha256':'synthetic'}
    frame={'status':'complete_frozen','binding':pipeline,'classifier_binding':classifier,
           'eligible_reference_pmids':sorted(set().union(*(refs._source_ids(row) for row in data)))}
    write_json(frame_path,frame)
    pilot,order=refs.final_order(data)
    manifest={'pipeline_version':'2.0','instrument_version':'2.0','evaluation_config_sha256':file_sha(cfg),
        'reference_revision':environment.provenance,
        'human_subset_frozen_before_oracle':True,'labels_read':False,'frame_manifest_sha256':file_sha(frame_path),
        'package_sha256':digest(data),'human_package_sha256':digest(data),'full_items':5,
        'human_pairs':3,'human_abstracts':2,'pairs':3,'abstracts':2,
        'scarcity':{'pairs_reduced':True,'abstracts_reduced':True},
        'seeds':{'oracle_order':20261123},'oracle_pilot_ids':pilot,'oracle_order':order}
    write_json(manifest_path,manifest)
    baseline_path=tmp_path/'baselines.json'
    write_json(baseline_path,{'version':'final-d0-companion-1','baseline':'saved_v1_retained',
        'reference_revision':environment.provenance,
        'package_sha256':digest(data),'package_manifest_sha256':file_sha(manifest_path),
        'evaluation_config_sha256':file_sha(cfg),'frozen_before_oracle_outputs':True,
        'baselines':{items[1]['article']['pmid']:{'pmid':items[1]['article']['pmid'],'claims':[]}}})
    for file in (public,human,manifest_path,frame_path,baseline_path):environment.sign(file)
    monkeypatch.setattr(refs,'FINAL_MANIFEST',manifest_path);monkeypatch.setattr(refs,'FINAL_PACKAGE',public);monkeypatch.setattr(refs,'FINAL_SUBSET',human)
    # Production certification is a separate component; all reference-specific
    # signatures, runtime hashes, companion, package and release checks stay real.
    def certified(config_path,*,require_classifier=False):
        assert Path(config_path)==cfg and require_classifier is True
        return {'_classifier_binding':classifier},pipeline
    monkeypatch.setattr(production,'production_guard',certified)
    plan=refs.load_plan('final_evaluation','astra',config_path=cfg)
    assert list(plan.pilot_ids)==pilot and plan.settings['concurrency']==4
    assert plan.binding['reference_revision']==environment.provenance
    assert str(environment.revision_path.resolve()) in plan.files
    corrupted={**manifest,'oracle_pilot_ids':list(reversed(pilot))};write_json(manifest_path,corrupted);environment.sign(manifest_path)
    with pytest.raises(refs.ExperimentBlocked,match='Pilot/order'):
        refs.load_plan('final_evaluation','astra',config_path=cfg)
    write_json(manifest_path,manifest);environment.sign(manifest_path)
    bad_human=[{**data[0],'arm':'drug' if data[0]['arm']!='drug' else 'diet'}]
    write_jsonl(human,bad_human);environment.sign(human)
    manifest['human_package_sha256']=digest(bad_human);write_json(manifest_path,manifest);environment.sign(manifest_path)
    with pytest.raises(refs.ExperimentBlocked,match='unchanged subset'):
        refs.load_plan('final_evaluation','astra',config_path=cfg)


def test_fable_disabled_before_key_or_transport_even_with_funding(tmp_path,items):
    plan=plan_fixture(tmp_path,items,judge='fable')
    with pytest.raises(RuntimeError,match='permanently disabled'):
        refs.run_item(plan,plan.expected_ids[0],allow_inference=True,
                      ledger=Ledger(),key_path=tmp_path/'must-not-open',
                      transport=lambda *a:pytest.fail('Disabled transport called'))
    assert not Path(plan.output_dir).exists()


def test_funding_pause_does_not_create_or_consume_an_attempt(tmp_path,items):
    plan=plan_fixture(tmp_path,items,judge='fable')
    class Paused(Ledger):
        def reserve(self,*args,**kwargs):raise AssertionError('Disabled vendor must not reserve')
    with pytest.raises(RuntimeError,match='permanently disabled'):
        refs.run_item(plan,plan.expected_ids[0],allow_inference=True,ledger=Paused(),key_path=tmp_path/'must-not-read')
    assert not (Path(plan.output_dir)/'items'/plan.expected_ids[0]/'attempt-1').exists()


def test_final_topup_flag_without_positive_phase_allocation_cannot_enable_calls(tmp_path,monkeypatch):
    funding=tmp_path/'funding.json'
    write_json(funding,{'status':'confirmed','authority':'synthetic coordinator','final_fable_topup_confirmed':True,
                       'vendors':{'anthropic':{'phase_alerts':{'final_reference':0}}}})
    monkeypatch.setattr(refs,'FUNDING',funding)
    monkeypatch.setattr(refs,'_signed_file',lambda p:pytest.fail('zero allocation must fail before signing check'))
    with pytest.raises(RuntimeError,match='permanently disabled'):
        refs._final_fable_gate({})


@pytest.mark.parametrize('damage',['missing_revision','unsigned_revision','changed_base','changed_artifact'])
def test_effective_reference_fixture_exercises_real_companion_gates(tmp_path,monkeypatch,damage):
    environment=reference_environment(tmp_path,monkeypatch)
    if damage=='missing_revision':environment.revision_path.unlink()
    elif damage=='unsigned_revision':environment.signed.pop(str(environment.revision_path.resolve()))
    elif damage=='changed_base':
        value=json.loads(environment.config_path.read_text());value['final_references_authorized']=False
        write_json(environment.config_path,value);environment.sign(environment.config_path)
    else:
        path=environment.root/environment.document['artifact_paths']['pair_runtime']
        path.write_text(path.read_text()+'\n# changed fixture\n');environment.sign(path)
    with pytest.raises((compatibility.FrozenCompatibilityError,refs.ExperimentBlocked)):
        refs.effective_reference_config(environment.config_path)


def test_package_reference_revision_cannot_be_missing_or_changed(tmp_path,monkeypatch):
    environment=reference_environment(tmp_path,monkeypatch)
    for provenance in ({}, {'reference_revision':{**environment.provenance,'reference_revision_sha256':'f'*64}}):
        with pytest.raises(compatibility.FrozenCompatibilityError,match='active reference revision'):
            refs.require_reference_provenance(provenance,environment.view)
