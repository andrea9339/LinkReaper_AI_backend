# Async AI validation — 22 October 2026

The backend now evaluates up to five links concurrently using `AsyncOpenAI`.
The earlier sequential and fallback behavior described below is historical.
The current filter stops without producing a CSV when a row exhausts four API
attempts. The Flask JSON and SSE routes and the CSV schema remain compatible.

## Automated checks

All 27 backend tests passed with Python 3.12.6, OpenAI 2.15.0, and pandas 2.3.3.
The new tests exercise the actual SDK through a synthetic HTTP transport.
They verify overlapping requests, a maximum of five active rows, sequential
and concurrent CSV parity, duplicate and nonconsecutive indices, completions
out of order, success on the fourth attempt, and failure after four attempts.

Additional checks cover pending-task cancellation before client closure,
disconnection during an HTTP request and a retry wait, direct task cancellation,
local parser errors without retry, an uncapped 120-second `Retry-After` under a
controlled clock, HTTP-date retry headers, invalid configuration, empty input,
and no-description searches. Failed JSON and SSE searches produce no new CSV
and preserve an existing file. An AST comparison with revision `32d82d2`
confirmed that the full prompt is unchanged.

## Real API benchmark

The sample contains 100 distinct public Google results about Enron, collected
once through SerpAPI and stored in `tests/fixtures/ai_benchmark_enron.json`.
Both runs reused those same rows without repeating Google searches.
Parameters were `gpt-6-luna`, reasoning `high`, `web_search`, the original prompt,
three application retries, and zero SDK retries. Certificate verification used
the Windows trust store for this local test. Production transport was unchanged.

| Concurrency | Evaluated | AI duration | Links/minute | Kept | Excluded | Errors | Retries |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | 100 | 227.62 s | 26.36 | 100 | 0 | 0 | 0 |
| 5 | 100 | 48.10 s | 124.74 | 100 | 0 | 0 | 0 |

Measured speedup was **4.73×**. The concurrent run completed all 100 assessments
within the 180-second target. Neither run encountered rate limits or changed
classification between runs. These timings cover the full AI filter, including
client lifecycle, and exclude Google collection time.

This is a performance measurement on one sample. All links in the sample were
classified as relevant, so it does not establish rejection quality or latency
for customer cases. Mocked tests separately verify positive and negative
answers and their assignment to the correct rows. The raw benchmark report,
sample hash, counters, and classification comparison are in
[`tests/async-benchmark.json`](tests/async-benchmark.json).

Reproduce the paid test with API environment variables configured:

```sh
python -m tests.benchmark_ai --sample tests/fixtures/ai_benchmark_enron.json
```

Append `--system-certificates` if this Windows environment requires its OS
certificate store. This retains TLS verification and does not change backend
configuration. The benchmark is opt-in and is not run by unittest discovery.

## Frontend and review

All 11 frontend parser tests, TypeScript checking, and the Next.js 16.0.10
production build passed. Chromium exercised the production frontend against
an isolated local backend using asynchronous synthetic provider responses.
Google and AI counts were displayed, success followed CSV completion, and the
browser saved a 245-byte CSV containing the expected header and three ordered
rows. An exhausted AI retry budget displayed the safe server error with no
success message or download button.

The local frontend build used `NEXT_PUBLIC_BACKEND_URL=http://127.0.0.1:5055`
and `NEXT_PUBLIC_ENABLE_SEARCH_PROGRESS=true`. These were command-scoped test
values. No production environment files or deployment settings were changed.

## Standards

Independent Matt Pocock review found no documented-standard violations or
justified code-smell findings in the implementation.

## Spec

Independent review found no missing or incorrect behavior against
`MODIFICHE_FILTRO_AI_ASYNC.md` and the supporting plan. CLI-specific output and
progress handling were adapted to the existing Flask routes and download flow.

Review totals: Standards 0 findings. Spec 0 findings.

---

# Validation — 15 October 2026

## Successful empty Google pages

The affected Google Light response was retrieved from SerpAPI's cache and
archive. It had `search_metadata.status: "Success"`, no `organic_results`,
and the message `Google Light hasn't returned any results for this query.`
The earlier guard treated every nonempty `error` field as a failed request,
stopping the search before AI filtering. The account had available quota;
the diagnostic replay used cached responses and left the quota unchanged.

The backend now respects the provider's successful status, counts empty pages,
and continues the remaining queries. Actual provider errors still stop work.
Safe failure logs include the query index, page, validated archive search ID,
and exception type, without query text, credentials, or raw provider messages.

All 18 backend tests passed. The regression first failed against the previous
guard, then passed after the fix. It covers Google and Google Light empty-page
messages, a spelling-suggestion state, preserved results from earlier and later
queries, page counts, JSON/stream CSV parity, and entirely empty output.
Additional checks confirm that failed or unknown provider responses still
produce an error and do not leak provider data into events or logs.

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
