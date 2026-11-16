"""Registered, checkpointed v2 extraction experiment; no calls on import.

Selection sources are fixed by the signed manifests. P calibration never reads S
or H outputs. H is bound persistently to one selection before source access.
"""
from __future__ import annotations

import argparse
import asyncio
import fcntl
import json
import math
import re
import subprocess
import time
from collections import Counter
from contextlib import contextmanager
from pathlib import Path

from jsonschema import ValidationError

from .io import digest, file_sha, read_jsonl, utcnow, write_json
from .llm import AccountingError, BudgetError, OutputError, RequestConfigurationError
from .model_client import TransportFailure, V2Client
from .extraction import apply_verification, extraction_request, validate_extraction, verify_request

CONFIG = Path("configs/routing_registry.json")
ROOT_OUTPUT = Path("data/derived/extractions")
CALIBRATION = Path("results/P_calibration.json")
SELECTION = Path("results/extraction_selection.json")
VERIFICATION_SELECTION = Path("results/verification_candidates.json")
PROMPT_FREEZE = Path("results/prompt_freeze.json")
PROMPT_ROLES = {"extraction", "verification", "metabolic_classifier", "judge_rubrics", "oracle", "instrument"}
INPUTS = ("configs/extraction_prompt.json", "configs/extraction_schema.json",
          "configs/extraction_wire_schema.json", "configs/synthetic.json", "data/derived/partitions.json",
          "data/derived/P_manifest.json", "data/derived/S_manifest.json",
          "data/derived/H_manifest.json")


class ExperimentBlocked(RuntimeError):
    pass


def _json(path):
    return json.loads(Path(path).read_text())


def _safe(value):
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", str(value)) or value in {".", ".."}:
        raise ValueError("Unsafe experiment identifier")
    return str(value)


def _signed_file(path):
    path = Path(path)
    relative = path.resolve().relative_to(Path.cwd().resolve()).as_posix()
    committed = subprocess.run(["git", "show", f"HEAD:{relative}"], capture_output=True, check=False)
    if committed.returncode or committed.stdout != path.read_bytes():
        raise ExperimentBlocked(f"Registered input is not identical to committed HEAD: {relative}")
    result = subprocess.run(["git", "log", "-1", "--format=%H", "--", relative],
                            check=True, capture_output=True, text=True)
    revision = result.stdout.strip()
    if not revision:
        raise ExperimentBlocked(f"No registration commit for {relative}")
    verified = subprocess.run(["git", "verify-commit", revision], capture_output=True, check=False)
    if verified.returncode:
        raise ExperimentBlocked(f"Registration commit is not verifiably signed: {relative}")
    return revision


def registration_guard(config_path=CONFIG):
    spec = _json(config_path)
    if spec.get("status") != "registered":
        raise ExperimentBlocked("Development amendment must be registered before any experiment call")
    protocol = spec.get("protocol", "PROTOCOL_AMENDMENT_05_V2_DEVELOPMENT.md")
    protocol_commit = _signed_file(protocol)
    _signed_file(config_path)
    declared = spec.get("source_sha256", {})
    for relative in INPUTS:
        if relative not in declared or file_sha(relative) != declared[relative]:
            raise ExperimentBlocked(f"Registered input hash missing or changed: {relative}")
        _signed_file(relative)
    for relative, expected in declared.items():
        if file_sha(relative) != expected:
            raise ExperimentBlocked(f"Registered source hash changed: {relative}")
    return {"protocol": protocol, "protocol_commit": protocol_commit,
            "config_sha256": file_sha(config_path), "source_sha256": declared}


@contextmanager
def _lock(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def _immutable(path, content):
    path = Path(path)
    if path.exists():
        prior = _json(path)
        if prior.get("binding") != content.get("binding"):
            raise ExperimentBlocked(f"Existing experiment binding differs: {path}")
        return prior
    write_json(path, content)
    return content


def _manifest(stage):
    path = Path(f"data/derived/{stage}_manifest.json")
    manifest = _json(path)
    if manifest["partition"] != stage or digest(manifest["items"]) != manifest["items_sha256"]:
        raise ExperimentBlocked("Partition manifest mismatch")
    if manifest.get("partition_manifest_sha256") != file_sha("data/derived/partitions.json"):
        raise ExperimentBlocked("Partition manifest is not bound to the registered source partition")
    return manifest


def calibration_guard(spec, config_path=CONFIG, path=CALIBRATION, *, config_ids=None):
    freeze = _json(PROMPT_FREEZE)
    if freeze.get("status") != "frozen" or set(freeze.get("prompt_files", {})) != PROMPT_ROLES:
        raise ExperimentBlocked("S requires a complete independently reviewed prompt freeze")
    protocol = freeze.get("protocol", "PROTOCOL_ADDENDUM_05a_PROMPT_FREEZE.md")
    if freeze.get("protocol_sha256") != file_sha(protocol):
        raise ExperimentBlocked("Prompt-freeze protocol hash mismatch")
    _signed_file(protocol)
    _signed_file(PROMPT_FREEZE)
    for source in freeze["prompt_files"].values():
        if freeze.get("reviewed_prompt_sha256", {}).get(source) != file_sha(source):
            raise ExperimentBlocked("Reviewed prompt changed after freeze: " + source)
        _signed_file(source)
    value = _json(path)
    if value.get("status") != "frozen":
        raise ExperimentBlocked("S requires frozen P-only output-cap calibration")
    for field, source in [("prompt", "configs/extraction_prompt.json"),
                          ("schema", "configs/extraction_schema.json"),
                          ("P_manifest", "data/derived/P_manifest.json")]:
        if value.get("source_sha256", {}).get(field) != file_sha(source):
            raise ExperimentBlocked("P calibration input hash mismatch: " + field)
    _signed_file(path)
    wanted = set(config_ids) if config_ids is not None else {c['id'] for c in spec['configurations'] if not c.get('execution_hold')}
    for config in spec["configurations"]:
        if config['id'] not in wanted or config.get('execution_hold') or not config['available']:
            continue
        entry = value.get('configurations', {}).get(config['id'])
        if not entry or type(entry.get('max_tokens')) is not int or entry['max_tokens'] <= 0:
            raise ExperimentBlocked('P calibration omits a requested active configuration: ' + config['id'])
        if entry.get('requires_P_rerun'):
            raise ExperimentBlocked('P calibration still requires the larger-cap P run: ' + config['id'])
        if entry.get('P_complete') is not True:
            raise ExperimentBlocked('P calibration lacks complete planned source coverage: ' + config['id'])
        if entry.get('configuration_descriptor') != configuration_descriptor(config, spec, entry['max_tokens']):
            raise ExperimentBlocked('P calibration model/configuration descriptor changed: ' + config['id'])
    return value


def selection_guard(config_path=CONFIG, selection_path=SELECTION):
    if _json(config_path).get('candidate_list_final') is not True:
        raise ExperimentBlocked('No selection/H/provider check before the candidate list is final')
    value = _json(selection_path)
    if value.get("status") != "selected" or not value.get("winner"):
        raise ExperimentBlocked("H/provider check requires the unique completed selection")
    if value.get("source_sha256", {}).get("dev_config") != file_sha(config_path):
        raise ExperimentBlocked("Selection does not bind the registered development configuration")
    if value.get("source_sha256", {}).get("P_calibration") != file_sha(CALIBRATION):
        raise ExperimentBlocked("Selection does not bind frozen P calibration")
    return value


def _selection_id(selection):
    return selection.get("base_configuration_id", selection["winner"])


def _p_items(pmids=None):
    manifest = _manifest("P")
    if pmids is None:
        chosen, arms = [], set()
        for row in manifest["items"]:
            if row["arm"] not in arms:
                chosen.append(row["pmid"]); arms.add(row["arm"])
    else:
        chosen = list(map(str, pmids))
        if len(set(chosen)) != len(chosen) or not set(chosen) <= {x["pmid"] for x in manifest["items"]}:
            raise ValueError("P overrides may use only distinct original P sources")
    synthetic = _json("configs/synthetic.json")["cases"]
    return chosen, {c["article"]["pmid"]: c["article"] for c in synthetic}


def _usage(response):
    usage = response.get("usage")
    usage = usage if isinstance(usage, dict) else {}
    detail = usage.get("completion_tokens_details")
    detail = detail if isinstance(detail, dict) else {}
    total = usage.get("completion_tokens")
    reasoning = detail.get("reasoning_tokens")
    def count(value):
        return value if type(value) in (int, float) and math.isfinite(value) and value >= 0 else None
    total, reasoning = count(total), count(reasoning)
    visible = total - reasoning if total is not None and reasoning is not None and reasoning <= total else None
    text = ((response.get('choices') or [{}])[0].get('message') or {}).get('content')
    byte_bound = len(text.encode('utf-8')) if isinstance(text, str) else None
    upper = visible if visible is not None else (min(byte_bound, total) if byte_bound is not None and total is not None else byte_bound)
    return {"input_tokens": count(usage.get("prompt_tokens")), "completion_tokens": total,
            "reasoning_tokens": reasoning, "visible_tokens": visible, "cost": count(usage.get("cost")),
            "visible_token_upper_bound": upper, "visible_utf8_bytes": byte_bound,
            "visible_accounting": "reported_completion_minus_reasoning" if visible is not None else "utf8_byte_upper_bound",
            "missing_reasoning_usage_is_zero": False}


def configuration_descriptor(config, spec, max_tokens):
    model = spec['models'][config['model']]
    keys = ('tag', 'provider_name', 'quantization', 'context_length', 'max_completion_tokens', 'max_prompt_tokens')
    return {'configuration_id': config['id'], 'model': config['model'], 'reasoning': config['reasoning'],
            'max_tokens': max_tokens, 'temperature': spec.get('temperature', 0), 'seed': spec.get('seed', 42),
            'canonical_slug': model.get('canonical_slug'),
            'routes': [{key: row.get(key) for key in keys} for row in model['routes']]}


def scientific_inputs():
    paths = ['configs/extraction_prompt.json', 'configs/extraction_schema.json',
             'configs/extraction_wire_schema.json', 'configs/source_policy.json',
             'data/derived/S_manifest.json', 'configs/verification_prompt.json']
    return {p: file_sha(p) for p in paths if Path(p).exists()}


def _parse_response(response):
    choice = response["choices"][0]
    content = choice["message"].get("content")
    finish = choice.get("finish_reason")
    if finish in {"length", "max_tokens"}:
        raise ValueError("truncated_output")
    if choice["message"].get("refusal") or finish in {"content_filter", "refusal"}:
        raise ValueError("refusal")
    if not isinstance(content, str):
        raise TypeError("missing_text_output")
    return json.loads(content)


async def _one(client, article, config, *, stage, directory, cap, registration,
               forced_provider=None, original=None):
    path = directory / f"{_safe(article['pmid'])}.json"
    request = (verify_request(article, original["extraction"]) if original is not None else
               extraction_request(article, model=config["model"], reasoning=config["reasoning"],
                                  max_output_tokens=cap, provider=forced_provider))
    binding = {"stage": stage, "configuration_id": config["id"], "pmid": article["pmid"],
               "request_identity": request["identity"], "cap": cap,
               "source_provenance": request.get("source_provenance"),
               "forced_provider": forced_provider, "registration": registration,
               "original_sha256": digest(original) if original is not None else None}
    if stage == 'S':
        binding.pop('registration')
        binding['configuration_descriptor'] = config['_comparison_descriptor']
        binding['scientific_inputs'] = scientific_inputs()
    identity = digest(binding)
    if path.exists():
        saved = _json(path)
        if saved.get("binding") != binding:
            raise ExperimentBlocked("Cached extraction identity changed: " + str(path))
        if saved.get("status") in {"valid", "invalid"}:
            return saved
    attempts_dir = directory / article["pmid"]
    attempts_dir.mkdir(parents=True, exist_ok=True)
    attempts = []
    provider = forced_provider
    accepted = None
    for number in (1, 2):
        attempt_path = attempts_dir / f"attempt-{number}.json"
        if attempt_path.exists():
            attempt = _json(attempt_path)
            if attempt.get("request_identity") != request["identity"]:
                raise ExperimentBlocked("Attempt identity changed")
            if attempt.get("status") == "started":
                raise ExperimentBlocked("Interrupted attempt needs cache/billing reconciliation before resuming")
            if attempt.get('status') == 'blocked':
                if (attempt.get('error_type') in {'BudgetError', 'ReservationCapacityWait'}
                        or (attempt.get('error_type') == 'RequestConfigurationError'
                            and attempt.get('error') == 'Scheduling paused after transport/accounting failure: BudgetError'
                            and 'response' not in attempt and 'meta' not in attempt)):
                    history = attempts_dir / 'preflight_history'
                    history.mkdir(exist_ok=True)
                    write_json(history / (digest(attempt)+'.json'), attempt)
                else:
                    raise ExperimentBlocked('Accounting/configuration blocked attempt needs explicit reconciliation; no blind replay')
            if attempt.get("status") in {"valid", "invalid"}:
                attempts.append(attempt)
                provider = attempt.get("meta", {}).get("requested_provider") or provider
                if attempt["status"] == "valid":
                    accepted = attempt["validated"]
                    break
                continue
        attempt = {"number": number, "started_at": utcnow(), "status": "started",
                   "request_identity": request["identity"], "messages_sha256": digest(request["messages"]),
                   "schema_sha256": digest(request["schema"]), "max_tokens": cap,
                   "requested_provider": provider}
        write_json(attempt_path, attempt)
        started = time.monotonic()
        try:
            response, meta = await client.generate(config["model"], request["messages"], request["schema"],
                config["reasoning"], cap, forced_provider=provider,
                cache_namespace=f"experiment/{identity}/attempt-{number}")
            provider = meta["requested_provider"]
            choice = (response.get("choices") or [{}])[0]
            attempt.update(response=response, meta=meta, usage=_usage(response),
                           raw_output=choice.get("message", {}).get("content"),
                           finish_reason=choice.get("finish_reason"))
            parsed = _parse_response(response)
            validated = (apply_verification(article, original["extraction"], parsed)
                         if original is not None else validate_extraction(article, parsed))
            attempt.update(status="valid", parsed_output=parsed, validated=validated)
            accepted = validated
        except TransportFailure as exc:
            # One original ordinal failed technically; retry at most once with the exact route.
            provider = exc.requested_provider
            attempt.update(status='invalid', error_type='TransportFailure', error=str(exc),
                           failure_category='transport_missing_output', schema_validation='not_evaluated_no_usable_response',
                           meta={'request_hash': exc.request_hash, 'requested_provider': provider,
                                 'billing_status': 'unknown', 'transport_response_received': exc.response_received,
                                 'missing_output_cause': exc.missing_output_cause})
        except (BudgetError, AccountingError, RequestConfigurationError) as exc:
            attempt.update(status="blocked", error_type=type(exc).__name__, error=str(exc),
                           ended_at=utcnow(), elapsed_seconds=time.monotonic() - started)
            write_json(attempt_path, attempt)
            raise
        except (KeyError, IndexError, TypeError, ValueError, ValidationError, OutputError) as exc:
            attempt.update(status="invalid", error_type=type(exc).__name__, error=str(exc))
        attempt.update(ended_at=utcnow(), elapsed_seconds=time.monotonic() - started)
        write_json(attempt_path, attempt)
        attempts.append(attempt)
        if accepted is not None:
            break
    record = {"binding": binding, "registration_provenance": registration, "status": "valid" if accepted is not None else "invalid",
              "stage": stage, "configuration_id": config["id"], "model": config["model"],
              "reasoning": config["reasoning"], "pmid": article["pmid"], "max_tokens": cap,
              "first_attempt_valid": bool(attempts and attempts[0]["status"] == "valid"),
              "final_valid": accepted is not None,
              "truncated": bool(attempts and attempts[-1].get("finish_reason") in {"length", "max_tokens"}),
              "attempt_count": len(attempts), "attempt_files": [str(attempts_dir / f"attempt-{a['number']}.json") for a in attempts],
              "elapsed_seconds": sum(a["elapsed_seconds"] for a in attempts),
              "usage": [a.get("usage", {}) for a in attempts],
              "providers": [a.get("meta", {}).get("requested_provider") for a in attempts],
              "completed_at": utcnow(), "output": accepted,
              "failure_reasons": [a.get("error") for a in attempts if a["status"] == "invalid"]}
    if original is not None:
        record["original_extraction_record"] = original
    write_json(path, record)
    return record


def _summary(records, *, stage, plan, elapsed):
    by_config = {}
    for config_id in plan["configuration_ids"]:
        rows = [r for r in records if r["configuration_id"] == config_id]
        times = sorted(r.get("elapsed_seconds", 0) for r in rows if r["status"] != "unavailable")
        usage = [u for r in rows for u in r.get("usage", [])]
        by_config[config_id] = {"requested": len(plan["pmids"]), "completed": len(rows),
            "first_attempt_valid": sum(r.get("first_attempt_valid", False) for r in rows),
            "final_valid": sum(r.get("final_valid", False) for r in rows),
            "status_counts": dict(Counter(r["status"] for r in rows)),
            "truncated_final": sum(r.get("truncated", False) for r in rows),
            "elapsed_seconds_sum": sum(times), "mean_seconds": sum(times) / len(times) if times else None,
            "p90_seconds": times[min(len(times)-1, math.ceil(.9*len(times))-1)] if times else None,
            "cost": sum(u["cost"] for u in usage) if usage and all(u.get("cost") is not None for u in usage) else None,
            "cost_known_sum_usd": sum(u.get("cost") or 0 for u in usage),
            "cost_unknown_attempts": sum(u.get("cost") is None for u in usage),
            **{key: (sum(u[key] for u in usage) if usage and all(u.get(key) is not None for u in usage) else None)
               for key in ("input_tokens", "completion_tokens", "reasoning_tokens", "visible_tokens")},
            "token_known_sums": {key: sum(u.get(key) or 0 for u in usage)
               for key in ("input_tokens", "completion_tokens", "reasoning_tokens", "visible_tokens")},
            "token_missing_attempts": {key: sum(u.get(key) is None for u in usage)
               for key in ("input_tokens", "completion_tokens", "reasoning_tokens", "visible_tokens")}}
    return {"stage": stage, "status": "complete", "completed_at": utcnow(),
            "plan_binding": plan["binding"], "records": len(records), "elapsed_seconds": elapsed,
            "throughput_abstracts_per_second": len(records) / elapsed if elapsed else None,
            "configurations": by_config, "no_manual_repairs": True}


async def run_extractions(stage, config_ids=None, pmids=None, forced_provider=None, *,
                          config_path=CONFIG, client=None, concurrency=128,
                          calibration_round=1, cap_overrides=None, selection_path=SELECTION):
    stage = stage.upper()
    if concurrency < 1 or calibration_round < 1:
        raise ValueError("Concurrency and calibration round must be positive")
    if stage not in {"P", "S", "H", "VERIFY", "PROVIDERCHECK"}:
        raise ValueError("Unknown extraction experiment stage")
    registration = registration_guard(config_path)
    spec = _json(config_path)
    if stage != 'P' and spec.get('comparison_authorized') is False:
        raise ExperimentBlocked('S/H and comparison extraction stages are retired by the fixed production plan')
    configs = {c["id"]: c for c in spec["configurations"]}
    calibration = None
    selection = selection_guard(config_path, selection_path) if stage in {"H", "PROVIDERCHECK"} else None
    held_ids = [cid for cid, row in configs.items() if row.get('execution_hold')]
    chosen = list(config_ids) if config_ids is not None else [cid for cid in configs if cid not in held_ids]
    if set(chosen) - set(configs) or len(set(chosen)) != len(chosen):
        raise ValueError("Unknown or duplicate configuration IDs")
    if set(chosen) & set(held_ids):
        raise ExperimentBlocked('Coordinator-held configurations cannot run and are not unavailable/weak results')
    if not chosen:
        raise ExperimentBlocked('No active configurations are authorized for this batch')
    if stage in {"H", "PROVIDERCHECK"}:
        selected_id = _selection_id(selection)
        if selected_id not in configs or (config_ids is not None and chosen != [selected_id]):
            raise ExperimentBlocked("H/provider check cannot select another configuration")
        chosen = [selected_id]
    if stage == "VERIFY":
        if spec.get('candidate_list_final') is not True:
            raise ExperimentBlocked('Verification ranking waits for the final candidate list')
        selected = _json(VERIFICATION_SELECTION)
        if (selected.get("status") != "complete" or len(selected.get("configuration_ids", [])) != 2
                or selected.get("source_sha256", {}).get("dev_config") != file_sha(config_path)):
            raise ExperimentBlocked("Verification requires the registered top-two-different-model decision")
        ids = selected["configuration_ids"]
        if len({configs[i]["model"] for i in ids}) != 2 or (config_ids is not None and chosen != ids):
            raise ExperimentBlocked("Verification must use exactly the two prespecified different models")
        chosen = ids
    if set(chosen) & set(held_ids):
        raise ExperimentBlocked('Selected configuration is still on coordinator hold')
    if stage != 'P':
        calibration = calibration_guard(spec, config_path, config_ids=chosen)
    if stage != "P" and pmids is not None:
        raise ExperimentBlocked("S/H/verification/provider source lists cannot be overridden")
    if stage != "P" and cap_overrides:
        raise ExperimentBlocked("Frozen output caps cannot change after P")
    if forced_provider is not None and stage not in {"P", "PROVIDERCHECK"}:
        raise ExperimentBlocked("Forced-provider comparisons belong only to P or provider check")
    if stage == "PROVIDERCHECK" and not forced_provider:
        raise ExperimentBlocked("Provider consistency check requires an explicit registered provider")
    if stage == "P":
        wanted, synthetic = _p_items(pmids)
        wanted += list(synthetic)
        stage_dir = ROOT_OUTPUT / "P" / f"round-{calibration_round:02d}"
    else:
        wanted = [row["pmid"] for row in _manifest("H" if stage == "H" else "S")["items"]]
        synthetic = {}
        stage_dir = ROOT_OUTPUT / stage
        if stage == "PROVIDERCHECK":
            stage_dir /= _safe(forced_provider.replace("/", "__"))
    source_hashes = {"manifest": file_sha(f"data/derived/{'H' if stage == 'H' else 'P' if stage == 'P' else 'S'}_manifest.json"),
                     "calibration": file_sha(CALIBRATION) if calibration else None,
                     "selection": file_sha(selection_path) if selection else None}
    caps = {cid: (cap_overrides or {}).get(cid, configs[cid]["initial_max_tokens"]) if stage == "P"
            else calibration["configurations"][cid]["max_tokens"] if configs[cid]["available"] else None
            for cid in chosen}
    for cid in chosen:
        config, cap = configs[cid], caps[cid]
        if not config["available"]:
            continue
        if not isinstance(cap, int) or isinstance(cap, bool) or cap <= 0:
            raise ExperimentBlocked("Invalid frozen generation limit: " + cid)
        if not any(r["max_completion_tokens"] >= cap and (forced_provider is None or r["tag"] == forced_provider)
                   for r in spec["models"][config["model"]]["routes"]):
            raise ExperimentBlocked("No registered endpoint can supply the frozen cap: " + cid)
    binding = {"stage": stage, "configuration_ids": chosen, "pmids": wanted, "caps": caps,
               "forced_provider": forced_provider, "registration": registration, "source_sha256": source_hashes}
    plan = {**binding, "binding": binding, "created_at": utcnow(),
            'held_configuration_ids': held_ids, 'candidate_list_final': spec.get('candidate_list_final') is True,
            "order": [{"pmid": pmid, "configuration_id": chosen[(j+i) % len(chosen)]}
                      for i, pmid in enumerate(wanted) for j in range(len(chosen))]}
    stage_dir.mkdir(parents=True, exist_ok=True)
    with _lock(stage_dir / "run.lock"):
        if stage == "H":
            _immutable(ROOT_OUTPUT / "H_started.json", {"binding": binding, "started_at": utcnow(),
                        "policy": "Only this selected configuration may access H; resumes retain this binding"})
        if stage == 'S':
            for cid in chosen:
                descriptor = configuration_descriptor(configs[cid], spec, caps[cid])
                config_binding = {'stage': 'S', 'configuration_id': cid, 'configuration_descriptor': descriptor,
                    'pmids': wanted, 'source_manifest_sha256': source_hashes['manifest'],
                    'scientific_inputs': scientific_inputs(), 'max_tokens': caps[cid]}
                _immutable(stage_dir / 'configurations' / f'{_safe(cid)}.json',
                    {'binding': config_binding, 'configuration_id': cid, 'pmids': wanted,
                     'created_at': utcnow(), 'registration_provenance': registration,
                     'calibration_entry_sha256': digest(calibration['configurations'].get(cid))})
            batch_dir = stage_dir / 'batches' / digest(binding)[:24]
            plan = _immutable(batch_dir / 'plan.json', plan)
            if not (stage_dir / 'plan.json').exists():
                write_json(stage_dir / 'plan.json', plan)
        else:
            batch_dir = stage_dir
            plan = _immutable(stage_dir / 'plan.json', plan)
        # Source text is loaded only after registration/calibration/selection and
        # the persistent H binding have passed.
        articles = {row["pmid"]: row for row in read_jsonl("data/derived/corpus.jsonl") if row["pmid"] in set(wanted)}
        articles.update(synthetic)
        if set(articles) != set(wanted):
            raise ExperimentBlocked("Frozen experiment source missing from corpus")
        own_client = client is None
        client = client or V2Client(config=config_path, concurrency=concurrency)
        semaphore = asyncio.Semaphore(concurrency)
        started = time.monotonic()
        records = []
        stop = asyncio.Event()
        failures = []
        async def work(row):
            config = {**configs[row["configuration_id"]]}
            if stage == 'S':
                config['_comparison_descriptor'] = configuration_descriptor(config, spec, caps[config['id']])
            directory = stage_dir / _safe(config["id"])
            if not config["available"]:
                result = {"status": "unavailable", "configuration_id": config["id"], "pmid": row["pmid"],
                          "reason": config["unavailable_reason"], "first_attempt_valid": False, "final_valid": False}
                write_json(directory / f"{row['pmid']}.json", result)
                return result
            cap = caps[config["id"]]
            if not isinstance(cap, int) or isinstance(cap, bool) or cap <= 0:
                raise ExperimentBlocked("Invalid frozen generation limit")
            if not any(r["max_completion_tokens"] >= cap and (forced_provider is None or r["tag"] == forced_provider)
                       for r in spec["models"][config["model"]]["routes"]):
                raise ExperimentBlocked("No registered endpoint can supply the frozen cap")
            original = None
            if stage == "VERIFY":
                original = _json(ROOT_OUTPUT / "S" / config["id"] / f"{row['pmid']}.json")
                if original["status"] != "valid":
                    result = {"status": "invalid", "configuration_id": config["id"], "pmid": row["pmid"],
                              "reason": "base_extraction_invalid", "first_attempt_valid": False, "final_valid": False}
                    write_json(directory / f"{row['pmid']}.json", result)
                    return result
                original = original["output"]
            async with semaphore:
                two_pass = bool(selection and selection.get("verify", False))
                result = await _one(client, articles[row["pmid"]], config, stage=stage,
                                    directory=directory / "base" if two_pass else directory,
                                    cap=cap, registration=registration,
                                    forced_provider=forced_provider, original=original)
                if not two_pass:
                    return result
                if result["status"] != "valid":
                    write_json(directory / f"{row['pmid']}.json", result)
                    return result
                verified = await _one(client, articles[row["pmid"]], config, stage=stage + "_VERIFY",
                                      directory=directory / "verification", cap=cap,
                                      registration=registration, forced_provider=forced_provider,
                                      original=result["output"])
                combined = {**verified, "stage": stage,
                            "pipeline_variant": "extract_then_verify", "selected_configuration_id": selection["winner"],
                            "first_attempt_valid": result["first_attempt_valid"] and verified["first_attempt_valid"],
                            "elapsed_seconds": result["elapsed_seconds"] + verified["elapsed_seconds"],
                            "usage": result["usage"] + verified["usage"],
                            "providers": result["providers"] + verified["providers"],
                            "base_record": result}
                write_json(directory / f"{row['pmid']}.json", combined)
                return combined
        queue = asyncio.Queue()
        for row in plan["order"]:
            queue.put_nowait(row)
        async def worker():
            while not stop.is_set():
                try:
                    row = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                try:
                    records.append(await work(row))
                    write_json(stage_dir / "progress.json", {"updated_at": utcnow(), "completed": len(records),
                               "requested": len(plan["order"]), "status_counts": dict(Counter(r["status"] for r in records))})
                except Exception as exc:  # noqa: BLE001 -- stop launches, drain calls, then re-raise.
                    failures.append(exc)
                    stop.set()
                    return
        tasks = [asyncio.create_task(worker()) for _ in range(min(concurrency, len(plan["order"])))]
        try:
            # Funding/configuration failures stop new work, while already
            # submitted requests drain so their billing and outputs are retained.
            await asyncio.gather(*tasks)
            if failures:
                write_json(stage_dir / "blocked.json", {"blocked_at": utcnow(),
                           "type": type(failures[0]).__name__, "reason": str(failures[0]),
                           "completed": len(records), "queued_unstarted": queue.qsize()})
                raise failures[0]
        except BaseException:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        finally:
            if own_client:
                await client.close()
        summary = _summary(records, stage=stage, plan=plan, elapsed=time.monotonic()-started)
        summary['held_configuration_ids'] = held_ids
        summary['candidate_list_final'] = spec.get('candidate_list_final') is True
        summary['complete_active_batch_only'] = stage == 'S'
        write_json(batch_dir / "summary.json", summary)
        if stage != 'S' or not (stage_dir / 'summary.json').exists():
            write_json(stage_dir / "summary.json", summary)
        return summary


def calibration_proposal(config_path=CONFIG, *, round_number=1, config_ids=None):
    """P-only, no inference; root signs the frozen accepted proposal before S."""
    spec = _json(config_path)
    directory = ROOT_OUTPUT / "P" / f"round-{round_number:02d}"
    plan = _json(directory / 'plan.json')
    natural, synthetic = _p_items()
    required_pmids = set(natural) | set(synthetic)
    fractions = {"low": .2, "medium": .5, "high": .8, "xhigh": .95, "max": .95}
    result = {}
    for config in spec["configurations"]:
        if config_ids is not None and config['id'] not in config_ids:
            continue
        if config.get('execution_hold'):
            result[config['id']] = {'status': 'execution_hold', 'max_tokens': None, 'not_a_quality_result': True}
            continue
        if not config["available"]:
            result[config["id"]] = {"status": "unavailable", "max_tokens": None,
                                    "reason": config["unavailable_reason"]}
            continue
        rows = [_json(p) for p in sorted((directory / config["id"]).glob("*.json"))]
        if not rows:
            result[config['id']] = {'status': 'pending_P', 'max_tokens': None}
            continue
        observed_pmids = {str(row['pmid']) for row in rows}
        planned_pmids = set(map(str, plan['pmids']))
        for row in rows:
            expected = {'stage': 'P', 'configuration_id': config['id'], 'pmid': row['pmid']}
            if any(row.get(k) != v or row.get('binding', {}).get(k) != v for k, v in expected.items()):
                raise ExperimentBlocked('P calibration record identity differs from its declared source/configuration')
        if (config['id'] not in plan['configuration_ids'] or
                observed_pmids != planned_pmids or len(rows) != len(observed_pmids)
                or not required_pmids <= observed_pmids):
            result[config['id']] = {'status': 'incomplete_P', 'max_tokens': None,
                'P_complete': False, 'P_records': len(rows),
                'missing_pmids': sorted((required_pmids | planned_pmids) - observed_pmids)}
            continue
        usage, response_hashes = [], {}
        for row in rows:
            for attempt_path in row.get('attempt_files', []):
                attempt = _json(attempt_path)
                if isinstance(attempt.get('response'), dict):
                    usage.append(_usage(attempt['response']))
                    response_hashes[attempt_path] = file_sha(attempt_path)
        if not usage:
            raise ExperimentBlocked('P calibration needs archived raw responses: ' + config['id'])
        visible = max((u.get('visible_token_upper_bound') or 0 for u in usage), default=0)
        total = max((u.get("completion_tokens") or 0 for u in usage), default=0)
        current = max(row["max_tokens"] for row in rows)
        endpoint_max = max(r["max_completion_tokens"] for r in spec["models"][config["model"]]["routes"])
        visible_reserve = max(4096, 2*visible)
        fraction = fractions.get(config["reasoning"])
        required = math.ceil(visible_reserve / (1-fraction)) if fraction is not None else (
            max(visible_reserve, 2*total) if config["reasoning"] == "on" else visible_reserve)
        needs_more = any(row.get("truncated") for row in rows) or total >= .9*current
        # Amendment05 requires twice the observed total completion at every
        # effort, independently of the visible-output/fraction stress margin.
        proposed = max(current * 2 if needs_more else current, required, 2*total)
        proposed = math.ceil(proposed / 1024)*1024
        capped = proposed > endpoint_max
        chosen_cap = min(proposed, endpoint_max)
        result[config["id"]] = {"status": "proposed_native_maximum" if capped else "proposed",
            "max_tokens": chosen_cap, "required_max_tokens": proposed,
            'configuration_descriptor': configuration_descriptor(config, spec, chosen_cap),
            'native_endpoint_stress_shortfall': capped,
            'effort_unchanged': True, 'truncations_on_S_count_normally': True,
            'usage_source': 'recomputed_from_archived_raw_attempt_responses', 'raw_response_sha256': response_hashes,
            'missing_reasoning_usage_count': sum(u['reasoning_tokens'] is None for u in usage),
            'visible_upper_bound_count': sum(u['visible_tokens'] is None for u in usage),
            "current_max_tokens": current, "endpoint_max_tokens": endpoint_max,
            "max_observed_visible": max((u['visible_tokens'] for u in usage if u['visible_tokens'] is not None), default=None),
            "visible_token_bound_for_calibration": visible, "max_observed_completion": total,
            "missing_completion_usage_count": sum(u['completion_tokens'] is None for u in usage),
            "visible_reserve": visible_reserve, "reasoning_fraction_scenario": fraction,
            "requires_P_rerun": chosen_cap > current,
            "P_complete": True, "P_plan_sha256": file_sha(directory / 'plan.json'),
            "P_records": len(rows), "P_valid": sum(row["final_valid"] for row in rows)}
    return {"status": "proposed_not_frozen", "created_at": utcnow(), "P_round": round_number,
            "source_sha256": {"dev_config": file_sha(config_path),
                              "prompt": file_sha("configs/extraction_prompt.json"),
                              "schema": file_sha("configs/extraction_schema.json"),
                              "P_manifest": file_sha("data/derived/P_manifest.json")},
            "configurations": result, "selection_outputs_read": False, "heldout_outputs_read": False}


def merge_calibration_entries(existing, proposal, configuration_ids):
    """Pure append-only calibration assembly; caller must sign it before S.

    Registry hashes are provenance only. Shared scientific inputs and existing
    per-configuration entries can never be changed by an append operation.
    """
    from copy import deepcopy
    result = deepcopy(existing) if existing is not None else {
        'status': 'frozen', 'source_sha256': deepcopy(proposal['source_sha256']),
        'configurations': {}, 'registration_history': []}
    if result.get('status') != 'frozen':
        raise ExperimentBlocked('Cannot append to unfrozen calibration')
    for key in ('prompt', 'schema', 'P_manifest'):
        if result['source_sha256'].get(key) != proposal['source_sha256'].get(key):
            raise ExperimentBlocked('Shared calibration source changed: ' + key)
    for config_id in configuration_ids:
        entry = proposal['configurations'][config_id]
        if (type(entry.get('max_tokens')) is not int or not entry.get('configuration_descriptor')
                or entry.get('P_complete') is not True):
            raise ExperimentBlocked('Requested calibration entry is held, unavailable or incomplete')
        if entry.get('requires_P_rerun'):
            raise ExperimentBlocked('Cannot freeze calibration before the larger-cap P run')
        prior = result['configurations'].get(config_id)
        if prior is not None and prior != entry:
            raise ExperimentBlocked('Cannot rewrite a frozen configuration calibration: ' + config_id)
        result['configurations'][config_id] = deepcopy(entry)
    provenance = proposal['source_sha256'].get('dev_config')
    history = result.setdefault('registration_history', [])
    if provenance not in history:
        history.append(provenance)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=["P", "S", "H", "VERIFY", "PROVIDERCHECK", "calibrate"])
    parser.add_argument("--config", default=str(CONFIG))
    parser.add_argument("--config-id", action="append")
    parser.add_argument("--pmid", action="append")
    parser.add_argument("--provider")
    parser.add_argument("--round", type=int, default=1)
    parser.add_argument("--caps", help="JSON object of P-only cap overrides")
    parser.add_argument("--concurrency", type=int, default=128)
    args = parser.parse_args()
    if args.stage == "calibrate":
        result = calibration_proposal(args.config, round_number=args.round, config_ids=args.config_id)
        write_json("results/P_calibration_proposal.json", result)
    else:
        result = asyncio.run(run_extractions(args.stage, args.config_id, args.pmid, args.provider,
            config_path=args.config, calibration_round=args.round,
            cap_overrides=_json(args.caps) if args.caps else None, concurrency=args.concurrency))
    print(json.dumps(result, indent=2))
