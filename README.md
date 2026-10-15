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

AI filtering evaluates one link at a time, in input order. It uses `gpt-6-luna`
with reasoning effort `high` and the `web_search` tool, matching the reference
script's model settings. Retries finish before the next link starts. A link is
retained if its AI assessment fails after all retries; the completion event
reports how many links could not be verified.

The progress bar measures the current phase: Google search, AI analysis, or CSV
preparation. CSV preparation is indeterminate. `complete` is emitted after the
CSV file has closed. It includes the existing result fields, elapsed seconds,
the number of links retained after final AI failures, and any AI skip reason.
The legacy JSON endpoint and CSV download keep their existing response format.

Each request owns its queue, counters, and cancellation event. The queue keeps
at most 32 pending updates; slow readers cannot block the search. Heartbeat
comments are sent every 12 seconds while external calls are pending. Closing
the connection stops work at safe boundaries and prevents subsequent AI calls from
starting; external calls already running can still finish. There is no refresh
recovery or automatic resubmission.

### Render configuration

The repository's `gunicorn.conf.py` is loaded automatically by Gunicorn when
started from the repository root. It uses one `gthread` worker and four request
threads, sized for one active client plus downloads and health checks. Each
search evaluates its AI links sequentially. Gunicorn 23's main threaded loop reports worker
liveness independently of a long-running request.

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
sequential AI calls, retries and fallback, empty results, input errors,
heartbeats during Google and AI calls, disconnected readers, bounded queues,
and independent progress for overlapping requests.
They do not consume API quota.

For an isolated test service only, run
`gunicorn tests.preview_app:app --bind 0.0.0.0:$PORT`. This module replaces both
external clients with synthetic responses. `fixedPart: "LONG_WAIT"` waits 905
seconds before the first Google page completes; `EMPTY` returns no results and
`ERROR` simulates a provider error. Never use this module for production.

