# Q&A Generation — User Guide

This is a reference for humans running batch generation of training examples
for the NH White Mountains hiking agent.

## Quick start

```bash
# Local llama.cpp server
cd qa_generation
./run_all_batches.sh local

# Claude Batch API (requires ANTHROPIC_API_KEY in .env.claude)
./run_all_batches.sh claude
```

Each run reads the batch files listed in `run_all_batches.sh` and produces
QA pair JSON files under `output/qa_pairs/`.

---

## Manual mode (Claude with web search)

To generate examples by hand in a Claude chat:

1. Start a new chat with web search enabled
2. Paste the model prompt (`QA_GENERATION_PROMPT.md`) at the top
3. Send batch requests as follow-up messages using the format below

### Request format

```
Generate [N] Q&A pairs.
Query type: [trail-lookup|recommendation|progression-planning|gear-and-safety|weather-and-conditions]
Experience level: [beginner|intermediate|experienced]
Trails: [trail-id, trail-id, ...]
Notes: [any specific scenarios, seasonal context, or constraints]
```

### Target distribution per batch

| Query type | Examples per trail/run |
|---|---|
| trail-lookup | 1–2 per trail |
| recommendation | 2–3 covering different criteria (season, group, objective) |
| progression-planning | 2–3 per objective/starting point |
| gear-and-safety | 1–2 per trail, vary season and level |
| weather-and-conditions | 1–2 per trail with above-treeline exposure |

### Overall dataset targets (≈800 total)

| Query type | % | Approx. count |
|---|---|---|
| trail-lookup | 20% | ~160 |
| recommendation | 20% | ~160 |
| progression-planning | 30% | ~240 |
| gear-and-safety | 15% | ~120 |
| weather-and-conditions | 15% | ~120 |

Progression-planning gets the highest allocation because it requires the most
complex reasoning and is the most distinctive feature of this agent versus a
simple trail database lookup.

---

## Config files

| File | Purpose |
|---|---|
| `.env.local` | Backend URL, model, seed for local runs |
| `.env.claude` | Claude model, API key, seed for API runs |
| `batch_*.json` | Batch definitions (query type, trails, count, notes) |

## Output

- `output/qa_pairs/` — individual JSON files, one per generated QA pair
- `output/failed/` — raw responses that couldn't be parsed or validated
- `output/pending_batches/` — submitted Claude batch metadata (cleaned up on success)

## CLI reference

```
python generate_qa.py --batch <file> --backend <backend>
```

Backends: `claude-batch` (default), `claude-sync`, `vllm`, `llamacpp`

Use `--help` for full options.
