"""One shared selected-extractor checkpoint per source across production/dev."""
from __future__ import annotations

import asyncio
import fcntl
from contextlib import asynccontextmanager
from pathlib import Path

from .io import digest, utcnow, write_json
from .model_runs import ExperimentBlocked, _one
from .production import _extract_path, _json, reuse_development_checkpoint


@asynccontextmanager
async def source_lock(pmid, config, binding):
    key = digest({'pmid': str(pmid), 'extractor': config['extraction'], 'freeze': binding})
    path = Path('data/raw/v2/selected_extraction_locks') / (key + '.lock')
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a') as handle:
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                await asyncio.sleep(.1)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


async def materialize(article, config, binding, *, client=None, allow_paid=False, requested_phase):
    """Reuse first; callers provide an appropriately phased shared client for calls."""
    pmid = str(article['pmid'])
    path = _extract_path(config, pmid)
    async with source_lock(pmid, config, binding):
        if path.exists():
            record = _json(path)
            if record.get('status') not in {'valid', 'invalid'} or record.get('binding', {}).get('registration') != binding:
                raise ExperimentBlocked('Shared selected-extractor checkpoint differs from frozen pipeline')
            return record
        reused = reuse_development_checkpoint(article, config, binding)
        if reused is not None:
            write_json(path, reused)
            return reused
        if not allow_paid or client is None:
            raise ExperimentBlocked('Selected extraction missing; explicit paid execution is disabled')
        ext = config['extraction']
        candidate = {'id': ext['configuration_id'], 'model': ext['model'], 'reasoning': ext['reasoning']}
        directory = path.parent
        base = await _one(client, article, candidate, stage='PRODUCTION_EXTRACTION',
                          directory=directory / 'base' if ext.get('verify') else directory,
                          cap=ext['max_tokens'], registration=binding)
        result = base
        if ext.get('verify') and base['status'] == 'valid':
            verified = await _one(client, article, candidate, stage='PRODUCTION_VERIFICATION',
                                  directory=directory / 'verification', cap=ext['max_tokens'],
                                  registration=binding, original=base['output'])
            result = {**verified, 'base_record': base, 'pipeline_variant': 'extract_then_verify',
                      'elapsed_seconds': base['elapsed_seconds'] + verified['elapsed_seconds'],
                      'usage': base['usage'] + verified['usage']}
        result = {**result, 'first_materialized_for': requested_phase,
                  'first_materialized_at': utcnow(),
                  'accounting_phase': getattr(getattr(client, 'ledger', None), 'phase', 'injected_client')}
        write_json(path, result)
        return result
