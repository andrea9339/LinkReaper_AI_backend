import csv
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

os.environ.setdefault("SERPAPI_KEY", "test-serpapi-not-a-real-key")
os.environ.setdefault("OPENAI_API_KEY", "test-openai-not-a-real-key")

import linkreaper_AI as backend


PAYLOAD = {"fixedPart": "Soggetto È", "keywords": "truffa", "additionalWords": "lunapop",
           "caseDescription": "Il soggetto è coinvolto nel caso."}


def recorded_google(params):
    if params["start"] != 0:
        return {"organic_results": []}
    if params["q"].endswith("truffa"):
        return {"organic_results": [
            {"title": "Processo duplicato", "link": "https://example.test/a"},
            {"title": "Truffa", "link": "https://example.test/c", "snippet": None},
        ]}
    return {"organic_results": [
        {"title": "Processo È", "snippet": "Il caso", "link": "https://example.test/a",
         "displayed_link": "https://www.example.test › articolo", "date": "ieri"},
        {"title": "Sport", "snippet": "Notizie sportive", "link": "https://example.test/sport"},
        {"title": "Musica", "snippet": "lunapop", "link": "https://example.test/custom"},
        {"title": "Indagine", "snippet": None, "link": "https://example.test/b"},
    ]}


class SearchTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        original_tempfile = tempfile.NamedTemporaryFile
        self.addCleanup(self.directory.cleanup)
        self.start_patch(patch.object(backend.tempfile, "NamedTemporaryFile",
                                     side_effect=lambda **kw: original_tempfile(dir=self.directory.name, **kw)))
        self.start_patch(patch.object(backend.tempfile, "gettempdir", return_value=self.directory.name))
        self.google_calls = []

        def google(params):
            self.google_calls.append(params.copy())
            return SimpleNamespace(get_dict=lambda: recorded_google(params))

        self.start_patch(patch.object(backend, "GoogleSearch", side_effect=google))
        self.start_patch(patch.object(backend.client.responses, "create",
                                     return_value=SimpleNamespace(output_text="SI")))
        self.start_patch(patch.object(backend, "RETRY_DELAY", 0))
        self.client = backend.app.test_client()

    def start_patch(self, mocker):
        value = mocker.start()
        self.addCleanup(mocker.stop)
        return value

    def read_csv(self, result):
        return Path(self.directory.name, result["file"]).read_text(encoding="utf-8")

    def stream(self, payload=PAYLOAD):
        response = self.client.post("/api/search-stream", json=payload)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "text/event-stream")
        events = [json.loads(record[6:]) for record in response.get_data(as_text=True).split("\n\n")
                  if record.startswith("data: ")]
        response.close()
        return events

    def test_json_stream_csv_parity_and_download(self):
        legacy = self.client.post("/api/search", json=PAYLOAD).get_json()
        events = self.stream()
        complete = events[-1]
        self.assertEqual(set(legacy), {"message", "file", "results_count"})
        self.assertEqual(events[0]["type"], "accepted")
        self.assertEqual(complete["type"], "complete")
        self.assertEqual(sum(e["type"] in ("complete", "error") for e in events), 1)
        self.assertEqual(self.read_csv(legacy), self.read_csv(complete))
        rows = list(csv.DictReader(io.StringIO(self.read_csv(complete))))
        self.assertEqual(list(rows[0]), backend.RESULT_COLUMNS)
        self.assertEqual([row["URL"] for row in rows], ["https://example.test/" + p
                                                       for p in ["a", "custom", "b", "c"]])
        self.assertEqual(rows[0]["Dominio"], "example.test")
        self.assertEqual(rows[0]["Query"], PAYLOAD["fixedPart"])
        downloaded = self.client.get("/api/download/" + complete["file"])
        self.addCleanup(downloaded.close)
        self.assertEqual(downloaded.status_code, 200)
        self.assertEqual(downloaded.get_data(), Path(self.directory.name, complete["file"]).read_bytes())
        downloaded.close()

    def test_google_counts_after_requests_and_existing_query_generation(self):
        updates = []

        def observe(event):
            if event["phase"] == "google":
                self.assertEqual(event["completed"], len(self.google_calls))
            updates.append(event)

        backend.run_search({**PAYLOAD, "keywords": " truffa,, "}, on_progress=observe)
        google = [e for e in updates if e["phase"] == "google"]
        self.assertEqual([e["completed"] for e in google], list(range(41)))
        self.assertTrue(all(e["total"] == 40 for e in google))
        self.assertEqual(google[-1]["query_index"], 4)
        self.assertEqual(google[-1]["page"], 10)
        self.assertEqual(self.google_calls[0]["engine"], "google_light")
        self.assertEqual([p["start"] for p in self.google_calls[:10]], list(range(0, 100, 10)))
        self.assertEqual([p["q"] for p in self.google_calls[::10]],
                         ['"Soggetto È"', '"Soggetto È" truffa', '"Soggetto È" ', '"Soggetto È" '])
        self.assertNotIn("cleanup", {e["phase"] for e in updates})
        self.assertEqual(updates[-1]["phase"], "csv")
        self.assertIsNone(updates[-1]["total"])

    def test_out_of_order_ai_retries_failures_and_row_order(self):
        attempts = {}
        lock = threading.Lock()

        def ai(**kwargs):
            url = kwargs["input"].split("  - URL: ")[1].splitlines()[0]
            self.assertEqual(kwargs["model"], "gpt-5-luna")
            self.assertEqual(kwargs["reasoning"], {"effort": "xhigh"})
            self.assertEqual(kwargs["tools"], [{"type": "web_search"}])
            with lock:
                attempts[url] = attempts.get(url, 0) + 1
                attempt = attempts[url]
            if url.endswith("/a"):
                time.sleep(0.03)
            if url.endswith("/b") or (url.endswith("/c") and attempt == 1):
                raise RuntimeError("429 rate limit: PRIVATE CASE AND KEY")
            return SimpleNamespace(output_text="NO" if url.endswith("custom") else "SI")

        with patch.object(backend.client.responses, "create", side_effect=ai):
            updates = []
            result = backend.run_search(PAYLOAD, on_progress=updates.append)
        ai_updates = [e for e in updates if e["phase"] == "ai"]
        self.assertEqual([e["completed"] for e in ai_updates], [0, 1, 2, 3, 4])
        self.assertTrue(all(e["total"] == 4 for e in ai_updates))
        self.assertEqual(ai_updates[-1]["stats"],
                         {"succeeded": 3, "failed": 1, "retried": 3, "rate_limit_errors": 4})
        self.assertEqual(result["ai_failures"], 1)
        rows = list(csv.DictReader(io.StringIO(self.read_csv(result))))
        self.assertEqual([r["URL"] for r in rows], ["https://example.test/" + p for p in ["a", "b", "c"]])

    def test_missing_description_skips_ai(self):
        with patch.object(backend.client.responses, "create") as ai:
            events = self.stream({**PAYLOAD, "caseDescription": "   "})
        ai.assert_not_called()
        skipped = [e for e in events if e.get("skipped")]
        self.assertEqual(skipped[0]["skipped"], "no_description")
        self.assertIsNone(skipped[0]["total"])
        self.assertEqual(events[-1]["ai_skipped"], "no_description")
        self.assertEqual(events[-1]["results_count"], 4)

    def test_empty_google_and_no_candidates_and_all_rejected(self):
        for google_result in [{}, {"organic_results": None}, {"organic_results": [
                {"title": "Sport", "snippet": None, "link": "https://example.test/empty"}]}]:
            with self.subTest(google_result=google_result), patch.object(
                    backend, "GoogleSearch", return_value=SimpleNamespace(get_dict=lambda: google_result)):
                events = self.stream()
                self.assertEqual(events[-1]["results_count"], 0)
                self.assertEqual(events[-1]["ai_skipped"], "no_candidates")
                self.assertEqual(self.read_csv(events[-1]), ",".join(backend.RESULT_COLUMNS) + "\n")
        with patch.object(backend.client.responses, "create", return_value=SimpleNamespace(output_text="NO")):
            result = backend.run_search(PAYLOAD)
            self.assertEqual(result["results_count"], 0)
            self.assertEqual(self.read_csv(result), ",".join(backend.RESULT_COLUMNS) + "\n")

    def test_invalid_payloads_rejected_before_work(self):
        for path in ["/api/search", "/api/search-stream"]:
            for payload in [None, [], {}, {**PAYLOAD, "fixedPart": " "},
                            {**PAYLOAD, "keywords": []}, {**PAYLOAD, "caseDescription": None}]:
                with self.subTest(path=path, payload=payload):
                    response = self.client.post(path, json=payload)
                    self.assertEqual(response.status_code, 400)
                    self.assertEqual(response.get_json()["code"], "invalid_request")
        self.assertEqual(len(self.google_calls), 0)

    def test_provider_errors_and_csv_errors_never_complete_or_leak(self):
        with patch.object(backend, "GoogleSearch", return_value=SimpleNamespace(
                get_dict=lambda: {"error": "PRIVATE KEY AND CASE"})):
            events = self.stream()
            self.assertEqual(events[-1]["type"], "error")
            self.assertNotIn("PRIVATE", json.dumps(events))
            self.assertEqual(self.client.post("/api/search", json=PAYLOAD).status_code, 500)
        with patch.object(backend.tempfile, "NamedTemporaryFile", side_effect=OSError("PRIVATE PATH")):
            events = self.stream()
            self.assertEqual(events[-1]["type"], "error")
            self.assertNotIn("PRIVATE", json.dumps(events))

    def test_success_sent_after_csv_is_closed(self):
        events = self.stream()
        result = events[-1]
        path = Path(self.directory.name, result["file"])
        # Windows refuses a rename while NamedTemporaryFile is still open.
        renamed = path.with_suffix(".ready.csv")
        path.rename(renamed)
        self.assertTrue(renamed.read_text(encoding="utf-8").startswith("Query,Posizione"))

    def test_heartbeat_and_disconnect_cancel_at_next_boundary(self):
        blocked = threading.Event()
        release = threading.Event()
        finished = threading.Event()
        captured = {}
        original_run = backend.run_search

        def slow_google(params):
            self.google_calls.append(params)
            blocked.set()
            release.wait(2)
            return {}

        def observe_run(data, on_progress, stop_event):
            captured["stopped"] = stop_event
            try:
                return original_run(data, on_progress=on_progress, stop_event=stop_event)
            finally:
                finished.set()

        with patch.object(backend, "HEARTBEAT_SECONDS", 0.02), patch.object(
                backend, "GoogleSearch", side_effect=lambda p: SimpleNamespace(get_dict=lambda: slow_google(p))), patch.object(
                backend, "run_search", side_effect=observe_run):
            response = self.client.post("/api/search-stream", json=PAYLOAD, buffered=False)
            iterator = iter(response.response)
            self.assertIn(b'"accepted"', next(iterator))
            self.assertIn(b'"completed": 0', next(iterator))
            self.assertTrue(blocked.wait(1))
            self.assertEqual(next(iterator), b": heartbeat\n\n")
            response.close()
            self.assertTrue(captured["stopped"].is_set())
            release.set()
            self.assertTrue(finished.wait(1))
        self.assertEqual(len(self.google_calls), 1)
        self.assertEqual(list(Path(self.directory.name).glob("*.csv")), [])

    def test_slow_consumer_does_not_block_terminal_event(self):
        finished = threading.Event()

        def fast_search(data, on_progress, stop_event):
            for i in range(2000):
                on_progress({"type": "progress", "phase": "google", "completed": i})
            finished.set()
            return {"message": "done", "file": "tmp_result.csv", "results_count": 0}

        with patch.object(backend, "run_search", side_effect=fast_search):
            response = self.client.post("/api/search-stream", json=PAYLOAD, buffered=False)
            iterator = iter(response.response)
            next(iterator)
            next(iterator)
            time.sleep(0.03)
            self.assertTrue(finished.wait(1))
            remaining = list(iterator)
            self.assertLessEqual(len(remaining), 33)
            self.assertIn(b'"complete"', remaining[-1])
            response.close()

    def test_overlapping_searches_keep_progress_and_outputs_separate(self):
        barrier = threading.Barrier(2)
        original_run = backend.run_search

        def synchronized_run(*args, **kwargs):
            barrier.wait(timeout=2)
            return original_run(*args, **kwargs)

        results = {}

        def request(subject):
            with backend.app.test_client() as client:
                response = client.post("/api/search-stream", json={**PAYLOAD, "fixedPart": subject})
                events = [json.loads(r[6:]) for r in response.get_data(as_text=True).split("\n\n")
                          if r.startswith("data: ")]
                response.close()
                results[subject] = events

        with patch.object(backend, "run_search", side_effect=synchronized_run):
            threads = [threading.Thread(target=request, args=(subject,)) for subject in ["Cliente A", "Cliente B"]]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=5)
                self.assertFalse(thread.is_alive())
        self.assertEqual(len(results), 2)
        self.assertNotEqual(results["Cliente A"][-1]["file"], results["Cliente B"][-1]["file"])
        for subject, events in results.items():
            self.assertEqual(events[-1]["type"], "complete")
            self.assertIn(subject, self.read_csv(events[-1]))
            self.assertNotIn("Cliente B" if subject == "Cliente A" else "Cliente A", self.read_csv(events[-1]))

    def test_progress_reporting_failure_does_not_change_output(self):
        def broken_report(event):
            raise RuntimeError("reader disconnected")

        result = backend.run_search({**PAYLOAD, "caseDescription": ""}, on_progress=broken_report)
        self.assertEqual(result["results_count"], 4)

    def test_disconnect_during_ai_prevents_queued_calls(self):
        entered = threading.Barrier(3)
        release = threading.Event()
        stopped = threading.Event()
        errors = []
        df = backend.pd.DataFrame([
            {"Titolo": "Processo", "Snippet": "Caso", "URL": f"https://example.test/{i}"}
            for i in range(8)
        ])

        def slow_ai(**kwargs):
            entered.wait(timeout=2)
            release.wait(timeout=2)
            return SimpleNamespace(output_text="SI")

        def run():
            try:
                backend.filter_with_openai_parallel(df, "Cliente", "Caso", max_workers=2, stop_event=stopped)
            except backend.SearchCancelled:
                errors.append("cancelled")

        with patch.object(backend.client.responses, "create", side_effect=slow_ai) as ai:
            thread = threading.Thread(target=run)
            thread.start()
            entered.wait(timeout=2)
            stopped.set()
            release.set()
            thread.join(timeout=2)
            self.assertFalse(thread.is_alive())
            self.assertEqual(ai.call_count, 2)
            self.assertEqual(errors, ["cancelled"])

    def test_health_cors_and_missing_download(self):
        health = self.client.get("/api/health")
        self.assertEqual(health.get_json()["search_stream_version"], 1)
        preflight = self.client.options("/api/search-stream", headers={
            "Origin": "https://preview.vercel.app", "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type"})
        self.assertEqual(preflight.status_code, 200)
        self.assertEqual(preflight.headers["Access-Control-Allow-Origin"], "https://preview.vercel.app")
        self.assertIn("POST", preflight.headers["Access-Control-Allow-Methods"])
        self.assertEqual(self.client.get("/api/download/missing.csv").status_code, 404)


if __name__ == "__main__":
    unittest.main()
