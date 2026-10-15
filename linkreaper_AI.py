from flask import Flask, Response, request, jsonify, send_file
from flask_cors import CORS
import os
import pandas as pd
from serpapi import GoogleSearch
from openai import OpenAI
import tempfile
import threading
import time
import json
import queue
from concurrent.futures import ThreadPoolExecutor, as_completed

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

# Initialize OpenAI client
client = OpenAI(api_key=openai_api_key)

# ============================================================================
# PARALLEL PROCESSING CONFIGURATION
# ============================================================================
MAX_WORKERS = 20      # Adjust between 20-50 based on performance
RETRY_ATTEMPTS = 2    # Number of retries for failed requests
RETRY_DELAY = 2       # Seconds to wait before retry
HEARTBEAT_SECONDS = 12
RESULT_COLUMNS = ["Query", "Posizione", "Titolo", "Snippet", "URL", "Dominio", "Data"]


class SearchCancelled(Exception):
    pass


def check_cancelled(stop_event):
    if stop_event is not None and stop_event.is_set():
        raise SearchCancelled()


# Thread-safe counters for tracking
class ProgressTracker:
    def __init__(self):
        self.lock = threading.Lock()
        self.succeeded = 0
        self.failed = 0
        self.retried = 0
        self.rate_limit_errors = 0

    def increment_success(self):
        with self.lock:
            self.succeeded += 1

    def increment_failure(self):
        with self.lock:
            self.failed += 1

    def increment_retry(self):
        with self.lock:
            self.retried += 1

    def increment_rate_limit(self):
        with self.lock:
            self.rate_limit_errors += 1

    def get_stats(self):
        with self.lock:
            return {
                'succeeded': self.succeeded,
                'failed': self.failed,
                'retried': self.retried,
                'rate_limit_errors': self.rate_limit_errors
            }


def is_relevant_with_openai(title, snippet, url, fixed_part, case_description, attempt=1, tracker=None,
                           stop_event=None):
    """
    Use OpenAI to determine if a search result is relevant to the case.
    Includes retry logic for robustness.
    """
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
    check_cancelled(stop_event)
    try:
        response = client.responses.create(
            model="gpt-5-luna",
            reasoning={"effort": "xhigh"},
            tools=[{"type": "web_search"}],
            input=prompt
        )
        answer = response.output_text.strip().upper()
        if tracker:
            tracker.increment_success()
        return answer.startswith("SI")

    except Exception as e:
        error_msg = str(e)

        # Check if it's a rate limit error
        is_rate_limit = "rate limit" in error_msg.lower() or "429" in error_msg

        if is_rate_limit and tracker:
            tracker.increment_rate_limit()

        # Retry logic
        if attempt <= RETRY_ATTEMPTS:
            if tracker:
                tracker.increment_retry()

            # Wait longer for rate limit errors
            wait_time = RETRY_DELAY * (2 if is_rate_limit else 1) * attempt
            if stop_event is None:
                time.sleep(wait_time)
            elif stop_event.wait(wait_time):
                raise SearchCancelled()

            return is_relevant_with_openai(
                title, snippet, url, fixed_part, case_description,
                attempt + 1, tracker, stop_event
            )
        else:
            # Failed after all retries — default to keeping the result
            if tracker:
                tracker.increment_failure()
            print(f"AI assessment failed after retries ({type(e).__name__}); keeping the link.")
            return True


def process_single_row(row_data, tracker, stop_event=None):
    """
    Process a single row with OpenAI API.
    Returns (index, is_relevant_bool)
    """
    idx, title, snippet, url, fixed_part, case_description = row_data

    is_relevant = is_relevant_with_openai(
        title, snippet, url, fixed_part, case_description,
        attempt=1, tracker=tracker, stop_event=stop_event
    )

    return (idx, is_relevant)


def filter_with_openai_parallel(df, fixed_part, case_description, max_workers=MAX_WORKERS,
                               report=None, stop_event=None, tracker=None):
    """
    Apply OpenAI filtering in parallel using ThreadPoolExecutor.

    Args:
        df: DataFrame to filter
        fixed_part: The subject/person name
        case_description: Description of the case
        max_workers: Maximum number of parallel threads

    Returns:
        Filtered DataFrame
    """
    if df.empty:
        return df

    tracker = tracker or ProgressTracker()

    # Prepare data for parallel processing
    row_data_list = [
        (idx, row["Titolo"], row["Snippet"], row["URL"], fixed_part, case_description)
        for idx, row in df.iterrows()
    ]

    results = {}

    print(f"Processing {len(row_data_list)} requests with {max_workers} parallel workers...")

    executor = ThreadPoolExecutor(max_workers=max_workers)
    try:
        check_cancelled(stop_event)
        futures = {
            executor.submit(process_single_row, row_data, tracker, stop_event): row_data[0]
            for row_data in row_data_list
        }

        for future in as_completed(futures):
            check_cancelled(stop_event)
            idx, is_relevant = future.result()
            results[idx] = is_relevant
            if report:
                report("ai", "Analisi AI in corso", len(results), len(row_data_list),
                       stats=tracker.get_stats())
    finally:
        cancelled = stop_event is not None and stop_event.is_set()
        executor.shutdown(wait=not cancelled, cancel_futures=cancelled)

    # Log statistics
    stats = tracker.get_stats()
    print(f"AI Filter - Succeeded: {stats['succeeded']} | Failed: {stats['failed']} | "
          f"Retried: {stats['retried']} | Rate limit hits: {stats['rate_limit_errors']}")

    # Apply results and filter
    filtered_df = df.loc[df.index.map(results)].copy()
    return filtered_df


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

    # Loop through each query and perform a search across the first 10 pages
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
            if results.get("error"):
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
            report("google", "Ricerca Google in corso", completed_pages, total_pages,
                   query_index=query_index, query_total=len(queries), page=page + 1)

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

    # Apply parallel OpenAI filter only if case_description is provided and df1 is not empty
    tracker = ProgressTracker()
    ai_skipped = None
    if case_description and not df1.empty:
        print(f"Starting AI filtering on {len(df1)} results with {MAX_WORKERS} parallel workers...")
        report("ai", "Analisi AI in corso", 0, len(df1), stats=tracker.get_stats())
        df1 = filter_with_openai_parallel(df1, fixed_part, case_description,
                                         report=report, stop_event=stop_event, tracker=tracker)
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
    except Exception:
        app.logger.error("Search processing failed.")
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
        except Exception:
            app.logger.error("Streamed search processing failed.")
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
