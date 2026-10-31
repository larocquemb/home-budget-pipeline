#!/usr/bin/env python3
"""Replay the saved receipt extraction input inside the Red Hat vLLM container."""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import time

REVISION = "2bf7f1646dc0875cff4404fdfef976cfb0ff2199"
REPOSITORY = "ulkaa/Qwen3.8-27B-AWQ-INT4"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    os.umask(0o077)
    raw = args.request.read_bytes()
    request = json.loads(raw)
    messages = request["messages"]
    if len(messages) != 1 or messages[0]["role"] != "user":
        parser.error("This probe expects the saved single-user receipt request")
    images = messages[0].get("images", [])
    if len(images) != 1:
        parser.error("Expected exactly one embedded receipt image")
    image = base64.b64decode(images[0], validate=True)
    if image.startswith(b"\x89PNG\r\n\x1a\n"):
        mime = "image/png"
    elif image.startswith(b"\xff\xd8"):
        mime = "image/jpeg"
    else:
        parser.error("Expected PNG or JPEG")
    options = request["options"]
    if options["num_ctx"] != 4096 or options["num_predict"] != 256 or options["temperature"] != 0:
        parser.error("Expected the saved 4096-context, 256-output, temperature-zero request")
    chat = [{"role": "user", "content": [
        {"type": "text", "text": messages[0]["content"]},
        {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{images[0]}"}},
    ]}]
    from vllm import LLM, SamplingParams
    import vllm

    model = Path("/models/hub/models--ulkaa--Qwen3.8-27B-AWQ-INT4/snapshots") / REVISION
    if not (model / "config.json").is_file():
        parser.error(f"Pinned model snapshot missing: {REVISION}")
    report = {
        "model": REPOSITORY, "revision": REVISION, "vllm": vllm.__version__,
        "request_sha256": hashlib.sha256(raw).hexdigest(),
        "image_sha256": hashlib.sha256(image).hexdigest(),
        "source_ollama_model": request["model"],
        "settings": {"num_ctx": 4096, "num_predict": 256, "temperature": 0,
                     "enable_thinking": False, "enforce_eager": True,
                     "enable_prefix_caching": False},
        "limitations": ["Different quantization from Ollama Q4_K_M",
                        "Receipt extraction, not the earlier product-review request",
                        "No automated receipt accuracy assessment"],
        "runs": [],
    }
    print(json.dumps({k: report[k] for k in ("model", "revision", "image_sha256")}), flush=True)
    started = time.monotonic()
    llm = LLM(model=str(model), trust_remote_code=False, dtype="bfloat16",
              tensor_parallel_size=1, max_model_len=4096,
              max_num_batched_tokens=4096, max_num_seqs=1,
              gpu_memory_utilization=0.90, limit_mm_per_prompt={"image": 1, "video": 0},
              enable_prefix_caching=False, enforce_eager=True, seed=42)
    report["startup_seconds"] = round(time.monotonic() - started, 3)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    for attempt in (1, 2):
        started = time.monotonic()
        result = llm.chat(chat, sampling_params=SamplingParams(temperature=0, max_tokens=256),
                          chat_template_kwargs={"enable_thinking": False})[0]
        output = result.outputs[0]
        text = output.text.strip()
        # Keep the full response for manual accuracy review; JSON alone is not accuracy.
        candidate = text
        if candidate.startswith("```") and candidate.endswith("```"):
            candidate = "\n".join(candidate.splitlines()[1:-1])
        try:
            parsed = json.loads(candidate)
            valid_json = True
        except json.JSONDecodeError:
            parsed, valid_json = None, False
        row = {"attempt": attempt, "wall_seconds": round(time.monotonic() - started, 3),
               "input_tokens": len(result.prompt_token_ids or []),
               "output_tokens": len(output.token_ids), "finish_reason": output.finish_reason,
               "json_valid": valid_json, "parsed": parsed, "response": text}
        report["runs"].append(row)
        with args.output.open("w") as handle:
            json.dump(report, handle, indent=2)
            handle.write("\n")
        os.chmod(args.output, 0o600)
        print(json.dumps({k: v for k, v in row.items() if k not in ("parsed", "response")}), flush=True)
    print(f"Saved private responses: {args.output}", flush=True)
    if any(not row["response"] or row["finish_reason"] == "length" or not row["json_valid"]
           for row in report["runs"]):
        raise SystemExit("Inspect report: empty, truncated, or non-JSON response")
    print("PASS: two receipt-image requests returned JSON; verify contents manually", flush=True)


if __name__ == "__main__":
    main()
