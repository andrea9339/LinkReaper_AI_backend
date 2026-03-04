from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
import os
import pandas as pd
from serpapi import GoogleSearch
from openai import OpenAI
import tempfile
import threading
import time
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


def is_relevant_with_openai(title, snippet, url, fixed_part, case_description, attempt=1, tracker=None):
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
    try:
        response = client.responses.create(
            model="gpt-5-mini",
            reasoning={"effort": "high"},
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
            time.sleep(wait_time)

            return is_relevant_with_openai(
                title, snippet, url, fixed_part, case_description,
                attempt + 1, tracker
            )
        else:
            # Failed after all retries — default to keeping the result
            if tracker:
                tracker.increment_failure()
            print(f"⚠️  Failed after {RETRY_ATTEMPTS} attempts: {error_msg[:100]}")
            return True


def process_single_row(row_data, tracker):
    """
    Process a single row with OpenAI API.
    Returns (index, is_relevant_bool)
    """
    idx, title, snippet, url, fixed_part, case_description = row_data

    is_relevant = is_relevant_with_openai(
        title, snippet, url, fixed_part, case_description,
        attempt=1, tracker=tracker
    )

    return (idx, is_relevant)


def filter_with_openai_parallel(df, fixed_part, case_description, max_workers=MAX_WORKERS):
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

    tracker = ProgressTracker()

    # Prepare data for parallel processing
    row_data_list = [
        (idx, row["Titolo"], row["Snippet"], row["URL"], fixed_part, case_description)
        for idx, row in df.iterrows()
    ]

    results = {}

    print(f"🚀 Processing {len(row_data_list)} requests with {max_workers} parallel workers...")

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(process_single_row, row_data, tracker): row_data[0]
            for row_data in row_data_list
        }

        for future in as_completed(futures):
            idx, is_relevant = future.result()
            results[idx] = is_relevant

    # Log statistics
    stats = tracker.get_stats()
    print(f"📊 AI Filter — Succeeded: {stats['succeeded']} | Failed: {stats['failed']} | "
          f"Retried: {stats['retried']} | Rate limit hits: {stats['rate_limit_errors']}")

    # Apply results and filter
    df["ai_keep"] = df.index.map(results)
    filtered_df = df[df["ai_keep"]].drop(columns=["ai_keep"])
    return filtered_df


@app.route('/api/search', methods=['POST'])
def search():
    data = request.json
    fixed_part = data['fixedPart']
    keywords = data['keywords'].split(',')
    additional_words = data.get('additionalWords', '').split(',')
    case_description = data.get('caseDescription', '').strip()

    fixed_part_with_quotes = f'"{fixed_part}"'
    queries = [fixed_part_with_quotes] + [
        f"{fixed_part_with_quotes} {keyword.strip()}" for keyword in keywords
    ]

    all_results_df = pd.DataFrame()

    # Loop through each query and perform a search across the first 10 pages
    for query in queries:
        data_rows = []

        for page in range(10):  # 10 pages → results 1–100
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
            organic_results = results.get("organic_results", [])

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

        # Create and sort dataframe
        query_df = pd.DataFrame(data_rows)
        query_df = query_df.sort_values(by="Posizione").reset_index(drop=True)

        # Append
        all_results_df = pd.concat([all_results_df, query_df], ignore_index=True)

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
    if case_description and not df1.empty:
        print(f"Starting AI filtering on {len(df1)} results with {MAX_WORKERS} parallel workers...")
        df1 = filter_with_openai_parallel(df1, fixed_part, case_description)
        print(f"✨ Filtered down to {len(df1)} relevant results")

    # Create CSV
    csv_string = df1.to_csv(index=False)

    with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.csv',
                                     encoding='utf-8', newline='') as tmp:
        tmp.write(csv_string)
        tmp_path = tmp.name

    return jsonify({
        "message": "Ricerca completata con successo",
        "file": os.path.basename(tmp_path),
        "results_count": len(df1)
    })


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
