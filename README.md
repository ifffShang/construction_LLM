# GPT scans each PDF page, extracts product names and specs into a catalogue, falling back to the previous page's title when no title is found.

This repo currently includes a simple ingestion pipeline that:

- loads `.txt`, `.md`, and `.pdf` files from a folder
- chunks them with overlap
- embeds them locally (SentenceTransformers)
- stores them in a persistent local Chroma database

## Setup

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## Ingest documents

1) Create a `docs/` folder and add files (`.txt`, `.md`, `.pdf`).

2) Run:

```bash
python ingestion_pipeline.py --docs_dir ./docs --persist_dir ./chroma_db
```

Re-running the command will skip chunks that were already added (based on a stable chunk id).
