from flask import Flask, Response, request, jsonify, send_file
from flask_cors import CORS
import os
import pandas as pd
from serpapi import GoogleSearch
from openai import APIError, AsyncOpenAI
import asyncio
import math
import random
from email.utils import parsedate_to_datetime
import tempfile
import threading
import time
import json
import queue

app = Flask(__name__)

# Restrict CORS to allow only the frontend on Vercel
CORS(
    app,
    resources={r"/api/*": {"origins": "*"}},
    allow_headers=["Content-Type", "Authorization"],
    methods=["GET", "POST", "OPTIONS"],
)

# Retrieve API keys from environment variables
api_key = os.getenv('SERPAPI_KEY')
openai_api_key = os.getenv('OPENAI_API_KEY')

if not api_key:
    raise ValueError("Error: SERPAPI_KEY environment variable is not set!")

if not openai_api_key:
    raise ValueError("Error: OPENAI_API_KEY environment variable is not set!")

OPENAI_MAX_CONCURRENCY = 5
OPENAI_MAX_RETRIES = 3
HEARTBEAT_SECONDS = 12
RESULT_COLUMNS = ["Query", "Posizione", "Titolo", "Snippet", "URL", "Dominio", "Data"]


class SearchCancelled(Exception):
    pass


class OpenAIFilterError(RuntimeError):
    """An API failure prevents exporting a fully assessed result."""


def validate_openai_configuration():
    if type(OPENAI_MAX_CONCURRENCY) is not int or OPENAI_MAX_CONCURRENCY < 1:
        raise ValueError("OPENAI_MAX_CONCURRENCY deve essere un intero positivo.")
    if type(OPENAI_MAX_RETRIES) is not int or OPENAI_MAX_RETRIES < 0:
        raise ValueError("OPENAI_MAX_RETRIES deve essere un intero non negativo.")


def retry_delay(error, attempt):
    """Exponential backoff with jitter and an uncapped Retry-After minimum."""
    delay = 2 ** (attempt - 1) + random.uniform(0, 0.5)
    response = getattr(error, "response", None)
    header = response.headers.get("Retry-After") if response is not None else None
    if header is not None:
        try:
            retry_after = float(header)
        except ValueError:
            try:
                retry_after = parsedate_to_datetime(header).timestamp() - time.time()
            except (TypeError, ValueError, OverflowError):
                retry_after = 0
        if math.isfinite(retry_after) and retry_after >= 0:
            delay = max(delay, retry_after)
    return delay


def check_cancelled(stop_event):
    if stop_event is not None and stop_event.is_set():
        raise SearchCancelled()


# Counters belong to one search and are updated on its event-loop thread.
class ProgressTracker:
    def __init__(self):
        self.succeeded = 0
        self.failed = 0
        self.retried = 0
        self.rate_limit_errors = 0

    def increment_success(self):
        self.succeeded += 1

    def increment_failure(self):
        self.failed += 1

    def increment_retry(self):
        self.retried += 1

    def increment_rate_limit(self):
        self.rate_limit_errors += 1

    def get_stats(self):
        return {
            'succeeded': self.succeeded,
            'failed': self.failed,
            'retried': self.retried,
            'rate_limit_errors': self.rate_limit_errors
        }


async def is_relevant_with_openai(client, title, snippet, url, fixed_part, case_description):
    """Evaluate one link, preserving the existing request and answer parser."""
    prompt = f"""
Developer: # Ruolo e Obiettivo
Sei un assistente legale incaricato di valutare la pertinenza di un articolo online rispetto a un caso.

## Obiettivo
Devi decidere se un link è rilevante e potenzialmente lesivo per il SOGGETTO relativamente al CASO fornito.
Utilizza i dati del link (titolo, snippet) per prendere la decisione.
Nota: un contenuto resta potenzialmente lesivo anche se il SOGGETTO è stato assolto o archiviato.

## Dati
- **SOGGETTO:** "{fixed_part}"
- **CASO:** "{case_description}"
- **Dati del link:**
  - Titolo: {title}
  - Snippet: {snippet}
  - URL: {url}

## Domanda
Il contenuto di questo link è POTENZIALMENTE rilevante per il caso descritto?

## Regole di Valutazione
- Rispondi "SI" se:
  1. Il risultato si riferisce con alta probabilità allo STESSO soggetto del CASO (persona o società).
  2. Collega il SOGGETTO al CASO oppure a fatti negativi/giudiziari/reputazionali coerenti col CASO.
- Rispondi "NO" se:
  - Il risultato riguarda chiaramente un altro SOGGETTO (es. omonimo, cognome uguale ma nome diverso, altra società).
  - Non ha collegamento con il CASO.
- Se non sei certo (confidenza < 0,9) che si tratti dello stesso SOGGETTO usando solo titolo e snippet, DEVI aprire l'URL e leggere la pagina.
- Se non sei certo (confidenza < 0,9) che sia relativo al CASO usando solo titolo e snippet, DEVI aprire l'URL e leggere la pagina.

## Output
Rispondi SOLO con:
SI
NO
"""
    response = await client.responses.create(
        model="gpt-6-luna",
        reasoning={"effort": "high"},
        tools=[{"type": "web_search"}],
        input=prompt
    )
    answer = response.output_text.strip().upper()
    return answer.startswith("SI")


async def filter_with_openai(df, fixed_part, case_description, report=None, stop_event=None, tracker=None):
    """Assess bounded concurrent rows and apply decisions in original row order."""
    validate_openai_configuration()
    check_cancelled(stop_event)
    if df.empty:
        return df.copy()

    tracker = tracker or ProgressTracker()
    started = time.monotonic()
    semaphore = asyncio.Semaphore(OPENAI_MAX_CONCURRENCY)
    keep_flags = [False] * len(df)
    max_attempts = OPENAI_MAX_RETRIES + 1
    failure = None

    async with AsyncOpenAI(api_key=openai_api_key, max_retries=0) as client:
        async def evaluate(position, row):
            nonlocal failure
            # Keep the slot during retries too: at most five active rows.
            async with semaphore:
                if failure is not None:
                    raise failure
                for attempt in range(1, max_attempts + 1):
                    check_cancelled(stop_event)
                    try:
                        keep = await is_relevant_with_openai(
                            client, row.Titolo, row.Snippet, row.URL, fixed_part, case_description)
                    except APIError as error:
                        if getattr(error, "status_code", None) == 429:
                            tracker.increment_rate_limit()
                        if attempt == max_attempts:
                            tracker.increment_failure()
                            message = (f"Riga {position + 1}: {type(error).__name__}. "
                                       f"Esauriti {max_attempts} tentativi. Nessun nuovo CSV prodotto.")
                            print(message)
                            failure = OpenAIFilterError(message)
                            raise failure from None
                        tracker.increment_retry()
                        delay = retry_delay(error, attempt)
                        print(f"Riga {position + 1}: {type(error).__name__}. "
                              f"Retry {attempt}/{OPENAI_MAX_RETRIES}, "
                              f"tentativo {attempt + 1}/{max_attempts} fra {delay:.2f} s.")
                        await asyncio.sleep(delay)
                    else:
                        check_cancelled(stop_event)
                        tracker.increment_success()
                        return position, keep

        coordinator = asyncio.current_task()

        async def watch_disconnect():
            # The SSE generator sets a threading.Event from another thread.
            while not stop_event.is_set():
                await asyncio.sleep(0.1)
            coordinator.cancel()

        tasks = [asyncio.create_task(evaluate(position, row))
                 for position, row in enumerate(df.itertuples(index=False))]
        watcher = asyncio.create_task(watch_disconnect()) if stop_event is not None else None
        try:
            for completed, result in enumerate(asyncio.as_completed(tasks), start=1):
                try:
                    position, keep = await result
                except OpenAIFilterError:
                    if report:
                        report("ai", "Analisi AI interrotta: nessun nuovo CSV prodotto.",
                               completed, len(df), stats=tracker.get_stats())
                    raise
                check_cancelled(stop_event)
                keep_flags[position] = keep
                if report:
                    report("ai", "Analisi AI in corso", completed, len(df), stats=tracker.get_stats())
        except asyncio.CancelledError:
            check_cancelled(stop_event)
            raise
        finally:
            # Drain every task before closing the shared HTTP client.
            pending = tasks + ([watcher] if watcher is not None else [])
            for task in pending:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)

    elapsed = time.monotonic() - started
    throughput = len(df) * 60 / elapsed if elapsed > 0 else 0
    stats = tracker.get_stats()
    print(f"AI: {len(df)} valutati, {sum(keep_flags)} mantenuti, {len(df) - sum(keep_flags)} esclusi. "
          f"Concorrenza: {OPENAI_MAX_CONCURRENCY}. Durata: {elapsed:.2f} s. "
          f"Link/min: {throughput:.1f}. "
          f"Retry: {stats['retried']}. Rate limit: {stats['rate_limit_errors']}.")
    return df.iloc[keep_flags].copy()


def read_search_data():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise ValueError("Invia i parametri di ricerca in formato JSON.")
    for name in ("fixedPart", "keywords", "additionalWords", "caseDescription"):
        value = data.get(name, "" if name in ("additionalWords", "caseDescription") else None)
        if not isinstance(value, str):
            raise ValueError(f"Il campo {name} deve essere un testo.")
    if not data["fixedPart"].strip():
        raise ValueError("Inserisci il nome del soggetto da cercare.")
    return data


def run_search(data, on_progress=None, stop_event=None):
    started = time.monotonic()

    def report(phase, message, completed=None, total=None, **details):
        if on_progress is not None:
            try:
                on_progress({"type": "progress", "phase": phase, "message": message,
                             "completed": completed, "total": total,
                             "elapsed_seconds": round(time.monotonic() - started, 1), **details})
            except Exception:
                app.logger.warning("Progress update could not be delivered.")

    check_cancelled(stop_event)
    fixed_part = data['fixedPart']
    keywords = data['keywords'].split(',')
    additional_words = data.get('additionalWords', '').split(',')
    case_description = data.get('caseDescription', '').strip()

    fixed_part_with_quotes = f'"{fixed_part}"'
    queries = [fixed_part_with_quotes] + [
        f"{fixed_part_with_quotes} {keyword.strip()}" for keyword in keywords
    ]

    # Keep columns defined even when queries return no organic results
    all_results_df = pd.DataFrame(columns=RESULT_COLUMNS)
    total_pages = len(queries) * 10
    completed_pages = 0
    report("google", "Ricerca Google in corso", 0, total_pages,
           query_index=1, query_total=len(queries), page=1)

    # Search up to 10 pages per query, stopping at the first empty page.
    for query_index, query in enumerate(queries, start=1):
        data_rows = []

        for page in range(10):  # 10 pages → results 1–100
            check_cancelled(stop_event)
            params = {
                "engine": "google_light",
                "q": query,
                "location": "Italy",
                "hl": "it",
                "gl": "it",
                "api_key": api_key,
                "start": page * 10,
                "num": 10
            }

            search = GoogleSearch(params)
            results = search.get_dict()
            check_cancelled(stop_event)
            metadata = results.get("search_metadata") or {}
            provider_status = metadata.get("status")
            # SerpAPI also includes an error message on successful empty pages.
            if provider_status == "Error" or (results.get("error") and provider_status != "Success"):
                search_id = metadata.get("id", "")
                if not (isinstance(search_id, str) and len(search_id) == 24
                        and all(c in "0123456789abcdef" for c in search_id)):
                    search_id = "unknown"
                app.logger.error("Google search failed (query_index=%d, page=%d, search_id=%s).",
                                 query_index, page + 1, search_id)
                raise RuntimeError("The search provider returned an error.")
            organic_results = results.get("organic_results") or []

            for i, result in enumerate(organic_results, start=1):
                progressive_position = page * 10 + i

                data_rows.append({
                    "Query": query,
                    "Posizione": progressive_position,
                    "Titolo": result.get("title"),
                    "Snippet": result.get("snippet"),
                    "URL": result.get("link"),
                    "Dominio": result.get("displayed_link"),
                    "Data": result.get("date")
                })
            completed_pages += 1
            if not organic_results:
                total_pages -= 9 - page  # Remove pages this query will not request.
            report("google", "Ricerca Google in corso", completed_pages, total_pages,
                   query_index=query_index, query_total=len(queries), page=page + 1)
            if not organic_results:
                break

        # Create and sort dataframe
        query_df = pd.DataFrame(data_rows, columns=RESULT_COLUMNS)
        query_df = query_df.sort_values(by="Posizione").reset_index(drop=True)

        # Append
        all_results_df = pd.concat([all_results_df, query_df], ignore_index=True)

    check_cancelled(stop_event)
    # Cleanup and keyword filtering intentionally have no separate progress phase.
    # Clean the 'Dominio' column
    all_results_df['Dominio'] = all_results_df['Dominio'].apply(
        lambda x: x.split('›')[0]
        .replace('https://', '')
        .replace('http://', '')
        .replace('www.', '')
        .strip() if isinstance(x, str) else x
    )

    # Remove quotes from Query
    all_results_df['Query'] = all_results_df['Query'].str.replace('"', '')

    # Remove duplicates by URL
    all_results_df = all_results_df.drop_duplicates(subset='URL', keep='first').reset_index(drop=True)

    # Keywords list
    words = [
        "interdittiva", "processo", "processi", "indagine", "indagini", "udienza", "udienze",
        "peculato", "corruzione", "arresto", "condanna", "condanne", "giudice", "giudici",
        "tribunale", "tribunali", "droga", "riciclaggio", "mafia", "mafioso", "camorra", "ndrangheta",
        "truffa", "truffe", "frode", "frodi", "reato", "reati", "carcere", "detenuto",
        "detenuti", "ricettazione", "reclusione", "condannata", "condannato", "condannati", "calunnia",
        "calunnie", "arrestato", "arrestati", "arresti", "Diffamazione", "Concussione",
        "furto", "furti", "rapina", "rapine", "tributario", "tributaria", "penale", "penali",
        "giudiziario", "giudiziaria", "sessuale", "sessuali", "fallimento", "fallimenti",
        "Contraffazione", "Copyright", "Stalking", "Abusivismo", "Bancarotta", "omicidio",
        "omicidi", "omicida", "Danneggiamento", "Ricettazione", "Estorsione", "estorsioni",
        "armi", "Arma", "prostituzione", "sentenza", "sentenze", "deposizione", "deposizioni",
        "inchiesta", "inchieste", "sequestro", "sequestri", "arrestata", "denunciato",
        "denunciati", "denunciata", "assoluzione", "assolto", "assolta", "assoluzioni",
        "assolti", "assolte", "indagato", "indagata", "illecito", "illeciti", "illecite",
        "antimafia", "giudizio", "sequestrato", "sequestrata", "querelare", "querela",
        "querelato", "querelata", "querelati", "querelate", "perquisito", "perquisita",
        "perquisiti", "perquisite", "perquisizione", "perquisizioni", "evasione", "evasioni",
        "associazione a delinquere", "galera", "fraudolenta", "tangente", "tangenti", "patteggia",
        "patteggiamento", "patteggiamenti", "turbativa", "intercettazione", "intercettazioni",
        "imputato", "imputata", "imputati", "imputate", "prescrizione"
    ]

    words.extend([w.strip() for w in additional_words if w.strip()])
    pattern = r'\b(?:' + '|'.join(words) + r')\b'

    df1 = all_results_df[
        all_results_df['Snippet'].str.contains(pattern, case=False, na=False) |
        all_results_df['Titolo'].str.contains(pattern, case=False, na=False)
    ]

    # Run an event loop within this synchronous Flask search worker.
    tracker = ProgressTracker()
    ai_skipped = None
    ai_elapsed_seconds = 0.0
    if case_description and not df1.empty:
        print(f"Starting AI filtering on {len(df1)} results (concurrency={OPENAI_MAX_CONCURRENCY})...")
        report("ai", "Analisi AI in corso", 0, len(df1), stats=tracker.get_stats())
        ai_started = time.monotonic()
        df1 = asyncio.run(filter_with_openai(df1, fixed_part, case_description,
                                            report=report, stop_event=stop_event, tracker=tracker))
        ai_elapsed_seconds = round(time.monotonic() - ai_started, 2)
        print(f"Filtered down to {len(df1)} relevant results")

    else:
        ai_skipped = "no_description" if not case_description else "no_candidates"
        report("ai", "Analisi AI saltata: manca la descrizione del caso." if not case_description
               else "Analisi AI saltata: nessun link da analizzare.", skipped=ai_skipped,
               stats=tracker.get_stats())

    check_cancelled(stop_event)
    report("csv", "Preparazione del CSV")
    # Create CSV
    csv_string = df1.to_csv(index=False)

    with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.csv',
                                     encoding='utf-8', newline='') as tmp:
        tmp.write(csv_string)
        tmp_path = tmp.name

    return {
        "message": "Ricerca completata con successo",
        "file": os.path.basename(tmp_path),
        "results_count": len(df1),
        "elapsed_seconds": round(time.monotonic() - started, 1),
        "ai_elapsed_seconds": ai_elapsed_seconds,
        "ai_failures": tracker.get_stats()["failed"],
        "ai_skipped": ai_skipped,
    }


@app.route('/api/search', methods=['POST'])
def search():
    try:
        data = read_search_data()
    except ValueError as exc:
        return jsonify({"error": str(exc), "code": "invalid_request"}), 400
    try:
        result = run_search(data)
        return jsonify({key: result[key] for key in ("message", "file", "results_count")})
    except OpenAIFilterError as exc:
        return jsonify({"error": str(exc), "code": "ai_filter_failed"}), 500
    except Exception as exc:
        app.logger.error("Search processing failed (%s).", type(exc).__name__)
        return jsonify({"error": "La ricerca non è riuscita. Riprova più tardi.",
                        "code": "search_failed"}), 500


@app.route('/api/search-stream', methods=['POST'])
def search_stream():
    try:
        data = read_search_data()
    except ValueError as exc:
        return jsonify({"error": str(exc), "code": "invalid_request"}), 400

    stopped = threading.Event()
    updates = queue.Queue(maxsize=32)

    def publish(event):
        # Keep recent updates; a slow reader must never block the producer.
        while not stopped.is_set():
            try:
                updates.put_nowait(event)
                return
            except queue.Full:
                try:
                    updates.get_nowait()
                except queue.Empty:
                    pass

    def produce():
        try:
            result = run_search(data, on_progress=publish, stop_event=stopped)
            publish({"type": "complete", **result})
        except SearchCancelled:
            pass
        except OpenAIFilterError as exc:
            publish({"type": "error", "code": "ai_filter_failed", "message": str(exc)})
        except Exception as exc:
            app.logger.error("Streamed search processing failed (%s).", type(exc).__name__)
            publish({"type": "error", "code": "search_failed",
                     "message": "La ricerca non è riuscita. Riprova più tardi."})

    def encode(event):
        return "data: " + json.dumps(event, ensure_ascii=False) + "\n\n"

    def events():
        try:
            yield encode({"type": "accepted", "version": 1,
                          "message": "Connessione stabilita. Avvio della ricerca."})
            threading.Thread(target=produce, name="search-producer", daemon=True).start()
            while not stopped.is_set():
                try:
                    event = updates.get(timeout=HEARTBEAT_SECONDS)
                except queue.Empty:
                    yield ": heartbeat\n\n"
                    continue
                yield encode(event)
                if event["type"] in ("complete", "error"):
                    return
        finally:
            stopped.set()

    response = Response(events(), mimetype="text/event-stream", headers={
        "Cache-Control": "no-cache, no-store, no-transform",
        "X-Accel-Buffering": "no",
    })
    response.call_on_close(stopped.set)
    return response


@app.route('/api/health', methods=['GET'])
def health():
    return jsonify({"status": "ok", "search_stream_version": 1})


@app.route('/api/download/<filename>', methods=['GET'])
def download(filename):
    directory = tempfile.gettempdir()
    file_path = os.path.join(directory, filename)

    if not os.path.exists(file_path):
        return jsonify({"error": "File not found"}), 404

    return send_file(
        file_path,
        mimetype='text/csv',
        as_attachment=True,
        download_name=f"{filename}"
    )


if __name__ == '__main__':
    app.run(debug=True)
