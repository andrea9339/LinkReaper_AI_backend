# Validation — 15 October 2026

## Sequential AI update

All 16 backend tests passed with the pinned production dependencies. The AI
filter now makes every call on the search's calling thread, in input order,
without a worker pool. The 1,000-link check verifies call order, output order,
and counters. Separate checks verify that retries finish before the next link,
AI failures retain the link, rejected links are removed, and cancellation
prevents the next call. Heartbeats continue while an AI call is waiting, with
the completed count unchanged. JSON/stream CSV parity and the existing Google,
validation, queue, and download checks also pass.

A single real Responses API request used the updated backend's exact request
payload with a synthetic Example Domain case and the production OpenAI
credential, kept out of test fixtures and reports. The request used
`gpt-6-luna`, reasoning effort `high`, and `web_search`. It returned HTTP 200,
status `completed`, and `SI` after 16.1 seconds. Model access checks returned
HTTP 404 (`model_not_found`) for the previous `gpt-5-luna` setting and HTTP 200
for `gpt-6-luna`. This verifies access and one successful assessment; it does
not measure filtering quality across customer cases. No Google search quota
was consumed by this live API check.

## Earlier streaming validation

The checks below predate the sequential AI update. Their long-wait measurements
remain evidence for the streaming transport, not a timing measurement of the
updated AI filter.

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
