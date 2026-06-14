# Writeup Notes

## project idea

## initial planning and setup

## gathering ground truth data

## synthetic data generation/QA pairs

### running local
* hardware ryzen ai max+ 395 with 128gb ram (framework desktop)
* too slow with 70b model, about 10min to generate pair when using cpu only
* lead to finding amd strix halo toolboxes
  * https://github.com/kyuz0/amd-strix-halo-toolboxes
  * https://strix-halo-toolboxes.com
* setup a rocm llama.cpp toolbox to run inference server
`llama-server -m models/qwen3-coder-30B-A3B/BF16/Qwen3-Coder-30B-A3B-Instruct-BF16-00001-of-00002.gguf   -c 8192 -ngl 999 -fa 1 --no-mmap --host 0.0.0.0`

* test qwen3 coder 30b instruct  ran less than a minute, but didn't generate usable json output.


`llama-server -hf unsloth/Llama-3.3-70B-Instruct-GGUF:Q4_K_S --host 0.0.0.0 --port 8000 -c 16384`
* ran in 3 minutes  -> almost 2 days to generate all examples without any failures
* trying to create too many examples at once causes the token length limit to be hit on both, producing malformed json and incomplete batches.  batches have to be broken into small chunks with 1 request for a few q-a pairs.  This could be setup to do single pairs per request as well.  The json format adds a lot of overhead to the pairs.
* claude made more expressive and interesting answers, llama 70b was very terse and uninstersting in its replys.


```
  from datasets import load_from_disk
  dataset = load_from_disk("/var/home/hobbit/Development/2026_hf_hackathon_utils_misc/qa_generation/output/hf_dataset")
  train_dataset = dataset["train"]
  eval_dataset  = dataset["eval"]

  # Apply train_on_responses_only to mask user/system tokens
  from unsloth.chat_templates import train_on_responses_only
  trainer = train_on_responses_only(
      trainer,
      # Llama 3.x:
      instruction_part = "<|start_header_id|>user<|end_header_id|>\n\n",
      response_part    = "<|start_header_id|>assistant<|end_header_id|>\n\n",
```