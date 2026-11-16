"""OpenRouter client with persistent cost reservations and sanitized caching.

The key is loaded only from the external credential file. Never log request
headers, httpx exception objects, or credential-file contents.
"""

from __future__ import annotations

import asyncio
import fcntl
import gzip
import json
import math
import os
import uuid
from contextlib import asynccontextmanager, contextmanager, suppress
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import httpx
import jsonschema
import orjson

from .io import canonical, digest, file_sha, utcnow, write_json


class BudgetError(RuntimeError):
    pass


class AccountingError(RuntimeError):
    pass


class OutputError(RuntimeError):
    def __init__(self, message, attempts=None):
        super().__init__(message)
        self.attempts = attempts or []


class RequestConfigurationError(RuntimeError):
    """Authentication, routing or request-configuration failure: stop the stage."""

    def __init__(self, message, attempts=None, status_code=None):
        super().__init__(message)
        self.attempts = attempts or []
        self.status_code = status_code


class RateLimitError(RequestConfigurationError):
    """Bounded HTTP429 retries exhausted; stop new work and drain the stage."""


# Operational retry policy. Only identified, generation-free HTTP429 responses
# use this allowance; a billable failure is never retried here. Canonical request
# content, provider pinning, decoding and the single schema repair are unchanged.
KNOWN_FREE_429_ATTEMPTS = 12
RATE_LIMIT_MAX_BASE_DELAY = 30.0
RATE_LIMIT_MAX_RETRY_AFTER = 120.0
MAX_SCHEMA_REPAIRS = 1
SYSTEMIC_HTTP_CODES = {400, 401, 402, 403, 404, 422}
PRICE_FIELDS = ("input_price", "output_price", "request_price")
CEILING_FIELDS = ("input_ceiling", "output_ceiling", "request_ceiling")
PRICE_IDENTITY_FIELDS = ("provider_tag", "canonical_slug", "quantization")
HTTP_DIAGNOSTIC_HEADERS = {
    "retry-after", "x-ratelimit-limit", "x-ratelimit-remaining", "x-ratelimit-reset",
    "x-ratelimit-limit-requests", "x-ratelimit-remaining-requests", "x-ratelimit-reset-requests",
    "x-ratelimit-limit-tokens", "x-ratelimit-remaining-tokens", "x-ratelimit-reset-tokens",
    "x-ratelimit-limit-requests-minute", "x-ratelimit-remaining-requests-minute", "x-ratelimit-reset-requests-minute",
    "x-ratelimit-limit-requests-day", "x-ratelimit-remaining-requests-day", "x-ratelimit-reset-requests-day",
}


def rate_retry_delay(retry_after, attempt):
    """Honor finite nonnegative Retry-After seconds within the operational 120s cap."""
    backoff = min(RATE_LIMIT_MAX_BASE_DELAY, 2 ** (attempt + 1))
    try:
        value = float(retry_after)
    except (TypeError, ValueError):
        return float(backoff)
    if not math.isfinite(value) or value < 0:
        return float(backoff)
    return max(float(backoff), min(value, RATE_LIMIT_MAX_RETRY_AFTER))


def runtime_price_overrides(models, path="configs/runtime_prices.json"):
    """Accept prices only when a hashed endpoint snapshot verifies the same route."""
    path = Path(path)
    if not path.exists():
        return {}
    try:
        document = json.loads(path.read_text())
        if set(document) != {"version", "models"} or document["version"] != 1:
            raise ValueError("Unsupported runtime-price configuration")
        overrides = document["models"]
        if not isinstance(overrides, dict):
            raise TypeError("Runtime models must be an object")
        allowed = {*PRICE_FIELDS, *CEILING_FIELDS, *PRICE_IDENTITY_FIELDS, "source_file", "source_sha256", "verified_at"}
        for model, override in overrides.items():
            if model not in models or set(override) != allowed:
                raise ValueError("Runtime prices may only change verified unit prices")
            base = models[model]
            if any(override[field] != base.get(field) for field in PRICE_IDENTITY_FIELDS):
                raise ValueError("Runtime price override changes the frozen provider/model identity")
            if datetime.fromisoformat(override["verified_at"]).tzinfo is None:
                raise ValueError("Runtime price verification timestamp needs a timezone")
            if file_sha(override["source_file"]) != override["source_sha256"]:
                raise ValueError("Runtime price endpoint snapshot checksum mismatch")
            snapshot = json.loads(Path(override["source_file"]).read_text())["data"]
            endpoints = [e for e in snapshot["endpoints"] if e.get("tag") == override["provider_tag"]]
            if snapshot["id"] != model or len(endpoints) != 1:
                raise ValueError("Snapshot does not uniquely identify the frozen endpoint")
            endpoint = endpoints[0]
            canonical_slug = endpoint.get("canonical_slug") or endpoint.get("name", "").rsplit(" | ", 1)[-1]
            if (endpoint.get("model_id") != model or canonical_slug != override["canonical_slug"]
                    or endpoint.get("quantization") != override["quantization"]):
                raise ValueError("Snapshot provider/model/quantization differs from frozen identity")
            for field, source_key in zip(PRICE_FIELDS, ("prompt", "completion", "request"), strict=True):
                value = override[field]
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                    raise ValueError("Runtime prices must be finite nonnegative numbers")
                if value != float(endpoint["pricing"].get(source_key, 0)):
                    raise ValueError("Runtime unit price differs from verified endpoint pricing")
            for field, ceiling_field in zip(PRICE_FIELDS, CEILING_FIELDS, strict=True):
                ceiling = override[ceiling_field]
                if (isinstance(ceiling, bool) or not isinstance(ceiling, (int, float))
                        or not math.isfinite(ceiling) or ceiling < override[field]):
                    raise ValueError("Runtime ceiling must be finite and at least its verified quote")
        return overrides
    except (OSError, KeyError, TypeError, ValueError) as exc:
        raise RequestConfigurationError(f"Invalid runtime-price override: {exc}") from None


def record_runtime_prices(models, overrides, config_path, history_path="results/runtime_price_revision.json"):
    if not overrides:
        return None
    revision_hash = digest(overrides)
    path = Path(history_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        history = json.loads(path.read_text()) if path.exists() else {"version": 1, "revisions": []}
        if not any(row["revision_sha256"] == revision_hash for row in history["revisions"]):
            history["revisions"].append({
                "revision_sha256": revision_hash, "recorded_at": utcnow(),
                "runtime_config_sha256": file_sha(config_path),
                "scope": "Operational price ceilings and cost reservations only; frozen model identity, prompts, decoding and test results unchanged",
                "models": {model: {**override,
                                   "frozen_base_prices": {key: models[model].get(key, 0) for key in PRICE_FIELDS}}
                           for model, override in overrides.items()},
            })
            write_json(path, history)
    return revision_hash


def systemic_http_status(status, payload):
    if status in SYSTEMIC_HTTP_CODES or status == 429:
        return status
    error = payload.get("error")
    if isinstance(error, dict):
        try:
            code = int(error.get("code"))
        except (TypeError, ValueError):
            return None
        if code in SYSTEMIC_HTTP_CODES or code == 429:
            return code
    return None


def process_identity(pid):
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return None if fields[0] == "Z" else f"{pid}:{fields[19]}"
    except (OSError, IndexError):
        return None


class Ledger:
    def __init__(self, path="results/spend.json", cap=40.0):
        self.path = Path(path)
        self.cap = cap
        self.path.parent.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def locked(self):
        with self.path.with_suffix(".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            value = (
                orjson.loads(self.path.read_bytes())
                if self.path.exists()
                else {
                    "currency": "USD",
                    "cap": self.cap,
                    "total_cost": 0.0,
                    "calls": [],
                    "pending": {},
                    "unknown_cost_calls": [],
                }
            )
            yield value
            value["updated_at"] = utcnow()
            # Fast JSON is limited to the ledger: request canonicalization and
            # cache identities stay unchanged. Atomic replacement and process
            # locks retain the same reservation/settlement transaction boundary.
            temporary = self.path.with_name(self.path.name + f".{os.getpid()}.tmp")
            temporary.write_bytes(orjson.dumps(value, option=orjson.OPT_APPEND_NEWLINE))
            temporary.replace(self.path)

    def reserve(self, amount, request_hash, model):
        if not math.isfinite(amount) or amount <= 0:
            raise BudgetError("Reservation must be positive and finite")
        blocked = False
        with self.locked() as value:
            self._mark_abandoned(value)
            if value["unknown_cost_calls"]:
                blocked = True
            else:
                projected = (
                    value["total_cost"]
                    + sum(p["reserved"] for p in value["pending"].values())
                    + sum(p["reserved"] for p in value.get("reconciled_reserves", {}).values())
                    + amount
                )
                if projected > min(self.cap, value["cap"]):
                    raise BudgetError(f"Projected cost USD {projected:.4f} exceeds cap")
                token = uuid.uuid4().hex
                value["pending"][token] = {
                    "reserved": amount,
                    "request_hash": request_hash,
                    "model": model,
                    "started_at": utcnow(),
                    "owner_pid": os.getpid(),
                    "owner_identity": process_identity(os.getpid()),
                }
                value["status"] = "active"
        if blocked:
            raise AccountingError("Unreconciled call cost: see results/spend.json")
        return token

    def check_abandoned(self):
        if not self.path.exists():
            return
        with self.locked() as value:
            self._mark_abandoned(value)

    @staticmethod
    def _mark_abandoned(value):
        unknown = {row.get("reservation_id") for row in value["unknown_cost_calls"]}
        for token, pending in value["pending"].items():
            if token in unknown or "owner_pid" not in pending:
                continue
            identity = process_identity(pending["owner_pid"])
            if identity is None or pending.get("owner_identity", identity) != identity:
                value["unknown_cost_calls"].append(
                    {
                        **pending,
                        "reservation_id": token,
                        "error": "owner_process_exited_with_unsettled_request",
                    }
                )
                value["status"] = "blocked_unknown_cost"

    def finish(self, token, response=None, *, known_free=False, error=None):
        response = response if isinstance(response, dict) else {}
        usage = response.get("usage")
        usage = usage if isinstance(usage, dict) else {}
        with self.locked() as value:
            pending = value["pending"].pop(token)
            cost = 0.0 if known_free else usage.get("cost")
            record = {
                **pending,
                "reservation_id": token,
                "finished_at": utcnow(),
                "generation_id": (response or {}).get("id"),
                "provider": (response or {}).get("provider"),
                "usage": (response or {}).get("usage"),
                "error": error,
            }
            if (
                isinstance(cost, bool)
                or not isinstance(cost, (int, float))
                or not math.isfinite(cost)
                or cost < 0
            ):
                value["pending"][token] = pending
                value["unknown_cost_calls"].append({**record, "reservation_id": token})
                value["status"] = "blocked_unknown_cost"
                blocked = True
            else:
                record["cost"] = cost
                value["calls"].append(record)
                value["total_cost"] = math.fsum(c["cost"] for c in value["calls"])
                blocked = cost > pending["reserved"] + 1e-9
                if blocked:
                    value["unknown_cost_calls"].append(
                        {"error": "reservation_exceeded", "generation_id": record["generation_id"]}
                    )
                    value["status"] = "blocked_price_mismatch"
                else:
                    value["status"] = (
                        "blocked_unknown_cost"
                        if value["unknown_cost_calls"]
                        else "active"
                        if value["pending"]
                        else "idle"
                    )
        if blocked:
            raise AccountingError(
                "Unknown cost or price exceeded reservation; further calls suspended"
            )

    def accounted(self, request_hash, response):
        with self.locked() as value:
            return any(
                row["request_hash"] == request_hash
                and row.get("generation_id") == response.get("id")
                for row in value["calls"]
            )

    def reconcile_zero_charge(self, first, second):
        """Use stable provider key accounting to resolve a no-response incident.

        No synthetic usage is invented. The original worst-case liability stays
        reserved for the rest of the run against possible delayed billing.
        """
        for snapshot in [first, second]:
            usage = snapshot.get("usage")
            if (
                isinstance(usage, bool)
                or not isinstance(usage, (int, float))
                or not math.isfinite(usage)
                or usage < 0
            ):
                raise AccountingError("Provider usage must be a finite nonnegative number")
            if datetime.fromisoformat(snapshot["checked_at"]).tzinfo is None:
                raise AccountingError("Provider usage snapshots need timezone-aware timestamps")
        elapsed = (
            datetime.fromisoformat(second["checked_at"])
            - datetime.fromisoformat(first["checked_at"])
        ).total_seconds()
        if elapsed < 60:
            raise AccountingError(
                "Usage reconciliation needs two snapshots at least60seconds apart"
            )
        with self.locked() as value:
            unknown_ids = {row.get("reservation_id") for row in value["unknown_cost_calls"]}
            if not unknown_ids or None in unknown_ids or unknown_ids != set(value["pending"]):
                raise AccountingError(
                    "Reconciliation requires only identified unknown requests in flight"
                )
            latest_activity = max(
                datetime.fromisoformat(row.get("finished_at", row["started_at"]))
                for row in value["unknown_cost_calls"] + value["calls"]
            )
            if datetime.fromisoformat(first["checked_at"]) < latest_activity:
                raise AccountingError(
                    "Usage snapshots predate the unresolved incident or settled calls"
                )
            if any(
                abs(snapshot["usage"] - value["total_cost"]) > 1e-9 for snapshot in [first, second]
            ):
                raise AccountingError(
                    "Provider usage differs from settled ledger; cannot assert zero charge"
                )
            value.setdefault("resolved_unknown_cost_calls", [])
            value.setdefault("reconciled_reserves", {})
            for unknown in value["unknown_cost_calls"]:
                token = unknown["reservation_id"]
                pending = value["pending"].pop(token)
                record = {
                    **unknown,
                    "cost": 0.0,
                    "usage": None,
                    "cost_source": "stable provider key-usage reconciliation; no additional charge observed",
                    "reconciled_at": utcnow(),
                    "usage_snapshots": [first, second],
                }
                value["calls"].append(record)
                value["resolved_unknown_cost_calls"].append(record)
                value["reconciled_reserves"][token] = {
                    **pending,
                    "reason": "Retained worst-case liability for delayed billing",
                }
            value["unknown_cost_calls"] = []
            value["status"] = "idle_with_reconciled_reserve"


def load_key(path=None):
    path = Path(path) if path else Path.home() / ".config/chl-openrouter.env"
    for line in path.read_text().splitlines():
        if line.startswith("OPENROUTER_API_KEY="):
            key = line.partition("=")[2].strip().strip("\"'")
            if key:
                return key
    raise RuntimeError("Credential file lacks a nonempty OPENROUTER_API_KEY")


def clean(value, secret):
    if isinstance(value, dict):
        return {
            k: clean(v, secret)
            for k, v in value.items()
            if k.lower() not in {"authorization", "api_key", "headers", "openrouter_api_key"}
        }
    if isinstance(value, list):
        return [clean(v, secret) for v in value]
    if isinstance(value, str):
        return value.replace(secret, "[REDACTED]") if secret else value
    return value


class Client:
    def __init__(
        self,
        concurrency=8,
        ledger=None,
        cache="data/raw/llm_cache",
        credential_path=None,
        transport=None,
        runtime_prices_path="configs/runtime_prices.json",
        http_error_dir="data/raw/http_errors",
    ):
        self.models = json.loads(Path("configs/models.json").read_text())["models"]
        self.runtime_prices = runtime_price_overrides(self.models, runtime_prices_path)
        self.price_revision_sha256 = record_runtime_prices(self.models, self.runtime_prices, runtime_prices_path)
        self.http_error_dir = Path(http_error_dir)
        self.secret = load_key(credential_path)
        self.ledger = ledger or Ledger()
        self.cache = Path(cache)
        self.cache.mkdir(parents=True, exist_ok=True)
        self.semaphore = asyncio.Semaphore(concurrency)
        self.http = httpx.AsyncClient(
            timeout=httpx.Timeout(600, connect=30),
            limits=httpx.Limits(
                max_connections=concurrency, max_keepalive_connections=concurrency,
                keepalive_expiry=5.0,
            ),
            transport=transport,
        )
        self.api_attempts = 0
        self.cache_hits = 0

    @asynccontextmanager
    async def cache_lock(self, request_hash):
        # Nonblocking flock protects identical requests across Client objects and
        # processes without blocking the event loop that will release the lock.
        fd = os.open(self.cache / f"{request_hash}.lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    await asyncio.sleep(0.05)
            yield
        finally:
            os.close(fd)

    async def close(self):
        await self.http.aclose()

    def save_http_rejection(self, request, request_hash, response, payload, attempt, retry_delay):
        self.http_error_dir.mkdir(parents=True, exist_ok=True)
        path = self.http_error_dir / f"{request_hash}-attempt{attempt + 1:02d}-{uuid.uuid4().hex}.json"
        diagnostic = {
            "timestamp": utcnow(), "request_hash": request_hash, "attempt_ordinal": attempt + 1,
            "model": request["model"], "provider_order": request.get("provider", {}).get("order", []),
            "status": response.status_code, "known_free": True, "payload": payload,
            "response_headers": {key.lower(): value for key, value in response.headers.items()
                                 if key.lower() in HTTP_DIAGNOSTIC_HEADERS},
            "retry_delay_seconds": retry_delay,
        }
        write_json(path, clean(diagnostic, self.secret))
        return str(path)

    def effective_prices(self, model):
        """Latest verified quotes, kept distinct from the permitted price bounds."""
        config = self.models[model]
        override = self.runtime_prices.get(model)
        if override and any(override[field] != config.get(field) for field in PRICE_IDENTITY_FIELDS):
            raise RequestConfigurationError("Runtime prices no longer match frozen provider identity")
        return {field: (override if override else config).get(field, 0) for field in PRICE_FIELDS}

    def price_ceilings(self, model):
        quotes = self.effective_prices(model)  # Also rechecks the pinned identity.
        override = self.runtime_prices.get(model)
        return {field: override[ceiling] if override else quotes[field]
                for field, ceiling in zip(PRICE_FIELDS, CEILING_FIELDS, strict=True)}

    def request(self, model, messages, schema, reasoning="off", max_tokens=2048):
        config = self.models[model]
        prices = self.price_ceilings(model)
        if reasoning == "off" and config.get("reasoning_mandatory"):
            raise ValueError(f"{model}: reasoning off is unavailable")
        provider = {
            "order": [config["provider_tag"]],
            "allow_fallbacks": False,
            "require_parameters": True,
            "max_price": {
                "prompt": float(Decimal(str(prices["input_price"])) * Decimal(1000000)),
                "completion": float(Decimal(str(prices["output_price"])) * Decimal(1000000)),
            },
        }
        if model in self.runtime_prices:
            provider["max_price"]["request"] = prices["request_price"]
        request = {
            "model": model,
            "provider": provider,
            "messages": messages,
            "temperature": 0,
            "seed": 42,
            "max_tokens": max_tokens,
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "result", "strict": True, "schema": schema},
            },
            "reasoning": {"enabled": False} if reasoning == "off" else {"effort": reasoning},
        }
        return request

    def reserve_cost(self, request):
        prices = self.price_ceilings(request["model"])
        # UTF-8 byte length is a conservative token bound; allowance covers
        # provider chat/schema wrappers. No cache discounts assumed.
        input_bound = len(canonical(request)) + 2048
        return (
            input_bound * prices["input_price"]
            + request["max_tokens"] * prices["output_price"]
            + prices["request_price"]
        ) * 1.05

    async def raw(self, request):
        request_hash = digest(request)
        path = self.cache / f"{request_hash}.json.gz"
        async with self.semaphore, self.cache_lock(request_hash):
            if path.exists():
                try:
                    with gzip.open(path, "rt") as handle:
                        result = json.load(handle)
                except (OSError, EOFError, ValueError) as exc:
                    raise RequestConfigurationError(
                        f"Unreadable cached response ({type(exc).__name__}); no request issued"
                    ) from None
                if (not isinstance(result, dict) or not isinstance(result.get("request"), dict)
                        or digest(result["request"]) != request_hash):
                    raise RequestConfigurationError("Cached request identity differs from the requested canonical hash")
                if not isinstance(result.get("response"), dict):
                    raise RequestConfigurationError("Cached response is not a JSON object")
                if not self.ledger.accounted(request_hash, result["response"]):
                    raise AccountingError(
                        "Cached generation has no settled ledger entry; reconcile billing"
                    )
                if result.get("http_status", 200) != 200 or result["response"].get("error"):
                    status = systemic_http_status(result.get("http_status", 200), result["response"])
                    error_class = RateLimitError if status == 429 else RequestConfigurationError if status else OutputError
                    raise error_class(
                        "Cached generation is an HTTP/provider failure",
                        attempts=[{"request_hash": request_hash, "cache_hit": True}],
                        **({"status_code": status} if status else {}),
                    )
                self.cache_hits += 1
                return result["response"], {"request_hash": request_hash, "cache_hit": True}
            # The semaphore and file lock remain held across bounded retries.
            for attempt in range(KNOWN_FREE_429_ATTEMPTS):
                reservation = self.ledger.reserve(
                    self.reserve_cost(request), request_hash, request["model"]
                )
                try:
                    self.api_attempts += 1
                    response = await self.http.post(
                        "https://openrouter.ai/api/v1/chat/completions",
                        json=request,
                        headers={
                            "Authorization": "Bearer " + self.secret,
                            "Content-Type": "application/json",
                            "X-Title": "chl-inconsistency-rag",
                        },
                    )
                except asyncio.CancelledError:
                    with suppress(AccountingError):
                        self.ledger.finish(reservation, error="cancelled_request_unknown_billing")
                    raise
                except httpx.HTTPError as exc:
                    self.ledger.finish(
                        reservation,
                        error=f"transport_failure_{type(exc).__name__}_unknown_billing: {clean(str(exc), self.secret)[:200]}",
                    )
                    raise AccountingError(
                        "Transport failure: reconcile billing before retry"
                    ) from None
                try:
                    payload = clean(response.json(), self.secret)
                except ValueError:
                    self.ledger.finish(reservation, error="non_json_response_unknown_billing")
                    raise AccountingError("Non-JSON response") from None
                if not isinstance(payload, dict):
                    self.ledger.finish(reservation, error="non_object_response_unknown_billing")
                    raise AccountingError("Expected a JSON object response")
                if (
                    response.status_code in {400, 401, 402, 403, 404, 422, 429}
                    and not payload.get("id")
                    and not payload.get("usage")
                ):
                    self.ledger.finish(
                        reservation, known_free=True, error=f"http_{response.status_code}"
                    )
                    retry_delay = (rate_retry_delay(response.headers.get("retry-after"), attempt)
                                   if response.status_code == 429 and attempt + 1 < KNOWN_FREE_429_ATTEMPTS else None)
                    diagnostic_path = self.save_http_rejection(
                        request, request_hash, response, payload, attempt, retry_delay
                    )
                    if response.status_code == 429 and attempt + 1 < KNOWN_FREE_429_ATTEMPTS:
                        await asyncio.sleep(retry_delay)
                        continue
                    error_class = RateLimitError if response.status_code == 429 else RequestConfigurationError
                    error = payload.get("error") or {}
                    message = error.get("message", "request rejected") if isinstance(error, dict) else str(error)
                    raise error_class(
                        f"OpenRouter HTTP {response.status_code}: {str(message)[:300]}",
                        attempts=[{"request_hash": request_hash, "cache_hit": False,
                                   "http_error_file": diagnostic_path}],
                        status_code=response.status_code,
                    )
                # Keep sanitized response even when accounting/validation fails.
                stored = {
                    "request": request,
                    "response": payload,
                    "saved_at": utcnow(),
                    "http_status": response.status_code,
                }
                temporary = path.with_name(path.name + f".{uuid.uuid4().hex}.tmp")
                with gzip.open(temporary, "wt") as handle:
                    json.dump(stored, handle, ensure_ascii=False)
                temporary.replace(path)
                self.ledger.finish(reservation, payload)
                if response.status_code != 200 or payload.get("error"):
                    status = systemic_http_status(response.status_code, payload)
                    error_class = RateLimitError if status == 429 else RequestConfigurationError if status else OutputError
                    raise error_class(
                        f"Generation failed: HTTP {response.status_code}",
                        attempts=[{"request_hash": request_hash, "cache_hit": False}],
                        **({"status_code": status} if status else {}),
                    )
                return payload, {"request_hash": request_hash, "cache_hit": False}

    async def complete(self, model, messages, schema, reasoning="off", max_tokens=2048):
        history = list(messages)
        attempts = []
        for attempt in range(MAX_SCHEMA_REPAIRS + 1):
            request = self.request(model, history, schema, reasoning, max_tokens)
            try:
                response, info = await self.raw(request)
            except (RequestConfigurationError, OutputError) as exc:
                exc.attempts = attempts + exc.attempts
                raise
            attempts.append(info)
            try:
                choice = response["choices"][0]
                if choice.get("finish_reason") == "length":
                    raise ValueError("length limit")
                parsed = json.loads(choice["message"]["content"])
                jsonschema.validate(parsed, schema)
                return parsed, {
                    "attempts": attempts,
                    "first_attempt_valid": attempt == 0,
                    "model": response.get("model"),
                    "provider": response.get("provider"),
                    "generation_id": response.get("id"),
                }
            except (KeyError, IndexError, TypeError, ValueError, jsonschema.ValidationError):
                if attempt:
                    raise OutputError(
                        "Structured output invalid after one repair; cached for audit",
                        attempts=attempts,
                    ) from None
                history.append(
                    {
                        "role": "user",
                        "content": "The previous response did not satisfy the requested JSON schema or was incomplete. Return only a complete JSON object following the schema. Keep all strings concise.",
                    }
                )
        raise AssertionError("Unreachable")
