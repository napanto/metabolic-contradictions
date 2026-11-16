# Amendment 08: two-judge run, exact order of commands

Run everything from the project checkout (`chl/project`), on `main`, with the YubiKey plugged in. Every `git commit -S` is a signed registration act. Nothing reaches a provider before step 5, and nothing reaches Anthropic before the lift of step 3.

## 1. Merge and register (no network)

1. Fill the two placeholders of `PROTOCOL_AMENDMENT_08_TWO_JUDGES.md`:
   - `{{REGISTERED_AT}}`: the registration time;
   - `{{BINDING_SHA}}`: the output of `sha256sum configs/oracle_binding.json`.
2. Merge the branch:
   ```bash
   git merge -S oracle-api-20261116
   ```
3. Write the configuration set. The script refuses while placeholders remain:
   ```bash
   uv run python <registration script, not distributed; its outputs are in configs/>
   ```
4. Commit the registration:
   ```bash
   git add PROTOCOL_AMENDMENT_08_TWO_JUDGES.md configs/evaluation_two_judges.json configs/two_judges_funding.json
   git commit -S -m "Register amendment 08: two-judge final oracle, 35 live pairs (Antonio)"
   ```

## 2. Offline check of the plan (no network, no key read)

```bash
uv run python scripts/run_reference_batches.py plan --judges astra
```

This builds and validates all 35 requests, the pilot (`v2-pair-1043`, `v2-pair-1087`), the cutoff and the binding. Fable is refused until step 3.

## 3. Antonio's two acts

Both files are signed commits. Each must quote `sha256sum configs/evaluation_two_judges.json` exactly; any mismatch keeps the run on HOLD and Fable disabled.

1. **Lift of the Anthropic stop**, for this run only:
   - copy the unsigned `anthropic_lift` template (not distributed) to `configs/two_judges_anthropic_lift.json`;
   - fill every `FILL` field and delete `_draft_note`;
   - `git add` the file, then `git commit -S`.
2. **Release for the pilot:**
   - copy the unsigned `release` template (not distributed) to `configs/two_judges_release.json`;
   - fill every field, with `"scope": "pilot_only"` and `protocol_sha256` from `sha256sum PROTOCOL_AMENDMENT_08_TWO_JUDGES.md`;
   - `git add` the file, then `git commit -S`.
3. Check both judges offline:
   ```bash
   uv run python scripts/run_reference_batches.py plan
   ```

## 4. Pilot (paid, synchronous full price)

```bash
uv run python scripts/run_reference_batches.py pilot --allow-inference
```

Both judges run concurrently on the 2 pilot pairs. A valid pilot answer is final for its pair.

## 5. Main run

1. Edit `configs/two_judges_release.json`: set `"scope": "full_run"`, a new `released_at` and Antonio's words. Commit with `git commit -S`.
2. Start the run:
   ```bash
   mkdir -p logs
   nohup uv run python scripts/run_reference_batches.py run --allow-inference > logs/two_judges_run.log 2>&1 &
   ```

The run works as follows:
- **Submission:** one single-request batch per pair, all sent at once, and all are polled in parallel.
- **Retries:** an invalid or truncated output gets one identical retry at once.
- **Rejections before generation** (HTTP 429, 529, 5xx, connection errors) are retried with backoff and never count as attempts.
- **At 11:00 CET:** open batches are cancelled, completed answers are kept, and pairs without a result are sent at full price in waves within the allocation. This happens in the same process.

## 6. Status (local files only)

```bash
uv run python scripts/run_reference_batches.py status
```

## 7. Fallback after 11:00

If the step 5 process stopped before 11:00, run the same command again after 11:00:

```bash
uv run python scripts/run_reference_batches.py run --allow-inference
```

It resumes from disk:
1. it collects or cancels any open batch;
2. it never resubmits a pair that has a result or an open request;
3. it sends the rest synchronously at full price, within the allocation.

Outputs are written under `annotation/oracles_two_judges/<judge>/max/`. Ledgers are in `results/openai_spend.json` and `results/anthropic_spend.json`.
