#!/usr/bin/env python3
"""Bounded CPU/GPU inference comparison; saves private results, never receipt database writes."""
import argparse
import base64
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import time
from urllib.request import ProxyHandler, Request, build_opener


def query(base, route, timeout, body=None):
    data = json.dumps(body).encode() if body is not None else None
    request = Request(base.rstrip('/') + route, data=data,
                      headers={'Content-Type': 'application/json'})
    with build_opener(ProxyHandler({})).open(request, timeout=timeout) as response:
        return json.load(response)


def placement(models, model, workload):
    loaded = next((m for m in models if m.get('name') == model or m.get('model') == model), None)
    if loaded is None or 'size_vram' not in loaded:
        return False
    vram = loaded['size_vram']
    return isinstance(vram, (int, float)) and (vram == 0 if workload == 'production' else vram > 0)


def measure(base, workload, body, timeout):
    start = time.monotonic()
    response = query(base, '/api/chat', timeout, body)
    wall = time.monotonic() - start
    models = query(base, '/api/ps', min(timeout, 10))['models']
    seconds = response.get('eval_duration', 0) / 1e9
    return {'workload': workload, 'endpoint': base, 'model': body['model'],
            'wall_seconds': wall, 'generation_seconds': seconds,
            'tokens': response.get('eval_count'),
            'tokens_per_second': response.get('eval_count', 0) / seconds if seconds else None,
            'load_seconds': response.get('load_duration', 0) / 1e9,
            'prompt_tokens': response.get('prompt_eval_count'),
            'prompt_seconds': response.get('prompt_eval_duration', 0) / 1e9,
            'completed': response.get('done', False),
            'placement_pass': placement(models, body['model'], workload),
            'loaded_models': models, 'response': response.get('message')}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--production-url', default='http://192.168.2.201:11434')
    parser.add_argument('--development-url', default='http://192.168.2.201:11436')
    parser.add_argument('--model', default='qwen3-vl-receipts:30b-instruct')
    parser.add_argument('--num-threads', type=int, default=5,
                        help='CPU threads for image-mode requests (default: production setting of 5); JSON replay is unchanged')
    parser.add_argument('--image', type=Path, help='Receipt PNG/JPEG, not a PDF')
    parser.add_argument('--request-json', type=Path, help='Replay an actual Ollama chat request/settings instead')
    parser.add_argument('--timeout', type=float, default=900)
    parser.add_argument('--repeats', type=int, default=2)
    parser.add_argument('--concurrent', action='store_true', help='Also exercise both services simultaneously')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if args.production_url == args.development_url or args.repeats < 1 or args.timeout <= 0 or args.num_threads < 1:
        parser.error('Use distinct service endpoints, positive repeats, timeout, and thread count')
    if bool(args.image) == bool(args.request_json):
        parser.error('Supply exactly one of --image or --request-json')
    if args.request_json:
        body = json.loads(args.request_json.read_text())
        if not isinstance(body, dict) or not body.get('model') or not isinstance(body.get('messages'), list):
            parser.error('Request JSON must contain model and messages')
        input_hash = hashlib.sha256(args.request_json.read_bytes()).hexdigest()
    else:
        image = args.image.read_bytes()
        if not (image.startswith(b'\x89PNG') or image.startswith(b'\xff\xd8')):
            parser.error('--image must be a PNG or JPEG; render receipt PDFs first')
        input_hash = hashlib.sha256(image).hexdigest()
        body = {'model': args.model,
                'messages': [{'role': 'user', 'content': 'Read this receipt. Return the merchant, date, total, and first five product rows as JSON.',
                              'images': [base64.b64encode(image).decode()]}],
                'options': {'temperature': 0, 'num_ctx': 4096, 'num_predict': 256,
                            'num_thread': args.num_threads}}
    # Identical payload for both services. Hardware placement is enforced by systemd, not request options.
    body.update(stream=False, keep_alive='2m')
    report = {'created': datetime.now(timezone.utc).isoformat(), 'input_sha256': input_hash,
              'model': body['model'], 'settings': body.get('options', {}), 'runs': []}
    for repeat in range(args.repeats):
        for workload, url in [('production', args.production_url), ('development', args.development_url)]:
            result = measure(url, workload, body, args.timeout)
            result.update(repeat=repeat + 1, concurrent=False)
            report['runs'].append(result)
            print(f"{workload}: {result['wall_seconds']:.2f}s, placement={'PASS' if result['placement_pass'] else 'FAIL'}", flush=True)
    if args.concurrent:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(measure, url, workload, body, args.timeout)
                       for workload, url in [('production', args.production_url), ('development', args.development_url)]]
            for future in futures:
                result = future.result()
                result.update(concurrent=True)
                report['runs'].append(result)
    output = args.output or Path('.model-evals') / ('ollama-split-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')) / 'report.json'
    output.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(output.parent, 0o700)
    with open(output, 'w', opener=lambda path, flags: os.open(path, flags, 0o600)) as file:
        json.dump(report, file, indent=2)
        file.write('\n')
    os.chmod(output, 0o600)
    print(f'Private report: {output}')
    if not all(run['placement_pass'] and run['completed'] for run in report['runs']):
        raise SystemExit('Inference failed hardware placement/completion checks; inspect the report')


if __name__ == '__main__':
    main()
