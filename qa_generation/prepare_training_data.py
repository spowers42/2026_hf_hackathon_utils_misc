#!/usr/bin/env python3
"""
prepare_training_data.py — Convert generated Q&A JSON files into a
HuggingFace Dataset ready for Unsloth SFT fine-tuning.

What this does:
  1. Loads all validated Q&A JSON files from output/qa_pairs/
  2. Strips the 'meta' and 'id' fields (not needed for training)
  3. Applies the model's chat template to produce a single 'text' string per example
  4. Splits into train / eval sets (stratified by query_type)
  5. Saves as HuggingFace Dataset to disk (ready to load_from_disk in your training notebook)
  6. Also saves a JSONL version as a backup

Usage:
    python prepare_training_data.py
    python prepare_training_data.py --input-dir output/qa_pairs --output-dir output/hf_dataset
    python prepare_training_data.py --model unsloth/Llama-3.2-3B-Instruct --eval-split 0.1
    python prepare_training_data.py --dry-run   # show stats without writing files
"""

import argparse
import json
import logging
import random
import sys
from collections import Counter, defaultdict
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("prepare")

SCRIPT_DIR = Path(__file__).parent
DEFAULT_INPUT_DIR  = SCRIPT_DIR / "output" / "qa_pairs"
DEFAULT_OUTPUT_DIR = SCRIPT_DIR / "output" / "hf_dataset"

# Supported base models. The tokenizer's built-in chat template is used
# (no unsloth dependency — this script runs outside the training environment).
SUPPORTED_MODELS = {
    "unsloth/Llama-3.2-3B-Instruct",
    "unsloth/Llama-3.2-1B-Instruct",
    "unsloth/Phi-4-mini-Instruct",
    "unsloth/gemma-3-4b-it",
    "unsloth/gemma-3-1b-it",
}
DEFAULT_MODEL = "unsloth/Llama-3.2-3B-Instruct"


# ---------------------------------------------------------------------------
# Load Q&A records
# ---------------------------------------------------------------------------

def load_qa_records(input_dir: Path) -> list[dict]:
    """Load and return all valid QA records from the output directory."""
    records = []
    skipped = 0
    for path in sorted(input_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            # Minimal sanity check — must have messages with at least system+user+assistant
            msgs = data.get("messages", [])
            roles = [m.get("role") for m in msgs]
            if "system" not in roles or "user" not in roles or "assistant" not in roles:
                log.warning("Skipping %s — missing required roles", path.name)
                skipped += 1
                continue
            records.append(data)
        except (json.JSONDecodeError, KeyError) as e:
            log.warning("Skipping %s — %s", path.name, e)
            skipped += 1
    log.info("Loaded %d records (%d skipped)", len(records), skipped)
    return records


# ---------------------------------------------------------------------------
# Dataset statistics
# ---------------------------------------------------------------------------

def print_stats(records: list[dict]) -> None:
    """Print a breakdown of the dataset by query type and experience level."""
    by_type  = Counter(r.get("meta", {}).get("query_type",       "unknown") for r in records)
    by_level = Counter(r.get("meta", {}).get("experience_level", "unknown") for r in records)
    by_hazard = Counter()
    for r in records:
        for h in r.get("meta", {}).get("hazard_types_present", []):
            by_hazard[h] += 1

    print(f"\n{'─'*50}")
    print(f"  Total records : {len(records)}")
    print(f"\n  By query type:")
    for k, v in sorted(by_type.items(), key=lambda x: -x[1]):
        pct = 100 * v / len(records) if records else 0
        print(f"    {k:<30} {v:>4}  ({pct:.0f}%)")
    print(f"\n  By experience level:")
    for k, v in sorted(by_level.items(), key=lambda x: -x[1]):
        pct = 100 * v / len(records) if records else 0
        print(f"    {k:<30} {v:>4}  ({pct:.0f}%)")
    print(f"\n  Hazard type coverage:")
    for k, v in sorted(by_hazard.items(), key=lambda x: -x[1]):
        pct = 100 * v / len(records) if records else 0
        print(f"    {k:<30} {v:>4}  ({pct:.0f}%)")
    print(f"{'─'*50}\n")


# ---------------------------------------------------------------------------
# Stratified train/eval split
# ---------------------------------------------------------------------------

def stratified_split(
    records: list[dict], eval_fraction: float, seed: int = 42
) -> tuple[list[dict], list[dict]]:
    """
    Split records into train/eval, stratified by query_type so all five
    query types are represented in the eval set proportionally.
    """
    rng = random.Random(seed)

    by_type: dict[str, list[dict]] = defaultdict(list)
    for r in records:
        qt = r.get("meta", {}).get("query_type", "unknown")
        by_type[qt].append(r)

    train, eval_ = [], []
    for qt, group in by_type.items():
        rng.shuffle(group)
        n_eval = max(1, round(len(group) * eval_fraction))
        eval_.extend(group[:n_eval])
        train.extend(group[n_eval:])

    rng.shuffle(train)
    rng.shuffle(eval_)
    return train, eval_


# ---------------------------------------------------------------------------
# Apply chat template
# ---------------------------------------------------------------------------

def apply_template(records: list[dict], tokenizer) -> list[dict]:
    """
    Apply the tokenizer's chat template to each record's messages list.
    Returns a list of dicts with a single 'text' key — the format SFTTrainer expects.

    The 'meta' and 'id' fields are intentionally dropped here;
    they are only needed for QA and review, not for training.
    """
    output = []
    failed = 0
    for rec in records:
        try:
            text = tokenizer.apply_chat_template(
                rec["messages"],
                tokenize=False,
                add_generation_prompt=False,
            )
            output.append({"text": text})
        except Exception as e:
            log.warning("Failed to apply template to %s: %s", rec.get("id", "?"), e)
            failed += 1
    if failed:
        log.warning("%d records dropped during template application", failed)
    return output


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    p = argparse.ArgumentParser(
        description="Prepare QA pairs for Unsloth SFT fine-tuning.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Default: Llama 3.2 3B, 10% eval split
  python prepare_training_data.py

  # Phi-4-mini with 15% eval
  python prepare_training_data.py --model unsloth/Phi-4-mini-Instruct --eval-split 0.15

  # Just show stats, don't write anything
  python prepare_training_data.py --dry-run
        """,
    )
    p.add_argument("--input-dir",  default=str(DEFAULT_INPUT_DIR),
                   help=f"Directory of QA JSON files (default: {DEFAULT_INPUT_DIR})")
    p.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR),
                   help=f"Where to write the HF dataset (default: {DEFAULT_OUTPUT_DIR})")
    p.add_argument("--model", default=DEFAULT_MODEL,
                   choices=sorted(SUPPORTED_MODELS),
                   help=f"Base model to apply chat template for (default: {DEFAULT_MODEL})")
    p.add_argument("--eval-split", type=float, default=0.1,
                   help="Fraction of data to reserve for eval (default: 0.1)")
    p.add_argument("--seed", type=int, default=42,
                   help="Random seed for train/eval split (default: 42)")
    p.add_argument("--dry-run", action="store_true",
                   help="Print stats without writing output files")
    args = p.parse_args()

    input_dir  = Path(args.input_dir)
    output_dir = Path(args.output_dir)

    if not input_dir.exists():
        log.error("Input directory not found: %s", input_dir)
        sys.exit(1)

    # ── 1. Load records ────────────────────────────────────────────────────
    records = load_qa_records(input_dir)
    if not records:
        log.error("No valid records found in %s", input_dir)
        sys.exit(1)

    print_stats(records)

    if args.dry_run:
        log.info("DRY RUN — no files written")
        return

    # ── 2. Load tokenizer ─────────────────────────────────────────────────
    log.info("Loading tokenizer for %s …", args.model)
    from transformers import AutoTokenizer
    tokenizer = AutoTokenizer.from_pretrained(args.model)

    # ── 3. Split ──────────────────────────────────────────────────────────
    train_records, eval_records = stratified_split(records, args.eval_split, args.seed)
    log.info("Split: %d train / %d eval", len(train_records), len(eval_records))

    # ── 4. Apply chat template ────────────────────────────────────────────
    log.info("Applying chat template …")
    train_text = apply_template(train_records, tokenizer)
    eval_text  = apply_template(eval_records,  tokenizer)
    log.info("Template applied: %d train / %d eval", len(train_text), len(eval_text))

    # ── 5. Save as HuggingFace Dataset ────────────────────────────────────
    from datasets import Dataset, DatasetDict

    ds = DatasetDict({
        "train": Dataset.from_list(train_text),
        "eval":  Dataset.from_list(eval_text),
    })

    output_dir.mkdir(parents=True, exist_ok=True)
    ds.save_to_disk(str(output_dir))
    log.info("HuggingFace dataset saved to %s", output_dir)
    log.info("  Train: %d examples", len(ds["train"]))
    log.info("  Eval:  %d examples", len(ds["eval"]))

    # ── 6. JSONL backup (train only) ──────────────────────────────────────
    jsonl_path = output_dir / "train.jsonl"
    with jsonl_path.open("w", encoding="utf-8") as f:
        for row in train_text:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    log.info("JSONL backup written to %s", jsonl_path)

    # ── 7. Print usage snippet ────────────────────────────────────────────
    print(f"""
╔══════════════════════════════════════════════════════════════╗
  Dataset ready. Use this in your Unsloth training notebook:
╚══════════════════════════════════════════════════════════════╝

  from datasets import load_from_disk
  dataset = load_from_disk("{output_dir}")
  train_dataset = dataset["train"]
  eval_dataset  = dataset["eval"]

  # Apply train_on_responses_only to mask user/system tokens
  from unsloth.chat_templates import train_on_responses_only
  trainer = train_on_responses_only(
      trainer,
      # Llama 3.x:
      instruction_part = "<|start_header_id|>user<|end_header_id|>\\n\\n",
      response_part    = "<|start_header_id|>assistant<|end_header_id|>\\n\\n",
      # Gemma 3:
      # instruction_part = "<start_of_turn>user\\n",
      # response_part    = "<start_of_turn>model\\n",
      # Phi-4-mini: check Unsloth docs for correct header tokens
  )
""")


if __name__ == "__main__":
    main()
