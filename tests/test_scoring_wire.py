"""Lossless wire transformations over synthetic facts; no inference or gold labels."""
from copy import deepcopy

import pytest

from chl_rag import judges as judges
from chl_rag import scoring_wire as wire
from chl_rag.io import digest


def job_fixture(batch=True):
    article={'pmid':'999999991','title':'Synthetic','sentences':[{'id':1,'section':'RESULTS','eligible':True,
        'text':'Treatment X reduced glucose in adults.'}],'publication_types':[]}
    evidence=[{'sentence_id':1,'quote':'Treatment X reduced glucose in adults.'}]
    inventory={'schema_version':'2.0','pmid':article['pmid'],'results':[{'result_id':'r001',
        'anchors':{'result_sentence_ids':[1],'exposure':'Treatment X','outcome':'glucose',
                   'comparison':None,'subgroup':'adults','analysis':None,'time':None},
        'statement':'Synthetic source result','multi_result_sentence':False,'evidence':evidence,
        'field_details':{**{group:[] for group in judges.GROUPS},'population':[
            {'detail_id':'r001:population:001','text':'adults','evidence':evidence,'metadata_evidence':[]}]}}]}
    claims=[{'claim_id':'c0001','exposure':'Treatment X','outcome':'glucose',
             'comparison':{'type':'association','index_group':{'population':'adults'},'comparator':None}},
            {'claim_id':'c0002','exposure':'Treatment X','outcome':'glucose',
             'comparison':{'type':'association','index_group':{'population':'adults'},'comparator':None}}]
    if batch:
        return judges.prepare_batch_score(article,inventory,[{'candidate_alias':f'candidate-{i:02x}','claims':deepcopy(claims)} for i in range(2)])
    return judges.prepare_score(article,inventory,'candidate-00',claims)


def canonical_fixture(job):
    candidates=job.payload['candidates'] if job.kind=='batch_scoring' else [job.payload['candidate']]
    out=[]
    for candidate in candidates:
        claims=[]
        for ordinal,row in enumerate(candidate['claims']):
            groups={group:{'judgment':'unclear','emitted_details':[
                {'detail_id':d['detail_id'],'text':d['text'],'judgment':'unclear',
                 'matched_inventory_detail_ids':['r001:population:001'] if group=='population' else []}
                for d in row['emitted_details'][group]]} for group in judges.GROUPS}
            claims.append({'claim_id':row['claim']['claim_id'],'core':'unclear',
                'critical_errors':['invented_comparison'] if ordinal else [],'matched_result_ids':['r001'],
                'field_groups':groups,'duplicate_of':'c0001' if ordinal else None,
                'note':'Synthetic note retained verbatim.'})
        out.append({'candidate_alias':candidate['alias'],'claims':claims})
    return {'schema_version':'2.0','pmid':job.pmid,**({'candidates':out} if job.kind=='batch_scoring' else out[0])}


@pytest.mark.parametrize('batch',[False,True])
def test_roundtrip_preserves_every_input_fact_and_every_output_judgment(batch):
    job=job_fixture(batch);original=deepcopy(job.payload)
    compact=wire.prepare_proposal(job)
    assert compact['status'].startswith('proposal_inactive')
    assert job.payload==original
    assert wire.restore_payload(compact['payload'],compact['mapping'])==original
    canonical=canonical_fixture(job)
    coded=wire.encode_known_response(canonical,compact['mapping'])
    decoded=wire.decode_response(coded,compact['mapping'])
    assert decoded==canonical
    if batch:judges.validate_batch_judgment(decoded,job)
    else:judges.validate_judgment(decoded,job)
    assert 'canonical_path' in str(compact['payload'])
    assert 'text' not in str(coded)
    assert 'matched_inventory_detail_ids' not in str(coded)


@pytest.mark.parametrize('bad',['unknown_candidate','duplicate_candidate','missing_candidate','unknown_claim',
                               'duplicate_claim','missing_claim','unknown_detail','duplicate_detail','missing_detail',
                               'unknown_inventory','cross_group_inventory','cross_candidate_duplicate'])
def test_unknown_duplicate_missing_or_cross_scope_ids_fail_closed(bad):
    job=job_fixture();proposal=wire.prepare_proposal(job)
    coded=wire.encode_known_response(canonical_fixture(job),proposal['mapping'])
    claim=coded['c'][0]['c'][0]
    detail=claim['g']['g2']['d'][0]
    if bad=='unknown_candidate':coded['c'][0]['i']='alien'
    elif bad=='duplicate_candidate':coded['c'].append(deepcopy(coded['c'][0]))
    elif bad=='missing_candidate':coded['c'].pop()
    elif bad=='unknown_claim':claim['i']='alien'
    elif bad=='duplicate_claim':coded['c'][0]['c'].append(deepcopy(claim))
    elif bad=='missing_claim':coded['c'][0]['c'].pop()
    elif bad=='unknown_detail':detail['i']='alien'
    elif bad=='duplicate_detail':claim['g']['g2']['d'].append(deepcopy(detail))
    elif bad=='missing_detail':claim['g']['g2']['d'].pop()
    elif bad=='unknown_inventory':claim['r']=['alien']
    elif bad=='cross_group_inventory':claim['g']['g0']['d'][0]['m']=['e0']
    elif bad=='cross_candidate_duplicate':claim['o']=coded['c'][1]['c'][0]['i']
    with pytest.raises(ValueError):wire.decode_response(coded,proposal['mapping'])


def test_mapping_and_wire_input_are_hash_bound_and_not_judgment_repairs():
    job=job_fixture();proposal=wire.prepare_proposal(job)
    bad=deepcopy(proposal['mapping']);bad['details']['d0']['text']='Changed meaning'
    with pytest.raises(ValueError,match='map was changed'):wire.check_map(bad)
    bad_payload=deepcopy(proposal['payload']);bad_payload['source']['title']='Changed source'
    with pytest.raises(ValueError,match='payload changed'):wire.restore_payload(bad_payload,proposal['mapping'])
    assert proposal['mapping']['canonical_payload_sha256']==digest(job.payload)
    assert proposal['instructions'].startswith(job.instructions.split('The input pre-enumerates')[0])
    assert 'Return exactly these detail_id/text pairs' not in proposal['instructions']


def test_repeated_reference_lists_and_critical_flags_roundtrip_without_deduplication():
    job=job_fixture();proposal=wire.prepare_proposal(job)
    canonical=canonical_fixture(job)
    claim=canonical['candidates'][0]['claims'][0]
    claim['critical_errors']=['reversed_direction','reversed_direction']
    claim['matched_result_ids']=['r001','r001']
    claim['field_groups']['population']['emitted_details'][0]['matched_inventory_detail_ids']=['r001:population:001','r001:population:001']
    judges.validate_batch_judgment(canonical,job)
    encoded=wire.encode_known_response(canonical,proposal['mapping'])
    decoded=wire.decode_response(encoded,proposal['mapping'])
    assert decoded==canonical
    assert encoded['c'][0]['c'][0]['e']==['reversed_direction','reversed_direction']
    assert encoded['c'][0]['c'][0]['r']==['r0','r0']
