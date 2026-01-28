from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
import os
import pandas as pd
from serpapi import GoogleSearch
from openai import OpenAI
import tempfile
import io

app = Flask(__name__)

# Restrict CORS to allow only the frontend on Vercel
CORS(app, resources={r"/api/*": {"origins": "https://linkreaperai.vercel.app"}})

# Retrieve API keys from environment variables
api_key = os.getenv('SERPAPI_KEY')
openai_api_key = os.getenv('OPENAI_API_KEY')

if not api_key:
    raise ValueError("Error: SERPAPI_KEY environment variable is not set!")

if not openai_api_key:
    raise ValueError("Error: OPENAI_API_KEY environment variable is not set!")

# Initialize OpenAI client
client = OpenAI(api_key=openai_api_key)

def is_relevant_with_openai(title, snippet, url, fixed_part, case_description):
    """
    Use OpenAI to determine if a search result is relevant to the case.
    """
    prompt = f"""
Developer: # Ruolo e Obiettivo
Sei un assistente legale incaricato di valutare la pertinenza di un articolo online rispetto a un caso.

## Obiettivo
Devi decidere se un link è rilevante e potenzialmente lesivo per il SOGGETTO relativamente al CASO fornito.
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
  1. Il risultato si riferisce con alta probabilità allo STESSO soggetto del caso (persona o società).
  2. Collega il soggetto al caso oppure a fatti negativi/giudiziari/reputazionali coerenti col caso.
- Rispondi "NO" se:
  - Il risultato riguarda chiaramente un altro soggetto (es. omonimo, altra società).
  - Non ha collegamento con il caso.
- Se le informazioni (titolo, snippet, url) non sono sufficienti a stabilire che si tratta dello stesso soggetto, rispondi "NO".

## Output
Rispondi SOLO con:
SI
NO
"""
    try:
        response = client.responses.create(
            model="gpt-5-mini",
            reasoning={"effort": "minimal"},
            input=prompt
        )
        answer = response.output_text.strip().upper()
        return answer.startswith("SI")
    except Exception as e:
        print(f"OpenAI API error: {e}")
        # In case of error, default to keeping the result
        return True

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
        "associazione a delinquere", "galera", "fraudolenta", "tangente", "tangenti",
        "patteggia", "patteggiamento", "patteggiamenti", "turbativa"
    ]

    words.extend([w.strip() for w in additional_words if w.strip()])
    pattern = r'\b(?:' + '|'.join(words) + r')\b'

    df1 = all_results_df[
        all_results_df['Snippet'].str.contains(pattern, case=False, na=False) |
        all_results_df['Titolo'].str.contains(pattern, case=False, na=False)
    ]

    # Apply OpenAI filter only if case_description is provided and df1 is not empty
    if case_description and not df1.empty:
        df1["ai_keep"] = df1.apply(
            lambda r: is_relevant_with_openai(
                r["Titolo"], 
                r["Snippet"], 
                r["URL"], 
                fixed_part,
                case_description
            ),
            axis=1,
        )
        # Keep only rows where ai_keep is True
        df1 = df1[df1["ai_keep"]].drop(columns=["ai_keep"])

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