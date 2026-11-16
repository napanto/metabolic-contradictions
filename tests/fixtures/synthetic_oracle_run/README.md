Fake-transport outputs of the two-judge runner, copied unmodified (result.json only).

- Source: branch oracle-api-20261116 at commit a493f9d, `chl_rag.reference_batches.TwoJudgeRun`
  driven by the test doubles `FakeProviders`/`Clock` of `tests/test_reference_batches.py`
  (scenarios of `test_per_item_batch_fanout_concurrent_poll_and_settlement` for Astra and
  `test_invalid_item_gets_one_immediate_identical_retry_never_a_third` for Fable), run from a
  read-only copy of that commit. No model or network call; synthetic package items v2-pair-001..004.
- Astra: 4 valid. Fable: v2-pair-001 valid after one retry, v2-pair-002 invalid after two attempts,
  v2-pair-003 and v2-pair-004 valid.
