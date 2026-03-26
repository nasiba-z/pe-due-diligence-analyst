import streamlit as st
import json
import requests

ACCOUNT_URL = st.secrets["snowflake"]["account_url"].rstrip("/")
PAT = st.secrets["snowflake"]["pat"]

SEARCH_DB = "PE_POC_DB"
SEARCH_SCHEMA = "SERVE"
SEARCH_SERVICE = "FINTECH_HTML_SEARCH"
LLM_MODEL = "claude-3-5-sonnet"
NUM_RESULTS = 20
MIN_RELEVANCE_SCORE = -8.0
NO_INFO_MSG = "At the moment, we don't have data to answer this question. Please try a different company or rephrase your question."

SYSTEM_PROMPT = """You are a PE (Private Equity) due diligence analyst assistant specializing in DACH fintech companies.
Answer questions using ONLY the provided context chunks. Do NOT use any prior knowledge.
If the context does not contain sufficient information to answer the question, respond exactly with: "The available data does not contain information to answer this question."
Always cite which company and source URL the information comes from.
Be concise and structured. Use bullet points for key findings."""

COMPANY_COUNTRY_MAP = {
    "N26": "Germany",
    "Trade Republic": "Germany",
    "Solaris": "Germany",
    "Scalable Capital": "Germany",
    "Raisin": "Germany",
    "Finanzguru": "Germany",
    "Bitpanda": "Austria",
    "Credi2": "Austria",
    "Wikifolio": "Austria",
    "Yapeal": "Switzerland",
    "Selma Finance": "Switzerland",
    "Relio": "Switzerland",
    "Teylor": "Switzerland",
}

COVERED_COMPANIES = list(COMPANY_COUNTRY_MAP.keys())

META_RESPONSES = {
    "coverage": f"""I cover **{len(COVERED_COMPANIES)} DACH fintech companies** with data sourced from their public web pages, press releases, and regulatory filings:

{chr(10).join(f'- **{c}**' for c in COVERED_COMPANIES)}

You can ask me about their products, regulatory licenses, partnerships, funding, leadership, and more.""",
}

META_KEYWORDS = {
    "coverage": [
        "what data do you cover", "what do you cover",
        "which companies do you cover", "which companies do you have",
        "what companies do you cover", "what companies do you have",
        "who do you cover", "what is your coverage", "list companies",
        "what topics", "what information", "introduce yourself",
        "what are you", "who are you", "what can you do",
        "help me", "how can you help", "what is this app",
    ],
}

LOCATION_QUALIFIERS = [
    "in ", "from ", "based in ", "operate in ", "headquartered in ",
    "located in ", "germany", "austria", "switzerland", "europe",
    "dach", "berlin", "vienna", "zurich",
]

COUNTRY_KEYWORDS = {
    "Germany": ["germany", "german", "berlin", "munich", "frankfurt"],
    "Austria": ["austria", "austrian", "vienna"],
    "Switzerland": ["switzerland", "swiss", "zurich"],
}

def check_country_query(query):
    q = query.lower().strip()
    country_patterns = [
        "which companies", "what companies", "who operates",
        "companies in", "companies from", "companies based",
        "fintechs in", "fintechs from", "operate in",
        "headquartered in", "based in", "located in",
    ]
    is_country_question = any(p in q for p in country_patterns)
    if not is_country_question:
        return None
    for country, keywords in COUNTRY_KEYWORDS.items():
        if any(kw in q for kw in keywords):
            return country
    return None

@st.cache_data(ttl=3600)
def get_companies_with_data():
    url = f"{ACCOUNT_URL}/api/v2/statements"
    headers = {
        "Authorization": f"Bearer {PAT}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-Snowflake-Authorization-Token-Type": "PROGRAMMATIC_ACCESS_TOKEN",
    }
    body = {
        "statement": "SELECT DISTINCT COMPANY_NAME FROM PE_POC_DB.CURATED.HTML_CHUNKS",
        "timeout": 60,
        "database": SEARCH_DB,
        "schema": SEARCH_SCHEMA,
        "warehouse": "PE_POC_WH",
    }
    resp = requests.post(url, headers=headers, json=body)
    resp.raise_for_status()
    data = resp.json()
    companies = set()
    for row in data.get("data", []):
        companies.add(row[0])
    return companies

def get_companies_by_country(country):
    return sorted([c for c, ctry in COMPANY_COUNTRY_MAP.items() if ctry == country])

def check_meta_query(query):
    q = query.lower().strip()
    for qualifier in LOCATION_QUALIFIERS:
        if qualifier in q:
            return None
    for category, keywords in META_KEYWORDS.items():
        for kw in keywords:
            if kw in q:
                return META_RESPONSES.get(category)
    return None

def search(query, company_filter=None, country_filter=None):
    url = f"{ACCOUNT_URL}/api/v2/databases/{SEARCH_DB}/schemas/{SEARCH_SCHEMA}/cortex-search-services/{SEARCH_SERVICE}:query"
    headers = {
        "Authorization": f"Bearer {PAT}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-Snowflake-Authorization-Token-Type": "PROGRAMMATIC_ACCESS_TOKEN",
    }

    filters = []
    if company_filter and company_filter != "All":
        filters.append({"@eq": {"COMPANY_NAME": company_filter}})
    if country_filter and country_filter != "All":
        filters.append({"@eq": {"COUNTRY": country_filter}})

    body = {
        "query": query,
        "columns": ["COMPANY_NAME", "COUNTRY", "DOC_TYPE", "CHUNK_TEXT", "CRAWL_URL"],
        "limit": NUM_RESULTS,
    }

    if len(filters) == 1:
        body["filter"] = filters[0]
    elif len(filters) > 1:
        body["filter"] = {"@and": filters}

    resp = requests.post(url, headers=headers, json=body)
    if resp.status_code != 200:
        st.error(f"Search API error {resp.status_code}: {resp.text[:500]}")
        return []
    return resp.json().get("results", [])

def filter_relevant_chunks(chunks):
    return [
        c for c in chunks
        if len(c.get("CHUNK_TEXT", "").strip()) > 50
    ]

def generate_answer(query, context_chunks):
    url = f"{ACCOUNT_URL}/api/v2/cortex/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {PAT}",
        "Content-Type": "application/json",
        "X-Snowflake-Authorization-Token-Type": "PROGRAMMATIC_ACCESS_TOKEN",
    }

    context = "\n\n---\n\n".join(
        [f"Company: {c['COMPANY_NAME']} | Country: {c.get('COUNTRY', 'N/A')} | Type: {c['DOC_TYPE']} | URL: {c['CRAWL_URL']}\n{c['CHUNK_TEXT']}"
         for c in context_chunks]
    )

    body = {
        "model": LLM_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"Context:\n{context}\n\nQuestion: {query}"},
        ],
    }

    resp = requests.post(url, headers=headers, json=body)
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]

st.set_page_config(page_title="Fintech Due Diligence", layout="wide")
st.title("Fintech Due Diligence Assistant")
st.caption("RAG-powered search over DACH fintech company data")

with st.sidebar:
    st.header("Filters")
    countries = ["All", "Germany", "Austria", "Switzerland"]
    selected_country = st.selectbox("Country", countries)
    companies = ["All", "N26", "Trade Republic", "Solaris", "Scalable Capital",
                 "Raisin", "Finanzguru", "Bitpanda", "Credi2", "Wikifolio",
                 "Yapeal", "Selma Finance", "Relio", "Teylor"]
    selected_company = st.selectbox("Company", companies)
    st.divider()
    st.markdown("**Sample questions:**")
    st.markdown("- What regulatory licenses does Bitpanda hold?")
    st.markdown("- What products does N26 offer?")
    st.markdown("- Which companies operate in Germany?")
    st.markdown("- What security measures does Yapeal have?")

if "messages" not in st.session_state:
    st.session_state.messages = []

for msg in st.session_state.messages:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])
        if "sources" in msg and msg["sources"]:
            with st.expander("Sources"):
                for s in msg["sources"]:
                    st.markdown(f"- **{s['company']}** ({s['doc_type']}): [{s['url']}]({s['url']})")

if query := st.chat_input("Ask about DACH fintech companies..."):
    st.session_state.messages.append({"role": "user", "content": query})
    with st.chat_message("user"):
        st.markdown(query)

    with st.chat_message("assistant"):
        meta_answer = check_meta_query(query)
        country_match = check_country_query(query)
        if meta_answer:
            response = meta_answer
            sources = []
        elif country_match:
            companies_in_country = get_companies_by_country(country_match)
            if companies_in_country:
                has_data = get_companies_with_data()
                with_data = [c for c in companies_in_country if c in has_data]
                without_data = [c for c in companies_in_country if c not in has_data]
                lines = []
                for c in with_data:
                    lines.append(f"- **{c}**")
                for c in without_data:
                    lines.append(f"- **{c}** _(no data yet)_")
                company_list = "\n".join(lines)
                response = f"The following companies in our database are headquartered in **{country_match}**:\n\n{company_list}"
                if without_data:
                    response += f"\n\nAt the moment, we don't have data about: {', '.join(without_data)}. Data ingestion is pending for these companies."
            else:
                response = f"No companies headquartered in {country_match} found in our database."
            sources = []
        else:
            with st.spinner("Searching..."):
                raw_chunks = search(query, selected_company, selected_country)
                chunks = filter_relevant_chunks(raw_chunks)

            if not chunks:
                response = NO_INFO_MSG
                sources = []
            else:
                with st.spinner("Analyzing..."):
                    response = generate_answer(query, chunks)
                    sources = [{"company": c["COMPANY_NAME"], "doc_type": c["DOC_TYPE"], "url": c["CRAWL_URL"]} for c in chunks]

        st.markdown(response)
        if sources:
            with st.expander("Sources"):
                for s in sources:
                    st.markdown(f"- **{s['company']}** ({s['doc_type']}): [{s['url']}]({s['url']})")

        st.session_state.messages.append({"role": "assistant", "content": response, "sources": sources})

