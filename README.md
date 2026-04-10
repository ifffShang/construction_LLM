# RAG — Construction Document Intelligence

Two AI-powered tools for extracting structured data from construction PDFs using vision LLMs.

## Quick Start

```bash
python3 -m venv venv
source venv/bin/activate
pip install flask flask-cors python-dotenv pymupdf langchain-core langchain-openai langchain-groq openpyxl
```

Create a `.env` file in this directory:

```
OPENAI_API_KEY=sk-...
GROQ_API_KEY=gsk_...
```

---

## 1. CAD Analyze — Construction Material Tracker

Extracts material information from CAD drawings and specification documents, then generates a structured Excel tracker in the NHC/ADNOC Architectural Material Tracker format.

**Port:** `8766`

```bash
cd CAD_analyze
python server.py
# → http://localhost:8766
```

### Workflow

1. **Create a project** — name, owner, EPCM, contractor metadata
2. **Upload CAD PDFs** — each page is rendered and analyzed by GPT-4o-mini vision to extract building IDs, material callouts, dimensions, room labels, and schedule items
3. **Select spec documents** — pick an existing company's specs from the global library or create a new company entry. The LLM extracts spec refs, international standards, approved vendors, and material descriptions
4. **Generate tracker** — CAD and spec data are merged via fuzzy matching against a material taxonomy, then exported as a styled `.xlsx` with three sheets:
   - **Material Tracker** — one row per building x material
   - **Quantity Basis** — building roof areas with VLOOKUP formulas
   - **AVL Reference** — approved vendor list

### Project Structure

```
CAD_analyze/
├── server.py            # Flask app — routes, API endpoints, job orchestration
├── database.py          # SQLite layer — projects, files, jobs, spec library, tracker rows
├── extractor_cad.py     # CAD PDF → GPT-4o-mini vision → building/material JSON
├── extractor_spec.py    # Spec PDF → GPT-4o-mini vision → standards/vendor JSON
├── merger.py            # Fuzzy-match CAD callouts to taxonomy + spec enrichment
├── excel_writer.py      # Styled .xlsx generation (3 sheets)
├── templates.py         # Domain config: MATERIAL_TAXONOMY, BUILDING_ID_HINTS, vendor fallbacks
├── home.html            # Landing page — create/list/delete projects
├── app.html             # Project workspace — upload, review, generate
├── tracker.db           # SQLite database (auto-created)
├── projects/            # Per-project uploaded PDFs
├── spec_library/        # Global spec PDFs organized by company
└── outputs/             # Generated Excel files
```

### API Reference

#### Projects

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/api/projects` | List all projects |
| `POST` | `/api/projects` | Create a new project |
| `GET` | `/api/projects/<id>` | Get project details + file lists |
| `DELETE` | `/api/projects/<id>` | Delete a project and all its data |
| `GET` | `/api/projects/<id>/files` | List uploaded files (`?type=cad\|spec`) |
| `DELETE` | `/api/projects/<id>/files` | Delete a specific file |
| `GET` | `/api/projects/<id>/jobs` | List extraction/generation jobs |
| `GET` | `/api/projects/<id>/materials` | Query extracted tracker rows |

#### Spec Library

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/api/spec-library` | List all companies with their spec files |
| `POST` | `/api/spec-library` | Create company + upload spec PDFs (reuses if name exists) |
| `POST` | `/api/spec-library/<id>/files` | Add files to a company (skips duplicates) |
| `DELETE` | `/api/spec-library/<id>` | Delete a company and its files |

#### Extraction & Generation

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/upload-cad` | Upload CAD PDFs and start extraction |
| `POST` | `/upload-spec` | Upload spec PDFs or use `company_id` from library |
| `GET` | `/job/<job_id>` | Poll job status, progress, and logs |
| `POST` | `/generate` | Merge CAD + spec → Excel tracker |
| `GET` | `/download/<gen_id>` | Download the generated `.xlsx` |

### Database Schema

| Table | Purpose |
|-------|---------|
| `projects` | Project metadata (name, owner, EPCM, contractor, etc.) |
| `uploaded_files` | Files per project, deduplicated by (project, type, filename) |
| `jobs` | Extraction/generation job records with status, progress, logs, results |
| `spec_companies` | Global spec library — one row per company |
| `spec_company_files` | Spec PDFs belonging to each company |
| `tracker_rows` | Final merged material tracker rows per project |

### Rate Limiting

Built-in handling for OpenAI rate limits:
- **Lower DPI rendering** (120 for CAD, 150 for spec) to reduce token usage
- **Exponential backoff** — retries up to 3 times on 429 errors
- **1-second delay** between pages to stay under TPM limits

---

## 2. Database Update — Product Catalogue Extractor

Extracts product catalogue data from PDF documents (e.g. building material catalogues) using Groq's Llama 4 Scout vision model, then provides a browser-based viewer for browsing and editing the results.

**Port:** `8765`

```bash
cd database_update
python server.py
# → http://localhost:8765
```

### Workflow

1. **Upload a PDF** — drag-and-drop a product catalogue PDF, optionally specify a page range
2. **LLM extraction** — each page is rendered to JPEG via PyMuPDF, sent to Groq (Llama 4 Scout vision). The model returns structured JSON: product names, descriptions, and item tables
3. **Incremental merge** — results are merged by product name after each page, deduplicated by item number, and saved to `gpt_catalogue_progress.json`
4. **Browse & edit** — the viewer loads the extracted catalogue, provides search/filter, inline table editing, add/delete rows and columns

### Project Structure

```
database_update/
├── server.py                      # Flask app — upload, extraction, job polling
├── index.html                     # Landing page — links to upload and viewer
├── upload.html                    # PDF upload with page range, progress polling
├── frontend.html                  # Catalogue viewer — search, edit tables in-browser
├── multi_modal_rag.ipynb          # Notebook: experimental RAG pipeline setup
├── catalogue_data.json            # Sample/hand-maintained catalogue data
└── gpt_catalogue_progress.json    # Live extraction output (updated per page)
```

### API Reference

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/` | Landing page |
| `GET` | `/upload.html` | Upload interface |
| `GET` | `/frontend.html` | Catalogue viewer |
| `GET` | `/gpt_catalogue_progress.json` | Current extraction results |
| `POST` | `/upload` | Upload PDF + start extraction (multipart, optional `page_from`/`page_to`) |
| `GET` | `/job/<job_id>` | Poll job status and progress |

### Data Format

The extracted catalogue is a JSON object keyed by product name:

```json
{
  "Product Name": {
    "description": "Technical description of the product",
    "items": [
      { "item_number": "001", "reference_code": "ABC-123", "size": "100mm", ... }
    ]
  }
}
```

Item columns are dynamic — whatever the LLM extracts from the PDF table headers.

### Environment

- **LLM:** Groq API with `meta-llama/llama-4-scout-17b-16e-instruct`
- **Requires:** `GROQ_API_KEY` in `.env`

---

## Environment Variables Summary

| Variable | Used by | Required |
|----------|---------|----------|
| `OPENAI_API_KEY` | CAD Analyze (GPT-4o-mini) | Yes, for CAD Analyze |
| `GROQ_API_KEY` | Database Update (Llama 4 Scout) | Yes, for Database Update |
