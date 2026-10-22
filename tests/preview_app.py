"""Isolated preview only: exercise the real routes without paid API calls.

Run: gunicorn tests.preview_app:app --bind 0.0.0.0:$PORT
Use fixedPart LONG_WAIT for a controlled 15-minute wait, EMPTY for no results,
or ERROR for a provider failure. Never use this module as the production app.
"""

import asyncio
from contextlib import asynccontextmanager
import os
import time
from types import SimpleNamespace
import httpx
from openai import APIConnectionError

from tests.test_search import backend, recorded_google


class PreviewGoogle:
    def __init__(self, params):
        self.params = params

    def get_dict(self):
        query = self.params["q"]
        if query == '"LONG_WAIT"' and self.params["start"] == 0:
            time.sleep(float(os.getenv("PREVIEW_WAIT_SECONDS", "905")))
        time.sleep(0.2)
        if "EMPTY" in query:
            return {}
        if query.startswith('"ERROR"'):
            return {"error": "Synthetic provider failure"}
        return recorded_google(self.params)


async def preview_ai(**kwargs):
    url = kwargs["input"].split("  - URL: ")[1].splitlines()[0]
    await asyncio.sleep({"a": 1, "custom": 2, "b": 3, "c": 4}[url.rsplit("/", 1)[-1]])
    if url.endswith("/b") and '**SOGGETTO:** "AI_ERROR"' in kwargs["input"]:
        raise APIConnectionError(request=httpx.Request("POST", "https://api.openai.com/v1/responses"))
    return SimpleNamespace(output_text="NO" if url.endswith("custom") else "SI")


backend.GoogleSearch = PreviewGoogle


@asynccontextmanager
async def preview_client(**kwargs):
    yield SimpleNamespace(responses=SimpleNamespace(create=preview_ai))


backend.AsyncOpenAI = preview_client
app = backend.app


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.getenv("PORT", "5055")), threaded=True)
