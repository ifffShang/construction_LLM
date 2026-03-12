import os
import json
import threading
import base64
import uuid
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from dotenv import load_dotenv
import fitz
from langchain_core.messages import HumanMessage
from langchain_groq import ChatGroq

load_dotenv()

app = Flask(__name__, static_folder=".")
CORS(app)

DOCS_DIR = "./docs"
PROGRESS_FILE = "gpt_catalogue_progress.json"

# In-memory job tracker: job_id -> {status, progress, total, log, catalogue}
jobs = {}


def page_to_base64(pdf_path: str, page_num: int, dpi: int = 150) -> str:
    doc = fitz.open(pdf_path)
    page = doc[page_num - 1]
    pix = page.get_pixmap(dpi=dpi)
    img_b64 = base64.b64encode(pix.tobytes("jpeg")).decode()
    doc.close()
    return img_b64


def extract_page_catalogue(pdf_path, page_num, llm, context_products=None):
    img_b64 = page_to_base64(pdf_path, page_num)
    context_hint = ""
    if context_products:
        products_str = ", ".join(f'"{p}"' for p in context_products)
        context_hint = f"""

    Note: The previous page introduced product(s): {products_str}.
    If any heading on THIS page looks like a SECTION HEADER rather than a product name
    (e.g. "PRODUCT INFORMATION", "TECHNICAL INFORMATION", "APPLICATION INFORMATION",
    "SYSTEM INFORMATION", "BASIS OF PRODUCT DATA", "MAINTENANCE", "ECOLOGY", etc.),
    do NOT use it as product_name — set product_name to null instead.
    The calling code will automatically assign items to the correct product.
    """

    prompt = """This is a page from a building materials product catalogue.

Extract ALL product data from this page and return a JSON ARRAY. Each element represents one product section:
[
  {
    "product_name": "ARCH LINTELS",
    "description": "brief product description",
    "items": [
      {"item_number": "SMAB_F_00054403", "attribute1": "value1", "attribute2": "value2"},
      ...
    ]
  }
]

Rules:
- If the page is a cover/title page with no table, return: [{"product_name": "THE TITLE", "description": "...", "items": []}]
- Only return [] if the page has absolutely no useful information
- Use the exact column names from each table as attribute keys
- product_name MUST be a large heading/title visibly printed on THIS page
- If there is no clear large title, set product_name to null
- Return ONLY a valid JSON array, no extra text""" + context_hint

    msg = HumanMessage(content=[
        {"type": "text", "text": prompt},
        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"}}
    ])

    response = llm.invoke([msg]).content.strip()
    if response.startswith("```"):
        response = response.split("```")[1]
        if response.startswith("json"):
            response = response[4:]
    try:
        parsed = json.loads(response)
        return parsed if isinstance(parsed, list) else [parsed]
    except json.JSONDecodeError:
        return []


def run_extraction(job_id, pdf_path, page_from=1, page_to=None):
    job = jobs[job_id]
    try:
        llm = ChatGroq(
            model="meta-llama/llama-4-scout-17b-16e-instruct",
            temperature=0,
            api_key=os.getenv("GROQ_API_KEY")
        )
        doc = fitz.open(pdf_path)
        total_pages = len(doc)
        doc.close()

        start = max(1, page_from)
        end = min(total_pages, page_to) if page_to else total_pages

        job["total"] = end - start + 1
        job["status"] = "processing"

        catalogue = {}
        last_product_name = None
        prev_product_names = []

        for page_num in range(start, end + 1):
            job["progress"] = page_num - start + 1
            job["log"].append(f"Processing page {page_num}/{end}...")

            results = extract_page_catalogue(
                pdf_path, page_num, llm,
                context_products=prev_product_names if prev_product_names else None
            )

            cur_product_names = []
            for entry in results:
                product_name = entry.get("product_name")
                items = entry.get("items", [])

                if product_name:
                    cur_product_names.append(product_name)

                if not product_name and items:
                    product_name = prev_product_names[-1] if prev_product_names else last_product_name

                if product_name and items:
                    if product_name not in catalogue:
                        catalogue[product_name] = {"description": entry.get("description", ""), "items": []}
                    existing_ids = {item.get("item_number") for item in catalogue[product_name]["items"]}
                    new_items = [item for item in items if item.get("item_number") not in existing_ids]
                    catalogue[product_name]["items"].extend(new_items)
                    last_product_name = product_name
                    job["log"][-1] = f"Page {page_num}/{end} ✅ {product_name} ({len(new_items)} items)"
                elif product_name:
                    if product_name not in catalogue:
                        catalogue[product_name] = {"description": entry.get("description", ""), "items": []}
                    job["log"][-1] = f"Page {page_num}/{end} 📌 {product_name} (title only)"
                else:
                    job["log"][-1] = f"Page {page_num}/{end} — skipped"

            prev_product_names = cur_product_names

            with open(PROGRESS_FILE, "w", encoding="utf-8") as f:
                json.dump(catalogue, f, ensure_ascii=False)
            job["catalogue"] = catalogue

        job["status"] = "done"
        job["log"].append(f"✅ Done! Extracted {len(catalogue)} products.")

    except Exception as e:
        job["status"] = "error"
        job["log"].append(f"❌ Error: {str(e)}")


@app.route("/")
def index():
    return send_from_directory(".", "upload.html")

@app.route("/frontend.html")
def frontend():
    return send_from_directory(".", "frontend.html")

@app.route("/gpt_catalogue_progress.json")
def progress_json():
    return send_from_directory(".", "gpt_catalogue_progress.json")

@app.route("/upload", methods=["POST"])
def upload():
    if "pdf" not in request.files:
        return jsonify({"error": "No file provided"}), 400

    f = request.files["pdf"]
    if not f.filename.lower().endswith(".pdf"):
        return jsonify({"error": "Only PDF files are accepted"}), 400

    os.makedirs(DOCS_DIR, exist_ok=True)
    pdf_path = os.path.join(DOCS_DIR, f.filename)
    f.save(pdf_path)

    try:
        page_from = int(request.form.get("page_from", 1))
    except (TypeError, ValueError):
        page_from = 1

    page_to_raw = request.form.get("page_to", "")
    try:
        page_to = int(page_to_raw) if page_to_raw else None
    except (TypeError, ValueError):
        page_to = None

    job_id = str(uuid.uuid4())
    jobs[job_id] = {
        "status": "queued",
        "progress": 0,
        "total": 0,
        "log": [],
        "catalogue": {}
    }

    thread = threading.Thread(target=run_extraction, args=(job_id, pdf_path, page_from, page_to), daemon=True)
    thread.start()

    return jsonify({"job_id": job_id})


@app.route("/job/<job_id>")
def job_status(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    return jsonify({
        "status": job["status"],
        "progress": job["progress"],
        "total": job["total"],
        "log": job["log"][-20:],
    })


if __name__ == "__main__":
    app.run(port=8765, debug=False)
