#!/usr/bin/env python3
"""
generate_qa.py — Generate Q&A training pairs for the NH White Mountains
hiking agent.

Three backend modes (select with --backend):

  claude-batch  Submit all requests to the Anthropic Batch API, poll until
  (DEFAULT)     complete, then retrieve and process results. ~50% cheaper
                than synchronous. Takes minutes to hours depending on queue.

  claude-sync   Send requests one at a time to the Anthropic Messages API.
                Immediate results, standard pricing.

  vllm          Send requests one at a time to a locally hosted vLLM server
                via its OpenAI-compatible endpoint.

Usage:
  # Default: Claude Batch API (cheapest, recommended)
  python generate_qa.py --batch batches/batch_01.json

  # Synchronous Claude (immediate results)
  python generate_qa.py --batch batches/batch_01.json --backend claude-sync

  # Local vLLM
  python generate_qa.py --batch batches/batch_01.json --backend vllm

  # Retrieve a previously submitted Anthropic batch by ID
  python generate_qa.py --retrieve-batch msgbatch_abc123

  # Dry run — build prompts without calling any API
  python generate_qa.py --batch batches/batch_01.json --dry-run

  # List available batch files
  python generate_qa.py --list-batches

Environment variables:
  ANTHROPIC_API_KEY   Required for claude-batch and claude-sync backends
  VLLM_URL            Override --vllm-url
  VLLM_MODEL          Override --vllm-model
"""

import argparse
import json
import logging
import os
import re
import sys
import time
import random
from datetime import datetime
from pathlib import Path
from typing import Any

import jsonschema
import requests

# ---------------------------------------------------------------------------
# Cached resources for performance
# ---------------------------------------------------------------------------

_VLLM_SESSION: requests.Session | None = None

def _get_vllm_session() -> requests.Session:
    global _VLLM_SESSION
    if _VLLM_SESSION is None:
        _VLLM_SESSION = requests.Session()
    return _VLLM_SESSION


_VALIDATOR: jsonschema.Draft7Validator | None = None

def _get_validator(schema: dict) -> jsonschema.Draft7Validator:
    global _VALIDATOR
    if _VALIDATOR is None:
        _VALIDATOR = jsonschema.Draft7Validator(schema)
    return _VALIDATOR


_TRAIL_CACHE: dict[str, str] = {}

def _trail_json(trail_id: str, sheet: dict) -> str:
    if trail_id not in _TRAIL_CACHE:
        _TRAIL_CACHE[trail_id] = json.dumps(sheet, indent=2)
    return _TRAIL_CACHE[trail_id]

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("generate_qa")

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

SCRIPT_DIR     = Path(__file__).parent
TRAILS_DIR     = SCRIPT_DIR.parent / "ground_truth" / "trail_data"
QA_PROMPT_FILE = SCRIPT_DIR / "QA_GENERATION_PROMPT.md"
QA_SCHEMA_FILE = SCRIPT_DIR / "qa_schema.json"
OUTPUT_DIR     = SCRIPT_DIR / "output" / "qa_pairs"
FAILED_DIR     = SCRIPT_DIR / "output" / "failed"
PENDING_DIR    = SCRIPT_DIR / "output" / "pending_batches"
BATCHES_DIR    = SCRIPT_DIR / "batches"

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

DEFAULT_BACKEND        = "claude-batch"
DEFAULT_CLAUDE_MODEL   = "claude-haiku-4-5-20251001"
DEFAULT_VLLM_URL       = "http://localhost:8000/v1/chat/completions"
DEFAULT_VLLM_MODEL     = "default"
DEFAULT_MAX_TOKENS     = 8192
DEFAULT_TEMPERATURE    = 0.7
DEFAULT_RETRY_ATTEMPTS = 3
DEFAULT_RETRY_DELAY    = 5      # seconds between retries on transient errors
DEFAULT_POLL_INTERVAL  = 30     # seconds between batch status polls
DEFAULT_POLL_TIMEOUT   = 7200   # 2 hours before giving up on a batch

ANTHROPIC_MESSAGES_URL     = "https://api.anthropic.com/v1/messages"
ANTHROPIC_BATCH_CREATE_URL = "https://api.anthropic.com/v1/messages/batches"
ANTHROPIC_API_VERSION      = "2023-06-01"
ANTHROPIC_BETA_HEADER      = "message-batches-2024-09-24"

# ---------------------------------------------------------------------------
# Anthropic pricing  (per million tokens, USD)
# ---------------------------------------------------------------------------
# Keys are matched as prefixes against the model name.
CLAUDE_PRICING: dict[str, tuple[float, float]] = {
    "claude-haiku-4-5":          (0.80,  4.00),
    "claude-haiku-3-5":          (0.80,  4.00),
    "claude-sonnet-4":           (3.00, 15.00),
    "claude-sonnet-3-5":         (3.00, 15.00),
    "claude-opus-4":             (15.00, 75.00),
    "claude-opus-3-5":           (15.00, 75.00),
}


def claude_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    """Return estimated USD cost for a single request."""
    inp_price, out_price = 15.00, 75.00  # fallback: opus pricing
    for prefix, (i, o) in CLAUDE_PRICING.items():
        if model.startswith(prefix):
            inp_price, out_price = i, o
            break
    return (input_tokens * inp_price + output_tokens * out_price) / 1_000_000

# ---------------------------------------------------------------------------
# Batch file config schema (comment block)
# ---------------------------------------------------------------------------
# Each batch file is a JSON object:
# {
#   "description": "Human-readable description of this batch",
#   "requests": [
#     {
#       "query_type": "trail-lookup",
#       "experience_level": "beginner",
#
#       // Trail selection — use ONE of:
#       "trails": ["trail-id", ...],          // explicit list
#       "trail_sample": 3,                    // random N from directory
#       "trail_sample": 3,                    // filtered random N:
#       "trail_filters": {
#         "tags":             ["4k-footer"],
#         "region":           "Presidential Range",
#         "min_fitness":      4,
#         "max_fitness":      5,
#         "min_technical":    1,
#         "max_technical":    3,
#         "weather_exposure": ["high", "severe"],
#         "fall_risk":        ["none", "low"]
#       },
#
#       "count": 4,
#       "notes": "optional generation context"
#     }
#   ]
# }


# ===========================================================================
# Shared assets
# ===========================================================================

def load_qa_prompt() -> str:
    if not QA_PROMPT_FILE.exists():
        log.error("QA generation prompt not found: %s", QA_PROMPT_FILE)
        sys.exit(1)
    return QA_PROMPT_FILE.read_text(encoding="utf-8")


def load_qa_schema() -> dict:
    if not QA_SCHEMA_FILE.exists():
        log.error("QA schema not found: %s", QA_SCHEMA_FILE)
        sys.exit(1)
    return json.loads(QA_SCHEMA_FILE.read_text(encoding="utf-8"))


def build_trail_index() -> dict[str, dict]:
    """Load every trail fact sheet from TRAILS_DIR into {trail_id: dict}."""
    index: dict[str, dict] = {}
    if not TRAILS_DIR.exists():
        log.warning("Trails directory not found: %s", TRAILS_DIR)
        return index
    for path in sorted(TRAILS_DIR.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            trail_id = data.get("id", path.stem)
            index[trail_id] = data
        except json.JSONDecodeError as e:
            log.warning("Skipping unparseable trail file %s: %s", path.name, e)
    log.debug("Trail index: %d trails loaded", len(index))
    return index


# ===========================================================================
# Trail selection
# ===========================================================================

def _matches_filters(trail: dict, filters: dict) -> bool:
    required_tags = filters.get("tags", [])
    if required_tags:
        if not all(t in set(trail.get("tags", [])) for t in required_tags):
            return False
    if "region" in filters and trail.get("region") != filters["region"]:
        return False
    fitness = trail.get("difficulty", {}).get("fitness")
    if fitness is not None:
        if "min_fitness" in filters and fitness < filters["min_fitness"]:
            return False
        if "max_fitness" in filters and fitness > filters["max_fitness"]:
            return False
    technical = trail.get("difficulty", {}).get("technical")
    if technical is not None:
        if "min_technical" in filters and technical < filters["min_technical"]:
            return False
        if "max_technical" in filters and technical > filters["max_technical"]:
            return False
    if "weather_exposure" in filters:
        rating = trail.get("exposure", {}).get("weather", {}).get("rating")
        if rating not in filters["weather_exposure"]:
            return False
    if "fall_risk" in filters:
        rating = trail.get("exposure", {}).get("fall_risk", {}).get("rating")
        if rating not in filters["fall_risk"]:
            return False
    return True


def select_trails(request: dict, trail_index: dict[str, dict],
                  rng: random.Random) -> dict[str, dict]:
    """Resolve trail selection for one request (explicit, sampled, or all)."""
    if "trails" in request:
        result = {}
        for tid in request["trails"]:
            if tid in trail_index:
                result[tid] = trail_index[tid]
            else:
                log.warning("Explicit trail '%s' not in index — skipping", tid)
        return result

    if "trail_sample" in request:
        n = int(request["trail_sample"])
        filters = request.get("trail_filters", {})
        candidates = (
            {tid: t for tid, t in trail_index.items() if _matches_filters(t, filters)}
            if filters else dict(trail_index)
        )
        if filters:
            log.debug("Filter matched %d/%d trails", len(candidates), len(trail_index))
        if not candidates:
            log.warning("No trails matched filters %s", filters)
            return {}
        if n > len(candidates):
            log.warning("trail_sample=%d > %d candidates — using all", n, len(candidates))
            return dict(candidates)
        chosen = rng.sample(sorted(candidates.keys()), n)
        log.info("Sampled %d trail(s)%s: %s", n,
                 f" (from {len(candidates)} matching)" if filters else "", chosen)
        return {tid: trail_index[tid] for tid in chosen}

    if len(trail_index) > 10:
        log.warning("No trail selection specified and index has %d trails — "
                    "prompts will be very large. Add trail_sample.", len(trail_index))
    return dict(trail_index)


# ===========================================================================
# Prompt construction
# ===========================================================================

def build_messages(system_prompt: str, request: dict,
                   trail_sheets: dict[str, dict]) -> list[dict]:
    """Return the [system, user] messages list for one generation request."""
    selected_ids = list(trail_sheets.keys())
    trails_line = ", ".join(selected_ids) if selected_ids else "any appropriate trails"

    lines = [
        f"Generate {request.get('count', 3)} Q&A pairs.",
        f"Query type: {request['query_type']}",
        f"Experience level: {request['experience_level']}",
        f"Trails: {trails_line}",
    ]
    if request.get("notes"):
        lines.append(f"Notes: {request['notes']}")
    if trail_sheets:
        lines.append("\n## Trail fact sheets for this batch\n")
        for tid, sheet in trail_sheets.items():
            lines.append(f"### {tid}\n```json\n{_trail_json(tid, sheet)}\n```\n")

    return [
        {"role": "system", "content": system_prompt},
        {"role": "user",   "content": "\n".join(lines)},
    ]


# ===========================================================================
# Output helpers
# ===========================================================================

def extract_json_array(text: str) -> list[Any] | None:
    """Extract a JSON array from a model response, handling fences and prose."""
    text = text.strip()
    if text.startswith("["):
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
    fence = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", text, re.DOTALL)
    if fence:
        try:
            return json.loads(fence.group(1))
        except json.JSONDecodeError:
            pass
    start = text.find("[")
    if start != -1:
        depth = 0
        for i, ch in enumerate(text[start:], start=start):
            if ch == "[":
                depth += 1
            elif ch == "]":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start:i + 1])
                    except json.JSONDecodeError:
                        break
    return None


def validate_record(record: Any, schema: dict) -> list[str]:
    return [e.message for e in _get_validator(schema).iter_errors(record)]


def write_qa_record(record: dict, output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    record_id = record.get("id", f"unknown_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}")
    safe_name = re.sub(r"[^\w\-]", "_", record_id) + ".json"
    path = output_dir / safe_name
    path.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def write_failed(label: str, request_ctx: dict, raw: str | None,
                 errors: list[str],
                 failed_dir: Path = FAILED_DIR) -> None:
    failed_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    path = failed_dir / f"failed_{label}_{ts}.json"
    path.write_text(json.dumps({
        "label": label, "request_ctx": request_ctx,
        "errors": errors, "raw_response": raw,
        "timestamp": datetime.now().isoformat(),
    }, indent=2, ensure_ascii=False), encoding="utf-8")
    log.info("Failure written: %s", path.name)


def process_raw_response(raw: str, label: str, request_ctx: dict,
                         qa_schema: dict,
                         output_dir: Path = OUTPUT_DIR,
                         failed_dir: Path = FAILED_DIR) -> tuple[int, int]:
    """Parse, validate, and save records from one raw model response.
    Returns (saved_count, failed_count)."""
    records = extract_json_array(raw)
    if records is None:
        log.error("[%s] Could not extract JSON array", label)
        write_failed(label, request_ctx, raw, ["JSON extraction failed"],
                     failed_dir=failed_dir)
        return 0, 1
    if not isinstance(records, list):
        log.error("[%s] Parsed JSON is not a list", label)
        write_failed(label, request_ctx, raw,
                     [f"Expected list, got {type(records).__name__}"],
                     failed_dir=failed_dir)
        return 0, 1

    saved = failed = 0
    for rec in records:
        errors = validate_record(rec, qa_schema)
        if errors:
            log.warning("[%s] Record failed validation: %s", label, errors[0])
            write_failed(label, request_ctx, raw, errors,
                         failed_dir=failed_dir)
            failed += 1
        else:
            p = write_qa_record(rec, output_dir)
            log.info("[%s] Saved: %s", label, p.name)
            saved += 1
    return saved, failed


# ===========================================================================
# Backend: vLLM (synchronous, one request at a time)
# ===========================================================================

def call_vllm(messages: list[dict], vllm_url: str, model: str,
              max_tokens: int, temperature: float,
              retry_attempts: int, retry_delay: int, timeout: int) -> str | None:
    payload = {"model": model, "messages": messages,
               "max_tokens": max_tokens, "temperature": temperature}
    session = _get_vllm_session()
    for attempt in range(1, retry_attempts + 1):
        try:
            resp = session.post(vllm_url, json=payload, timeout=timeout,
                                headers={"Content-Type": "application/json"})
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"]
        except requests.exceptions.Timeout:
            log.warning("vLLM timeout (attempt %d/%d)", attempt, retry_attempts)
        except requests.exceptions.ConnectionError as e:
            log.warning("vLLM connection error (attempt %d/%d): %s", attempt, retry_attempts, e)
        except requests.exceptions.HTTPError as e:
            log.warning("vLLM HTTP error (attempt %d/%d): %s", attempt, retry_attempts, e)
            if resp.status_code < 500:
                log.error("Non-retryable %d — aborting", resp.status_code)
                return None
        except (KeyError, IndexError) as e:
            log.warning("vLLM unexpected response shape: %s", e)
        if attempt < retry_attempts:
            time.sleep(retry_delay)
    log.error("vLLM: all %d attempts failed", retry_attempts)
    return None


# ===========================================================================
# Backend: Claude synchronous (one request at a time)
# ===========================================================================

def _anthropic_headers(api_key: str, batch: bool = False) -> dict:
    h = {
        "Content-Type": "application/json",
        "x-api-key": api_key,
        "anthropic-version": ANTHROPIC_API_VERSION,
    }
    if batch:
        h["anthropic-beta"] = ANTHROPIC_BETA_HEADER
    return h


def call_claude_sync(messages: list[dict], api_key: str, model: str,
                     max_tokens: int, temperature: float,
                     retry_attempts: int, retry_delay: int,
                     timeout: int) -> tuple[str, dict] | None:
    """Returns (response_text, usage_dict) on success, None on failure."""
    # Anthropic API: system is a top-level field, not a message role
    system = next((m["content"] for m in messages if m["role"] == "system"), "")
    user_messages = [m for m in messages if m["role"] != "system"]
    payload = {
        "model": model,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "system": system,
        "messages": user_messages,
    }
    for attempt in range(1, retry_attempts + 1):
        try:
            resp = requests.post(
                ANTHROPIC_MESSAGES_URL,
                json=payload,
                headers=_anthropic_headers(api_key),
                timeout=timeout,
            )
            resp.raise_for_status()
            data = resp.json()
            return data["content"][0]["text"], data.get("usage", {})
        except requests.exceptions.Timeout:
            log.warning("Claude sync timeout (attempt %d/%d)", attempt, retry_attempts)
        except requests.exceptions.HTTPError as e:
            log.warning("Claude sync HTTP error (attempt %d/%d): %s", attempt, retry_attempts, e)
            try:
                err_body = resp.json()
                log.warning("Error detail: %s", err_body.get("error", {}).get("message", ""))
            except Exception:
                pass
            # 429 rate-limit: back off longer; 4xx otherwise non-retryable
            if resp.status_code == 429:
                backoff = retry_delay * (2 ** attempt)
                log.info("Rate limited — backing off %ds", backoff)
                time.sleep(backoff)
                continue
            if resp.status_code < 500:
                return None
        except (KeyError, IndexError) as e:
            log.warning("Claude sync unexpected response: %s", e)
        if attempt < retry_attempts:
            time.sleep(retry_delay)
    log.error("Claude sync: all %d attempts failed", retry_attempts)
    return None


# ===========================================================================
# Backend: Claude Batch API
# ===========================================================================

def submit_claude_batch(
    requests_payload: list[dict],
    api_key: str,
    batch_file_stem: str,
) -> str | None:
    """
    Submit a list of requests to the Anthropic Batch API.

    Each entry in requests_payload is:
      {"custom_id": str, "params": {Anthropic Messages API params}}

    Returns the batch ID on success, None on failure.
    Saves a pending-batch record so --retrieve-batch can pick it up later.
    """
    payload = {"requests": requests_payload}
    try:
        resp = requests.post(
            ANTHROPIC_BATCH_CREATE_URL,
            json=payload,
            headers=_anthropic_headers(api_key, batch=True),
            timeout=60,
        )
        resp.raise_for_status()
        batch_id = resp.json()["id"]
        log.info("Batch submitted: %s (%d requests)", batch_id, len(requests_payload))

        # Persist metadata so we can retrieve results later
        PENDING_DIR.mkdir(parents=True, exist_ok=True)
        meta_path = PENDING_DIR / f"{batch_id}.json"
        meta_path.write_text(json.dumps({
            "batch_id": batch_id,
            "batch_file": batch_file_stem,
            "submitted_at": datetime.now().isoformat(),
            "request_count": len(requests_payload),
            "custom_ids": [r["custom_id"] for r in requests_payload],
        }, indent=2), encoding="utf-8")
        log.info("Pending batch metadata saved: %s", meta_path.name)
        return batch_id

    except requests.exceptions.HTTPError as e:
        log.error("Batch submission failed: %s", e)
        try:
            log.error("Detail: %s", resp.json().get("error", {}).get("message", ""))
        except Exception:
            pass
        return None


def poll_claude_batch(batch_id: str, api_key: str,
                      poll_interval: int, poll_timeout: int) -> str | None:
    """
    Poll until the batch reaches a terminal state.
    Returns "succeeded", "errored", "canceled", or None on timeout.
    """
    url = f"{ANTHROPIC_BATCH_CREATE_URL}/{batch_id}"
    deadline = time.time() + poll_timeout
    log.info("Polling batch %s (interval=%ds, timeout=%ds)…",
             batch_id, poll_interval, poll_timeout)
    while time.time() < deadline:
        try:
            resp = requests.get(url, headers=_anthropic_headers(api_key, batch=True),
                                timeout=30)
            resp.raise_for_status()
            data = resp.json()
            status = data.get("processing_status", "unknown")
            counts = data.get("request_counts", {})
            log.info("  status=%-12s  succeeded=%s  errored=%s  processing=%s",
                     status,
                     counts.get("succeeded", "?"),
                     counts.get("errored", "?"),
                     counts.get("processing", "?"))
            if status == "ended":
                return "succeeded"
            if status in ("errored", "canceled", "expired"):
                return status
        except requests.exceptions.RequestException as e:
            log.warning("Poll request failed: %s — retrying", e)
        time.sleep(poll_interval)

    log.error("Batch %s did not complete within %ds", batch_id, poll_timeout)
    return None


def retrieve_claude_batch_results(
    batch_id: str,
    api_key: str,
    qa_schema: dict,
    request_ctx_map: dict[str, dict],
    output_dir: Path = OUTPUT_DIR,
    failed_dir: Path = FAILED_DIR,
    model: str = "unknown",
) -> tuple[int, int]:
    """
    Stream results from a completed batch.
    request_ctx_map maps custom_id → original request dict (for failure records).
    Returns (total_saved, total_failed).
    """
    url = f"{ANTHROPIC_BATCH_CREATE_URL}/{batch_id}/results"
    log.info("Retrieving results for batch %s…", batch_id)
    try:
        resp = requests.get(url, headers=_anthropic_headers(api_key, batch=True),
                            timeout=120, stream=True)
        resp.raise_for_status()
    except requests.exceptions.RequestException as e:
        log.error("Failed to retrieve batch results: %s", e)
        return 0, 0

    total_saved = total_failed = 0
    total_input_tokens = total_output_tokens = 0

    for line in resp.iter_lines():
        if not line:
            continue
        try:
            result = json.loads(line)
        except json.JSONDecodeError:
            log.warning("Skipping unparseable result line")
            continue

        custom_id = result.get("custom_id", "unknown")
        result_type = result.get("result", {}).get("type")
        request_ctx = request_ctx_map.get(custom_id, {})

        if result_type == "succeeded":
            msg = result["result"]["message"]
            raw = msg["content"][0]["text"]
            usage = msg.get("usage", {})
            total_input_tokens += usage.get("input_tokens", 0)
            total_output_tokens += usage.get("output_tokens", 0)
            saved, failed = process_raw_response(raw, custom_id, request_ctx, qa_schema,
                                                 output_dir=output_dir,
                                                 failed_dir=failed_dir)
            total_saved += saved
            total_failed += failed
        elif result_type == "errored":
            err = result.get("result", {}).get("error", {})
            log.error("[%s] API error: %s — %s",
                      custom_id, err.get("type"), err.get("message"))
            write_failed(custom_id, request_ctx, None,
                         [f"API error: {err.get('type')}: {err.get('message')}"],
                         failed_dir=failed_dir)
            total_failed += 1
        else:
            log.warning("[%s] Unexpected result type: %s", custom_id, result_type)
            total_failed += 1

    cost = claude_cost(model, total_input_tokens, total_output_tokens)
    log.info("Batch %s usage: %d in / %d out — cost: $%.4f",
             batch_id, total_input_tokens, total_output_tokens, cost)
    print(f"  Cost: ${cost:.4f}")
    return total_saved, total_failed, cost


# ===========================================================================
# Core: build the list of (custom_id, messages, request_ctx) tuples
# ===========================================================================

def build_request_list(
    batch_path: Path,
    qa_prompt: str,
    trail_index: dict,
    rng: random.Random,
) -> tuple[list[tuple[str, list[dict], dict]], str]:
    """
    Load a batch config file and resolve all trail selections.
    Returns ([(custom_id, messages, request_ctx), ...], batch_id_stem).
    """
    try:
        batch = json.loads(batch_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        log.error("Could not load batch file: %s", e)
        sys.exit(1)

    batch_stem = batch_path.stem
    requests_list = batch.get("requests", [])
    log.info("Batch '%s': %s — %d request(s)",
             batch_stem, batch.get("description", "(no description)"), len(requests_list))

    result = []
    for idx, request in enumerate(requests_list, start=1):
        trail_sheets = select_trails(request, trail_index, rng)
        log.info(
            "Request %d/%d  type=%-24s level=%-14s trails=%s",
            idx, len(requests_list),
            request.get("query_type", "?"),
            request.get("experience_level", "?"),
            list(trail_sheets.keys()),
        )
        messages = build_messages(qa_prompt, request, trail_sheets)
        custom_id = f"{batch_stem}_req{idx:03d}"
        result.append((custom_id, messages, request))

    return result, batch_stem


# ===========================================================================
# Top-level runners
# ===========================================================================

def run_vllm(request_list, qa_schema, args,
             output_dir: Path = OUTPUT_DIR,
             failed_dir: Path = FAILED_DIR):
    total_saved = total_failed = 0
    for custom_id, messages, request_ctx in request_list:
        raw = call_vllm(
            messages=messages,
            vllm_url=args.vllm_url,
            model=args.vllm_model,
            max_tokens=args.max_tokens,
            temperature=args.temperature,
            retry_attempts=args.retries,
            retry_delay=args.retry_delay,
            timeout=args.timeout,
        )
        if raw is None:
            write_failed(custom_id, request_ctx, None, ["No response from vLLM"],
                         failed_dir=failed_dir)
            total_failed += 1
        else:
            s, f = process_raw_response(raw, custom_id, request_ctx, qa_schema,
                                        output_dir=output_dir,
                                        failed_dir=failed_dir)
            total_saved += s
            total_failed += f
    return total_saved, total_failed, 0.0


def run_claude_sync(request_list, qa_schema, args,
                    output_dir: Path = OUTPUT_DIR,
                    failed_dir: Path = FAILED_DIR):
    api_key = _require_api_key()
    total_saved = total_failed = 0
    total_input_tokens = total_output_tokens = 0
    for custom_id, messages, request_ctx in request_list:
        result = call_claude_sync(
            messages=messages,
            api_key=api_key,
            model=args.claude_model,
            max_tokens=args.max_tokens,
            temperature=args.temperature,
            retry_attempts=args.retries,
            retry_delay=args.retry_delay,
            timeout=args.timeout,
        )
        if result is None:
            write_failed(custom_id, request_ctx, None,
                         ["No response from Claude sync"],
                         failed_dir=failed_dir)
            total_failed += 1
        else:
            raw, usage = result
            total_input_tokens += usage.get("input_tokens", 0)
            total_output_tokens += usage.get("output_tokens", 0)
            s, f = process_raw_response(raw, custom_id, request_ctx, qa_schema,
                                        output_dir=output_dir,
                                        failed_dir=failed_dir)
            total_saved += s
            total_failed += f

    cost = claude_cost(args.claude_model, total_input_tokens, total_output_tokens)
    log.info("Claude sync usage: %d in / %d out — cost: $%.4f",
             total_input_tokens, total_output_tokens, cost)
    print(f"  Cost: ${cost:.4f}")
    return total_saved, total_failed, cost


def run_claude_batch(request_list, qa_schema, batch_stem, args,
                     output_dir: Path = OUTPUT_DIR,
                     failed_dir: Path = FAILED_DIR):
    api_key = _require_api_key()

    # Build Anthropic batch payload
    requests_payload = []
    request_ctx_map = {}
    system = next(
        (m["content"] for _, msgs, _ in request_list
         for m in msgs if m["role"] == "system"), ""
    )
    for custom_id, messages, request_ctx in request_list:
        user_messages = [m for m in messages if m["role"] != "system"]
        requests_payload.append({
            "custom_id": custom_id,
            "params": {
                "model": args.claude_model,
                "max_tokens": args.max_tokens,
                "temperature": args.temperature,
                "system": system,
                "messages": user_messages,
            },
        })
        request_ctx_map[custom_id] = request_ctx

    # Submit
    batch_id = submit_claude_batch(requests_payload, api_key, batch_stem)
    if not batch_id:
        log.error("Batch submission failed — exiting")
        sys.exit(1)

    log.info("Batch ID: %s", batch_id)
    log.info("To retrieve results later:  python generate_qa.py --retrieve-batch %s", batch_id)

    if args.no_wait:
        log.info("--no-wait set — exiting without polling. Use --retrieve-batch to collect results.")
        return 0, 0, 0.0

    # Poll
    final_status = poll_claude_batch(
        batch_id, api_key, args.poll_interval, args.poll_timeout
    )
    if final_status != "succeeded":
        log.error("Batch ended with status: %s", final_status)
        return 0, len(request_list), 0.0

    # Retrieve
    return retrieve_claude_batch_results(batch_id, api_key, qa_schema, request_ctx_map,
                                         output_dir=output_dir, failed_dir=failed_dir,
                                         model=args.claude_model)


def run_retrieve_batch(batch_id: str, qa_schema: dict, args,
                       output_dir: Path = OUTPUT_DIR,
                       failed_dir: Path = FAILED_DIR) -> None:
    """Retrieve and process results for a previously submitted batch."""
    api_key = _require_api_key()

    # Load pending metadata if available
    meta_path = PENDING_DIR / f"{batch_id}.json"
    request_ctx_map = {}
    if meta_path.exists():
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        log.info("Found pending batch metadata: %d requests, submitted %s",
                 meta.get("request_count", "?"), meta.get("submitted_at", "?"))
        # Build a minimal ctx map from stored custom_ids
        for cid in meta.get("custom_ids", []):
            request_ctx_map[cid] = {"custom_id": cid}
    else:
        log.warning("No pending metadata for %s — failure records will have minimal context", batch_id)

    # Check current status first
    status_url = f"{ANTHROPIC_BATCH_CREATE_URL}/{batch_id}"
    resp = requests.get(status_url, headers=_anthropic_headers(api_key, batch=True), timeout=30)
    resp.raise_for_status()
    data = resp.json()
    status = data.get("processing_status")
    log.info("Batch status: %s", status)

    if status != "ended":
        log.info("Batch not yet complete — polling…")
        final = poll_claude_batch(batch_id, api_key, args.poll_interval, args.poll_timeout)
        if final != "succeeded":
            log.error("Batch ended with status: %s", final)
            sys.exit(1)

    saved, failed, cost = retrieve_claude_batch_results(
        batch_id, api_key, qa_schema, request_ctx_map,
        output_dir=output_dir, failed_dir=failed_dir,
        model=args.claude_model,
    )
    log.info("Retrieve complete — %d saved, %d failed, cost $%.4f", saved, failed, cost)

    # Clean up pending metadata
    if meta_path.exists() and saved > 0:
        meta_path.unlink()
        log.debug("Removed pending metadata for %s", batch_id)


def _require_api_key() -> str:
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        log.error("ANTHROPIC_API_KEY environment variable not set")
        sys.exit(1)
    return key


# ===========================================================================
# CLI
# ===========================================================================

def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Generate Q&A training pairs for the NH hiking agent.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Default: Claude Batch API (cheapest — ~50% off standard rates)
  python generate_qa.py --batch batches/batch_01.json

  # Submit batch and exit immediately (retrieve later)
  python generate_qa.py --batch batches/batch_01.json --no-wait

  # Retrieve a previously submitted batch
  python generate_qa.py --retrieve-batch msgbatch_abc123

  # Synchronous Claude (immediate results, standard pricing)
  python generate_qa.py --batch batches/batch_01.json --backend claude-sync

  # Local vLLM
  python generate_qa.py --batch batches/batch_01.json --backend vllm \\
      --vllm-url http://localhost:8000/v1/chat/completions \\
      --vllm-model meta-llama/Llama-3.3-70B-Instruct

  # Use a specific Claude model
  python generate_qa.py --batch batches/batch_01.json \\
      --claude-model claude-sonnet-4-6

  # Dry run — shows prompts without calling any API
  python generate_qa.py --batch batches/batch_01.json --dry-run

  # List available batch files
  python generate_qa.py --list-batches

Environment variables:
  ANTHROPIC_API_KEY   Required for claude-batch and claude-sync
  VLLM_URL            Override --vllm-url
  VLLM_MODEL          Override --vllm-model
        """,
    )

    # Primary actions
    action = p.add_mutually_exclusive_group()
    action.add_argument("--batch", metavar="FILE",
                        help="Batch config file to process")
    action.add_argument("--retrieve-batch", metavar="BATCH_ID",
                        help="Retrieve and process results for a previously submitted Anthropic batch")
    action.add_argument("--list-batches", action="store_true",
                        help="List available batch config files and exit")

    # Backend selection
    p.add_argument(
        "--backend",
        choices=["claude-batch", "claude-sync", "vllm", "llamacpp"],
        default=DEFAULT_BACKEND,
        help=f"Which API to use for generation (default: {DEFAULT_BACKEND})",
    )

    # Claude options
    p.add_argument(
        "--claude-model",
        default=os.environ.get("CLAUDE_MODEL", DEFAULT_CLAUDE_MODEL),
        help=f"Anthropic model ID (default: {DEFAULT_CLAUDE_MODEL})",
    )
    p.add_argument(
        "--no-wait", action="store_true",
        help="(claude-batch only) Submit and exit without polling for results",
    )
    p.add_argument(
        "--poll-interval", type=int, default=DEFAULT_POLL_INTERVAL,
        help=f"Seconds between batch status polls (default: {DEFAULT_POLL_INTERVAL})",
    )
    p.add_argument(
        "--poll-timeout", type=int, default=DEFAULT_POLL_TIMEOUT,
        help=f"Max seconds to wait for batch completion (default: {DEFAULT_POLL_TIMEOUT})",
    )

    # vLLM options
    p.add_argument(
        "--vllm-url",
        default=os.environ.get("VLLM_URL", DEFAULT_VLLM_URL),
        help=f"vLLM endpoint (default: {DEFAULT_VLLM_URL})",
    )
    p.add_argument(
        "--vllm-model",
        default=os.environ.get("VLLM_MODEL", DEFAULT_VLLM_MODEL),
        help=f"Model name in vLLM (default: {DEFAULT_VLLM_MODEL})",
    )

    # Shared generation options
    p.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS,
                   help=f"Max response tokens (default: {DEFAULT_MAX_TOKENS})")
    p.add_argument("--temperature", type=float, default=DEFAULT_TEMPERATURE,
                   help=f"Sampling temperature (default: {DEFAULT_TEMPERATURE})")
    p.add_argument("--retries", type=int, default=DEFAULT_RETRY_ATTEMPTS,
                   help=f"Retry attempts on transient errors (default: {DEFAULT_RETRY_ATTEMPTS})")
    p.add_argument("--retry-delay", type=int, default=DEFAULT_RETRY_DELAY,
                   help=f"Seconds between retries (default: {DEFAULT_RETRY_DELAY})")
    p.add_argument("--timeout", type=int, default=600,
                   help="HTTP request timeout in seconds (default: 600)")
    p.add_argument("--output-dir", default=str(OUTPUT_DIR),
                   help=f"Directory for saved QA records (default: {OUTPUT_DIR})")
    p.add_argument("--seed", type=int, default=None,
                   help="Random seed for reproducible trail sampling")
    p.add_argument("--dry-run", action="store_true",
                   help="Build prompts without calling any API")
    p.add_argument("--verbose", "-v", action="store_true",
                   help="Enable debug logging")

    return p


# ===========================================================================
# Entry point
# ===========================================================================

def main() -> None:
    parser = build_arg_parser()
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    output_dir = Path(args.output_dir)
    failed_dir = output_dir.parent / "failed"

    # ── --list-batches ────────────────────────────────────────────────────
    if args.list_batches:
        if not BATCHES_DIR.exists():
            log.info("No batches/ directory at %s", BATCHES_DIR)
            return
        files = sorted(BATCHES_DIR.glob("*.json"))
        if not files:
            log.info("No batch files found")
            return
        print(f"\nBatch files in {BATCHES_DIR}:\n")
        for bf in files:
            try:
                data = json.loads(bf.read_text())
                n = len(data.get("requests", []))
                print(f"  {bf.name:<42} {n} request(s)  —  {data.get('description','')}")
            except Exception:
                print(f"  {bf.name:<42} (could not parse)")
        print()
        return

    # ── --retrieve-batch ─────────────────────────────────────────────────
    if args.retrieve_batch:
        qa_schema = load_qa_schema()
        run_retrieve_batch(args.retrieve_batch, qa_schema, args,
                           output_dir=output_dir, failed_dir=failed_dir)
        return

    # ── --batch ───────────────────────────────────────────────────────────
    if not args.batch:
        parser.print_help()
        sys.exit(1)

    batch_path = Path(args.batch)
    if not batch_path.exists():
        log.error("Batch file not found: %s", batch_path)
        sys.exit(1)

    qa_prompt   = load_qa_prompt()
    qa_schema   = load_qa_schema()
    trail_index = build_trail_index()

    seed = args.seed if args.seed is not None else random.randint(0, 2**32 - 1)
    rng  = random.Random(seed)

    log.info("Backend       : %s", args.backend)
    log.info("Trail index   : %d trails", len(trail_index))
    log.info("Random seed   : %d  (--seed %d to reproduce)", seed, seed)
    log.info("Output dir    : %s", output_dir)

    # Resolve all requests up front (trail sampling happens here)
    request_list, batch_stem = build_request_list(batch_path, qa_prompt, trail_index, rng)

    if args.dry_run:
        log.info("DRY RUN — %d request(s) built, no API calls made", len(request_list))
        for custom_id, messages, _ in request_list:
            user_content = next(m["content"] for m in messages if m["role"] == "user")
            log.info("  [%s] prompt length: %d chars", custom_id, len(user_content))
        return

    start = time.time()

    if args.backend in ("vllm", "llamacpp"):
        saved, failed, cost = run_vllm(request_list, qa_schema, args,
                                       output_dir=output_dir, failed_dir=failed_dir)
    elif args.backend == "claude-sync":
        saved, failed, cost = run_claude_sync(request_list, qa_schema, args,
                                              output_dir=output_dir, failed_dir=failed_dir)
    else:  # claude-batch (default)
        saved, failed, cost = run_claude_batch(request_list, qa_schema, batch_stem, args,
                                               output_dir=output_dir, failed_dir=failed_dir)

    elapsed = time.time() - start
    log.info("Done in %.1fs — %d saved, %d failed", elapsed, saved, failed)
    cost_str = f"  Cost: ${cost:.4f}" if cost else ""
    print(f"Run time: {elapsed:.1f}s  |  Saved: {saved}  Failed: {failed}{cost_str}")


if __name__ == "__main__":
    main()
