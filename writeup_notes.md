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