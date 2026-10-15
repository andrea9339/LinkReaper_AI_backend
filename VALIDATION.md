# Validation — 15 October 2026

All 15 backend tests passed with the pinned production dependencies. The suite
includes 1,000 AI candidates, exactly 20 concurrent workers, accurate completion
counts, retained row ordering, retries, fallback, empty output, safe failures,
bounded queues, cancellation, and overlapping requests with independent counters.

The JSON endpoint was compared against revision `8919362` using identical
synthetic responses. CSV bytes, row order, Google request parameters, AI prompts,
and model settings matched for normal filtering, final AI failure, missing case
description, comma-separated queries with empty entries, and zero results.

An isolated Render Free service in Frankfurt ran `tests.preview_app:app` with
Gunicorn 23.0.0 and the repository's `gthread` configuration. The test held the
first Google call for 905 seconds. The browser received 75 heartbeats throughout
the wait; the maximum gap between chunks was 12.112 seconds. Google completion
stayed at zero until the call returned. The complete search lasted 924.094
seconds, then produced a downloadable three-row CSV, including one AI-fallback
link. All 46 concurrent health checks passed; the slowest took 118 ms.

The recorded event counts and timings are in
[`tests/long-stream-validation.json`](tests/long-stream-validation.json).
The tested application source is revision `b993534`; subsequent changes add
tests and this report, without changing the production pipeline.

External API responses were synthetic. These checks establish application and
streaming behavior, rather than checking production API credentials or provider
availability. Cleanup and keyword filtering still run without progress events.
