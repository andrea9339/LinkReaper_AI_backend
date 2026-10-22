"""Paid, opt-in comparison on a fixed JSON sample, without repeating Google searches.

Set OPENAI_API_KEY and SERPAPI_KEY, then run from the backend directory:
python -m tests.benchmark_ai --sample tests/fixtures/ai_benchmark_enron.json
"""

import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import ssl
import time
from unittest.mock import patch

import httpx
from openai import AsyncOpenAI
import linkreaper_AI as backend


async def benchmark(sample, subject, case, system_certificates=False):
    raw = sample.read_bytes()
    frame = backend.pd.DataFrame(json.loads(raw))
    runs = []
    outputs = []

    def factory(**kwargs):
        if system_certificates:
            kwargs["http_client"] = httpx.AsyncClient(verify=ssl.create_default_context())
        return AsyncOpenAI(**kwargs)

    for concurrency in [1, 5]:
        tracker = backend.ProgressTracker()
        started = time.perf_counter()

        def report(phase, message, completed, total, **details):
            if completed % 10 == 0 or completed == total:
                print(f"concurrency={concurrency}: {completed}/{total}", flush=True)

        with patch.object(backend, "OPENAI_MAX_CONCURRENCY", concurrency), \
                patch.object(backend, "AsyncOpenAI", side_effect=factory):
            output = await backend.filter_with_openai(frame, subject, case, tracker=tracker, report=report)
        elapsed = time.perf_counter() - started
        outputs.append(set(output["URL"]))
        runs.append({"concurrency": concurrency, "evaluated": len(frame), "kept": len(output),
                     "excluded": len(frame) - len(output), "seconds": round(elapsed, 2),
                     "links_per_minute": round(len(frame) * 60 / elapsed, 2), **tracker.get_stats()})

    return {"sample": str(sample), "sample_sha256": hashlib.sha256(raw).hexdigest(),
            "subject": subject, "case": case, "model": "gpt-6-luna", "reasoning": {"effort": "high"},
            "tools": [{"type": "web_search"}], "max_retries": backend.OPENAI_MAX_RETRIES,
            "system_certificates": system_certificates, "runs": runs,
            "speedup": round(runs[0]["seconds"] / runs[1]["seconds"], 2),
            "classification_differences": sorted(outputs[0] ^ outputs[1])}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sample", required=True, type=Path)
    parser.add_argument("--subject", default="Enron")
    parser.add_argument("--case", default="Frode contabile Enron e procedimenti giudiziari collegati allo scandalo.")
    parser.add_argument("--system-certificates", action="store_true")
    parser.add_argument("--output", type=Path, default=Path("tests/async-benchmark.json"))
    args = parser.parse_args()
    result = asyncio.run(benchmark(args.sample, args.subject, args.case, args.system_certificates))
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
