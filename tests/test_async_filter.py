"""Exercise the real SDK at its HTTP boundary, without paid requests."""

import asyncio
from collections import Counter
from contextlib import contextmanager
from email.utils import formatdate
import json
import os
import threading
import time
import unittest
from unittest.mock import patch

import httpx
from openai import AsyncOpenAI, RateLimitError

os.environ.setdefault("SERPAPI_KEY", "test-serpapi-not-a-real-key")
os.environ.setdefault("OPENAI_API_KEY", "test-openai-not-a-real-key")
import linkreaper_AI as backend


def answer(text):
    return httpx.Response(200, json={
        "id": "resp_test", "object": "response", "created_at": 0,
        "model": "gpt-6-luna", "status": "completed",
        "output": [{"type": "message", "id": "msg_test", "role": "assistant",
                    "status": "completed", "content": [
                        {"type": "output_text", "text": text, "annotations": []}]}],
    })


class AsyncFilterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.df = backend.pd.DataFrame([
            {"Titolo": f"Processo {i}", "Snippet": "Caso", "URL": f"https://example.test/{i}"}
            for i in range(12)
        ], index=[2, 2, 7, 11, 14, 20, 21, 25, 30, 31, 40, 100])
        self.sessions = []

    @contextmanager
    def transport(self, handler):
        def factory(**kwargs):
            self.assertEqual(kwargs["max_retries"], 0)
            session = AsyncOpenAI(**kwargs, http_client=httpx.AsyncClient(
                transport=httpx.MockTransport(handler)))
            self.sessions.append(session)
            return session

        with patch.object(backend, "AsyncOpenAI", side_effect=factory):
            yield

    async def test_concurrent_out_of_order_answers_match_sequential_csv(self):
        active = 0
        peak = 0
        calls = []
        finished = []

        async def handler(request):
            nonlocal active, peak
            body = json.loads(request.content)
            self.assertEqual(set(body), {"model", "reasoning", "tools", "input"})
            self.assertEqual(body["model"], "gpt-6-luna")
            self.assertEqual(body["reasoning"], {"effort": "high"})
            self.assertEqual(body["tools"], [{"type": "web_search"}])
            self.assertIn('- **SOGGETTO:** "Cliente È"', body["input"])
            self.assertIn('- **CASO:** "Caso specifico"', body["input"])
            position = int(body["input"].split("  - URL: https://example.test/")[1].splitlines()[0])
            calls.append(position)
            active += 1
            peak = max(peak, active)
            try:
                await asyncio.sleep(0.004 * (5 - position % 5))
                finished.append(position)
                return answer(" si, pertinente " if position % 2 == 0 else "NO")
            finally:
                active -= 1

        outputs = []
        for concurrency in [1, 5]:
            active = peak = 0
            calls.clear()
            finished.clear()
            counts = []
            with self.transport(handler), patch.object(backend, "OPENAI_MAX_CONCURRENCY", concurrency):
                output = await backend.filter_with_openai(self.df, "Cliente È", "Caso specifico",
                    report=lambda phase, message, completed, total, **kw: counts.append(completed))
            self.assertEqual(peak, concurrency)
            self.assertEqual(Counter(calls), Counter(range(12)))
            self.assertEqual(counts, list(range(1, 13)))
            self.assertEqual(output["URL"].tolist(), [f"https://example.test/{i}" for i in range(0, 12, 2)])
            self.assertEqual(output.index.tolist(), [2, 7, 14, 21, 30, 40])
            self.assertTrue(self.sessions[-1].is_closed())
            if concurrency == 5:
                self.assertNotEqual(finished, list(range(12)))
            outputs.append(output.to_csv(index=False))
        self.assertEqual(outputs[0], outputs[1])
        self.assertEqual(len(self.sessions), 2)

    async def test_api_failure_cancels_pending_requests_before_client_closes(self):
        attempts = Counter()
        cancelled = []
        tracker = backend.ProgressTracker()
        counts = []

        async def handler(request):
            url = json.loads(request.content)["input"].split("  - URL: ")[1].splitlines()[0]
            attempts[url] += 1
            if url.endswith("/0"):
                return httpx.Response(429, json={"error": {"message": "PRIVATE KEY AND CASE"}})
            try:
                await asyncio.sleep(30)
                return answer("SI")
            except asyncio.CancelledError:
                self.assertFalse(self.sessions[-1].is_closed())
                cancelled.append(url)
                raise

        with self.transport(handler), patch.object(backend, "retry_delay", return_value=0):
            with self.assertRaisesRegex(backend.OpenAIFilterError, "Riga 1.*4 tentativi.*Nessun nuovo CSV") as error:
                await backend.filter_with_openai(self.df, "Cliente", "Caso", tracker=tracker,
                    report=lambda phase, message, completed, total, **kw: counts.append(completed))
        self.assertNotIn("PRIVATE", str(error.exception))
        self.assertEqual(attempts["https://example.test/0"], 4)
        self.assertEqual(len(attempts), 5)
        self.assertEqual(len(cancelled), 4)
        self.assertEqual(counts, [1])
        self.assertEqual(tracker.get_stats(),
                         {"succeeded": 0, "failed": 1, "retried": 3, "rate_limit_errors": 4})
        self.assertTrue(self.sessions[-1].is_closed())

    async def test_local_error_is_not_retried_and_closes_client(self):
        calls = 0

        async def handler(request):
            nonlocal calls
            calls += 1
            # Invalid text reaches the local output parser after HTTP succeeds.
            return answer(123)

        with self.transport(handler), patch.object(backend, "OPENAI_MAX_CONCURRENCY", 1):
            with self.assertRaises(TypeError):
                await backend.filter_with_openai(self.df.iloc[:1], "Cliente", "Caso")
        self.assertEqual(calls, 1)
        self.assertTrue(self.sessions[-1].is_closed())

    async def test_retry_after_keeps_slot_and_does_not_block_other_requests(self):
        calls = Counter()
        delays = []
        other_completed = asyncio.Event()
        real_sleep = asyncio.sleep

        async def controlled_sleep(delay):
            delays.append(delay)
            await asyncio.wait_for(other_completed.wait(), timeout=1)
            # The third row cannot start while the first row holds its retry slot.
            self.assertNotIn("2", calls)

        async def handler(request):
            position = json.loads(request.content)["input"].split("https://example.test/")[1].splitlines()[0]
            calls[position] += 1
            if position == "0" and calls[position] == 1:
                return httpx.Response(429, headers={"Retry-After": "120"}, json={"error": {"message": "wait"}})
            if position == "1":
                await real_sleep(0.01)
                other_completed.set()
                # Keep row 1 occupied until row 0's retry has started.
                while calls["0"] < 2:
                    await real_sleep(0.001)
            return answer("SI")

        with self.transport(handler), patch.object(backend, "OPENAI_MAX_CONCURRENCY", 2), \
                patch.object(backend.asyncio, "sleep", side_effect=controlled_sleep):
            output = await backend.filter_with_openai(self.df.iloc[:3], "Cliente", "Caso")
        self.assertEqual(delays, [120])
        self.assertEqual(calls, {"0": 2, "1": 1, "2": 1})
        self.assertEqual(len(output), 3)

    async def test_disconnect_interrupts_inflight_calls_and_retry_waits(self):
        for retry in [False, True]:
            with self.subTest(retry=retry):
                stopped = threading.Event()
                entered = asyncio.Event()
                calls = 0

                async def handler(request):
                    nonlocal calls
                    calls += 1
                    entered.set()
                    if retry:
                        return httpx.Response(429, headers={"Retry-After": "120"}, json={"error": {"message": "wait"}})
                    await asyncio.sleep(30)
                    return answer("SI")

                with self.transport(handler), patch.object(backend, "OPENAI_MAX_CONCURRENCY", 1):
                    task = asyncio.create_task(backend.filter_with_openai(
                        self.df, "Cliente", "Caso", stop_event=stopped))
                    await asyncio.wait_for(entered.wait(), timeout=1)
                    stopped.set()
                    with self.assertRaises(backend.SearchCancelled):
                        await asyncio.wait_for(task, timeout=1)
                self.assertEqual(calls, 1)
                self.assertTrue(self.sessions[-1].is_closed())

    async def test_direct_task_cancellation_drains_requests(self):
        entered = asyncio.Event()
        cancelled = 0

        async def handler(request):
            nonlocal cancelled
            entered.set()
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                cancelled += 1
                raise

        with self.transport(handler):
            task = asyncio.create_task(backend.filter_with_openai(self.df, "Cliente", "Caso"))
            await entered.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(cancelled, 5)
        self.assertTrue(self.sessions[-1].is_closed())

    async def test_empty_and_invalid_configuration_do_not_open_client(self):
        with patch.object(backend, "AsyncOpenAI") as client:
            output = await backend.filter_with_openai(self.df.iloc[:0], "Cliente", "Caso")
            self.assertTrue(output.empty)
            self.assertEqual(output.columns.tolist(), self.df.columns.tolist())
            for name, values in [("OPENAI_MAX_CONCURRENCY", [0, -1, 1.5, True]),
                                 ("OPENAI_MAX_RETRIES", [-1, 1.5, True])]:
                for value in values:
                    with patch.object(backend, name, value), self.assertRaises(ValueError):
                        await backend.filter_with_openai(self.df, "Cliente", "Caso")
            client.assert_not_called()

    def test_retry_after_numbers_dates_and_invalid_values(self):
        for header, expected in [("120", 120), (formatdate(1120, usegmt=True), 120),
                                 ("invalid", 2), ("NaN", 2), ("inf", 2), ("-1", 2)]:
            error = RateLimitError("wait", response=httpx.Response(429, headers={"Retry-After": header},
                request=httpx.Request("POST", "https://api.openai.com/v1/responses")), body=None)
            with patch.object(backend.random, "uniform", return_value=0), \
                    patch.object(backend.time, "time", return_value=1000):
                self.assertEqual(backend.retry_delay(error, 2), expected)


if __name__ == "__main__":
    unittest.main()
