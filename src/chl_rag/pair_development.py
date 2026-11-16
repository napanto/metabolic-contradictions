"""Synthetic P-only technical prompt checks using the external benchmark winner.

This does not select the primary v2 classifier; that separate selection uses the
new metabolic development-pair experiment under decisions section14.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
from collections import Counter
from pathlib import Path

from .io import digest, file_sha, utcnow, write_json
from .model_client import V2Client
from .model_runs import ExperimentBlocked, _signed_file, registration_guard
from .production import _classify_one, _workers

FIXTURE = Path('configs/pair_synthetic.json')
SELECTION = Path('../project-clf/results/v2_classifier_selection.json')


def fixture_inputs(case):
    item = case['item']
    articles, extractions = {}, {}
    for side in ['a', 'b']:
        article, claim = item[side]['article'], item[side]['claim']
        articles[article['pmid']] = article
        extractions[article['pmid']] = {'schema_version': '2.0', 'pmid': article['pmid'],
                                       'study': article['study'], 'claims': [claim]}
    pair = {'pair_id': item['item_id'], 'arm': item['arm'],
            'pmid_a': item['a']['article']['pmid'], 'pmid_b': item['b']['article']['pmid'],
            'claim_a': item['a']['claim']['claim_id'], 'claim_b': item['b']['claim']['claim_id']}
    return pair, articles, extractions


def selected_classifier(path=SELECTION):
    path = Path(path).resolve()
    root_result = subprocess.run(['git', '-C', str(path.parent), 'rev-parse', '--show-toplevel'],
                                 capture_output=True, text=True, check=True)
    root = Path(root_result.stdout.strip())
    relative = path.relative_to(root).as_posix()
    committed = subprocess.run(['git', '-C', str(root), 'show', f'HEAD:{relative}'], capture_output=True, check=True)
    if committed.stdout != path.read_bytes():
        raise ExperimentBlocked('Classifier selection artifact differs from signed worker checkout')
    revision = subprocess.run(['git', '-C', str(root), 'log', '-1', '--format=%H', '--', relative],
                              capture_output=True, text=True, check=True).stdout.strip()
    if subprocess.run(['git', '-C', str(root), 'verify-commit', revision], capture_output=True, check=False).returncode:
        raise ExperimentBlocked('Classifier selection is not verifiably signed')
    value = json.loads(path.read_text())
    if value.get('status') == 'prespecified':
        if (value.get('production_design') != 'prespecified_fixed' or
                value.get('selected') != {'model': 'z-ai/glm-5.3-flash', 'reasoning': 'low'}):
            raise ExperimentBlocked('Only the explicitly prespecified GLM-low classifier is authorized')
        _signed_file(value['protocol'])
    elif value.get('status') != 'selected' or value.get('selection_split') != 'dev':
        raise ExperimentBlocked('P classifier checks require signed development or prespecified authority')
    return value, {'path': str(path), 'sha256': file_sha(path), 'commit': revision, 'worktree_read_only': True}


async def run_p_classifier(*, max_tokens, round_number=1, concurrency=12,
                           config_path='configs/routing_registry.json', fixture_path=FIXTURE,
                           selection_path=SELECTION, client=None):
    if type(max_tokens) is not int or max_tokens <= 0 or round_number < 1:
        raise ValueError('Positive P generation cap and round required')
    registration = registration_guard(config_path)
    _signed_file(fixture_path)
    fixture = json.loads(Path(fixture_path).read_text())
    if fixture.get('purpose') != 'synthetic_P_only' or len(fixture.get('cases', [])) not in range(8, 13):
        raise ExperimentBlocked('Only the registered 8–12 synthetic P fixtures are allowed')
    selection, selected_source = selected_classifier(selection_path)
    selected = selection['selected']
    config = {'classification': {'model': selected['model'], 'reasoning': selected['reasoning'], 'max_tokens': max_tokens}}
    binding = {'stage': 'P_metabolic_classifier', 'registration': registration,
               'selected_classifier': selected_source, 'fixture_sha256': file_sha(fixture_path),
               'prompt_sha256': file_sha('configs/classification_prompt.json'),
               'schema_sha256': file_sha('configs/classification_wire_schema.json'),
               'max_tokens': max_tokens, 'round': round_number}
    destination = Path('data/derived/pair_development') / f'round-{round_number:02d}'
    plan_path = destination / 'plan.json'
    if plan_path.exists() and json.loads(plan_path.read_text())['binding'] != binding:
        raise ExperimentBlocked('P pair round identity changed; use a documented new revision round')
    write_json(plan_path, {'binding': binding, 'created_at': utcnow(),
                          'item_ids': [c['item']['item_id'] for c in fixture['cases']],
                          'expected_answers_sent_to_model': False})
    own = client is None
    client = client or V2Client('extraction_development', config=config_path, concurrency=concurrency)
    async def work(case):
        pair, articles, extractions = fixture_inputs(case)
        return await _classify_one(client, pair, articles, extractions, config, binding, destination / 'items')
    try:
        records = await _workers(fixture['cases'], work, concurrency=concurrency, progress_path=destination / 'progress.json')
    finally:
        if own:
            await client.close()
    by_id = {r['pair_id']: r for r in records}
    comparisons = []
    for case in fixture['cases']:
        result = by_id[case['item']['item_id']]
        expected = case['private_expected']
        actual = result.get('verdict') or {}
        comparisons.append({'item_id': case['item']['item_id'], 'case': case['private_case'],
                            'valid': result['status'] == 'valid', 'expected': expected,
                            'observed_label': actual.get('label'), 'observed_factor': actual.get('factor'),
                            'label_matches': actual.get('label') == expected['label'],
                            'material_error_matches': actual.get('diagnostics', {}).get('material_extraction_error') == expected['material_extraction_error']})
    report = {'status': 'complete', 'stage': 'P_only', 'created_at': utcnow(), 'binding': binding,
              'selected_model': selected['model'], 'selected_reasoning': selected['reasoning'],
              'requested': len(records), 'valid': sum(r['status'] == 'valid' for r in records),
              'label_matches': sum(r['label_matches'] for r in comparisons),
              'expected_label_counts': dict(Counter(r['expected']['label'] for r in comparisons)),
              'comparisons': comparisons, 'selection_changed': False, 'expected_answers_sent_to_model': False,
              'not_a_final_evaluation': True, 'primary_classifier_selection': False,
              'purpose': 'technical_prompt_and_output_cap_check',
              'model_choice': ('prespecified_fixed_configuration' if selection.get('status') == 'prespecified'
                               else 'external_benchmark_winner_as_technical_probe'),
              'result_identity': digest([r['identity'] for r in records])}
    write_json(destination / 'summary.json', report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--max-tokens', type=int, required=True)
    parser.add_argument('--round', type=int, default=1)
    parser.add_argument('--concurrency', type=int, default=12)
    parser.add_argument('--selection', default=str(SELECTION))
    args = parser.parse_args()
    print(json.dumps(asyncio.run(run_p_classifier(max_tokens=args.max_tokens, round_number=args.round,
                     concurrency=args.concurrency, selection_path=args.selection)), indent=2))


if __name__ == '__main__':
    main()
