"""Read the frozen v1 extraction as D0; never generates or reads oracle labels.

The original valid raw cache response supplies every emitted occurrence before
v1's deterministic evidence/duplicate filters. Legacy context retains its
original ambiguous scope instead of acquiring invented v2 groups or totals.
"""
from __future__ import annotations

import gzip
import json
from collections import Counter
from copy import deepcopy
from pathlib import Path

import jsonschema

from .io import digest, file_sha

V1_MODEL = "qwen/qwen3.6-35b-a3b"
CONTEXT_FIELDS = ("population", "n", "design", "region", "duration", "dose", "sample")
RAW_FIELDS = {"arm", "exposure", "exposure_class", "comparator", "outcome", "kind",
              "sign", "significant", "hedged", "context", "evidence"}


def adapt_claim(claim, ordinal):
    """Faithful partial projection plus visible original legacy-only information."""
    if set(claim) - RAW_FIELDS:
        raise ValueError("D0 raw claim includes unexpected fields/provenance")
    if claim.get("sign") not in {"+", "-", "0"}:
        raise ValueError("Unrecognized frozen v1 sign")
    if type(claim.get("significant")) is not bool and claim.get("significant") is not None:
        raise ValueError("Unrecognized frozen v1 significance flag")
    context = claim.get("context", {})
    if set(context) - set(CONTEXT_FIELDS):
        raise ValueError("Unexpected frozen v1 context field")
    return {"claim_id": f"c{ordinal:04d}",
            **{k: deepcopy(claim.get(k)) for k in ("arm", "exposure", "exposure_class", "outcome", "kind")},
            "comparison": {"comparator": claim.get("comparator")},
            "result": {"assertion": "explicit_no_difference" if claim["sign"] == "0" else "directional",
                       "estimate_direction": None if claim["sign"] == "0" else claim["sign"],
                       "hedged": claim.get("hedged", False)},
            "legacy_significant": claim.get("significant"),
            "legacy_context": {k: deepcopy(context.get(k)) for k in CONTEXT_FIELDS},
            "legacy_evidence_ids": deepcopy(claim.get("evidence", []))}


def load_d0(pmids, *, root=Path('.'), allow_heldout=False):
    """Return per-PMID candidate payload and private file provenance.

    Call only after prospective registration. H is refused by default; opening
    it also requires the controller's unique-winner/one-use authorization.
    No credentials, model client, previous labels, or participant files are read.
    """
    root = Path(root)
    pmids = list(map(str, pmids))
    if len(pmids) != len(set(pmids)) or any(not p.isdigit() for p in pmids):
        raise ValueError("D0 expects unique numeric PMIDs")
    manifest_path = root / "data/derived/partitions.json"
    if manifest_path.exists():
        partitions = json.loads(manifest_path.read_text())["partition_pmids"]
        if set(pmids) - set(partitions["S"]) - set(partitions["H"]):
            raise ValueError("D0 is restricted to the frozen S/H sources")
        if set(pmids) & set(partitions["H"]) and not allow_heldout:
            raise ValueError("Held-out D0 access requires the locked winner's one-use authorization")
    return _cached_d0(pmids, root=root)


def _cached_d0(pmids, *, root, final_retained=False, final_sources=None):
    """Internal cache reader; callers enforce their distinct source authorization."""
    schema_path = root / "configs/prototype_extraction_prompt.json"
    schema = json.loads(schema_path.read_text())["schema"]
    checkpoints = root / "data/raw/extraction/qwen__qwen3.6-35b-a3b__full"
    rows = {}
    for pmid in pmids:
        checkpoint_path = checkpoints / (pmid + ".json")
        if not checkpoint_path.exists():
            raise ValueError("Frozen D0 checkpoint missing; regeneration is forbidden: " + pmid)
        checkpoint = json.loads(checkpoint_path.read_text())
        if str(checkpoint["pmid"]) != pmid:
            raise ValueError("Frozen D0 checkpoint source mismatch")
        provenance = {"checkpoint_sha256": file_sha(checkpoint_path),
                      "status": checkpoint["status"], "raw_cache_sha256": None,
                      "original_schema_sha256": file_sha(schema_path)}
        if checkpoint["status"] == "failed":
            raw_claims = []
            provenance["failure_counts_as_no_recovered_results"] = True
        elif checkpoint["status"] == "ok":
            attempts = checkpoint.get("meta", {}).get("attempts", [])
            if not attempts and checkpoint.get("meta", {}).get("no_eligible_sentences"):
                raw_claims = []
            elif attempts:
                key = attempts[-1]["request_hash"]
                if len(key) != 64 or any(c not in '0123456789abcdef' for c in key):
                    raise ValueError("Invalid D0 cache identity")
                cache_path = root / "data/raw/llm_cache" / (key + ".json.gz")
                with gzip.open(cache_path, "rt") as handle:
                    cached = json.load(handle)
                request, response = cached["request"], cached["response"]
                if digest(request) != key or request.get("model") != V1_MODEL:
                    raise ValueError("Frozen D0 cache request identity/model mismatch")
                if final_sources is not None:
                    messages=[m for m in request.get('messages',[]) if m.get('role')=='user']
                    if len(messages)!=1:
                        raise ValueError('Final D0 source request is not uniquely reconstructible')
                    source=json.loads(messages[0]['content']);expected=final_sources[pmid]
                    if (str(source.get('pmid'))!=pmid
                            or [(r['id'],r['text']) for r in source.get('sentences',[])]
                                != [(r['id'],r['text']) for r in expected['sentences']]
                            or source.get('title')!=expected['title']
                            or source.get('publication_types')!=expected['publication_types']):
                        raise ValueError('Saved D0 and frozen C abstract source differ')
                    provenance['original_source_payload_sha256']=digest(source)
                if response.get("id") != checkpoint["meta"].get("generation_id"):
                    raise ValueError("Frozen D0 cache generation mismatch")
                choice = response["choices"][0]
                if choice.get("finish_reason") == "length":
                    raise ValueError("Successful D0 checkpoint references truncated output")
                parsed = json.loads(choice["message"]["content"])
                jsonschema.validate(parsed, schema)
                raw_claims = parsed["claims"]
                provenance["raw_cache_sha256"] = file_sha(cache_path)
                provenance["request_sha256"] = key
            else:
                raise ValueError("Frozen D0 has no valid response provenance")
            if len(raw_claims) != checkpoint.get("raw_claim_count"):
                raise ValueError("Frozen D0 raw emission count mismatch")
            if len(checkpoint["claims"]) + len(checkpoint["dropped"]) != len(raw_claims):
                raise ValueError("Frozen D0 retained/dropped accounting mismatch")
        else:
            raise ValueError("Unknown frozen D0 checkpoint status")
        accepted=Counter(digest({k:v for k,v in claim.items() if k in RAW_FIELDS}) for claim in checkpoint.get("claims",[]))
        retained=[]
        for i,claim in enumerate(raw_claims,1):
            key=digest(claim)
            if accepted[key]>0:
                retained.append(f"c{i:04d}")
                accepted[key]-=1
        if any(accepted.values()):
            raise ValueError("D0 retained occurrences cannot be recovered reliably from frozen cache/checkpoint")
        rows[pmid] = {"pmid": pmid, "claims": [adapt_claim(c, i) for i, c in enumerate(raw_claims, 1)],
                      "recovery_eligible_ids":retained,"recovery_mask_reliable":True,
                      "study": None, "unrepresented_groups": [], "legacy_baseline": True,
                      "provenance": provenance,
                      "interpretation": "Original emitted v1 occurrences, including deterministic drops. Legacy population/n keep their unanchored scope; no v2 group or study total is invented. Composite rich-field score is not comparable."}
        if final_retained:
            rows[pmid] = final_d0_from_checkpoint(checkpoint, pmid, provenance=provenance)
    return rows


def final_d0_from_checkpoint(checkpoint, pmid, *, provenance=None):
    """Pure retained-output projection for final same-abstract comparison.

    Preserve raw v1 semantics. No invented v2 groups/types, null completions or
    regenerated findings. The caller separately verifies the original raw cache.
    """
    from .prototype_schemas import EXTRACTION
    if str(checkpoint.get('pmid'))!=str(pmid) or checkpoint.get('status') not in {'ok','failed'}:
        raise ValueError('Final D0 checkpoint identity/status mismatch')
    kept=checkpoint.get('claims',[])
    if checkpoint['status']=='failed' and kept:
        raise ValueError('A failed D0 checkpoint cannot retain claims')
    claims=[]; original_ids=[]
    extra={'claim_id','pmid','source_query_arms','evidence_text','evidence_rules','priority','retracted'}
    for i,claim in enumerate(kept,1):
        if set(claim)-RAW_FIELDS-extra:
            raise ValueError('Unexpected final D0 claim/provenance field')
        raw={k:deepcopy(v) for k,v in claim.items() if k in RAW_FIELDS}
        jsonschema.validate({'claims':[raw]},EXTRACTION)
        if claim.get('pmid',str(pmid))!=str(pmid):
            raise ValueError('Final D0 retained claim source mismatch')
        claims.append({**raw,'claim_id':f'd0-c{i:04d}'})
        original_ids.append(claim.get('claim_id'))
    raw_count=checkpoint.get('raw_claim_count')
    drops=checkpoint.get('dropped',[])
    if checkpoint['status']=='ok' and (type(raw_count) is not int or raw_count!=len(kept)+len(drops)):
        raise ValueError('Final D0 retained/drop accounting mismatch')
    return {'pmid':str(pmid),'claims':claims,'legacy_baseline':True,
        'provenance':{**(provenance or {}),'original_retained_claim_ids':original_ids,
            'status':checkpoint['status'],'raw_claim_count':raw_count,'retained_claim_count':len(kept),
            'deterministic_drop_count':len(drops),'drop_reasons':dict(Counter(x.get('reason','unknown') for x in drops))},
        'unrepresented_diagnostics':['comparison','population','sample_size','analysis','significance'],
        'interpretation':'Saved final retained v1 extraction; raw emissions/drops are audit only. Original ambiguous legacy scope and nulls remain unchanged. No rich-field diagnostic accuracy is imputed.'}


def load_final_d0(pmids, *, root=Path('.'), evaluation_config_path='configs/evaluation_system.json'):
    """Open only all frozen C extraction sources after signed amendment06.

    No S/H access, regeneration, inference or label reading. This loader is for
    freezing the private baseline companion before the first final oracle call.
    """
    from .io import read_jsonl
    from .model_runs import _signed_file
    root=Path(root)
    config_path=root/evaluation_config_path
    if not config_path.exists():
        raise ValueError('Final D0 requires registered amendment06 before source access')
    config=json.loads(config_path.read_text())
    if (config.get('status')!='registered' or config.get('amendment')!=6
            or config.get('pipeline_version')!='2.0'
            or config.get('extraction_comparison',{}).get('baseline')!='saved_v1_retained'):
        raise ValueError('Final D0 requires the registered saved-v1-retained comparison')
    _signed_file(config_path)
    _signed_file(root/config.get('protocol','PROTOCOL_AMENDMENT_06_V2_EVALUATION.md'))
    package_path=root/'annotation/package/items.jsonl'
    manifest_path=root/'annotation/package/manifest.json'
    _signed_file(package_path);_signed_file(manifest_path)
    package=list(read_jsonl(package_path));manifest=json.loads(manifest_path.read_text())
    if (manifest.get('evaluation_config_sha256')!=file_sha(config_path)
            or manifest.get('package_sha256')!=digest(package)
            or manifest.get('human_subset_frozen_before_oracle') is not True):
        raise ValueError('Final D0 requires the frozen prospective C package')
    expected=[str(i['article']['pmid']) for i in package if i['task']=='extraction']
    pmids=list(map(str,pmids))
    if len(pmids)!=len(set(pmids)) or set(pmids)!=set(expected) or any(not p.isdigit() for p in pmids):
        raise ValueError('Final D0 can open exactly the frozen C extraction sources')
    partitions=json.loads((root/'data/derived/partitions.json').read_text())['partition_pmids']
    if set(pmids)&(set(partitions['S'])|set(partitions['H'])):
        raise ValueError('Final D0 cannot open unused S/H sources')
    sources={str(i['article']['pmid']):i['article'] for i in package if i['task']=='extraction'}
    return _cached_d0(pmids,root=root,final_retained=True,final_sources=sources)
