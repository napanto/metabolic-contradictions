"""Warm the frozen production query cache from terminal F extractions only.

No model API, GPU, candidate pairing, sampling or reference data. The local CPU
retriever, query construction, cache identity and payload match build_frame.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import math
import os
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

from .io import canonical, digest, file_sha, read_jsonl, utcnow, write_json
from .entity_normalize import Normalizer
from .extraction import flatten_extraction
from .candidate_pairs import normalize_record
from .production import CONFIG, _extract_path, _scope, production_guard, readonly_retriever

CACHE = Path('data/raw/v2/production_retrieval')
PROGRESS = Path('results/retrieval_prefetch.json')
K = 21


class PrefetchError(ValueError):
    """Safe short diagnostic; no checkpoint contents or query text in messages."""


def cache_key(index_identity, query):
    return digest({'index': index_identity, 'query': query, 'k': K})


def validate_cache(value, index_identity, query):
    if (not isinstance(value, dict) or set(value) != {'index_sha256', 'query', 'hits'}
            or value.get('index_sha256') != index_identity or value.get('query') != query):
        raise PrefetchError('retrieval_cache_identity_mismatch')
    hits = value['hits']
    if not isinstance(hits, list) or len(hits) > K:
        raise PrefetchError('retrieval_cache_invalid_hits')
    seen, order = set(), []
    for hit in hits:
        if (not isinstance(hit, dict) or set(hit) != {'pmid', 'rrf_score'}
                or not isinstance(hit['pmid'], str) or not hit['pmid'] or hit['pmid'] in seen
                or type(hit['rrf_score']) not in {int, float}
                or not math.isfinite(hit['rrf_score']) or hit['rrf_score'] < 0):
            raise PrefetchError('retrieval_cache_invalid_hits')
        seen.add(hit['pmid']); order.append((-hit['rrf_score'], hit['pmid']))
    if order != sorted(order):
        raise PrefetchError('retrieval_cache_invalid_rank_order')
    return value


def _read_cache(path, index_identity, query):
    if path.is_symlink() or not path.is_file():
        raise PrefetchError('retrieval_cache_invalid_file')
    try:
        return validate_cache(json.loads(path.read_text()), index_identity, query)
    except (OSError, json.JSONDecodeError) as exc:
        raise PrefetchError('retrieval_cache_unreadable') from exc


@contextmanager
def _query_lock(key):
    directory = CACHE / '.prefetch_locks'
    if CACHE.is_symlink() or directory.is_symlink():
        raise PrefetchError('retrieval_cache_symlink')
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / (key + '.lock')).open('a') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def _publish(path, payload, index_identity, query):
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, suffix='.prefetch.tmp')
    try:
        with os.fdopen(descriptor, 'w') as stream:
            stream.write(json.dumps(payload, ensure_ascii=False, indent=2)+'\n')
            stream.flush(); os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
            return 'published'
        except FileExistsError:
            existing = _read_cache(path, index_identity, query)
            if canonical(existing) != canonical(payload):
                raise PrefetchError('concurrent_retrieval_result_conflict') from None
            return 'concurrent_identical'
    finally:
        Path(temporary).unlink(missing_ok=True)


def warm_query(index_identity, query, get_retriever):
    """Use identical query-wide results; never filter self or non-F hits here."""
    key = cache_key(index_identity, query)
    path = CACHE / (key + '.json')
    with _query_lock(key):
        if path.exists():
            _read_cache(path, index_identity, query)
            return 'cache_hit'
        hits = get_retriever().search(query, k=K)
        payload = validate_cache({'index_sha256':index_identity, 'query':query, 'hits':hits}, index_identity, query)
        return _publish(path, payload, index_identity, query)


class Prefetcher:
    def __init__(self, config_path=CONFIG, *, retriever=None):
        self.config_path = Path(config_path)
        self.config, self.binding = production_guard(self.config_path)
        self.order, _partitions = _scope('full', self.binding)
        self.allowed = set(self.order)
        if len(self.allowed) != len(self.order):
            raise PrefetchError('duplicate_F_source')
        self.index_identity = self.config['source_sha256'].get('results/index.json')
        if not self.index_identity:
            raise PrefetchError('missing_frozen_index_identity')
        self._retriever = retriever
        self._normalizer = None
        self.seen = {}
        self.queries = set()
        self.pending_queries = set()
        self.cache_proofs = {}
        self.stats = {'searched_queries':0, 'published_queries':0, 'cache_hit_queries':0,
                      'concurrent_identical_queries':0, 'normalized_claims':0,
                      'hedged_claims_skipped':0, 'unclear_comparison_claims_skipped':0,
                      'metadata_only_checkpoint_rewrites':0, 'polls':0}
        self._check_frozen()

    def _check_frozen(self):
        if file_sha(self.config_path) != self.binding['config_sha256']:
            raise PrefetchError('registered_configuration_changed')
        for path in ('results/index.json', 'configs/vocabularies.json'):
            if file_sha(path) != self.config['source_sha256'].get(path):
                raise PrefetchError('frozen_normalization_or_index_metadata_changed')

    def _get_normalizer(self):
        if self._normalizer is None:
            path = 'data/derived/pubtator.jsonl.gz'
            expected = self.config['source_sha256'].get(path)
            if not expected or file_sha(path) != expected:
                raise PrefetchError('frozen_pubtator_changed')
            annotations = {row['pmid']:row for row in read_jsonl(path) if row['pmid'] in self.allowed}
            if file_sha(path) != expected:
                raise PrefetchError('pubtator_changed_while_loading')
            self._normalizer = Normalizer(pubtator=annotations)
        return self._normalizer

    def _get_retriever(self):
        if self._retriever is None:
            index = json.loads(Path('results/index.json').read_text())
            if (file_sha('data/index/medcpt.faiss') != index.get('index_sha256')
                    or json.loads(Path('configs/medcpt.json').read_text()) != index.get('models')):
                raise PrefetchError('frozen_local_index_or_encoder_revision_changed')
            self._retriever = readonly_retriever()
            if any(parameter.device.type != 'cpu' for parameter in self._retriever.model.parameters()):
                raise PrefetchError('retrieval_requires_CPU_only')
        return self._retriever

    def _terminal(self, pmid):
        if pmid not in self.allowed or Path(pmid).name != pmid:
            raise PrefetchError('checkpoint_outside_F')
        path = _extract_path(self.config, pmid)
        if path.is_symlink():
            raise PrefetchError('checkpoint_symlink')
        if not path.exists():
            if pmid in self.seen:raise PrefetchError('terminal_checkpoint_disappeared')
            return None
        stat = path.stat()
        fingerprint = (stat.st_ino, stat.st_size, stat.st_mtime_ns)
        prior = self.seen.get(pmid)
        if prior and prior['fingerprint'] == fingerprint:
            return None
        raw = path.read_bytes()
        try:
            record = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise PrefetchError('checkpoint_not_atomic_JSON') from exc
        if record.get('status') not in {'valid', 'invalid'}:
            if prior:raise PrefetchError('terminal_checkpoint_became_nonterminal')
            return None
        ext = self.config['extraction']; proof = record.get('binding', {})
        if (record.get('pmid') != pmid or proof.get('pmid') != pmid
                or proof.get('registration') != self.binding
                or proof.get('stage') != 'PRODUCTION_EXTRACTION' or record.get('stage') != 'PRODUCTION_EXTRACTION'
                or proof.get('configuration_id') != ext['configuration_id']
                or record.get('configuration_id') != ext['configuration_id']
                or proof.get('cap') != ext['max_tokens']
                or any(record.get(key) != ext[key] for key in ('model', 'reasoning'))
                or record.get('max_tokens') != ext['max_tokens']):
            raise PrefetchError('terminal_checkpoint_binding_or_PMID_mismatch')
        output = record.get('output')
        if record['status'] == 'valid':
            if (not isinstance(output, dict) or output.get('extraction', {}).get('pmid') != pmid
                    or record.get('final_valid') is not True):
                raise PrefetchError('terminal_extraction_PMID_or_validity_mismatch')
        elif output is not None or record.get('final_valid') is not False:
            raise PrefetchError('invalid_terminal_checkpoint_contains_output')
        scientific = digest({'binding':proof, 'status':record['status'], 'output':output})
        if prior and prior['scientific_sha256'] != scientific:
            raise PrefetchError('terminal_scientific_output_changed')
        import hashlib
        self.seen[pmid] = {'fingerprint':fingerprint, 'sha256':hashlib.sha256(raw).hexdigest(),
                           'scientific_sha256':scientific, 'status':record['status']}
        if prior:
            self.stats['metadata_only_checkpoint_rewrites'] += 1
            return None
        return record

    def once(self):
        self._check_frozen()
        self.stats['polls'] += 1
        for query, proof in self.cache_proofs.items():
            path = CACHE / (cache_key(self.index_identity, query)+'.json')
            if not path.is_file() or path.is_symlink():
                raise PrefetchError('previously_warmed_cache_missing_or_symlink')
            stat = path.stat(); fingerprint = (stat.st_ino,stat.st_size,stat.st_mtime_ns)
            if fingerprint != proof['fingerprint']:
                current = _read_cache(path,self.index_identity,query)
                if digest(current) != proof['sha256']:
                    raise PrefetchError('previously_warmed_cache_changed')
                proof['fingerprint'] = fingerprint
        for pmid in self.order:
            record = self._terminal(pmid)
            if record is None or record['status'] != 'valid':continue
            normalizer = self._get_normalizer()
            for row in flatten_extraction(record['output']['extraction']):
                normalized = normalize_record(row, normalizer)
                self.stats['normalized_claims'] += 1
                if normalized['hedged']:
                    self.stats['hedged_claims_skipped'] += 1
                    continue
                if not normalized['comparison_identifiable']:
                    self.stats['unclear_comparison_claims_skipped'] += 1
                    continue
                query = normalized['normalization']['exposure']['label'] + ' ' + normalized['normalization']['outcome']['label']
                if query not in self.queries:self.pending_queries.add(query)
        for query in sorted(self.pending_queries):
            self._check_frozen()
            state = warm_query(self.index_identity, query, self._get_retriever)
            if state != 'cache_hit':self.stats['searched_queries'] += 1
            self.stats[{'published':'published_queries', 'cache_hit':'cache_hit_queries',
                        'concurrent_identical':'concurrent_identical_queries'}[state]] += 1
            self.queries.add(query)
            path = CACHE / (cache_key(self.index_identity,query)+'.json')
            current = _read_cache(path,self.index_identity,query)
            stat = path.stat()
            self.cache_proofs[query] = {'sha256':digest(current), 'fingerprint':(stat.st_ino,stat.st_size,stat.st_mtime_ns)}
            self.pending_queries.remove(query)
            if len(self.queries) % 25 == 0:self.progress('warming')
        complete = len(self.seen) == len(self.order)
        return self.progress('complete' if complete else 'snapshot_complete')

    def progress(self, status):
        report = {'status':status, 'updated_at':utcnow(), 'config_sha256':self.binding['config_sha256'],
                  'index_sha256':self.index_identity, 'module_sha256':file_sha(__file__),
                  'expected_F_sources':len(self.order), 'terminal_sources':len(self.seen),
                  'valid_terminal_sources':sum(row['status']=='valid' for row in self.seen.values()),
                  'invalid_terminal_sources':sum(row['status']=='invalid' for row in self.seen.values()),
                  'pending_sources':len(self.order)-len(self.seen), 'unique_queries_warmed':len(self.queries),
                  'pending_queries':len(self.pending_queries),
                  'checkpoint_set_sha256':digest({p:row['scientific_sha256'] for p,row in sorted(self.seen.items())}),
                  'checkpoint_bytes_set_sha256':digest({p:row['sha256'] for p,row in sorted(self.seen.items())}),
                  'query_keys_sha256':digest(sorted(cache_key(self.index_identity,q) for q in self.queries)),
                  **self.stats, 'model_API_calls':0, 'device':'cpu', 'candidate_pairs_created':0,
                  'C_or_human_data_read':False, 'query_cache_contract':'unchanged production build_frame k=21'}
        write_json(PROGRESS, report)
        return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--once', action='store_true', help='One pass over current terminal F outputs (default)')
    mode.add_argument('--watch', action='store_true', help='Poll until all F sources are terminal; stop before build_frame')
    parser.add_argument('--poll-seconds', type=int, choices=[30,60], default=30)
    parser.add_argument('--config', default=str(CONFIG))
    args = parser.parse_args()
    lock = Path('data/raw/v2/retrieval_prefetch.lock'); lock.parent.mkdir(parents=True,exist_ok=True)
    with lock.open('a') as handle:
        try:
            fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit('A retrieval prefetch controller is already active') from None
        prefetch = None
        try:
            prefetch = Prefetcher(args.config)
            while True:
                result = prefetch.once()
                print(json.dumps(result), flush=True)
                if not args.watch or result['status']=='complete':break
                time.sleep(args.poll_seconds)
        except KeyboardInterrupt:
            print(json.dumps(prefetch.progress('stopped') if prefetch else {'status':'stopped'}), flush=True)
        except Exception as exc:  # noqa: BLE001 -- CLI exposes safe codes/counts, never raw extraction/query text.
            result = prefetch.progress('blocked') if prefetch else {'status':'blocked'}
            result.update(error_type=type(exc).__name__, reason=str(exc) if isinstance(exc,PrefetchError) else 'offline_validation_failed')
            write_json(PROGRESS,result)
            print(json.dumps(result),flush=True)
            raise SystemExit(1) from None


if __name__=='__main__':main()
