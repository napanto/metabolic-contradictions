"""Claim-level semantic pairing (addendum 08E DRAFT): offline candidate counts.

Each eligible claim is described by two short texts, its extracted exposure and
its extracted outcome. Both are encoded separately with the MedCPT query
encoder and L2-normalised, so that a claim pair has two cosines, one per role.
A pair is a semantic candidate at threshold t when both cosines are >= t, the
claims come from different abstracts and share domain and comparison type.

Exhaustiveness: within each (domain, comparison type) partition, unique
(exposure text, outcome text) units are indexed by the concatenation of the two
unit vectors. Its inner product is cos_exposure + cos_outcome, so a FAISS exact
range search with radius 2*t_min returns a superset of every unit pair whose two
cosines both reach t_min; the per-role cosines are then checked exactly. No
nearest-neighbour cut-off is involved, so nothing above the threshold is missed.

Claim-pair counts per sign pattern are obtained from unit pairs without
materialising quadratic products: cross counts minus same-PMID pairs.
No model API is called; MedCPT runs locally on CPU.
"""
from __future__ import annotations

import json
import re
import time
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np

ELIGIBLE_SIGNS = ("+", "-", "0")
PATTERNS = ("opposite", "effect_null", "concordant")
# Generic exposures excluded from the semantic route only (08E DRAFT §2; marshall2020 p. 1907, rosemblat2019).
GENERIC_EXPOSURES = frozenset({
    "placebo", "control", "controls", "control group", "usual care", "patients", "participants",
    "intervention", "treatment", "therapy", "diet", "standard care",
})


def text_form(value):
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def pattern(s1, s2):
    if s1 == s2:
        return "concordant"
    return "effect_null" if "0" in (s1, s2) else "opposite"


def load_claims(path):
    """Eligible claims: not hedged, identifiable comparison type, sign in + - 0."""
    rows = []
    with open(path) as handle:
        for line in handle:
            r = json.loads(line)
            if r.get("hedged") or not r.get("comparison_identifiable") or r.get("sign") not in ELIGIBLE_SIGNS:
                continue
            claim = r["claim"]
            exposure, outcome = text_form(claim.get("exposure")), text_form(claim.get("outcome"))
            if not exposure or not outcome:
                continue
            rows.append({"claim_id": r["claim_id"], "pmid": str(r["pmid"]), "arm": r["arm"],
                         "comparison_type": r["comparison_type"], "sign": r["sign"],
                         "exposure": exposure, "outcome": outcome,
                         "exposure_key": r.get("exposure_key"), "outcome_key": r.get("outcome_key"),
                         "exposure_class_key": r.get("exposure_class_key")})
    return rows


def strict_key_compatible(a, b):
    """The registered key route (pairs.compatible_key, retrieval=False), for already-normalised claims."""
    if a["pmid"] == b["pmid"] or a["arm"] != b["arm"] or a["comparison_type"] != b["comparison_type"]:
        return False
    exposure = (a["exposure_key"] == b["exposure_key"]
                or (a["exposure_class_key"] is not None and a["exposure_class_key"] == b["exposure_key"])
                or (b["exposure_class_key"] is not None and b["exposure_class_key"] == a["exposure_key"]))
    return bool(exposure and a["outcome_key"] == b["outcome_key"])


SAPBERT = {"model": "cambridgeltl/SapBERT-from-PubMedBERT-fulltext",
           "revision": "090663c3ae57bf35ffe4d0d468a2a88d03051a4d",
           "sha256": {"pytorch_model.bin": "07f7672c7ac852d8efff83e4a7a63985bf50c03d5b57f6a7909c11fe66532137",
                      "config.json": "c0fa35def52bf7d81865d23dba0417fdbb67d416fdde70cae5c7beab476b584e",
                      "vocab.txt": "79489a52be45e6fa033521e8ce8e4f62aedc0a742ee2aa6fc04667e5b0b1454d",
                      "tokenizer_config.json": "76eb84b4b5b0d1bdba02906d7fae249be541c41d72e2024094aea513f98f2be7",
                      "special_tokens_map.json": "303df45a03609e4ead04bc3dc1536d0ab19b5358db685b6f3da123d05ec200e3"}}


def load_sapbert(threads=24, cache_dir="data/models/hf"):
    """SapBERT at a pinned revision from the project model cache; hashes checked before use."""
    import hashlib

    import torch
    from huggingface_hub import snapshot_download
    from transformers import AutoModel, AutoTokenizer
    torch.set_num_threads(threads)
    root = Path(snapshot_download(SAPBERT["model"], revision=SAPBERT["revision"], cache_dir=cache_dir,
                                  local_files_only=True))
    for name, expected in SAPBERT["sha256"].items():
        got = hashlib.sha256((root / name).read_bytes()).hexdigest()
        if got != expected:
            raise RuntimeError(f"SapBERT file changed: {name}")
    tokenizer = AutoTokenizer.from_pretrained(root, local_files_only=True)
    model = AutoModel.from_pretrained(root, local_files_only=True).eval().to("cpu")
    return tokenizer, model


def encode_texts(texts, *, threads=24, batch_size=256, max_length=64, encoder=None, model="medcpt"):
    """CLS vectors, L2-normalised. model: 'medcpt' (query encoder) or 'sapbert'. `encoder` may be injected."""
    if encoder is None:
        from .index import encode, load_encoder
        if model == "medcpt":
            tokenizer, net, _ = load_encoder("Query", threads)
        elif model == "sapbert":
            tokenizer, net = load_sapbert(threads)
        else:
            raise ValueError(model)
        encoder = lambda chunk: encode(chunk, tokenizer, net, max_length)  # noqa: E731
    order = sorted(range(len(texts)), key=lambda i: len(texts[i]))
    parts = {}
    for start in range(0, len(order), batch_size):
        idx = order[start:start + batch_size]
        parts[start] = (idx, encoder([texts[i] for i in idx]))
    dim = next(iter(parts.values()))[1].shape[1] if parts else 0
    out = np.zeros((len(texts), dim), dtype=np.float32)
    for idx, vectors in parts.values():
        out[idx] = vectors
    norms = np.linalg.norm(out, axis=1, keepdims=True)
    norms[norms == 0] = 1
    return out / norms


class Unit:
    __slots__ = ("exposure", "outcome", "claims", "sign_totals", "pmid_signs")

    def __init__(self, exposure, outcome):
        self.exposure, self.outcome = exposure, outcome
        self.claims = []
        self.sign_totals = Counter()
        self.pmid_signs = defaultdict(Counter)

    def add(self, claim):
        self.claims.append(claim)
        self.sign_totals[claim["sign"]] += 1
        self.pmid_signs[claim["pmid"]][claim["sign"]] += 1


def build_units(claims):
    partitions = defaultdict(dict)
    for c in claims:
        units = partitions[(c["arm"], c["comparison_type"])]
        key = (c["exposure"], c["outcome"])
        if key not in units:
            units[key] = Unit(*key)
        units[key].add(c)
    return {p: list(u.values()) for p, u in partitions.items()}


def cross_counts(u, v):
    """Claim pairs a in u, b in v, from different PMIDs, by sign pattern (u != v)."""
    counts = Counter()
    for s1, n1 in u.sign_totals.items():
        for s2, n2 in v.sign_totals.items():
            counts[pattern(s1, s2)] += n1 * n2
    shared = u.pmid_signs.keys() & v.pmid_signs.keys()
    for pmid in shared:
        for s1, n1 in u.pmid_signs[pmid].items():
            for s2, n2 in v.pmid_signs[pmid].items():
                counts[pattern(s1, s2)] -= n1 * n2
    return counts


def within_counts(u):
    """Unordered claim pairs inside one unit, from different PMIDs, by sign pattern."""
    counts = Counter()
    signs = sorted(u.sign_totals)
    for i, s1 in enumerate(signs):
        for s2 in signs[i:]:
            if s1 == s2:
                total = u.sign_totals[s1] * (u.sign_totals[s1] - 1) // 2
                same = sum(c[s1] * (c[s1] - 1) // 2 for c in u.pmid_signs.values())
            else:
                total = u.sign_totals[s1] * u.sign_totals[s2]
                same = sum(c[s1] * c[s2] for c in u.pmid_signs.values())
            counts[pattern(s1, s2)] += total - same
    return counts


def unit_pairs(units, exposure_vec, outcome_vec, t_min, *, threads=24):
    """Exact superset search, then exact per-role check. Yields (i, j, min_cos), i <= j."""
    import faiss
    faiss.omp_set_num_threads(threads)
    n = len(units)
    if n == 0:
        return
    concat = np.hstack([exposure_vec, outcome_vec]).astype(np.float32)
    index = faiss.IndexFlatIP(concat.shape[1])
    index.add(concat)
    step = 4096
    for start in range(0, n, step):
        lims, _dist, ids = index.range_search(concat[start:start + step], 2 * t_min - 1e-4)
        for row in range(lims.size - 1):
            i = start + row
            js = ids[lims[row]:lims[row + 1]]
            js = js[js >= i]
            if js.size == 0:
                continue
            ce = exposure_vec[js] @ exposure_vec[i]
            co = outcome_vec[js] @ outcome_vec[i]
            m = np.minimum(ce, co)
            keep = m >= t_min
            for j, value, e_cos, o_cos in zip(js[keep], m[keep], ce[keep], co[keep]):
                yield i, int(j), float(value), float(e_cos), float(o_cos)


def count_grid(claims, exposure_vectors, outcome_vectors, thresholds, *, threads=24,
               enumerate_floor=None, existing_pairs=frozenset()):
    """Counts per threshold and sign pattern.

    Aggregate counts cover every threshold. Pairs with score >= enumerate_floor (default: the lowest
    threshold) are also enumerated one by one and split into key-compatible (strict 08A key route),
    in an existing registered frame, and new; the scores of the new pairs are returned per pattern,
    so that budget-matched volumes can be computed without re-running the search.
    """
    thresholds = sorted(thresholds)
    t_min = thresholds[0]
    floor = t_min if enumerate_floor is None else max(t_min, enumerate_floor)
    excluded_generic = sum(1 for c in claims if c["exposure"] in GENERIC_EXPOSURES)
    claims = [c for c in claims if c["exposure"] not in GENERIC_EXPOSURES]
    partitions = build_units(claims)
    grid = {t: {"pairs": Counter(), "key_compatible": Counter(), "in_existing_frame": Counter(),
                "new": Counter()} for t in thresholds}
    new_scores = {p: [] for p in PATTERNS}
    unit_pair_total = 0
    started = time.monotonic()
    for (arm, ctype), units in sorted(partitions.items()):
        ev = np.vstack([exposure_vectors[u.exposure] for u in units])
        ov = np.vstack([outcome_vectors[u.outcome] for u in units])
        for i, j, score, _ce, _co in unit_pairs(units, ev, ov, t_min, threads=threads):
            unit_pair_total += 1
            counts = within_counts(units[i]) if i == j else cross_counts(units[i], units[j])
            active = [t for t in thresholds if score >= t - 1e-6]
            for t in active:
                grid[t]["pairs"].update(counts)
            if score < floor - 1e-6 or not sum(counts.values()):
                continue
            split = {"key_compatible": Counter(), "in_existing_frame": Counter(), "new": Counter()}
            left, right = units[i].claims, units[j].claims
            for ai, a in enumerate(left):
                for b in (left[ai + 1:] if i == j else right):
                    if a["pmid"] == b["pmid"]:
                        continue
                    pat = pattern(a["sign"], b["sign"])
                    if strict_key_compatible(a, b):
                        split["key_compatible"][pat] += 1
                    elif tuple(sorted((a["claim_id"], b["claim_id"]))) in existing_pairs:
                        split["in_existing_frame"][pat] += 1
                    else:
                        split["new"][pat] += 1
            for pat, n in split["new"].items():
                new_scores[pat].extend([score] * n)
            for t in active:
                for name, counter in split.items():
                    grid[t][name].update(counter)
    out = {"partitions": len(partitions), "units": sum(len(u) for u in partitions.values()),
           "excluded_generic_exposure_claims": excluded_generic, "enumerate_floor": floor,
           "unit_pairs_at_t_min": unit_pair_total, "seconds": round(time.monotonic() - started, 1),
           "thresholds": {}}
    for t in thresholds:
        g = grid[t]
        enumerated = t >= floor - 1e-9
        out["thresholds"][f"{t:.2f}"] = {
            "claim_pairs_by_pattern": dict(g["pairs"]),
            "potential_contradictions": g["pairs"]["opposite"] + g["pairs"]["effect_null"],
            "enumerated_split": enumerated,
            "key_compatible": dict(g["key_compatible"]) if enumerated else None,
            "in_existing_frame_not_key": dict(g["in_existing_frame"]) if enumerated else None,
            "new_not_in_existing_frames": dict(g["new"]) if enumerated else None,
        }
    out["new_scores"] = {p: np.sort(np.asarray(v, dtype=np.float32))[::-1] for p, v in new_scores.items()}
    return out


def enumerate_new_pairs(claims, exposure_vectors, outcome_vectors, floor, *, threads=24, existing_pairs=frozenset()):
    """Yield every semantic-route claim pair with score >= floor that is neither strict-key-compatible
    nor already in a registered frame: dict(a, b, pattern, score, cos_exposure, cos_outcome)."""
    claims = [c for c in claims if c["exposure"] not in GENERIC_EXPOSURES]
    for (_arm, _ctype), units in sorted(build_units(claims).items()):
        ev = np.vstack([exposure_vectors[u.exposure] for u in units])
        ov = np.vstack([outcome_vectors[u.outcome] for u in units])
        for i, j, score, ce, co in unit_pairs(units, ev, ov, floor, threads=threads):
            left, right = units[i].claims, units[j].claims
            for ai, a in enumerate(left):
                for b in (left[ai + 1:] if i == j else right):
                    if a["pmid"] == b["pmid"] or strict_key_compatible(a, b):
                        continue
                    if tuple(sorted((a["claim_id"], b["claim_id"]))) in existing_pairs:
                        continue
                    yield {"a": a, "b": b, "pattern": pattern(a["sign"], b["sign"]), "score": score,
                           "cos_exposure": ce, "cos_outcome": co}


def semantic_pair_record(a, b, score, cos_exposure, cos_outcome, *, encoder="medcpt-query"):
    """A frame record in the 08A/08D format (as candidate_pairs.make_pair builds it), for a semantic-route pair.

    a, b are full normalised claim records (claims_normalized.jsonl). make_pair itself refuses pairs
    without a compatible 08A key, which is exactly this route, so the same fields are filled here
    with the same helpers.
    """
    from .io import digest
    from .entity_keys import pubtator_baseline
    from .candidate_pairs import _differences
    if a["claim_id"] > b["claim_id"]:
        a, b = b, a
    signs = {a["sign"], b["sign"]}
    sign_pattern = ("unrepresented" if None in signs else "concordant" if len(signs) == 1
                    else "effect_null" if "0" in signs else "opposite")
    key = "|".join([a["arm"], "SEMANTIC_E:" + digest(sorted([text_form(a["claim"]["exposure"]),
                                                               text_form(b["claim"]["exposure"])]))[:16],
                    "SEMANTIC_O:" + digest(sorted([text_form(a["claim"]["outcome"]),
                                                   text_form(b["claim"]["outcome"])]))[:16],
                    "comparison:" + a["comparison_type"]])
    return {"pipeline_version": "2.0",
            "pair_id": "v2-pair-source-" + digest(sorted([a["claim_id"], b["claim_id"]]))[:24],
            "claim_a": a["claim_id"], "claim_b": b["claim_id"],
            "pmid_a": str(a["pmid"]), "pmid_b": str(b["pmid"]), "arm": a["arm"],
            "pair_key": key, "comparison_type": a["comparison_type"],
            "sign_pattern": sign_pattern, "origin": "semantic_claim", "origins": ["semantic_claim"],
            "retrieval_score": round(score, 6), "context_differences": _differences(a, b),
            "pubtator_baseline": pubtator_baseline(a, b),
            "semantic": {"encoder": encoder, "score_min_cosine": round(score, 6),
                         "cos_exposure": round(cos_exposure, 6), "cos_outcome": round(cos_outcome, 6),
                         "exposure_texts": [text_form(a["claim"]["exposure"]), text_form(b["claim"]["exposure"])],
                         "outcome_texts": [text_form(a["claim"]["outcome"]), text_form(b["claim"]["outcome"])]}}
