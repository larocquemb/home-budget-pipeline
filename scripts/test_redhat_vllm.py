#!/usr/bin/env python3
"""Run inside Red Hat's vLLM container to test RTX 3090 CUDA and real inference."""
import json
import time

MODEL = 'Qwen/Qwen3-0.6B'
REVISION = 'c1899de289a04d12100db370d81485cdf75e47ca'


def main():
    import torch
    import vllm
    from vllm import LLM, SamplingParams

    print(json.dumps({'torch': torch.__version__, 'vllm': vllm.__version__,
                      'cuda_build': torch.version.cuda}), flush=True)
    if not torch.cuda.is_available():
        raise RuntimeError('CUDA is unavailable in this container')
    gpu = torch.cuda.get_device_name(0)
    capability = torch.cuda.get_device_capability(0)
    print(json.dumps({'gpu': gpu, 'compute_capability': capability}), flush=True)
    if '3090' not in gpu or capability != (8, 6):
        raise RuntimeError('This probe expects the Arsene RTX 3090 (compute capability 8.6)')
    matrix = torch.randn((1024, 1024), device='cuda', dtype=torch.float16)
    product = matrix @ matrix
    torch.cuda.synchronize()
    if not torch.isfinite(product).all().item():
        raise RuntimeError('CUDA matrix multiplication produced invalid values')
    del matrix, product
    torch.cuda.empty_cache()
    print('PASS: RTX 3090 CUDA FP16 matrix multiplication', flush=True)

    started = time.monotonic()
    llm = LLM(model=MODEL, revision=REVISION, tokenizer_revision=REVISION,
              dtype='float16', trust_remote_code=False, tensor_parallel_size=1,
              max_model_len=1024, max_num_batched_tokens=1024, max_num_seqs=1,
              gpu_memory_utilization=0.25, enforce_eager=True, seed=42)
    tokenizer = llm.get_tokenizer()
    prompt = tokenizer.apply_chat_template(
        [{'role': 'user', 'content': 'Answer briefly: what is 2 + 2?'}],
        tokenize=False, add_generation_prompt=True, enable_thinking=False)
    loaded = time.monotonic()
    results = llm.generate([prompt], SamplingParams(temperature=0, max_tokens=24))
    if not results or not results[0].outputs or not results[0].outputs[0].text.strip():
        raise RuntimeError('vLLM produced no response')
    output = results[0].outputs[0]
    print(json.dumps({'model': MODEL, 'revision': REVISION, 'gpu': gpu,
                      'startup_seconds_including_download': round(loaded - started, 3),
                      'generation_seconds': round(time.monotonic() - loaded, 3),
                      'output_tokens': len(output.token_ids), 'response': output.text}), flush=True)
    print('PASS: real vLLM model inference on RTX 3090', flush=True)
    print('Not established: production Qwen compatibility, receipt quality/latency, '
          'concurrency, or Red Hat support coverage.', flush=True)


if __name__ == '__main__':
    # vLLM worker processes may use spawn; keep GPU/model initialization out of imports.
    main()
