import gzip, json, sys
from pathlib import Path
from collections import Counter
OUT = Path(sys.argv[1]); proj = Path('.')
sel = json.loads((OUT/'selection.json').read_text()); test = set(sel['test'])
def metrics(claims, studies):
    n = len(claims); c = Counter()
    for cl in claims:
        r = cl['result']; neg = r['assertion'] in ('explicit_no_difference','not_confirmed')
        c['neg'] += neg; c['dir'] += r['assertion']=='directional'
        c['hard'] += neg and r.get('authors_significance')=='significant'
        c['neg_dir'] += neg and r.get('estimate_direction') in ('+','-')
        c['sample'] += cl['context'].get('sample') is not None
        c['region'] += cl['context'].get('region') is not None
        if cl['comparison']['type']=='association':
            c['assoc'] += 1; c['assoc_pop'] += cl['analysis'].get('analysis_population') is not None
    c['studies'] = len(studies); c['total_n'] = sum(s.get('total_n') is not None for s in studies)
    pct = lambda a,b: f"{100*a/b:.0f}%" if b else '-'
    return {'abstracts': len(studies), 'claims': n, 'directional': pct(c['dir'],n), 'neg': pct(c['neg'],n),
            'HARD neg+significant': f"{c['hard']} ({pct(c['hard'],n)})", 'neg+direction': pct(c['neg_dir'],n),
            'total_n filled': pct(c['total_n'],c['studies']), 'sample filled': pct(c['sample'],n),
            'region filled': pct(c['region'],n), 'assoc population': pct(c['assoc_pop'],c['assoc'])}
done = {arm: {p.stem for p in (OUT/arm).glob('*.json')} for arm in ('control','order_prompt','order_prompt3')}
common = test & done['control'] & done['order_prompt'] & done['order_prompt3']
old_claims, old_studies = [], {}
for line in gzip.open(proj/'data/snapshots/v2_full_claims.jsonl.c0753147a17e005d.gz','rt'):
    r = json.loads(line)
    if r['pmid'] in common: old_claims.append(r['claim']); old_studies[r['pmid']] = r['study']
rows = {'frozen run': metrics(old_claims, list(old_studies.values()))}
for arm in ('control','order_prompt','order_prompt3'):
    cl, st, inv = [], [], 0
    for p in common:
        rec = json.loads((OUT/arm/f'{p}.json').read_text())
        if rec['status']!='valid': inv += 1; continue
        ex = rec['extraction']['extraction']; st.append(ex['study']); cl += ex['claims']
    rows[arm] = {**metrics(cl, st), 'invalid': inv}
print('abstracts in common:', len(common))
keys = list(rows['control'])
print(f"{'':24s}" + ''.join(f"{a:>14s}" for a in rows))
for k in keys: print(f"{k:24s}" + ''.join(f"{str(rows[a].get(k,'')):>14s}" for a in rows))
