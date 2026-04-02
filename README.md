# PE Due Diligence RAG Pipeline

Automated web intelligence platform for Private Equity due diligence on DACH fintech companies. Crawls public websites, extracts and classifies content with Snowflake Cortex AI, and serves answers through a RAG-powered Streamlit assistant.

## What This Tool Does

A PE analyst types a question like *"What regulatory licenses does Bitpanda hold?"* into a chat interface. Behind the scenes, the system:

1. **Crawls** the target company's public web pages and regulatory filings (BaFin, FMA, FINMA)
2. **Extracts** raw HTML text from fetched documents
3. **Cleans** boilerplate (cookie banners, nav bars, CTAs) via regex stripping
4. **Classifies** each page using `AI_CLASSIFY` (product page, compliance page, news, etc.)
5. **Chunks** cleaned text into ~800-character passages with overlap for retrieval quality
6. **Indexes** chunks into a Cortex Search service for semantic search
7. **Answers** the analyst's question using RAG: retrieve top chunks, feed them to an LLM with a grounded system prompt, cite sources

### Covered Companies (13 DACH Fintechs)

| Germany | Austria | Switzerland |
|---------|---------|-------------|
| N26 | Bitpanda | Yapeal |
| Trade Republic | Credi2 | Selma Finance |
| Solaris | Wikifolio | Relio |
| Scalable Capital | | Teylor |
| Raisin | | |
| Finanzguru | | |

## Architecture

```
                        INGESTION (SQL Scripts)                           TRANSFORMATION (dbt)                         SERVING
                     ┌─────────────────────────┐                  ┌──────────────────────────────┐          ┌──────────────────────┐
                     │                         │                  │                              │          │                      │
  Fintech Websites ──┤  01  Warehouse + DB     │                  │  stg_source_registry ───┐    │          │  Cortex Search       │
  BaFin/FMA/FINMA   │  02  Stage               │                  │  stg_document_manifest ─┤    │          │  Service             │
                     │  03  Control tables      │                  │  stg_raw_html_text ─────┼──► │          │  (FINTECH_HTML_      │
                     │  04  Network rules       │                  │  stg_bafin_enrichment ──┘    │          │   SEARCH)            │
                     │  05  Link scoring (Py)   │                  │         │                    │          │       │              │
                     │  06  Fetch & stage (SP)  │   ──────►        │  int_consolidated_html_text  │  ──────► │       ▼              │
                     │  07  Crawl QA            │  Raw tables      │         │                    │  Serving │  Streamlit App       │
                     │  08  Dedup maintenance   │  land here       │  clean_html_pages (table)    │  view    │  (RAG Chat UI)       │
                     │  09  BaFin enrichment    │                  │         │                    │          │  Claude 3.5 Sonnet   │
                     │  10  BaFin QA            │                  │  html_classified (table)     │          │       │              │
                     │                         │                  │         │                    │          │       ▼              │
                     └─────────────────────────┘                  │  html_chunks (table)         │          │  Analyst gets        │
                                                                  │         │                    │          │  sourced answers     │
                                                                  │  v_html_retrieval (view) ────┼──────────┤                      │
                                                                  │                              │          └──────────────────────┘
                                                                  └──────────────────────────────┘
```

### Three Layers

#### 1. Ingestion Layer (SQL Scripts `01`-`10`)
Infrastructure and data collection. Runs outside dbt because it involves stored procedures, network rules, external access, and Python UDFs.

| Script | Purpose |
|--------|---------|
| `01_create_warehouse.sql` | `PE_POC_WH` warehouse, `PE_POC_DB` database, `RAW`/`CURATED`/`SERVE` schemas |
| `02_create_stage.sql` | Internal stage for fetched documents |
| `03_create_control_tables.sql` | `SOURCE_REGISTRY`, `DOCUMENT_MANIFEST`, `DISCOVERED_URLS`, `CRAWL_DEBUG`, `PIPELINE_ERRORS` |
| `04_outbound_web_access.sql` | Network rule + external access integration for 15 fintech domains |
| `05_set_discovery.py` | Python link scorer (prioritizes `/api`, `/platform`, `/product` URLs; deprioritizes `/careers`, `/privacy`) |
| `06_fetch_and_stage.sql` | Stored procedure: crawls pages, extracts HTML, stages to `RAW_HTML_TEXT` |
| `07_crawl_quality.sql` | QA queries on crawl coverage |
| `08_maintainance_after_f_s.sql` | Deduplication of `DISCOVERED_URLS`, `DOCUMENT_MANIFEST`, `RAW_HTML_TEXT`, `CRAWL_DEBUG` |
| `09_bafin_enrichment.sql` | Stored procedure: searches BaFin regulator site per company, stores results |
| `10_bafin_checks.sql` | QA queries on BaFin enrichment results |

#### 2. Transformation Layer (dbt Project `pe_poc_dbt/`)
All SQL transformations managed by dbt with full lineage, testing, and documentation.

```
pe_poc_dbt/
├── dbt_project.yml
├── profiles.yml
├── macros/
│   └── generate_schema_name.sql      # Routes to exact schema names (RAW/CURATED/SERVE)
└── models/
    ├── staging/          (views → PE_POC_DB.RAW)
    │   ├── stg_source_registry       # Deduped company registry
    │   ├── stg_document_manifest     # Deduped crawl metadata
    │   ├── stg_raw_html_text         # Extracted HTML content
    │   └── stg_bafin_enrichment      # Regulator search results
    ├── intermediate/     (views → PE_POC_DB.RAW)
    │   └── int_consolidated_html_text  # 4-way join: registry + manifest + HTML + BaFin
    ├── curated/          (tables → PE_POC_DB.CURATED)
    │   ├── clean_html_pages          # Boilerplate removal, URL signal scoring, min-length filter
    │   ├── html_classified           # AI_CLASSIFY into 8 document types
    │   └── html_chunks               # SPLIT_TEXT_RECURSIVE_CHARACTER (800 chars, 100 overlap)
    └── serve/            (views → PE_POC_DB.SERVE)
        └── v_html_retrieval          # Final view consumed by Cortex Search
```

**DAG:**
```
stg_source_registry ──┐
stg_document_manifest ┼──► int_consolidated_html_text
stg_raw_html_text ────┤           │
stg_bafin_enrichment ─┘    clean_html_pages
                                  │
                           html_classified
                                  │
                            html_chunks
                                  │
                          v_html_retrieval ──► Cortex Search
```

**Test Coverage (34 tests):**
- `not_null` on all critical columns
- `unique` on primary keys (`SOURCE_ID`, `DOC_ID`, `CHUNK_ID`)
- `accepted_values` on `DOC_TYPE` (8 categories), `URL_GROUP` (high/medium signal), BaFin `STATUS` (found/not_found/error)
- Source-level uniqueness warnings for raw data quality monitoring

#### 3. Serving Layer
| Component | Detail |
|-----------|--------|
| **Cortex Search Service** | `PE_POC_DB.SERVE.FINTECH_HTML_SEARCH` — semantic index over `v_html_retrieval`, 1-day target lag |
| **Streamlit App** | Chat UI with company/country filters, RAG pipeline using Claude 3.5 Sonnet |
| **RAG Flow** | Query → Cortex Search (top 20 chunks) → filter by relevance → LLM generates grounded, cited answer |

## How to Run

### Full Rebuild (Transformation Layer)
```bash
dbt run --project-dir pe_poc_dbt
```

### Run Tests
```bash
dbt test --project-dir pe_poc_dbt
```

### Run a Single Model + Downstream
```bash
dbt run --project-dir pe_poc_dbt --select clean_html_pages+
```

### Refresh Cortex Search
After a dbt run, the Cortex Search service auto-refreshes from `v_html_retrieval` within its target lag (1 day). To force:
```sql
ALTER CORTEX SEARCH SERVICE PE_POC_DB.SERVE.FINTECH_HTML_SEARCH RESUME;
```

## Key Design Decisions

| Decision | Rationale |
|----------|-----------|
| **Staging dedup via `ROW_NUMBER()`** | Raw sources have duplicates from re-crawls. Staging layer handles it so downstream models stay clean. |
| **Curated layer materialized as tables** | `clean_html_pages`, `html_classified`, `html_chunks` are expensive (regex, AI_CLASSIFY, chunking). Tables avoid recomputation. |
| **Staging/intermediate as views** | Cheap reads from raw tables. No storage cost. |
| **`generate_schema_name` macro** | Overrides dbt's default schema naming so models land in exact target schemas (`RAW`, `CURATED`, `SERVE`). |
| **800-char chunks with 100 overlap** | Balances context window (enough for a product description or compliance paragraph) with retrieval precision. 89% of pages are longer, so they get chunked rather than passed through. |
| **Ingestion stays outside dbt** | Stored procedures, network rules, and external access integrations are imperative operations that don't fit dbt's declarative model. |

## Roadmap

Improvement backlog organized by pipeline step. Items marked with **[HIGH]** have the most immediate impact.

### Step 1: Crawl

| # | Priority | To-Do | Why |
|---|----------|-------|-----|
| 1 | **[HIGH]** | Add PDF parsing with `AI_PARSE_DOCUMENT` for staged PDFs (annual reports, investor decks) | High-value documents are fetched to stage but never extracted. |
| 2 | | Schedule re-crawls with a Snowflake Task (weekly CRON) or Airflow | Currently manual — data goes stale between `CALL` invocations. |
| 3 | | Add FMA (Austria) and FINMA (Switzerland) enrichment stored procedures | Network rule allows both domains but no SP exists. 7 of 13 companies have zero regulator data. |
| 4 | | Respect `robots.txt` — fetch and parse before crawling each domain | Legal/ethical hygiene for production use. |

### Step 2: Extract

| # | Priority | To-Do | Why |
|---|----------|-------|-----|
| 5 | | Extract structured metadata (`<title>`, `<meta description>`, `og:` tags) into separate columns before stripping HTML | High-density signals currently lost in HTML-to-text conversion. Useful for classification and retrieval. |
| 6 | | Add language detection — tag each page as `en`/`de`/`fr` | ~40% of pages are German. Knowing language enables translation before indexing or query-time filtering. |
| 7 | | Add extraction failure monitoring — dbt test that NULL rate of `HTML_TEXT` per company stays below threshold | Silent extraction failures degrade coverage without anyone noticing. |

### Step 3: Clean

| # | Priority | To-Do | Why |
|---|----------|-------|-----|
| 8 | | Move regex patterns to a seed CSV or Jinja macro | The 7-deep nested `REGEXP_REPLACE` is hard to maintain. Externalizing lets non-engineers add patterns. |
| 9 | | Add German-language boilerplate patterns (`"Folgen Sie uns"`, `"Jetzt anmelden"`, `"Datenschutz"`, `"Impressum"`) | Current patterns are English-only. German boilerplate passes through untouched. |
| 10 | | Track cleaning effectiveness — add `RAW_TEXT_LEN`, `COMPRESSION_RATIO` columns and a dbt test flagging pages where >90% of text was removed | Catches regex bugs that over-strip good content. |

### Step 4: Classify

| # | Priority | To-Do | Why |
|---|----------|-------|-----|
| 11 | **[HIGH]** | Store `AI_CLASSIFY` confidence score (`scores[0]`) as `DOC_TYPE_CONFIDENCE` and add a dbt test for `confidence > 0.4` | Low-confidence classifications pollute retrieval. Currently no way to know if a label is trustworthy. |
| 12 | | Add PE-specific categories: `investor_relations`, `regulatory_filing`, `partnership_announcement`, `financial_data` | Current 8 categories are generic. PE analysts care about different page types. |
| 13 | | Build classification accuracy test — manually label ~50 pages as a seed, compare AI labels vs ground truth | No current measure of classifier quality. |

### Step 5: Chunk

| # | Priority | To-Do | Why |
|---|----------|-------|-----|
| 14 | | Make chunk size configurable via `dbt run --vars '{chunk_size: 1200}'` using `{{ var('chunk_size', 800) }}` | Hardcoded values prevent easy experimentation. |
| 15 | | Add parent-document linking — store `PAGE_ID` (hash of CRAWL_URL) so full pages can be reconstructed at query time | Enables "expand context" for long-form answers when multiple chunks come from the same page. |
| 16 | | Filter near-duplicate chunks via `MD5(CHUNK_TEXT)` dedup | Navbars/footers produce identical chunks across URLs, wasting retrieval slots. |

### Step 6: Index (Cortex Search)

| # | Priority | To-Do | Why |
|---|----------|-------|-----|
| 17 | **[HIGH]** | Add `COUNTRY` as a search attribute in `v_html_retrieval` | The Streamlit sidebar filters by country but the column doesn't exist in the serving view — filter is silently broken. |
| 18 | | Reduce `TARGET_LAG` from `1 day` to `1 hour` | Day-old data is too slow during active deal due diligence. |
| 19 | | Attach a freshness DMF to `html_chunks` checking `MAX(CREATED_AT) > DATEADD('day', -7, CURRENT_TIMESTAMP())` | Alerts if the pipeline breaks and data goes stale without anyone noticing. |

### Step 7: Answer (RAG / Streamlit)

| # | Priority | To-Do | Why |
|---|----------|-------|-----|
| 20 | | Add conversation memory — pass last N messages to the LLM for follow-up questions | Chat without memory restarts from zero on every question. |
| 21 | **[HIGH]** | Wire up relevance scoring in `filter_relevant_chunks` — use Cortex Search `relevance_score` with existing `MIN_RELEVANCE_SCORE = -8.0` | Threshold is defined but never used. Low-relevance chunks dilute context and cause hallucination. |
| 22 | | Add export feature — save Q&A thread as PDF/Markdown for deal memos | Due diligence findings need to end up in a document. |
| 23 | | Add "Compare Companies" mode — multi-select retrieval for 2-3 companies with structured comparison table output | Company comparison is the #1 PE analyst workflow. |
