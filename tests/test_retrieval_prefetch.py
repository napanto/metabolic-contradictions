"""Fake-search compatibility tests; no real retrieval, inference APIs or sources."""
import gzip
import json
from copy import deepcopy
from pathlib import Path

import pytest

from chl_rag import retrieval_prefetch as prefetch
from chl_rag.io import digest, file_sha, write_json, write_jsonl
from chl_rag.extraction import validate_extraction

FIXTURE=json.loads(Path('configs/synthetic.json').read_text())['cases'][0]
MODEL='z-ai/glm-5.3-flash'


class FakeRetriever:
    def __init__(self,hits):self.hits=hits;self.calls=[]
    def search(self,query,k=20,exclude_pmid=None):
        assert k==21 and exclude_pmid is None
        self.calls.append(query)
        return deepcopy(self.hits)


@pytest.fixture
def project(tmp_path,monkeypatch):
    monkeypatch.chdir(tmp_path)
    pmids=['899900001','899900002','899900003']
    articles=[];outputs={}
    for pmid in pmids:
        article=deepcopy(FIXTURE['article']);article['pmid']=pmid
        articles.append(article)
        outputs[pmid]=validate_extraction(article,deepcopy(FIXTURE['expected_model_output']))
    write_jsonl('data/derived/corpus.jsonl',articles)
    write_json('configs/evaluation_system.json',{'synthetic_only':True})
    write_json('configs/vocabularies.json',{'concepts':[]})
    path=Path('data/derived/pubtator.jsonl.gz')
    with gzip.open(path,'wt') as stream:
        for pmid in pmids:stream.write(json.dumps({'pmid':pmid,'entities':[],'relations':[]})+'\n')
    write_json('results/index.json',{'synthetic_only':True})
    binding={'config_sha256':file_sha('configs/evaluation_system.json'),'production_design':'prespecified_fixed'}
    config={'production_design':'prespecified_fixed','extraction':{'configuration_id':'glm-5.3-flash__high',
        'model':MODEL,'reasoning':'high','max_tokens':131072,'verify':False},
        'source_sha256':{p:file_sha(p) for p in ['results/index.json','configs/vocabularies.json','data/derived/pubtator.jsonl.gz']}}
    from chl_rag import production as prod
    guard=lambda *a,**k:(config,binding)
    scope=lambda *a,**k:(pmids,{'partition_pmids':{'F':pmids}})
    monkeypatch.setattr(prefetch,'production_guard',guard);monkeypatch.setattr(prefetch,'_scope',scope)
    monkeypatch.setattr(prod,'production_guard',guard);monkeypatch.setattr(prod,'_scope',scope)
    def save(pmid,status='valid',output=None):
        ext=config['extraction'];record={
            'status':status,'pmid':pmid,'stage':'PRODUCTION_EXTRACTION','configuration_id':ext['configuration_id'],
            'model':MODEL,'reasoning':'high','max_tokens':131072,'final_valid':status=='valid',
            'binding':{'registration':binding,'pmid':pmid,'stage':'PRODUCTION_EXTRACTION',
                       'configuration_id':ext['configuration_id'],'cap':131072,'request_identity':'f'*64},
            'completed_at':'2026-11-14T00:00:00+00:00',
            'output':deepcopy(output or outputs[pmid]) if status=='valid' else None}
        path=prod._extract_path(config,pmid);write_json(path,record)
        return path,record
    hits=[{'pmid':pmids[0],'rrf_score':.04},{'pmid':pmids[1],'rrf_score':.03},{'pmid':'outside-F','rrf_score':.02}]
    return config,binding,pmids,outputs,save,FakeRetriever(hits)


def test_exact_queries_and_cache_contract_retain_self_and_nonF_hits(project):
    config,_binding,pmids,_outputs,save,retriever=project
    save(pmids[0]);save(pmids[1])
    worker=prefetch.Prefetcher(retriever=retriever)
    result=worker.once()
    assert retriever.calls==['Nuvora marker Alben','Nuvora marker Borin']
    assert result['terminal_sources']==2 and result['pending_sources']==1
    assert result['unique_queries_warmed']==2 and result['model_API_calls']==0
    for query in retriever.calls:
        key=digest({'index':config['source_sha256']['results/index.json'],'query':query,'k':21})
        body=json.loads((prefetch.CACHE/(key+'.json')).read_text())
        assert body=={'index_sha256':config['source_sha256']['results/index.json'],'query':query,'hits':retriever.hits}
    assert not Path('data/derived/production_uncorrected/full/claims_normalized.jsonl').exists()
    assert not Path('annotation/package/items.jsonl').exists()


def test_watch_progress_reuses_queries_and_accepts_metadata_only_materialization(project):
    _config,_binding,pmids,_outputs,save,retriever=project
    path,record=save(pmids[0]);worker=prefetch.Prefetcher(retriever=retriever)
    worker.once()
    record['first_materialized_for']='production';write_json(path,record)
    save(pmids[1]);save(pmids[2],'invalid')
    result=worker.once()
    assert len(retriever.calls)==2
    assert result['status']=='complete' and result['invalid_terminal_sources']==1
    assert result['metadata_only_checkpoint_rewrites']==1
    assert result['normalized_claims']==4
    original={p:p.read_bytes() for p in prefetch.CACHE.glob('*.json')}
    assert worker.once()['status']=='complete'
    assert {p:p.read_bytes() for p in prefetch.CACHE.glob('*.json')}==original


def test_new_process_validates_cache_hits_without_loading_CPU_encoder(project,monkeypatch):
    _config,_binding,pmids,_outputs,save,retriever=project
    save(pmids[0]);prefetch.Prefetcher(retriever=retriever).once()
    monkeypatch.setattr(prefetch,'readonly_retriever',lambda:pytest.fail('Warm cache loaded an encoder'))
    result=prefetch.Prefetcher().once()
    assert result['cache_hit_queries']==2 and result['searched_queries']==0


@pytest.mark.parametrize('change',['record_pmid','binding_pmid','registration','model','output_pmid','stage'])
def test_foreign_or_mismatched_terminal_checkpoint_never_warms_cache(project,change):
    _config,_binding,pmids,_outputs,save,retriever=project
    path,row=save(pmids[0])
    if change=='record_pmid':row['pmid']='foreign'
    elif change=='binding_pmid':row['binding']['pmid']='foreign'
    elif change=='registration':row['binding']['registration']={'foreign':True}
    elif change=='model':row['model']='other'
    elif change=='output_pmid':row['output']['extraction']['pmid']='foreign'
    else:row['stage']='H'
    write_json(path,row)
    with pytest.raises(prefetch.PrefetchError):prefetch.Prefetcher(retriever=retriever).once()
    assert not retriever.calls and not list(prefetch.CACHE.glob('*.json'))


def test_nonterminal_and_foreign_source_files_are_not_consumed(project):
    config,_binding,pmids,_outputs,save,retriever=project
    save(pmids[0],'started')
    directory=prefetch._extract_path(config,pmids[0]).parent
    write_json(directory/'not-in-F.json',{'status':'valid','untrusted_output':'not read'})
    result=prefetch.Prefetcher(retriever=retriever).once()
    assert result['terminal_sources']==0 and not retriever.calls


def test_hedged_and_unclear_comparison_do_not_generate_queries(project):
    _config,_binding,pmids,outputs,save,retriever=project
    output=deepcopy(outputs[pmids[0]])
    output['extraction']['claims'][0]['result']['hedged']=True
    output['extraction']['claims'][1]['comparison']['type']='unclear'
    save(pmids[0],output=output)
    result=prefetch.Prefetcher(retriever=retriever).once()
    assert result['hedged_claims_skipped']==result['unclear_comparison_claims_skipped']==1
    assert result['unique_queries_warmed']==0 and not retriever.calls


def test_changed_scientific_checkpoint_and_changed_cache_fail_closed(project):
    _config,_binding,pmids,_outputs,save,retriever=project
    path,row=save(pmids[0]);worker=prefetch.Prefetcher(retriever=retriever);worker.once()
    row['output']['extraction']['claims'][0]['exposure']='changed'
    write_json(path,row)
    with pytest.raises(prefetch.PrefetchError,match='scientific_output_changed'):worker.once()
    save(pmids[0])
    cache=next(prefetch.CACHE.glob('*.json'));body=json.loads(cache.read_text());body['query']='wrong query'
    write_json(cache,body)
    with pytest.raises(prefetch.PrefetchError,match='cache_identity_mismatch'):worker.once()


def test_invalid_existing_cache_is_not_overwritten_or_used(project):
    config,_binding,pmids,_outputs,save,retriever=project;save(pmids[0])
    query='Nuvora marker Alben';index=config['source_sha256']['results/index.json']
    path=prefetch.CACHE/(prefetch.cache_key(index,query)+'.json')
    write_json(path,{'index_sha256':index,'query':'wrong','hits':[]});before=path.read_bytes()
    with pytest.raises(prefetch.PrefetchError,match='identity_mismatch'):
        prefetch.Prefetcher(retriever=retriever).once()
    assert path.read_bytes()==before and not retriever.calls


def test_atomic_publish_preserves_concurrent_different_result(project,monkeypatch):
    config,_binding,_pmids,_outputs,_save,retriever=project
    query='Nuvora marker Alben';index=config['source_sha256']['results/index.json']
    path=prefetch.CACHE/(prefetch.cache_key(index,query)+'.json')
    original=prefetch.os.link
    concurrent={'index_sha256':index,'query':query,'hits':[{'pmid':'different','rrf_score':.1}]}
    def race(temporary,target):
        write_json(target,concurrent)
        original(temporary,target)
    monkeypatch.setattr(prefetch.os,'link',race)
    with pytest.raises(prefetch.PrefetchError,match='concurrent_retrieval_result_conflict'):
        prefetch.warm_query(index,query,lambda:retriever)
    assert json.loads(path.read_text())==concurrent
    assert not list(prefetch.CACHE.glob('*.tmp'))


def test_config_change_blocks_before_queries(project):
    _config,_binding,pmids,_outputs,save,retriever=project;save(pmids[0])
    worker=prefetch.Prefetcher(retriever=retriever)
    write_json('configs/evaluation_system.json',{'changed':True})
    with pytest.raises(prefetch.PrefetchError,match='configuration_changed'):worker.once()
    assert not retriever.calls


@pytest.mark.parametrize('hits',[
    [{'pmid':'same','rrf_score':.04},{'pmid':'same','rrf_score':.03}],
    [{'pmid':'low','rrf_score':.01},{'pmid':'high','rrf_score':.03}],
    [{'pmid':'bad','rrf_score':float('nan')}],
    [{'pmid':'bad','rrf_score':True}],
])
def test_invalid_search_output_is_never_published(project,hits):
    config,*_=project
    with pytest.raises(prefetch.PrefetchError):
        prefetch.warm_query(config['source_sha256']['results/index.json'],'invented query',lambda:FakeRetriever(hits))
    assert not list(prefetch.CACHE.glob('*.json'))


def test_prefetched_cache_is_consumed_by_unmodified_build_frame_with_identical_outputs(project):
    _config,_binding,pmids,_outputs,save,retriever=project
    for pmid in pmids[:2]:save(pmid)
    save(pmids[2],'invalid')
    worker=prefetch.Prefetcher(retriever=retriever);assert worker.once()['status']=='complete'
    from chl_rag import production as prod
    class NoSearch:
        def search(self,*a,**k):pytest.fail('Frozen frame builder ignored matching prefetched cache')
    warm=prod.build_frame('full',retriever=NoSearch())
    files=[prod.BASE/'full'/name for name in ['claims_normalized.jsonl','pairs_candidates.jsonl','pairs_key.jsonl']]
    warm_bytes={str(path):path.read_bytes() for path in files}
    prefetch.CACHE.rename(prefetch.CACHE.with_name('saved_prefetch'))
    cold_retriever=FakeRetriever(retriever.hits)
    cold=prod.build_frame('full',retriever=cold_retriever)
    assert len(cold_retriever.calls)==2
    assert {str(path):path.read_bytes() for path in files}==warm_bytes
    assert cold['pairs']==warm['pairs'] and cold['retrieval_queries']==warm['retrieval_queries']==2
