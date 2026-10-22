# This is the **backend** developement for the new linkreaper with AI filtering

## Enviroment variables
The APIs used are protected with  ***environment variables***
I create an enviromental variable  to secure serpapi and openai on the web
```
api_key = os.getenv('SERPAPI_KEY')
openai_api_key = os.getenv('OPENAI_API_KEY')
```

And put the key-value on the Render environment variables.

Then I deployed my flask app on a cloud hosting platform (render)

## Requirements issues

In the deployment process usually the problem is with the **requirements.txt**, specifically compatibility between the packages and python version on Render.
So, to solve the problem check with chatGPT which version of the packages are compatible with the python version used by Render and you are good to go.

## Gunicorn settings

I’ve put the gunicorn code line since gunicorn provides us the WSGI server(you have to put the name of the app in the folder). So, if the name in the folder is like “**Linkreaper**.py”, you should write:
```
gunicorn Linkreaper:app
```

## Update frontend URL

after succefully deploying the flask app on render, I finally have the link of my web app, so to connect it to my frontend, I’ve simply replaced the link: 

```https://linkreaper-deploy.onrender.com/api/:path*```

instead of the local host:

```http://localhost:5000/api/:path*```

in my ***next.config.ts*** file in the frontend folder.

## Update backend URL

once I’ve deployed the frontend on vercel,
I’ve updated in my flask app the CORS origin to match new Vercel app URL:
```
CORS(app, resources={r"/*": {"origins": "https://link-reaper.vercel.app/"}})
```
And push on render again to the updated commit.

## Live search progress

`POST /api/search-stream` accepts the same JSON fields as `POST /api/search`:
`fixedPart`, `keywords`, and optional `additionalWords` and `caseDescription`.
It sends JSON SSE records with `type` values `accepted`, `progress`, `complete`,
or `error`. Google page counts advance after each response has been handled.
AI counts advance once per completed link, after retries. Cleanup and keyword
filtering still run, without a separate progress phase.

SerpAPI can return a successful Google page with no results and an `error`
message explaining that nothing was found. Pages with
`search_metadata.status: "Success"` are handled normally, including empty
pages, so other queries and collected results are preserved. Failed provider
responses still stop the search. Logs record the query index, page, and a
validated archive search ID when available, without logging query text or keys.

AI filtering uses one `AsyncOpenAI` client per search and up to five concurrent
link assessments. `OPENAI_MAX_CONCURRENCY` controls this limit, including rows
waiting between retries. Set it to `1` for a sequential comparison. Model
`gpt-6-luna`, reasoning effort `high`, `web_search`, prompt, and answer parsing
remain unchanged. Decisions are applied by row position, preserving CSV order.

`OPENAI_MAX_RETRIES = 3` permits four API attempts per row. SDK retries are
disabled. API errors use asynchronous exponential backoff with jitter and honor
`Retry-After`, including waits longer than 60 seconds. Local programming errors
and cancellation are not retried. Exhausted attempts stop the search with code
`ai_filter_failed`, cancel pending tasks, and close the client. No new CSV is
written. Previously downloaded files remain valid.

The progress bar measures the current phase: Google search, AI analysis, or CSV
preparation. CSV preparation is indeterminate. `complete` is emitted after the
CSV file has closed. It includes the existing result fields, total elapsed
seconds, separate `ai_elapsed_seconds`, and any AI skip reason. Successful searches
report `ai_failures: 0`. AI timing, throughput, evaluated/kept/excluded counts,
concurrency, and retry statistics are also logged without case data or keys.
The legacy JSON endpoint and CSV download keep their existing response format.

Each request owns its queue, counters, and cancellation event. The queue keeps
at most 32 pending updates; slow readers cannot block the search. Heartbeat
comments are sent every 12 seconds while external calls are pending. Closing
the connection stops Google work at safe boundaries. During AI filtering, a
monitor checks the stop event every 100 ms, cancels local HTTP requests and retry
waits, then drains tasks before closing the client. Already submitted requests
may continue at the provider. There is no refresh
recovery or automatic resubmission.

### Render configuration

The repository's `gunicorn.conf.py` is loaded automatically by Gunicorn when
started from the repository root. It uses one `gthread` worker and four request
threads, sized for one active client plus downloads and health checks. Each
search owns an asyncio event loop inside its synchronous worker and evaluates
up to five AI links concurrently. The limit belongs to each search, rather than
the entire service. Gunicorn 23's main threaded loop reports worker liveness
independently of a long-running request.

The existing Render start command can stay:

```sh
gunicorn linkreaper_AI:app --bind 0.0.0.0:$PORT --timeout 400 --graceful-timeout 30
```

Its explicit timeout overrides the configuration's default. The threaded worker
allows a request to exceed that duration while the worker remains responsive.
`GET /api/health` returns the streaming protocol version without calling paid
APIs. Restarts and deployments still interrupt active searches; CSV files retain
the existing temporary-storage lifetime.

### Tests and isolated preview

```sh
python -m unittest discover -s tests -v
```

Tests use synthetic Google and AI responses, covering CSV parity, counts,
concurrent AI calls, retry budgets, fail-fast errors, empty results, input errors,
heartbeats during Google and AI calls, disconnected readers, bounded queues,
and independent progress for overlapping requests.
They do not consume API quota.

For an isolated test service only, run
`gunicorn tests.preview_app:app --bind 0.0.0.0:$PORT`. This module replaces both
external clients with synthetic responses. `fixedPart: "LONG_WAIT"` waits 905
seconds before the first Google page completes; `EMPTY` returns no results and
`ERROR` simulates a provider error. `AI_ERROR` exhausts the AI retry budget.
Never use this module for production.


### Paid performance comparison

`tests/fixtures/ai_benchmark_enron.json` contains 100 distinct public Google
results about Enron, collected once for the performance check. The benchmark
reuses the same sample for concurrency one and five and does not call SerpAPI.
It requires the usual API environment variables and consumes OpenAI quota:

```sh
python -m tests.benchmark_ai --sample tests/fixtures/ai_benchmark_enron.json
```

On a Windows machine requiring the OS certificate store, append
`--system-certificates`. TLS verification remains enabled. The report records
sample hash, duration, throughput, retries, classification differences, and
measured speedup in `tests/async-benchmark.json`. Model outputs can vary between
runs. A result on this public sample does not establish classification quality
or latency for customer cases.
