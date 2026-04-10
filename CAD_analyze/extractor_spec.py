"""
Specification document extractor — reads ADNOC AGES specs and construction standards,
extracting: spec refs, material descriptions, international standards, approved vendors.
"""

import json
import base64
import os
import time
import fitz
from langchain_core.messages import HumanMessage
from langchain_openai import ChatOpenAI
from dotenv import load_dotenv

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "..", ".env"))

KNOWN_SYSTEMS = (
    "Roof | Walls | Floor | Ceiling | Doors | Partitions | "
    "External Finishes | Internal Finishes | Protection | Joints | "
    "Roof Drainage | Substructure Protection | External Works | Plaster / Mortar"
)

KNOWN_CATEGORIES = (
    "Waterproofing | Insulation | Roof Finish | Roof Drainage | Wall System | "
    "External Finishes | Internal Finishes | Floor Finish | Floor System | "
    "Ceiling | Doors | Hardware | Partitions | Coating | Sealant | Joints | "
    "Metal Works | Paving | Treatment | Plaster / Mortar | Protection"
)


def _get_llm():
    return ChatOpenAI(
        model="gpt-4o-mini",
        temperature=0,
        api_key=os.getenv("OPENAI_API_KEY"),
    )


def page_to_base64(pdf_bytes: bytes, page_num: int, dpi: int = 150) -> str:
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    page = doc[page_num - 1]
    pix = page.get_pixmap(dpi=dpi)
    img_b64 = base64.b64encode(pix.tobytes("jpeg")).decode()
    doc.close()
    return img_b64


def extract_spec_page(
    pdf_bytes: bytes,
    page_num: int,
    llm,
    prev_doc_ref: str | None = None,
    prev_material_names: list[str] | None = None,
) -> dict:
    img_b64 = page_to_base64(pdf_bytes, page_num)

    context = ""
    if prev_doc_ref:
        context += f'\nHint: The previous page had document reference "{prev_doc_ref}". Use this if no different reference appears on this page.'
    if prev_material_names:
        names_str = ", ".join(f'"{n}"' for n in prev_material_names[-3:])
        context += f"\nHint: The previous page discussed materials: {names_str}. If this page continues those topics without a new heading, carry them forward."

    prompt = f"""You are analyzing a construction specification document for an ADNOC industrial project.

Extract the following into a JSON object:

{{
  "document_ref": "<specification number from header/footer/cover e.g. AGES-SP-01-017, GG-000-20-40-007 — null if not found>",
  "document_title": "<full title of this specification document>",
  "materials_covered": [
    {{
      "material_name": "<canonical material name, e.g. 'SBS waterproofing membrane', 'Bitumen primer', 'Steel door'>",
      "system": "<one of: {KNOWN_SYSTEMS}>",
      "category": "<one of: {KNOWN_CATEGORIES}>",
      "description": "<technical description including key specs: thickness, grade, application method>",
      "international_standards": ["<e.g. ASTM D6163>", "<BS EN ...>"],
      "performance_requirements": "<key performance criteria as brief text>",
      "approved_vendors": [
        {{
          "vendor_name": "<full local trading entity name>",
          "manufacturer": "<manufacturer brand name>",
          "origin": "<country of manufacture>"
        }}
      ]
    }}
  ]
}}

Rules:
- document_ref: look in headers, footers, title block, cover page. Typical ADNOC format: "AGES-SP-01-017" or "GG-000-20-40-007"
- material_name: use standard construction terminology. Do NOT use section header text as material name.
- approved_vendors: only include if explicitly listed as approved/qualified suppliers. Extract the full company name.
- If this page is a cover page, index, or has no material requirements, return materials_covered as [].
- Return ONLY valid JSON, no explanation text.{context}"""

    msg = HumanMessage(content=[
        {"type": "text", "text": prompt},
        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"}},
    ])

    for attempt in range(4):
        try:
            response = llm.invoke([msg]).content.strip()
            print(f"  [SPEC p{page_num}] raw response ({len(response)} chars): {response[:300]}")
            if response.startswith("```"):
                response = response.split("```")[1]
                if response.startswith("json"):
                    response = response[4:]
            return json.loads(response)
        except json.JSONDecodeError as e:
            print(f"  [SPEC p{page_num}] JSON parse error: {e}\n  Response was: {response[:500]}")
            return {}
        except Exception as e:
            if "rate_limit" in str(e).lower() and attempt < 3:
                wait = 2 ** attempt
                print(f"  [SPEC p{page_num}] rate limited, retrying in {wait}s…")
                time.sleep(wait)
                continue
            print(f"  [SPEC p{page_num}] LLM error: {type(e).__name__}: {e}")
            return {}


def run_spec_extraction(job_id: str, files: list[dict], jobs: dict):
    """
    files: list of {"filename": str, "bytes": bytes}
    Accumulates results into jobs[job_id]["result"]:
      { doc_ref: { title, materials: [{ material_name, system, category, description,
                                        international_standards, approved_vendors }] } }
    """
    job = jobs[job_id]
    job["status"] = "processing"

    try:
        llm = _get_llm()
        total_pages = sum(
            len(fitz.open(stream=f["bytes"], filetype="pdf"))
            for f in files
        )
        job["total"] = total_pages
        job["progress"] = 0

        # spec_data: { doc_ref: { title, materials: [] } }
        spec_data: dict = {}
        processed = 0

        for file_info in files:
            filename = file_info["filename"]
            pdf_bytes = file_info["bytes"]
            doc = fitz.open(stream=pdf_bytes, filetype="pdf")
            n_pages = len(doc)
            doc.close()

            prev_doc_ref = None
            prev_material_names: list[str] = []

            for page_num in range(1, n_pages + 1):
                processed += 1
                job["progress"] = processed
                job["log"].append(f"[{filename}] page {page_num}/{n_pages}…")

                result = extract_spec_page(pdf_bytes, page_num, llm, prev_doc_ref, prev_material_names)
                time.sleep(1)

                doc_ref = result.get("document_ref") or prev_doc_ref or "UNKNOWN"
                doc_title = result.get("document_title", "")
                materials = result.get("materials_covered", [])

                if doc_ref not in spec_data:
                    spec_data[doc_ref] = {"title": doc_title, "materials": []}
                if doc_title and not spec_data[doc_ref]["title"]:
                    spec_data[doc_ref]["title"] = doc_title

                # Deduplicate by material_name
                existing_names = {m["material_name"] for m in spec_data[doc_ref]["materials"]}
                cur_names = []
                for mat in materials:
                    name = mat.get("material_name", "")
                    if not name:
                        continue
                    cur_names.append(name)
                    if name not in existing_names:
                        spec_data[doc_ref]["materials"].append(mat)
                        existing_names.add(name)
                    else:
                        # Merge vendors into existing entry
                        for existing in spec_data[doc_ref]["materials"]:
                            if existing["material_name"] == name:
                                new_vendors = mat.get("approved_vendors", [])
                                ev_names = {v["vendor_name"] for v in existing.get("approved_vendors", [])}
                                for v in new_vendors:
                                    if v["vendor_name"] not in ev_names:
                                        existing.setdefault("approved_vendors", []).append(v)

                prev_doc_ref = doc_ref
                prev_material_names = cur_names if cur_names else prev_material_names

                job["log"][-1] = (
                    f"[{filename}] page {page_num}/{n_pages} ✅ "
                    f"{doc_ref} | {len(materials)} materials"
                )

        job["result"] = spec_data
        job["status"] = "done"
        n_materials = sum(len(v["materials"]) for v in spec_data.values())
        job["log"].append(
            f"✅ Spec extraction done. {len(spec_data)} documents, {n_materials} materials total."
        )

    except Exception as e:
        job["status"] = "error"
        job["log"].append(f"❌ Error: {e}")
