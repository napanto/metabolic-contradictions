"""Descriptive corpus analyses and figures for the report and the slides.

Offline only: no model call, no oracle or human answer, no sample membership.
Everything here describes classifier *predictions* on the whole corpus; only the
35-pair sample has human and oracle validation.

Usage (from the project root, PYTHONPATH=src):
    python -m chl_rag.descriptive analyze --scope v2
    python -m chl_rag.descriptive analyze --scope v21      # after the v2.1 frame is frozen
    python -m chl_rag.descriptive compare --before v2 --after v21

Visible text follows ../report-narrative.md: no version labels, no person names.
"""
from __future__ import annotations

import argparse
import json
import statistics
import textwrap
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

from .io import digest, file_sha, utcnow, write_json

ARMS = ('biomarker', 'drug', 'diet')
ARM_NAMES = {'biomarker': 'Biomarkers', 'drug': 'Drugs', 'diet': 'Diet'}
FLAGS = ('contradictory', 'apparent_contradiction', 'not_replicated')
LABELS = FLAGS + ('concordant', 'not_comparable')
LABEL_NAMES = {'contradictory': 'Contradictory', 'apparent_contradiction': 'Apparent contradiction',
               'not_replicated': 'Not replicated', 'concordant': 'Concordant', 'not_comparable': 'Not comparable'}
FACTOR_NAMES = {'population': 'Population', 'region': 'Region', 'disease_subtype': 'Disease subtype',
                'design': 'Study design', 'comparator': 'Comparator', 'molecule_class': 'Molecule or class',
                'dose': 'Dose', 'duration': 'Duration', 'outcome_definition': 'Outcome definition',
                'sample': 'Biological sample', 'biological_sample': 'Biological sample', 'unknown': 'Unknown',
                'none': 'None', 'other': 'Other'}
ASSERTIONS = ('directional', 'explicit_no_difference', 'not_confirmed', 'unclear')

# Reference palette (dataviz skill, light mode): categorical slots 1-5 in fixed order,
# validated with validate_palette.js (all checks pass; contrast relief = direct labels).
CAT = {'contradictory': '#2a78d6', 'apparent_contradiction': '#eb6834', 'not_replicated': '#1baf7a',
       'concordant': '#eda100', 'not_comparable': '#e87ba4'}
SEQ = ['#9ec5f4', '#5598e7', '#256abf', '#104281']          # blue ramp steps 200/350/500/650
SURFACE, INK, INK2, MUTED, GRID = '#fcfcfb', '#0b0b0b', '#52514e', '#8a8984', '#e4e3df'
SERIES1 = '#2a78d6'

SCOPES = {
    'uncorrected': {'data': 'data/derived/production_uncorrected/full',
           'frame': 'results/production_uncorrected/frozen_frame_full.json',
           'window': ('2026-11-14T00:30:00+00:00', '2026-11-14T10:30:00+00:00')},
    'system': {'data': 'data/derived/production/full',
            'frame': 'results/production/frozen_frame_full.json',
            'status': 'results/production/status.json',
            'window': None},
    # Addendum 08D: the uncapped frame, primary corpus output. Same extraction as v21; the cost
    # window runs from the v2.1 start (extraction) to the uncapped freeze, so it covers the
    # extraction, the capped classification and the new uncapped classifications.
    'v21u': {'data': 'data/derived/production_v21_uncapped/full',
             'frame': 'results/production_v21_uncapped/frozen_frame_full.json',
             'status': 'results/production/status.json',
             'window': None},
}
LEDGER = 'results/openrouter_spend.json'
CORPUS = 'data/derived/corpus.jsonl'
OUT = Path('report/generated_descriptive')
FIG = Path('figures/descriptive')
ROOT = Path('.')


def _p(path):
    """Data paths resolve against --data-root (the main project tree); outputs go to the cwd."""
    return ROOT / path


import re
GENE_SYMBOL = re.compile(r'^[A-Z0-9-]{2,8}$')
FILES = ('extractions.jsonl', 'claims_normalized.jsonl', 'pairs_candidates.jsonl', 'pairs_classified.jsonl')


# ---------------------------------------------------------------- loading

def _jsonl(path):
    with open(path) as handle:
        for line in handle:
            if line.strip():
                yield json.loads(line)


def load_frame(scope, *, verify=True):
    spec = SCOPES[scope]
    path = _p(spec['frame'])
    if not path.is_file():
        raise SystemExit(f'{scope}: frozen frame {path} not found; the run is not finished')
    frame = json.loads(path.read_text())
    if frame.get('status') != 'complete_frozen':
        raise SystemExit(f'{scope}: frame status is {frame.get("status")!r}, not complete_frozen')
    paths = {}
    for name in FILES:
        entry = frame.get('files', {}).get(name)
        if entry is None and name == 'extractions.jsonl':
            continue
        p = _p(entry['path'])
        if verify and file_sha(p) != entry['sha256']:
            raise SystemExit(f'{scope}: {name} changed after the freeze')
        paths[name] = p
    return frame, paths


def load_claims(path):
    claims = {}
    for row in _jsonl(path):
        norm = row['normalization']
        result = row['claim']['result']
        claims[row['claim_id']] = {
            'pmid': str(row['pmid']), 'arm': row['arm'], 'sign': row['sign'], 'hedged': bool(row.get('hedged')),
            'comparison_type': row['comparison_type'], 'assertion': result.get('assertion'),
            'direction': result.get('estimate_direction'),
            'exposure_key': row['exposure_key'], 'outcome_key': row['outcome_key'],
            'exposure_label': norm['exposure']['label'] or row['claim']['exposure'],
            'outcome_label': norm['outcome']['label'] or row['claim']['outcome'],
            'exposure_text': row['claim']['exposure'], 'outcome_text': row['claim']['outcome'],
            'design': design_class((row.get('study') or {}).get('design'))}
    return claims


DESIGNS = ('Meta-analysis', 'RCT', 'Cohort', 'Case-control', 'Cross-sectional', 'Other')


def design_class(text):
    """Coarse, deterministic design class from the extracted design text (None if missing)."""
    if not text:
        return None
    t = text.lower()
    if 'meta-analy' in t or 'meta analy' in t or 'systematic review' in t or 'pooled analysis' in t:
        return 'Meta-analysis'
    if 'random' in t or 'rct' in t:
        return 'RCT'
    if 'case-control' in t or 'case control' in t:
        return 'Case-control'
    if 'cohort' in t or 'prospective' in t or 'retrospective' in t or 'longitudinal' in t or 'registry' in t:
        return 'Cohort'
    if 'cross-sectional' in t or 'cross sectional' in t:
        return 'Cross-sectional'
    return 'Other'


def load_pairs(path):
    pairs = []
    for row in _jsonl(path):
        verdict = row.get('verdict') or {}
        valid = row.get('classification_status') == 'valid'
        pairs.append({'pair_id': row['pair_id'], 'arm': row['arm'], 'claim_a': row['claim_a'], 'claim_b': row['claim_b'],
                      'pmid_a': str(row['pmid_a']), 'pmid_b': str(row['pmid_b']), 'pair_key': row['pair_key'],
                      'origins': sorted(set(row.get('origins') or [row.get('origin')])),
                      'sign_pattern': row.get('sign_pattern'), 'valid': valid,
                      'label': verdict.get('label') if valid else None,
                      'factor': verdict.get('factor') if valid else None,
                      'material_error': bool((verdict.get('diagnostics') or {}).get('material_extraction_error')) if valid else None})
    return pairs


def load_corpus_meta(pmids):
    meta = {}
    for row in _jsonl(_p(CORPUS)):
        pmid = str(row['pmid'])
        if pmid in pmids:
            meta[pmid] = {'year': row.get('year'), 'title': row.get('title'), 'journal': row.get('journal')}
    return meta


# ---------------------------------------------------------------- analysis

def _counts(values, order=None):
    counter = Counter(values)
    if order:
        return {k: counter.get(k, 0) for k in order} | {k: v for k, v in sorted(counter.items()) if k not in order}
    return dict(sorted(counter.items()))


def _origin_class(origins):
    s = set(origins)
    return 'both' if s == {'entity_key', 'hybrid_retrieval'} else ('key_only' if s == {'entity_key'} else 'retrieval_only')


def _topic(pair):
    parts = pair['pair_key'].split('|')
    return (pair['arm'], parts[1], parts[2]) if len(parts) >= 3 else (pair['arm'], pair['pair_key'], '')


def _mode(counter):
    return min(counter, key=lambda k: (-counter[k], k)) if counter else ''


def _flagged(pair):
    return pair['valid'] and pair['label'] in FLAGS


def _quantiles(values):
    if not values:
        return None
    values = sorted(values)
    q = statistics.quantiles(values, n=4) if len(values) > 1 else [values[0]] * 3
    return {'n': len(values), 'median': statistics.median(values), 'q1': q[0], 'q3': q[2],
            'share_ge_10y': sum(v >= 10 for v in values) / len(values)}


def _year_range(meta):
    years = sorted(m['year'] for m in meta.values() if isinstance(m.get('year'), int))
    if not years:
        return None
    return {'min': years[0], 'max': years[-1], 'median': statistics.median(years),
            'share_since_2020': sum(y >= 2020 for y in years) / len(years), 'abstracts_with_year': len(years)}


def _ledger(scope, frame):
    spec = SCOPES[scope]
    if not _p(LEDGER).is_file():
        return None
    if spec['window']:
        start, end = spec['window']
    else:
        status = json.loads(_p(spec['status']).read_text())
        start, end = status['started_at'], frame.get('frozen_at')
    calls = json.loads(_p(LEDGER).read_text())['calls']
    phases = {}
    for phase in ('production_extraction', 'production_classification'):
        rows = [c for c in calls if c.get('phase') == phase and c.get('model') == 'z-ai/glm-5.3-flash'
                and start <= (c.get('started_at') or '') and (end is None or (c.get('started_at') or '') <= end)]
        if not rows:
            continue
        usage = [c.get('usage') or {} for c in rows]
        providers = Counter(((c.get('response_metadata') or {}).get('provider') if isinstance(c.get('response_metadata'), dict) else None)
                            or 'no provider recorded' for c in rows)
        t0 = min(c['started_at'] for c in rows)
        t1 = max(c.get('finished_at') or c['started_at'] for c in rows)
        known = [c['cost'] for c in rows if isinstance(c.get('cost'), (int, float))]
        phases[phase] = {
            'calls': len(rows), 'cost_known_usd': round(sum(known), 6), 'calls_unknown_cost': len(rows) - len(known),
            'prompt_tokens': sum(u.get('prompt_tokens') or 0 for u in usage),
            'completion_tokens': sum(u.get('completion_tokens') or 0 for u in usage),
            'reasoning_tokens': sum((u.get('completion_tokens_details') or {}).get('reasoning_tokens') or 0 for u in usage),
            'providers': dict(providers.most_common()), 'first_call': t0, 'last_call': t1,
            'wall_minutes': round((datetime.fromisoformat(t1) - datetime.fromisoformat(t0)).total_seconds() / 60, 1)}
    return {'window': [start, end], 'source': LEDGER, 'ledger_sha256': file_sha(_p(LEDGER)), 'phases': phases,
            'note': 'Known costs only; calls with unknown billing are counted, not imputed. Window-based attribution.'}


def analyze(scope, *, verify=True):
    frame, paths = load_frame(scope, verify=verify)
    claims = load_claims(paths['claims_normalized.jsonl'])
    pairs = load_pairs(paths['pairs_classified.jsonl'])
    ex, cl = frame['extraction'], frame['classification']
    flagged = [p for p in pairs if _flagged(p)]
    # 1. funnel
    funnel = {'abstracts': ex['requested_sources'], 'valid_extractions': ex['valid_sources'],
              'failed_extractions': ex['invalid_sources'], 'zero_claim_abstracts': ex['zero_claim_sources'],
              'abstracts_with_claims': len({c['pmid'] for c in claims.values()}), 'claims': len(claims),
              'candidate_pairs': len(pairs), 'candidate_origin': _counts(_origin_class(p['origins']) for p in pairs),
              'valid_classifications': sum(p['valid'] for p in pairs), 'invalid_classifications': sum(not p['valid'] for p in pairs),
              'flagged_pairs': len(flagged), 'labels': _counts((p['label'] for p in pairs if p['valid']), LABELS),
              'material_extraction_error_flags': sum(bool(p['material_error']) for p in pairs if p['valid'])}
    if cl['valid_pairs'] != funnel['valid_classifications'] or len(claims) != ex['retained_claims']:
        raise SystemExit(f'{scope}: loaded files disagree with the frozen frame aggregates')
    per_arm = {}
    for arm in ARMS:
        ap = [p for p in pairs if p['arm'] == arm]
        ac = [c for c in claims.values() if c['arm'] == arm]
        per_arm[arm] = {'claims': len(ac), 'abstracts_with_claims': len({c['pmid'] for c in ac}),
                        'candidate_pairs': len(ap), 'candidate_origin': _counts(_origin_class(p['origins']) for p in ap),
                        'valid_classifications': sum(p['valid'] for p in ap),
                        'flagged_pairs': sum(_flagged(p) for p in ap),
                        'labels': _counts((p['label'] for p in ap if p['valid']), LABELS),
                        'assertions': _counts((c['assertion'] for c in ac), ASSERTIONS),
                        'signs': _counts(c['sign'] if c['sign'] is not None else 'unrepresented' for c in ac)}
    # 2. context factors
    apparent = [p for p in pairs if p['valid'] and p['label'] == 'apparent_contradiction']
    factors = {'apparent_contradiction': _counts(p['factor'] for p in apparent),
               'apparent_contradiction_per_arm': {arm: _counts(p['factor'] for p in apparent if p['arm'] == arm) for arm in ARMS},
               'all_flagged': _counts(p['factor'] for p in flagged)}
    # 3. contested topics
    topics = defaultdict(list)
    for p in pairs:
        topics[_topic(p)].append(p)
    def display(rows):
        exp, out, exp_raw, out_raw = Counter(), Counter(), Counter(), Counter()
        for p in rows:
            for side in ('claim_a', 'claim_b'):
                c = claims[p[side]]
                exp[c['exposure_label']] += 1
                out[c['outcome_label']] += 1
                exp_raw[c['exposure_text'].strip().lower()] += 1
                out_raw[c['outcome_text'].strip().lower()] += 1
        # PubTator gene symbols (e.g. INS, CHI3L1) read badly: fall back to the most frequent extracted wording
        pick = lambda norm, raw: _mode(raw) if GENE_SYMBOL.match(_mode(norm) or '') else _mode(norm)
        return pick(exp, exp_raw), pick(out, out_raw)
    topic_rows = []
    for key, rows in topics.items():
        f = [p for p in rows if _flagged(p)]
        exp, out = display(rows)
        topic_rows.append({'arm': key[0], 'exposure_key': key[1], 'outcome_key': key[2], 'exposure': exp, 'outcome': out,
                           'candidate_pairs': len(rows), 'flagged_pairs': len(f),
                           'flagged_share': len(f) / sum(p['valid'] for p in rows) if any(p['valid'] for p in rows) else None,
                           'abstracts': len({x for p in rows for x in (p['pmid_a'], p['pmid_b'])}),
                           'flag_abstracts': len({x for p in f for x in (p['pmid_a'], p['pmid_b'])}),
                           'labels': _counts((p['label'] for p in rows if p['valid']), LABELS)})
    rank = lambda t: (-t['flagged_pairs'], -t['flag_abstracts'], t['exposure_key'], t['outcome_key'])
    contested = {arm: sorted((t for t in topic_rows if t['arm'] == arm and t['flagged_pairs']), key=rank)[:8] for arm in ARMS}
    network = {arm: sorted((t for t in topic_rows if t['arm'] == arm),
                           key=lambda t: (-t['candidate_pairs'], -t['flagged_pairs'], t['exposure_key'], t['outcome_key']))[:12]
               for arm in ARMS}
    # illustrative pair per arm: deterministic, from the most contested topic, preferring 'contradictory'
    need = {x for p in flagged for x in (p['pmid_a'], p['pmid_b'])}
    meta_all = load_corpus_meta({c['pmid'] for c in claims.values()})
    meta = {k: meta_all[k] for k in need if k in meta_all}
    examples = {}
    for arm in ARMS:
        if not contested[arm]:
            continue
        top = contested[arm][0]
        rows = [p for p in topics[(arm, top['exposure_key'], top['outcome_key'])] if _flagged(p)]
        rows.sort(key=lambda p: (p['label'] != 'contradictory', digest([20261116, p['pair_id']])))
        p = rows[0]
        sides = []
        for side in ('a', 'b'):
            c, m = claims[p['claim_' + side]], meta.get(p['pmid_' + side], {})
            sides.append({'pmid': p['pmid_' + side], 'year': m.get('year'), 'title': m.get('title'), 'journal': m.get('journal'),
                          'exposure': c['exposure_text'], 'outcome': c['outcome_text'], 'assertion': c['assertion'],
                          'direction': c['direction'], 'comparison_type': c['comparison_type']})
        examples[arm] = {'pair_id': p['pair_id'], 'label': p['label'], 'factor': p['factor'],
                         'topic': f"{top['exposure']} → {top['outcome']}", 'sides': sides,
                         'selection_rule': 'most contested topic of the arm; contradictory first; then digest order (seed 20261116)'}
    # 7. publication-year gaps in flagged pairs
    gaps = defaultdict(list)
    for p in flagged:
        ya, yb = (meta.get(p['pmid_a'], {}).get('year'), meta.get(p['pmid_b'], {}).get('year'))
        if isinstance(ya, int) and isinstance(yb, int):
            gaps[p['label']].append(abs(ya - yb))
            gaps['all_flagged'].append(abs(ya - yb))
    design = {}
    for k in LABELS:
        rows = [p for p in pairs if p['valid'] and p['label'] == k]
        known = [p for p in rows if claims[p['claim_a']]['design'] and claims[p['claim_b']]['design']]
        differ = [p for p in known if claims[p['claim_a']]['design'] != claims[p['claim_b']]['design']]
        design[k] = {'pairs': len(rows), 'both_designs_known': len(known), 'designs_differ': len(differ),
                     'share_differ': len(differ) / len(known) if known else None}
    fk = [p for p in flagged if claims[p['claim_a']]['design'] and claims[p['claim_b']]['design']]
    combos = Counter(' vs '.join(sorted((claims[p['claim_a']]['design'], claims[p['claim_b']]['design']))) for p in fk)
    design['flagged_all'] = {'both_designs_known': len(fk),
                             'designs_differ': sum(claims[p['claim_a']]['design'] != claims[p['claim_b']]['design'] for p in fk),
                             'top_combinations': dict(combos.most_common(8))}
    design['flagged_all']['share_differ'] = design['flagged_all']['designs_differ'] / len(fk) if fk else None
    design['claims'] = _counts(c['design'] or 'missing' for c in claims.values())
    design['rule'] = 'coarse keyword classes from the extracted design text (design_class); descriptive, not validated'
    assertions = {'all': _counts((c['assertion'] for c in claims.values()), ASSERTIONS)}
    total = sum(assertions['all'].values())
    assertions['share_explicit_no_difference'] = assertions['all'].get('explicit_no_difference', 0) / total
    assertions['share_no_effect_or_not_confirmed'] = (assertions['all'].get('explicit_no_difference', 0)
                                                      + assertions['all'].get('not_confirmed', 0)) / total
    for arm in ARMS:
        a = per_arm[arm]['assertions']
        n = sum(a.values()) or 1
        per_arm[arm]['share_explicit_no_difference'] = a.get('explicit_no_difference', 0) / n
        per_arm[arm]['share_no_effect_or_not_confirmed'] = (a.get('explicit_no_difference', 0) + a.get('not_confirmed', 0)) / n
    result = {'scope': scope, 'generated_at': utcnow(), 'interpretation': 'classifier predictions on the whole corpus; not validated except on the 35-pair sample',
              'frame': {'path': SCOPES[scope]['frame'], 'sha256': file_sha(_p(SCOPES[scope]['frame'])), 'frozen_at': frame.get('frozen_at'),
                        'files_verified': verify},
              'funnel': funnel, 'per_arm': per_arm, 'factors': factors, 'contested_topics': contested, 'network_topics': network,
              'examples': examples, 'design_mismatch': design,
              'corpus_years': _year_range(meta_all), 'year_gaps': {k: _quantiles(v) for k, v in gaps.items()}, 'assertions': assertions,
              'cost_time': _ledger(scope, frame), 'analysis_sha256': file_sha(__file__)}
    ct = result['cost_time']
    if ct and ct['phases']:
        ext, cls = ct['phases'].get('production_extraction'), ct['phases'].get('production_classification')
        result['cost_time']['per_abstract_usd'] = ext['cost_known_usd'] / funnel['abstracts'] if ext else None
        result['cost_time']['per_pair_usd'] = cls['cost_known_usd'] / funnel['candidate_pairs'] if cls else None
        result['cost_time']['total_usd'] = sum(p['cost_known_usd'] for p in ct['phases'].values())
        if ext and ext['wall_minutes']:
            result['cost_time']['abstracts_per_minute'] = funnel['abstracts'] / ext['wall_minutes']
        if cls and cls['wall_minutes']:
            result['cost_time']['pairs_per_minute'] = funnel['candidate_pairs'] / cls['wall_minutes']
    return result, claims, pairs


def compare(before, after):
    out = {'before': before, 'after': after, 'generated_at': utcnow(),
           'note': 'Abstract pairs are matched by arm and the two PMIDs; claim and pair IDs differ between runs.'}
    data = {}
    for scope in (before, after):
        res_path = OUT / scope / 'results.json'
        if not res_path.is_file():
            raise SystemExit(f'run "analyze --scope {scope}" first')
        res = json.loads(res_path.read_text())
        _, paths = load_frame(scope, verify=False)
        pairs = load_pairs(paths['pairs_classified.jsonl'])
        data[scope] = (res, pairs)
    def abstract_pairs(pairs, flagged_only):
        return {(p['arm'],) + tuple(sorted((p['pmid_a'], p['pmid_b']))) for p in pairs if (not flagged_only or _flagged(p))}
    (rb, pb), (ra, pa) = data[before], data[after]
    out['assertions'] = {s: {'all': data[s][0]['assertions']['share_explicit_no_difference'],
                             'all_no_effect_or_not_confirmed': data[s][0]['assertions']['share_no_effect_or_not_confirmed'],
                             **{arm: data[s][0]['per_arm'][arm]['share_explicit_no_difference'] for arm in ARMS}}
                         for s in (before, after)}
    out['flagged_pairs'] = {s: {'all': data[s][0]['funnel']['flagged_pairs'], **{arm: data[s][0]['per_arm'][arm]['flagged_pairs'] for arm in ARMS}}
                            for s in (before, after)}
    out['candidate_pairs'] = {s: data[s][0]['funnel']['candidate_pairs'] for s in (before, after)}
    out['labels'] = {s: data[s][0]['funnel']['labels'] for s in (before, after)}
    fb, fa = abstract_pairs(pb, True), abstract_pairs(pa, True)
    cb, ca = abstract_pairs(pb, False), abstract_pairs(pa, False)
    out['flagged_abstract_pairs'] = {'before': len(fb), 'after': len(fa), 'kept': len(fb & fa),
                                     'disappeared': len(fb - fa), 'disappeared_still_candidates': len((fb - fa) & ca),
                                     'appeared': len(fa - fb), 'appeared_were_candidates': len((fa - fb) & cb)}
    out['per_arm_flagged_abstract_pairs'] = {}
    for arm in ARMS:
        b = {x for x in fb if x[0] == arm}; a = {x for x in fa if x[0] == arm}
        out['per_arm_flagged_abstract_pairs'][arm] = {'before': len(b), 'after': len(a), 'kept': len(b & a),
                                                     'disappeared': len(b - a), 'appeared': len(a - b)}
    return out


# ---------------------------------------------------------------- figures

VARIANTS = {'slide': {'w': 6.2, 'h': 3.1, 'fs': 8.0}, 'report': {'w': 3.35, 'h': 2.5, 'fs': 6.5},
            'report_wide': {'w': 7.0, 'h': 2.6, 'fs': 6.5}}


def _plt(fs):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    plt.rcParams.update({'font.size': fs, 'axes.titlesize': fs + 0.5, 'axes.labelsize': fs, 'xtick.labelsize': fs - 0.5,
                         'ytick.labelsize': fs - 0.5, 'legend.fontsize': fs - 0.5, 'font.family': 'DejaVu Sans',
                         'axes.edgecolor': MUTED, 'axes.labelcolor': INK2, 'xtick.color': INK2, 'ytick.color': INK2,
                         'text.color': INK, 'figure.facecolor': SURFACE, 'axes.facecolor': SURFACE,
                         'savefig.facecolor': SURFACE, 'pdf.fonttype': 42})
    return plt


def _clean(ax, *, grid_axis='x'):
    for s in ('top', 'right', 'left'):
        ax.spines[s].set_visible(False)
    ax.spines['bottom'].set_color(GRID)
    ax.tick_params(length=0)
    ax.grid(axis=grid_axis, color=GRID, linewidth=0.6)
    ax.set_axisbelow(True)


def _save(fig, stem):
    stem.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(stem.with_suffix('.pdf'), bbox_inches='tight', metadata={'Creator': 'CHL descriptive corpus analysis'})
    fig.savefig(stem.with_suffix('.png'), dpi=220, bbox_inches='tight')
    return [str(stem.with_suffix('.pdf')), str(stem.with_suffix('.png'))]


def _fmt(n):
    return f'{n:,}'


def fig_funnel(res, stem, v):
    """Hero-number pipeline: stages carry different units, so tiles, not one bar scale."""
    plt = _plt(v['fs'])
    f = res['funnel']
    stages = [('Abstracts', f['abstracts'], f"{_fmt(f['abstracts_with_claims'])} with at least one result"),
              ('Extracted results', f['claims'], f"{f['claims'] / max(1, f['abstracts_with_claims']):.1f} per abstract"),
              ('Candidate pairs', f['candidate_pairs'],
               f"{_fmt(f['candidate_origin'].get('key_only', 0))} by key, {_fmt(f['candidate_origin'].get('retrieval_only', 0))} by retrieval, "
               f"{_fmt(f['candidate_origin'].get('both', 0))} by both"),
              ('Flagged pairs', f['flagged_pairs'],
               f"{f['flagged_pairs'] / max(1, f['valid_classifications']):.0%} of {_fmt(f['valid_classifications'])} classified")]
    fig, ax = plt.subplots(figsize=(v['w'], v['h'] * 0.62))
    ax.set_axis_off()
    n = len(stages)
    for i, (name, value, sub) in enumerate(stages):
        x = i / n + 0.5 / n
        ax.text(x, 0.72, _fmt(value), ha='center', va='center', fontsize=v['fs'] * 2.1, color=INK, fontweight='bold', transform=ax.transAxes)
        ax.text(x, 0.47, name, ha='center', va='center', fontsize=v['fs'] * 1.05, color=INK, transform=ax.transAxes)
        ax.text(x, 0.34, '\n'.join(textwrap.wrap(sub, 26)), ha='center', va='top', fontsize=v['fs'] * 0.82, color=INK2, transform=ax.transAxes)
        if i < n - 1:
            ax.annotate('', xy=((i + 1) / n + 0.03 / n, 0.72), xytext=((i + 1) / n - 0.03 / n, 0.72), xycoords='axes fraction',
                        arrowprops={'arrowstyle': '-|>,head_length=0.6,head_width=0.35', 'color': MUTED, 'lw': 1.2})
    out = _save(fig, stem)
    plt.close(fig)
    return out


def fig_categories(res, stem, v):
    plt = _plt(v['fs'])
    fig, ax = plt.subplots(figsize=(v['w'], v['h'] * 0.8))
    arms = list(ARMS)[::-1]
    for y, arm in enumerate(arms):
        labels = res['per_arm'][arm]['labels']
        total = sum(labels.get(k, 0) for k in LABELS) or 1
        left = 0.0
        for k in LABELS:
            share = labels.get(k, 0) / total
            ax.barh(y, share, left=left, height=0.62, color=CAT[k], edgecolor=SURFACE, linewidth=1.5)
            if share >= 0.07:
                ax.text(left + share / 2, y, f'{share:.0%}', ha='center', va='center', fontsize=v['fs'] * 0.85, color='white' if k in ('contradictory',) else INK)
            left += share
        ax.text(1.01, y, f"n = {_fmt(total)}", va='center', ha='left', fontsize=v['fs'] * 0.85, color=INK2)
    ax.set_yticks(range(len(arms)), [ARM_NAMES[a] for a in arms])
    ax.set_xlim(0, 1)
    ax.xaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f'{x:.0%}'))
    _clean(ax)
    handles = [plt.Rectangle((0, 0), 1, 1, color=CAT[k]) for k in LABELS]
    ax.legend(handles, [LABEL_NAMES[k] for k in LABELS], ncol=3 if v['w'] < 5 else 5, loc='lower left', bbox_to_anchor=(0, 1.02),
              frameon=False, handlelength=1.0, columnspacing=1.0)
    ax.set_xlabel('Share of classified candidate pairs (predictions)')
    out = _save(fig, stem)
    plt.close(fig)
    return out


def fig_factors(res, stem, v):
    plt = _plt(v['fs'])
    counts = Counter(res['factors']['apparent_contradiction'])
    for k in ('none', 'unknown'):
        counts.pop(k, None)
    top = counts.most_common(8)
    other = sum(counts.values()) - sum(c for _, c in top)
    rows = top + ([('other_rest', other)] if other else [])
    total = sum(res['factors']['apparent_contradiction'].values()) or 1
    fig, ax = plt.subplots(figsize=(v['w'], v['h'] * 0.85))
    names = [FACTOR_NAMES.get(k, 'All other factors' if k == 'other_rest' else k) for k, _ in rows][::-1]
    vals = [c for _, c in rows][::-1]
    ax.barh(range(len(vals)), vals, height=0.6, color=SERIES1, edgecolor=SURFACE, linewidth=1.5)
    for i, val in enumerate(vals):
        ax.text(val + max(vals) * 0.01, i, f'{_fmt(val)} ({val / total:.0%})', va='center', fontsize=v['fs'] * 0.85, color=INK2)
    ax.set_yticks(range(len(vals)), names)
    ax.set_xlim(0, max(vals) * 1.25 if vals else 1)
    _clean(ax)
    ax.set_xlabel(f'Apparent contradictions explained by each context factor (of {_fmt(total)})')
    out = _save(fig, stem)
    plt.close(fig)
    return out


def fig_design(res, stem, v):
    """Share of classified pairs whose two studies have different coarse designs, per predicted category."""
    plt = _plt(v['fs'])
    d = res['design_mismatch']
    rows = [(k, d[k]) for k in LABELS if d[k]['share_differ'] is not None][::-1]
    fig, ax = plt.subplots(figsize=(v['w'], v['h'] * 0.8))
    vals = [r['share_differ'] for _, r in rows]
    ax.barh(range(len(rows)), vals, height=0.6, color=SERIES1, edgecolor=SURFACE, linewidth=1.5)
    for i, (k, r) in enumerate(rows):
        ax.text(r['share_differ'] + 0.01, i, f"{r['share_differ']:.0%}  ({_fmt(r['designs_differ'])} of {_fmt(r['both_designs_known'])})",
                va='center', fontsize=v['fs'] * 0.85, color=INK2)
    ax.set_yticks(range(len(rows)), [LABEL_NAMES[k] for k, _ in rows])
    ax.set_xlim(0, min(1.0, max(vals) * 1.45) if vals else 1)
    ax.xaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f'{x:.0%}'))
    _clean(ax)
    ax.set_xlabel('Pairs whose two studies have different designs (e.g. trial vs cohort)')
    out = _save(fig, stem)
    plt.close(fig)
    return out


def _short(text, width):
    return textwrap.shorten(text, width=width, placeholder='…')


def fig_topics(res, stem, v):
    plt = _plt(v['fs'])
    wide = v['w'] > 5
    fig, axes = plt.subplots(1 if wide else 3, 3 if wide else 1, figsize=(v['w'], v['h'] * (1.0 if wide else 2.2)))
    for ax, arm in zip(axes, ARMS):
        rows = res['contested_topics'][arm][:5][::-1]
        vals = [r['flagged_pairs'] for r in rows]
        labels = [_short(f"{r['exposure']} → {r['outcome']}", 34 if wide else 46) for r in rows]
        ax.barh(range(len(rows)), vals, height=0.6, color=SERIES1, edgecolor=SURFACE, linewidth=1.5)
        for i, r in enumerate(rows):
            ax.text(r['flagged_pairs'] + (max(vals) if vals else 1) * 0.02, i, str(r['flagged_pairs']), va='center', fontsize=v['fs'] * 0.85, color=INK2)
        ax.set_yticks(range(len(rows)), ['\n'.join(textwrap.wrap(l, 24 if wide else 44)) for l in labels], fontsize=v['fs'] * 0.8)
        ax.set_xlim(0, (max(vals) if vals else 1) * 1.3)
        ax.set_title(ARM_NAMES[arm], loc='left', color=INK)
        _clean(ax)
    fig.supxlabel('Flagged pairs per topic (exposure → outcome; predictions)', fontsize=v['fs'] * 0.9, color=INK2)
    fig.tight_layout()
    out = _save(fig, stem)
    plt.close(fig)
    return out


def fig_network(res, stem, v):
    """Bipartite exposure -> outcome panels per arm; edge shade = share of classified pairs flagged."""
    plt = _plt(v['fs'])
    from matplotlib.lines import Line2D
    wide = v['w'] > 5
    fig, axes = plt.subplots(1 if wide else 3, 3 if wide else 1, figsize=(v['w'], v['h'] * (1.25 if wide else 3.0)))
    bins = [0.25, 0.5, 0.75]
    def shade(share):
        if share is None:
            return GRID
        return SEQ[sum(share >= b for b in bins)]
    for ax, arm in zip(axes, ARMS):
        rows = res['network_topics'][arm][:10]
        ax.set_axis_off()
        ax.set_title(ARM_NAMES[arm], loc='left', color=INK)
        exps = sorted({r['exposure'] for r in rows}, key=lambda e: -sum(r['candidate_pairs'] for r in rows if r['exposure'] == e))
        outs = sorted({r['outcome'] for r in rows}, key=lambda o: -sum(r['candidate_pairs'] for r in rows if r['outcome'] == o))
        ypos = lambda i, n: 1 - (i + 0.5) / max(n, 1)
        pe = {e: (0.0, ypos(i, len(exps))) for i, e in enumerate(exps)}
        po = {o: (1.0, ypos(i, len(outs))) for i, o in enumerate(outs)}
        top_pairs = max((r['candidate_pairs'] for r in rows), default=1)
        for r in sorted(rows, key=lambda r: r['flagged_share'] or 0):
            (x0, y0), (x1, y1) = pe[r['exposure']], po[r['outcome']]
            ax.plot([x0, x1], [y0, y1], color=shade(r['flagged_share']), linewidth=0.8 + 3.2 * (r['candidate_pairs'] / top_pairs) ** 0.5,
                    solid_capstyle='round', zorder=1)
        for e, (x, y) in pe.items():
            ax.scatter([x], [y], s=22, color=SURFACE, edgecolor=INK2, linewidth=0.8, zorder=3)
            ax.text(x - 0.04, y, _short(e, 24), ha='right', va='center', fontsize=v['fs'] * 0.78, color=INK)
        for o, (x, y) in po.items():
            ax.scatter([x], [y], s=22, color=SURFACE, edgecolor=INK2, linewidth=0.8, zorder=3)
            ax.text(x + 0.04, y, _short(o, 24), ha='left', va='center', fontsize=v['fs'] * 0.78, color=INK)
        ax.set_xlim(-0.95, 1.95)
        ax.set_ylim(-0.02, 1.02)
    handles = [Line2D([0], [0], color=c, lw=3) for c in SEQ]
    names = ['< 25% flagged', '25–50%', '50–75%', '≥ 75%']
    fig.legend(handles, names, loc='lower center', ncol=4, frameon=False, bbox_to_anchor=(0.5, -0.02))
    fig.text(0.5, -0.07, 'Ten largest exposure → outcome topics per arm by candidate pairs; line width = candidate pairs. Predictions, not validated findings.',
             ha='center', fontsize=v['fs'] * 0.78, color=INK2)
    fig.tight_layout(rect=(0, 0.06, 1, 1))
    out = _save(fig, stem)
    plt.close(fig)
    return out


def fig_compare(cmp, stem, v):
    """Two panels with their own axes (different measures): no-difference share and flagged pairs, per arm."""
    plt = _plt(v['fs'])
    before, after = cmp['before'], cmp['after']
    fig, axes = plt.subplots(1, 2, figsize=(v['w'], v['h'] * 0.85))
    names = {before: 'Extraction before the correction', after: 'The system'}
    colors = {before: '#8a8984', after: SERIES1}
    for ax, key, title, fmt in [(axes[0], 'assertions', 'Results extracted as "no difference"', lambda x: f'{x:.0%}'),
                                (axes[1], 'flagged_pairs', 'Flagged pairs', _fmt)]:
        arms = list(ARMS)[::-1]
        for y, arm in enumerate(arms):
            b, a = cmp[key][before][arm], cmp[key][after][arm]
            ax.plot([b, a], [y, y], color=GRID, linewidth=2.0, zorder=1)
            for s, val in ((before, b), (after, a)):
                ax.scatter([val], [y], s=36, color=colors[s], edgecolor=SURFACE, linewidth=1.5, zorder=3)
            ax.text(a, y + 0.28, fmt(a), ha='center', fontsize=v['fs'] * 0.8, color=INK2)
            ax.text(b, y - 0.38, fmt(b), ha='center', fontsize=v['fs'] * 0.8, color=MUTED)
        ax.set_yticks(range(len(arms)), [ARM_NAMES[a] for a in arms])
        ax.set_ylim(-0.7, len(arms) - 0.3)
        ax.set_title(title, loc='left', color=INK)
        lo = 0
        hi = max(max(cmp[key][before][a] for a in ARMS), max(cmp[key][after][a] for a in ARMS)) * 1.15
        ax.set_xlim(lo, hi)
        if key == 'assertions':
            ax.xaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f'{x:.0%}'))
        _clean(ax)
    handles = [plt.Line2D([0], [0], marker='o', linestyle='', markersize=6, color=colors[s]) for s in (before, after)]
    fig.legend(handles, [names[before], names[after]], loc='lower center', ncol=2, frameon=False, bbox_to_anchor=(0.5, -0.04))
    fig.tight_layout(rect=(0, 0.07, 1, 1))
    out = _save(fig, stem)
    plt.close(fig)
    return out


# ---------------------------------------------------------------- macros

def _tex_num(n):
    return f'{n:,}'


def _pct(x):
    return f'{100 * x:.0f}\\%' if x is not None else '--'


def macros(res, prefix='Desc'):
    f, ct = res['funnel'], res.get('cost_time') or {}
    m = {'Abstracts': _tex_num(f['abstracts']), 'AbstractsWithClaims': _tex_num(f['abstracts_with_claims']),
         'FailedExtractions': _tex_num(f['failed_extractions']), 'Claims': _tex_num(f['claims']),
         'CandidatePairs': _tex_num(f['candidate_pairs']), 'KeyOnlyPairs': _tex_num(f['candidate_origin'].get('key_only', 0)),
         'RetrievalOnlyPairs': _tex_num(f['candidate_origin'].get('retrieval_only', 0)),
         'BothOriginPairs': _tex_num(f['candidate_origin'].get('both', 0)),
         'ValidPairs': _tex_num(f['valid_classifications']), 'InvalidPairs': _tex_num(f['invalid_classifications']),
         'FlaggedPairs': _tex_num(f['flagged_pairs']), 'FlaggedShare': _pct(f['flagged_pairs'] / max(1, f['valid_classifications'])),
         'NoDifferenceShare': _pct(res['assertions']['share_explicit_no_difference'])}
    for k in LABELS:
        m['Label' + ''.join(w.title() for w in k.split('_'))] = _tex_num(f['labels'].get(k, 0))
    for arm in ARMS:
        a = res['per_arm'][arm]
        m[f'Flagged{arm.title()}'] = _tex_num(a['flagged_pairs'])
        m[f'Pairs{arm.title()}'] = _tex_num(a['candidate_pairs'])
        m[f'Claims{arm.title()}'] = _tex_num(a['claims'])
        # Report tables (16 November): abstracts, valid classifications and categories per arm.
        m[f'AbstractsWithClaims{arm.title()}'] = _tex_num(a['abstracts_with_claims'])
        m[f'ValidPairs{arm.title()}'] = _tex_num(a['valid_classifications'])
        m[f'FlaggedShare{arm.title()}'] = _pct(a['flagged_pairs'] / max(1, a['valid_classifications']))
        m[f'NoDifferenceShare{arm.title()}'] = _pct(a['share_explicit_no_difference'])
        for k in LABELS:
            m['Label' + ''.join(w.title() for w in k.split('_')) + arm.title()] = _tex_num(a['labels'].get(k, 0))
    app = res['factors']['apparent_contradiction']
    named = Counter({k: v for k, v in app.items() if k not in ('none', 'unknown', 'other')})
    m['ApparentOtherShare'] = _pct(app.get('other', 0) / max(1, sum(app.values())))
    if named:
        top, n = named.most_common(1)[0]
        m['TopApparentFactor'] = FACTOR_NAMES.get(top, top).lower()
        m['TopApparentFactorShare'] = _pct(n / max(1, sum(app.values())))
    d = res.get('design_mismatch') or {}
    if d.get('flagged_all', {}).get('share_differ') is not None:
        m['DesignDifferFlagged'] = _pct(d['flagged_all']['share_differ'])
    if (d.get('concordant') or {}).get('share_differ') is not None:
        m['DesignDifferConcordant'] = _pct(d['concordant']['share_differ'])
    yr = res.get('corpus_years')
    if yr:
        m['CorpusYearMin'], m['CorpusYearMax'] = str(yr['min']), str(yr['max'])
        m['CorpusSinceTwentyTwenty'] = _pct(yr['share_since_2020'])
    g = (res.get('year_gaps') or {}).get('all_flagged')
    if g:
        m['FlaggedYearGapMedian'] = f"{g['median']:.0f}"
        m['FlaggedYearGapTenPlus'] = _pct(g['share_ge_10y'])
    if ct.get('total_usd') is not None:
        m['CostTotalUSD'] = f"{ct['total_usd']:.2f}"
        m['CostPerAbstractCents'] = f"{100 * ct['per_abstract_usd']:.2f}" if ct.get('per_abstract_usd') else '--'
        m['CostPerPairCents'] = f"{100 * ct['per_pair_usd']:.3f}" if ct.get('per_pair_usd') else '--'
        if ct.get('abstracts_per_minute'):
            m['AbstractsPerMinute'] = f"{ct['abstracts_per_minute']:.0f}"
        ex = ct['phases'].get('production_extraction')
        if ex:
            m['ExtractionProviders'] = _tex_num(len([p for p in ex['providers'] if p != 'no provider recorded']))
        # Report table of cost and time per phase (16 November).
        for phase, name in (('production_extraction', 'Extraction'), ('production_classification', 'Classification')):
            p = ct['phases'].get(phase)
            if not p:
                continue
            m[f'{name}Calls'] = _tex_num(p['calls'])
            m[f'{name}CostUSD'] = f"{p['cost_known_usd']:.2f}"
            m[f'{name}WallHours'] = f"{p['wall_minutes'] / 60:.1f}"
            m[f'{name}PromptMTok'] = f"{p['prompt_tokens'] / 1e6:.1f}"
            m[f'{name}CompletionMTok'] = f"{p['completion_tokens'] / 1e6:.1f}"
    return '\n'.join(f'\\providecommand{{\\{prefix}{k}}}{{{v}}}' for k, v in m.items()) + '\n'


def compare_macros(cmp, prefix='DescCmp'):
    b, a = cmp['before'], cmp['after']
    fa = cmp['flagged_abstract_pairs']
    m = {'NoDiffBefore': _pct(cmp['assertions'][b]['all']), 'NoDiffAfter': _pct(cmp['assertions'][a]['all']),
         'FlaggedBefore': _tex_num(cmp['flagged_pairs'][b]['all']), 'FlaggedAfter': _tex_num(cmp['flagged_pairs'][a]['all']),
         'FlaggedAbstractPairsKept': _tex_num(fa['kept']), 'FlaggedAbstractPairsGone': _tex_num(fa['disappeared']),
         'FlaggedAbstractPairsNew': _tex_num(fa['appeared'])}
    return '\n'.join(f'\\providecommand{{\\{prefix}{k}}}{{{v}}}' for k, v in m.items()) + '\n'


# ---------------------------------------------------------------- entry points

def run_analyze(scope, *, verify=True):
    res, _claims, _pairs = analyze(scope, verify=verify)
    out, fig = OUT / scope, FIG / scope
    out.mkdir(parents=True, exist_ok=True)
    artifacts = {}
    for name, fn in [('pipeline', fig_funnel), ('categories', fig_categories), ('apparent_factors', fig_factors), ('design_mismatch', fig_design),
                     ('contested_topics', fig_topics), ('network', fig_network)]:
        for variant in ('slide', 'report'):
            v = VARIANTS[variant]
            if name in ('pipeline', 'network') and variant == 'report':
                v = VARIANTS['report_wide']
            artifacts[f'{name}_{variant}'] = fn(res, fig / f'{name}_{variant}', v)
    res['figures'] = artifacts
    write_json(out / 'results.json', res)
    (out / 'macros.tex').write_text(macros(res))
    return res


def run_compare(before, after):
    cmp = compare(before, after)
    out, fig = OUT / 'compare', FIG / 'compare'
    out.mkdir(parents=True, exist_ok=True)
    cmp['figures'] = {f'correction_{variant}': fig_compare(cmp, fig / f'correction_{variant}', VARIANTS[variant])
                      for variant in ('slide', 'report')}
    write_json(out / 'results.json', cmp)
    (out / 'macros.tex').write_text(compare_macros(cmp))
    return cmp


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='cmd', required=True)
    a = sub.add_parser('analyze')
    a.add_argument('--scope', choices=sorted(SCOPES), required=True)
    a.add_argument('--no-verify', action='store_true', help='skip the sha256 check of the frozen files')
    parser.add_argument('--data-root', default='.', help='project tree holding the frozen data (read only)')
    c = sub.add_parser('compare')
    c.add_argument('--before', default='uncorrected')
    c.add_argument('--after', default='system')
    args = parser.parse_args()
    global ROOT
    ROOT = Path(args.data_root)
    if args.cmd == 'analyze':
        res = run_analyze(args.scope, verify=not args.no_verify)
        print(json.dumps({'scope': res['scope'], 'abstracts': res['funnel']['abstracts'], 'claims': res['funnel']['claims'],
                          'candidate_pairs': res['funnel']['candidate_pairs'], 'flagged': res['funnel']['flagged_pairs'],
                          'labels': res['funnel']['labels']}, ensure_ascii=False))
    else:
        cmp = run_compare(args.before, args.after)
        print(json.dumps({k: cmp[k] for k in ('flagged_pairs', 'assertions', 'flagged_abstract_pairs')}, ensure_ascii=False))


if __name__ == '__main__':
    main()
