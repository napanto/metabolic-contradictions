"""Inactive, lossless compact scoring-wire prototype; no model or filesystem I/O.

Known input identities/paths/text are copied through an auditable map. Every
judgment, match, critical flag, duplicate reference and note comes from the wire
response. The normal canonical judgment validator must still run afterwards.
"""
from __future__ import annotations

from copy import deepcopy

from jsonschema import Draft202012Validator

from .io import digest
from .prototype_schemas import obj

VERSION = "v2-scoring-wire-compact-1"
GROUPS = ("comparison", "significance", "population", "sample_size", "analysis")
GROUP_CODES = {f"g{index}": group for index, group in enumerate(GROUPS)}
JUDGMENTS = ["supported", "unsupported", "unclear"]
CRITICAL_ERRORS = ["invented_comparison", "reversed_direction"]
TEXT = {"type": "string"}


def arr(item):
    return {"type": "array", "items": item}


DETAIL = obj({"i": TEXT, "j": {"enum": JUDGMENTS}, "m": arr(TEXT)})
GROUP = obj({"j": {"enum": [*JUDGMENTS, "not_represented"]}, "d": arr(DETAIL)})
CLAIM = obj({"i": TEXT, "j": {"enum": JUDGMENTS}, "e": arr({"enum": CRITICAL_ERRORS}),
             "r": arr(TEXT), "g": obj({code: GROUP for code in GROUP_CODES}),
             "o": {"type": ["string", "null"]}, "n": TEXT})
RESPONSE_SCHEMA = obj({"c": arr(obj({"i": TEXT, "c": arr(CLAIM)}))})
FORMAT_INSTRUCTIONS = """The response uses a lossless compact JSON encoding. This changes output notation only, never the annotation rules.
The input preserves every source passage and candidate assertion; candidate assertions can be wrong. Each candidate has a short alias; each claim has a short claim_id. Every emitted detail has a short detail_id plus its immutable canonical_path and full text. The source-only inventory has short result_id and detail_id values. Use those short IDs, not paths or copied text, in the response.
Return c as the array of candidates. For each candidate, i is its alias and c is its claim array. For each claim: i is claim_id; j is the unchanged supported/unsupported/unclear core judgment; e lists the unchanged critical-error names; r lists matching inventory result IDs; g contains all five field groups; o is duplicate claim_id within this same candidate or null; n is the required note-text field; use an empty string when no note is needed.
The group keys are g0=comparison, g1=significance, g2=population, g3=sample_size, g4=analysis. Each group has j for its unchanged group judgment and d for every emitted detail. Each detail has i for detail_id, j for its unchanged support judgment, and m for matching inventory detail IDs. Keep supported/unsupported/unclear/not_represented words exactly; no abbreviated or reinterpreted judgments.
Return every candidate, claim and emitted detail row exactly once. Do not invent IDs, omit difficult rows, repeat declaration rows, or match another field group's expected details. Use an empty array when no inventory result or detail matches. Reference lists and critical flags are preserved exactly, including any repetition; repetition does not add matching credit. Only the code copies known paths/text and restores original identifiers; you supply all judgments, matches, critical flags, duplicate references and notes. Do not restate immutable paths or field values in the output."""


def compact_instructions(base, *, batch):
    marker = "The input pre-enumerates each claim's emitted_details using canonical JSON paths."
    if marker not in base:
        raise ValueError("Unrecognized reviewed scoring rubric; no automatic prompt rewrite")
    # Preserve the complete core/critical/legacy semantics prefix byte-for-byte.
    prefix = base.split(marker, 1)[0]
    details = ("Judge each pre-enumerated emitted detail supported/unsupported/unclear and match only equivalent "
        "expected details from your own inventory in the same field group. Null values are not emitted details and "
        "earn no precision credit. Expected source details absent from the candidate remain unrecovered; absence is "
        "correct only when the source is silent. For each group also give supported/unsupported/unclear overall; "
        "use not_represented only for a group explicitly listed in unrepresented_groups for the legacy baseline. "
        "Do not call rich fields missing from the legacy schema false positives. Preserve all claims, including "
        "incorrect or unclear claims; do not rewrite a claim.\n")
    batching = ("The input contains up to ten anonymised candidates from the same source. Judge EACH separately "
        "against the source and your own frozen source-only inventory. Do not rank, merge, infer origin, or treat "
        "agreement between candidates as evidence. Order is arbitrary. Duplicate references are local to one "
        "candidate; a claim in another candidate is never a duplicate.\n") if batch else ""
    return prefix + details + batching + FORMAT_INSTRUCTIONS + "\nReturn only JSON matching the supplied schema."


def prepare_proposal(job):
    """Build an inactive proposal; the live judge runner does not import/use it."""
    result = encode_payload(job.payload, job.kind)
    result["instructions"] = compact_instructions(job.instructions, batch=job.kind == "batch_scoring")
    result["canonical_request_hash"] = job.request_hash
    result["wire_identity_sha256"] = digest({key: result[key] for key in ("payload", "schema", "instructions")})
    result["map_sha256"] = result["mapping"]["map_sha256"]
    result["status"] = "proposal_inactive_pending_review_and_P_calibration"
    return result


def _unique(values, expected=None, what="IDs"):
    if len(values) != len(set(values)):
        raise ValueError("Duplicate " + what)
    if expected is not None and set(values) != set(expected):
        raise ValueError("Unknown or missing " + what)


def _sealed(mapping):
    return {**mapping, "map_sha256": digest(mapping)}


def check_map(mapping):
    value = {key: val for key, val in mapping.items() if key != "map_sha256"}
    if mapping.get("version") != VERSION or mapping.get("map_sha256") != digest(value):
        raise ValueError("Compact-wire map was changed or has an unsupported version")


def encode_payload(payload, kind):
    if kind not in {"scoring", "batch_scoring"}:
        raise ValueError("Only scoring is compacted; source inventories keep their own wire")
    value = deepcopy(payload)
    candidates = value["candidates"] if kind == "batch_scoring" else [value["candidate"]]
    mapping = {"version": VERSION, "kind": kind, "canonical_payload_sha256": digest(payload),
               "pmid": payload["source"]["pmid"], "candidates": {}, "claims": {}, "details": {},
               "inventory_results": {}, "inventory_details": {}, "group_codes": GROUP_CODES}
    inverse_results, inverse_details = {}, {}
    for index, result in enumerate(value["own_inventory"]["results"]):
        short = f"r{index}"
        original = result["result_id"]
        if original in inverse_results:
            raise ValueError("Duplicate inventory result identity")
        inverse_results[original] = short
        mapping["inventory_results"][short] = original
        result["result_id"] = short
        for group in GROUPS:
            for detail in result["field_details"][group]:
                old = detail["detail_id"]
                if old in inverse_details:
                    raise ValueError("Duplicate inventory detail identity")
                code = f"e{len(mapping['inventory_details'])}"
                inverse_details[old] = code
                mapping["inventory_details"][code] = {"id": old, "group": group}
                detail["detail_id"] = code
    for index, candidate in enumerate(candidates):
        alias, original_alias = f"a{index}", candidate["alias"]
        if any(row["alias"] == original_alias for row in mapping["candidates"].values()):
            raise ValueError("Duplicate candidate alias")
        mapping["candidates"][alias] = {"alias": original_alias, "claims": []}
        candidate["alias"] = alias
        seen = set()
        for row in candidate["claims"]:
            old = row["claim"]["claim_id"]
            if old in seen:
                raise ValueError("Duplicate claim occurrence in candidate")
            seen.add(old)
            claim_id = f"c{len(mapping['claims'])}"
            mapping["candidates"][alias]["claims"].append(claim_id)
            claim_map = {"candidate": alias, "id": old, "groups": {code: [] for code in GROUP_CODES}}
            mapping["claims"][claim_id] = claim_map
            row["claim"]["claim_id"] = claim_id
            for group_code, group in GROUP_CODES.items():
                local_details = set()
                for detail in row["emitted_details"][group]:
                    original_path = detail["detail_id"]
                    if original_path in local_details:
                        raise ValueError("Duplicate emitted detail path")
                    local_details.add(original_path)
                    detail_id = f"d{len(mapping['details'])}"
                    mapping["details"][detail_id] = {"claim": claim_id, "group": group,
                        "group_code": group_code, "path": original_path, "text": detail["text"]}
                    claim_map["groups"][group_code].append(detail_id)
                    detail["canonical_path"] = original_path
                    detail["detail_id"] = detail_id
    value["wire_encoding"] = {"version": VERSION, "group_codes": GROUP_CODES}
    mapping["wire_payload_sha256"] = digest(value)
    mapping = _sealed(mapping)
    # This verifies preservation of every original fact, not merely key counts.
    if restore_payload(value, mapping) != payload:
        raise AssertionError("Compact encoding dropped or altered an input fact")
    return {"payload": value, "mapping": mapping, "schema": deepcopy(RESPONSE_SCHEMA),
            "format_instructions": FORMAT_INSTRUCTIONS}


def restore_payload(payload, mapping):
    check_map(mapping)
    if digest(payload) != mapping["wire_payload_sha256"]:
        raise ValueError("Compact input payload changed")
    restored = deepcopy(payload)
    restored.pop("wire_encoding")
    for result in restored["own_inventory"]["results"]:
        result["result_id"] = mapping["inventory_results"][result["result_id"]]
        for group in GROUPS:
            for detail in result["field_details"][group]:
                detail["detail_id"] = mapping["inventory_details"][detail["detail_id"]]["id"]
    candidates = restored["candidates"] if mapping["kind"] == "batch_scoring" else [restored["candidate"]]
    for candidate in candidates:
        candidate["alias"] = mapping["candidates"][candidate["alias"]]["alias"]
        for row in candidate["claims"]:
            row["claim"]["claim_id"] = mapping["claims"][row["claim"]["claim_id"]]["id"]
            for details in row["emitted_details"].values():
                for detail in details:
                    stored = mapping["details"][detail["detail_id"]]
                    if detail.pop("canonical_path") != stored["path"] or detail["text"] != stored["text"]:
                        raise ValueError("An immutable input field changed")
                    detail["detail_id"] = stored["path"]
    if digest(restored) != mapping["canonical_payload_sha256"]:
        raise ValueError("Decoded input does not equal the complete canonical input")
    return restored


def decode_response(response, mapping):
    """Restore only known values; never fill, repair or infer a missing judgment."""
    check_map(mapping)
    Draft202012Validator(RESPONSE_SCHEMA).validate(response)
    _unique([row["i"] for row in response["c"]], mapping["candidates"], "candidate IDs")
    candidates = []
    for candidate in response["c"]:
        alias = candidate["i"]
        _unique([row["i"] for row in candidate["c"]], mapping["candidates"][alias]["claims"], "claim IDs")
        claims = []
        for claim in candidate["c"]:
            cid, info = claim["i"], mapping["claims"][claim["i"]]
            if not set(claim["r"]) <= set(mapping["inventory_results"]):
                raise ValueError("Unknown inventory result ID")
            duplicate = claim["o"]
            if duplicate is not None and (duplicate == cid or duplicate not in mapping["candidates"][alias]["claims"]):
                raise ValueError("Duplicate reference must be another claim in the same candidate")
            groups = {}
            for group_code, group in GROUP_CODES.items():
                field = claim["g"][group_code]
                _unique([row["i"] for row in field["d"]], info["groups"][group_code], "emitted detail IDs")
                details = []
                for detail in field["d"]:
                    stored = mapping["details"][detail["i"]]
                    if any(mid not in mapping["inventory_details"] or mapping["inventory_details"][mid]["group"] != group for mid in detail["m"]):
                        raise ValueError("Unknown or cross-group inventory detail ID")
                    details.append({"detail_id": stored["path"], "text": stored["text"],
                                    "judgment": detail["j"], "matched_inventory_detail_ids":
                                        [mapping["inventory_details"][mid]["id"] for mid in detail["m"]]})
                groups[group] = {"judgment": field["j"], "emitted_details": details}
            claims.append({"claim_id": info["id"], "core": claim["j"], "critical_errors": claim["e"],
                "matched_result_ids": [mapping["inventory_results"][rid] for rid in claim["r"]],
                "field_groups": groups, "duplicate_of": None if duplicate is None else mapping["claims"][duplicate]["id"],
                "note": claim["n"]})
        candidates.append({"candidate_alias": mapping["candidates"][alias]["alias"], "claims": claims})
    if mapping["kind"] == "scoring":
        if len(candidates) != 1:
            raise ValueError("A single-candidate response cannot contain another candidate")
        return {"schema_version": "2.0", "pmid": mapping["pmid"], **candidates[0]}
    return {"schema_version": "2.0", "pmid": mapping["pmid"], "candidates": candidates}


def encode_known_response(response, mapping):
    """Offline serialization/check helper, never a generator of scientific labels."""
    check_map(mapping)
    inv_results = {value: key for key, value in mapping["inventory_results"].items()}
    inv_details = {value["id"]: key for key, value in mapping["inventory_details"].items()}
    aliases = {value["alias"]: key for key, value in mapping["candidates"].items()}
    source = response["candidates"] if mapping["kind"] == "batch_scoring" else [response]
    output = []
    for candidate in source:
        alias = aliases[candidate["candidate_alias"]]
        claim_ids = {mapping["claims"][key]["id"]: key for key in mapping["candidates"][alias]["claims"]}
        claims = []
        for claim in candidate["claims"]:
            cid = claim_ids[claim["claim_id"]]
            groups = {}
            for code, group in GROUP_CODES.items():
                detail_ids = {mapping["details"][key]["path"]: key for key in mapping["claims"][cid]["groups"][code]}
                field = claim["field_groups"][group]
                groups[code] = {"j": field["judgment"], "d": [{"i": detail_ids[d["detail_id"]],
                    "j": d["judgment"], "m": [inv_details[x] for x in d["matched_inventory_detail_ids"]]}
                    for d in field["emitted_details"]]}
            claims.append({"i": cid, "j": claim["core"], "e": claim["critical_errors"],
                "r": [inv_results[x] for x in claim["matched_result_ids"]], "g": groups,
                "o": None if claim["duplicate_of"] is None else claim_ids[claim["duplicate_of"]], "n": claim["note"]})
        output.append({"i": alias, "c": claims})
    encoded = {"c": output}
    if decode_response(encoded, mapping) != response:
        raise ValueError("Known-response roundtrip differs; input texts or identities were inconsistent")
    return encoded
