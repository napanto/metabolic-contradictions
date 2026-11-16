"""Synthetic transports only: registration, checkpoints, retries and H boundaries."""
from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from pathlib import Path

import pytest

from chl_rag import model_runs as exp
from chl_rag.io import digest, file_sha, write_json, write_jsonl
from chl_rag.llm import BudgetError

SYNTHETIC = json.loads(Path("configs/synthetic.json").read_text())
PROMPT = json.loads(Path("configs/extraction_prompt.json").read_text())
VERIFY_PROMPT = json.loads(Path("configs/verification_prompt.json").read_text())
WIRE_SCHEMA = json.loads(Path("configs/extraction_wire_schema.json").read_text())
SCHEMA = json.loads(Path("configs/extraction_schema.json").read_text())


class FakeClient:
    def __init__(self, answers, invalid_count=0, budget_failure=False):
        self.answers = answers
        self.invalid_count = invalid_count
        self.calls = []
        self.budget_failure = budget_failure

    async def generate(self, model, messages, schema, reasoning, max_tokens, *, forced_provider=None, cache_namespace=None):
        self.calls.append({"model": model, "messages": deepcopy(messages), "schema": deepcopy(schema),
                           "cap": max_tokens, "forced_provider": forced_provider, "namespace": cache_namespace})
        if self.budget_failure:
            raise BudgetError("Synthetic funding alert")
        payload = json.loads(messages[-1]["content"])
        if "extraction" in payload:
            answer = {"claims": [{"claim_id": c["claim_id"], "support": "supported",
                                   "contested_fields": [], "evidence": [2]}
                                  for c in payload["extraction"]["claims"]]}
        else:
            answer = self.answers[payload["pmid"]]
        invalid = len(self.calls) <= self.invalid_count
        response = {"id": f"fake-{len(self.calls)}", "provider": "Synthetic",
                    "choices": [{"message": {"content": "not JSON" if invalid else json.dumps(answer)}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 100, "completion_tokens": 20,
                              "completion_tokens_details": {"reasoning_tokens": 5}, "cost": .001}}
        return response, {"requested_provider": "synthetic/fp8", "request_hash": digest(self.calls[-1]),
                          "provider": "Synthetic", "cache_hit": False}


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(exp, "_signed_file", lambda path: "synthetic-signed-commit")
    articles, answers = [], {}
    items = []
    for index, case in enumerate([SYNTHETIC["cases"][3], SYNTHETIC["cases"][1], SYNTHETIC["cases"][0]]):
        article = deepcopy(case["article"]); article["pmid"] = str(988800001 + index)
        articles.append(article); answers[article["pmid"]] = case["expected_model_output"]
        items.append({"pmid": article["pmid"], "arm": article["arms"][0]})
    for stage, index in [("S", 10), ("H", 11)]:
        article = deepcopy(SYNTHETIC["cases"][0]["article"]); article["pmid"] = str(988800001 + index)
        articles.append(article); answers[article["pmid"]] = SYNTHETIC["cases"][0]["expected_model_output"]
    for case in SYNTHETIC["cases"]:
        answers[case["article"]["pmid"]] = case["expected_model_output"]
    write_jsonl("data/derived/corpus.jsonl", articles)
    write_json("data/derived/partitions.json", {"synthetic": True})
    for stage, rows in [("P", items), ("S", [{"pmid": "988800011", "arm": "diet"}]),
                        ("H", [{"pmid": "988800012", "arm": "diet"}])]:
        write_json(f"data/derived/{stage}_manifest.json", {"partition": stage, "items": rows,
                   "items_sha256": digest(rows), "partition_manifest_sha256": file_sha("data/derived/partitions.json")})
    write_json("configs/synthetic.json", SYNTHETIC)
    write_json("configs/extraction_prompt.json", PROMPT)
    write_json("configs/extraction_schema.json", SCHEMA)
    write_json("configs/extraction_wire_schema.json", WIRE_SCHEMA)
    write_json("configs/verification_prompt.json", VERIFY_PROMPT)
    Path("PROTOCOL_AMENDMENT_05_V2_DEVELOPMENT.md").write_text("Synthetic registered test protocol")
    route = {"tag": "synthetic/fp8", "provider_name": "Synthetic", "max_completion_tokens": 262144}
    config = {"status": "registered", "candidate_list_final": True, "protocol": "PROTOCOL_AMENDMENT_05_V2_DEVELOPMENT.md",
              "models": {"model-a": {"routes": [route]}, "model-b": {"routes": [route]}},
              "configurations": [{"id": "candidate-a", "model": "model-a", "reasoning": "off",
                                  "initial_max_tokens": 8192, "available": True, "unavailable_reason": None},
                                 {"id": "candidate-b", "model": "model-b", "reasoning": "low",
                                  "initial_max_tokens": 8192, "available": True, "unavailable_reason": None}],
              "source_sha256": {p: file_sha(p) for p in exp.INPUTS}}
    write_json(exp.CONFIG, config)
    return tmp_path, config, answers


def freeze_calibration(project):
    _, config, _ = project
    protocol = "PROTOCOL_ADDENDUM_05a_PROMPT_FREEZE.md"
    Path(protocol).write_text("Synthetic independently reviewed prompt freeze")
    prompts = {role: "configs/verification_prompt.json" if role == "verification" else
               "configs/extraction_prompt.json" for role in exp.PROMPT_ROLES}
    write_json(exp.PROMPT_FREEZE, {"status": "frozen", "protocol": protocol,
               "protocol_sha256": file_sha(protocol), "prompt_files": prompts,
               "reviewed_prompt_sha256": {path: file_sha(path) for path in prompts.values()}})
    proposal = {"status": "frozen", "source_sha256": {
        "dev_config": file_sha(exp.CONFIG), "prompt": file_sha("configs/extraction_prompt.json"),
        "schema": file_sha("configs/extraction_schema.json"), "P_manifest": file_sha("data/derived/P_manifest.json")},
        "configurations": {c["id"]: {"max_tokens": c["initial_max_tokens"], "P_complete": True, "configuration_descriptor": exp.configuration_descriptor(c, config, c["initial_max_tokens"])} for c in config["configurations"]}}
    write_json(exp.CALIBRATION, proposal)


def select(project, candidate="candidate-a", verify=False):
    write_json(exp.SELECTION, {"status": "selected", "winner": candidate + ("__verify" if verify else ""),
                              "base_configuration_id": candidate, "verify": verify,
                              "source_sha256": {"dev_config": file_sha(exp.CONFIG), "P_calibration": file_sha(exp.CALIBRATION)}})


def test_unsigned_unregistered_gate_before_any_client_call(project):
    _, config, answers = project
    config["status"] = "draft"; write_json(exp.CONFIG, config)
    client = FakeClient(answers)
    with pytest.raises(exp.ExperimentBlocked, match="registered"):
        asyncio.run(exp.run_extractions("P", client=client))
    assert not client.calls


def test_source_drift_fails_before_calls(project):
    _, _, answers = project
    Path("configs/synthetic.json").write_text("{}")
    client = FakeClient(answers)
    with pytest.raises(exp.ExperimentBlocked, match="hash"):
        asyncio.run(exp.run_extractions("P", client=client))
    assert not client.calls


def test_p_default_three_sources_plus_six_synthetic_and_resume(project):
    _, _, answers = project; client = FakeClient(answers)
    summary = asyncio.run(exp.run_extractions("P", ["candidate-a"], client=client, concurrency=2))
    assert summary["records"] == 9
    assert summary["configurations"]["candidate-a"]["final_valid"] == 9
    assert len(client.calls) == 9
    asyncio.run(exp.run_extractions("P", ["candidate-a"], client=client, concurrency=2))
    assert len(client.calls) == 9


def test_exact_retry_keeps_messages_schema_cap_and_provider(project):
    _, _, answers = project; client = FakeClient(answers, invalid_count=1)
    summary = asyncio.run(exp.run_extractions("P", ["candidate-a"], client=client, concurrency=1))
    assert len(client.calls) == 10
    first, retry = client.calls[:2]
    assert first["messages"] == retry["messages"] and first["schema"] == retry["schema"]
    assert first["cap"] == retry["cap"]
    assert retry["forced_provider"] == "synthetic/fp8"
    assert first["namespace"] != retry["namespace"]
    assert summary["configurations"]["candidate-a"]["first_attempt_valid"] == 8
    assert summary["configurations"]["candidate-a"]["final_valid"] == 9


def test_terminal_invalid_output_is_never_retried_a_third_time(project):
    _, _, answers = project; client = FakeClient(answers, invalid_count=2)
    summary = asyncio.run(exp.run_extractions("P", ["candidate-a"], client=client, concurrency=1))
    assert summary["configurations"]["candidate-a"]["final_valid"] == 8
    before = len(client.calls)
    asyncio.run(exp.run_extractions("P", ["candidate-a"], client=client, concurrency=1))
    assert len(client.calls) == before


def test_s_requires_frozen_calibration_and_refuses_cherrypicking(project):
    _, _, answers = project; client = FakeClient(answers)
    with pytest.raises(FileNotFoundError):
        asyncio.run(exp.run_extractions("S", client=client))
    freeze_calibration(project)
    partial = asyncio.run(exp.run_extractions("S", ["candidate-a"], client=client))
    assert partial["records"] == 1
    with pytest.raises(exp.ExperimentBlocked, match="cannot be overridden"):
        asyncio.run(exp.run_extractions("S", pmids=["988800011"], client=client))
    summary = asyncio.run(exp.run_extractions("S", client=client))
    assert summary["records"] == 2


def test_h_is_bound_to_one_selection_and_cannot_switch_on_resume(project):
    _, _, answers = project; client = FakeClient(answers)
    freeze_calibration(project); select(project)
    result = asyncio.run(exp.run_extractions("H", client=client))
    assert result["records"] == 1 and len(client.calls) == 1
    select(project, "candidate-b")
    with pytest.raises(exp.ExperimentBlocked, match="binding differs"):
        asyncio.run(exp.run_extractions("H", client=client))
    assert len(client.calls) == 1


def test_h_checks_selected_two_pass_variant(project):
    _, _, answers = project; client = FakeClient(answers)
    freeze_calibration(project); select(project, verify=True)
    result = asyncio.run(exp.run_extractions("H", client=client))
    assert result["records"] == 1 and len(client.calls) == 2
    record = json.loads(Path("data/derived/extractions/H/candidate-a/988800012.json").read_text())
    assert record["pipeline_variant"] == "extract_then_verify"
    assert "verification" in record["output"]
    assert record["output"]["original_extraction"] == record["base_record"]["output"]["extraction"]


def test_provider_check_requires_explicit_registered_route(project):
    _, _, answers = project; client = FakeClient(answers)
    freeze_calibration(project); select(project)
    with pytest.raises(exp.ExperimentBlocked, match="explicit registered provider"):
        asyncio.run(exp.run_extractions("PROVIDERCHECK", client=client))
    with pytest.raises(exp.ExperimentBlocked, match="No registered endpoint"):
        asyncio.run(exp.run_extractions("PROVIDERCHECK", forced_provider="unknown", client=client))
    assert not client.calls
    asyncio.run(exp.run_extractions("PROVIDERCHECK", forced_provider="synthetic/fp8", client=client))
    assert all(c["forced_provider"] == "synthetic/fp8" for c in client.calls)


def test_output_cap_change_requires_new_p_round(project):
    _, _, answers = project; client = FakeClient(answers)
    asyncio.run(exp.run_extractions("P", ["candidate-a"], client=client))
    with pytest.raises(exp.ExperimentBlocked, match="binding differs"):
        asyncio.run(exp.run_extractions("P", ["candidate-a"], cap_overrides={"candidate-a": 16384}, client=client))
    asyncio.run(exp.run_extractions("P", ["candidate-a"], cap_overrides={"candidate-a": 16384}, calibration_round=2, client=client))
    assert len(client.calls) == 18


def test_p_only_calibration_uses_registered_margin_and_no_s_or_h(project):
    _, _, answers = project; client = FakeClient(answers)
    asyncio.run(exp.run_extractions("P", client=client))
    proposal = exp.calibration_proposal()
    assert proposal["selection_outputs_read"] is False and proposal["heldout_outputs_read"] is False
    assert proposal["configurations"]["candidate-a"]["visible_reserve"] == 4096
    assert proposal["configurations"]["candidate-b"]["reasoning_fraction_scenario"] == .2
    assert proposal["status"] == "proposed_not_frozen"


def test_funding_failure_stops_new_jobs_without_inventing_invalid_labels(project):
    _, _, answers = project; client = FakeClient(answers, budget_failure=True)
    with pytest.raises(BudgetError):
        asyncio.run(exp.run_extractions("P", ["candidate-a"], client=client, concurrency=1))
    assert len(client.calls) == 1
    blocked = json.loads(Path("data/derived/extractions/P/round-01/blocked.json").read_text())
    assert blocked["queued_unstarted"] == 8
    assert not Path("data/derived/extractions/P/round-01/candidate-a/988800001.json").exists()


def test_partial_hold_is_not_an_unavailable_or_quality_result(project):
    _, config, answers = project
    config['candidate_list_final'] = False
    config['configurations'][1]['execution_hold'] = True
    write_json(exp.CONFIG, config)
    client = FakeClient(answers)
    summary = asyncio.run(exp.run_extractions('P', client=client))
    assert summary['held_configuration_ids'] == ['candidate-b']
    assert summary['records'] == 9 and len(client.calls) == 9
    assert not Path('data/derived/extractions/P/round-01/candidate-b').exists()
    with pytest.raises(exp.ExperimentBlocked, match='Coordinator-held'):
        asyncio.run(exp.run_extractions('P', ['candidate-b'], client=client))
    with pytest.raises(exp.ExperimentBlocked, match='candidate list is final'):
        exp.selection_guard()


def test_incremental_s_preserves_old_bytes_and_only_runs_new_model(project):
    _, config, answers = project
    config['candidate_list_final'] = False
    config['configurations'][1]['execution_hold'] = True
    write_json(exp.CONFIG, config)
    project = (project[0], config, answers)
    freeze_calibration(project)
    client = FakeClient(answers)
    asyncio.run(exp.run_extractions('S', client=client))
    old_path = Path('data/derived/extractions/S/candidate-a/988800011.json')
    old_bytes = old_path.read_bytes()
    old_plan = Path('data/derived/extractions/S/plan.json').read_bytes()
    assert len(client.calls) == 1
    config['configurations'][1]['execution_hold'] = False
    config['candidate_list_final'] = True
    write_json(exp.CONFIG, config)
    freeze_calibration(project)
    asyncio.run(exp.run_extractions('S', ['candidate-b'], client=client))
    assert len(client.calls) == 2
    assert old_path.read_bytes() == old_bytes
    assert Path('data/derived/extractions/S/plan.json').read_bytes() == old_plan
    assert len(list(Path('data/derived/extractions/S/batches').glob('*/plan.json'))) == 2
    asyncio.run(exp.run_extractions('S', ['candidate-a'], client=client))
    assert len(client.calls) == 2 and old_path.read_bytes() == old_bytes


def test_missing_reasoning_is_unknown_with_utf8_visible_upper_bound():
    value = exp._usage({'usage': {'completion_tokens': 20, 'completion_tokens_details': {'reasoning_tokens': None}},
                        'choices': [{'message': {'content': '{"x":"µ"}'}}]})
    assert value['reasoning_tokens'] is None and value['visible_tokens'] is None
    assert value['visible_token_upper_bound'] == len('{"x":"µ"}'.encode())
    assert value['missing_reasoning_usage_is_zero'] is False


def test_native_endpoint_ceiling_reports_stress_shortfall_without_dropping_effort(project):
    _, config, answers = project
    config['configurations'][0]['reasoning'] = 'max'
    config['models']['model-a']['routes'][0]['max_completion_tokens'] = 32768
    write_json(exp.CONFIG, config)
    client = FakeClient(answers)
    asyncio.run(exp.run_extractions('P', ['candidate-a'], client=client))
    proposal = exp.calibration_proposal(config_ids=['candidate-a'])['configurations']['candidate-a']
    assert proposal['max_tokens'] == 32768
    assert proposal['required_max_tokens'] > 32768
    assert proposal['native_endpoint_stress_shortfall'] is True
    assert proposal['configuration_descriptor']['reasoning'] == 'max'
    assert proposal['usage_source'] == 'recomputed_from_archived_raw_attempt_responses'


def test_calibration_entries_append_without_rewriting_prior_configuration(project):
    _, _, answers = project
    client = FakeClient(answers)
    asyncio.run(exp.run_extractions('P', client=client))
    proposal = exp.calibration_proposal()
    first = exp.merge_calibration_entries(None, proposal, ['candidate-a'])
    before = json.dumps(first['configurations']['candidate-a'], sort_keys=True)
    appended = exp.merge_calibration_entries(first, proposal, ['candidate-b'])
    assert json.dumps(appended['configurations']['candidate-a'], sort_keys=True) == before
    changed = deepcopy(proposal)
    changed['configurations']['candidate-a']['max_tokens'] += 1024
    with pytest.raises(exp.ExperimentBlocked, match='Cannot rewrite'):
        exp.merge_calibration_entries(appended, changed, ['candidate-a'])


def test_calibration_doubles_total_tokens_even_below_truncation_trigger(project):
    _, _, answers = project
    client = FakeClient(answers)
    asyncio.run(exp.run_extractions('P', ['candidate-b'], client=client))
    record = json.loads(next(Path('data/derived/extractions/P/round-01/candidate-b').glob('*.json')).read_text())
    attempt_path = Path(record['attempt_files'][0])
    attempt = json.loads(attempt_path.read_text())
    cap = record['max_tokens']
    used = int(cap * .75)
    attempt['response']['usage']['completion_tokens'] = used
    attempt['response']['usage']['completion_tokens_details'] = {'reasoning_tokens': used - 10}
    write_json(attempt_path, attempt)
    proposal = exp.calibration_proposal(config_ids=['candidate-b'])
    row = proposal['configurations']['candidate-b']
    assert row['max_observed_completion'] == used
    assert row['required_max_tokens'] >= 2 * used
    assert row['requires_P_rerun'] is True
    with pytest.raises(exp.ExperimentBlocked, match='larger-cap P run'):
        exp.merge_calibration_entries(None, proposal, ['candidate-b'])


def test_partial_larger_cap_round_cannot_be_frozen(project):
    _, _, answers = project
    client = FakeClient(answers)
    asyncio.run(exp.run_extractions('P', ['candidate-a'], client=client))
    records = sorted(Path('data/derived/extractions/P/round-01/candidate-a').glob('*.json'))
    records[-1].unlink()
    proposal = exp.calibration_proposal(config_ids=['candidate-a'])
    entry = proposal['configurations']['candidate-a']
    assert entry['P_complete'] is False
    assert entry['status'] == 'incomplete_P' and entry['max_tokens'] is None
    assert len(entry['missing_pmids']) == 1
    with pytest.raises(exp.ExperimentBlocked, match='incomplete'):
        exp.merge_calibration_entries(None, proposal, ['candidate-a'])


def test_fixed_plan_blocks_all_comparison_stages_before_source_or_selection_access(project):
    _, config, answers = project
    config['comparison_authorized'] = False
    write_json(exp.CONFIG, config)
    client = FakeClient(answers)
    for stage in ['S', 'H', 'VERIFY', 'PROVIDERCHECK']:
        with pytest.raises(exp.ExperimentBlocked, match='retired'):
            asyncio.run(exp.run_extractions(stage, client=client))
    assert not client.calls
