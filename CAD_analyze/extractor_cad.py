"""
CAD PDF extractor — renders each page as an image and asks the vision LLM
to identify building IDs, material callouts, and dimensions.
"""

import json
import base64
import os
import time
import fitz  # PyMuPDF
from langchain_core.messages import HumanMessage
from langchain_openai import ChatOpenAI
from dotenv import load_dotenv
from templates import BUILDING_ID_HINTS

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "..", ".env"))


def _get_llm():
    return ChatOpenAI(
        model="gpt-4o-mini",
        temperature=0,
        api_key=os.getenv("OPENAI_API_KEY"),
    )


def page_to_base64(pdf_bytes: bytes, page_num: int, dpi: int = 200) -> str:
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    page = doc[page_num - 1]
    pix = page.get_pixmap(dpi=dpi)
    img_b64 = base64.b64encode(pix.tobytes("jpeg")).decode()
    doc.close()
    return img_b64


def _building_hint_from_filename(filename: str) -> str | None:
    """Try to resolve building ID from the filename using known doc-number patterns."""
    filename_upper = filename.upper()
    for pattern, building_id in BUILDING_ID_HINTS.items():
        if pattern.upper().replace("-", "") in filename_upper.replace("-", "").replace("_", ""):
            return building_id
    return None


def extract_cad_page(pdf_bytes: bytes, page_num: int, llm, prev_building_id: str | None = None, filename: str = "") -> dict:
    """Extract structured data from one CAD drawing page."""
    img_b64 = page_to_base64(pdf_bytes, page_num)

    hint_from_file = _building_hint_from_filename(filename)
    building_hint = ""
    if hint_from_file:
        building_hint = f'\n\nHint: Based on the document number in the filename, this drawing likely belongs to building "{hint_from_file}". Use this only if no clearer building ID is visible on the page.'
    elif prev_building_id:
        building_hint = f'\n\nHint: The previous page belonged to building "{prev_building_id}". If no different building ID is visible on this page, you may carry it forward.'

    prompt = f"""You are analyzing a construction CAD drawing page from an industrial facility project (ADNOC gas plant, UAE).

Extract the following into a JSON object:

{{
  "building_id": "<building tag visible on this page, e.g. IES-201, SS-202, OMS-201 — from title block or room labels. null if not determinable>",
  "drawing_type": "<PLAN | SECTION | ELEVATION | SCHEDULE | DETAIL | TITLE>",
  "sheet_title": "<sheet title from title block, e.g. 'GROUND FLOOR PLAN'>",
  "drawing_ref": "<document/drawing number from title block, e.g. HE-H4-278-20-22-001>",
  "scale": "<drawing scale e.g. 1:100, or null>",
  "dimensions_found": [
    {{"label": "<e.g. Roof Plan, Room A>", "value": "<e.g. 38400 x 19200mm>", "area_m2": <computed numeric or null>}}
  ],
  "material_callouts": [
    {{
      "location": "<Roof | External Walls | Internal Walls | Floor | Ceiling | Door | Partition | External Works | Foundation>",
      "material_text": "<exact annotation text from drawing, e.g. '4mm SBS waterproofing membrane'>",
      "thickness_or_spec": "<e.g. 4mm, 200mm, null>",
      "quantity_hint": <numeric area or length if readable, else null>
    }}
  ],
  "room_labels": ["<list of room or zone names visible, e.g. EQUIPMENT ROOM, BATTERY ROOM>"],
  "schedule_items": [
    {{"type": "<DOOR | WINDOW | FINISH | ROOM>", "mark": "<e.g. D01>", "description": "<brief description>"}}
  ]
}}

Rules:
- building_id: look for labels like "IES-201", "SS-202", "OMS-203" in the title block, room labels, or drawing number. Also look for text like "INSTRUMENT ELECTRICAL ROOM", "SUBSTATION", "MAINTENANCE ROOM" which map to IES, SS, OMS respectively.
- drawing_ref: extract the full document number (e.g. "30201_50150_LT_HE-H4-278-20-22-001")
- area_m2: if both dimensions are visible and scale is given, compute. If scale says 1:100 and dimensions show 384x192 on paper, area = 38.4 x 19.2 = 737.3m². Otherwise null.
- material_callouts: extract EVERY annotated material or specification note visible on the page.
- Do NOT invent data. Only extract what is visibly printed.
- Return ONLY valid JSON, no explanation text.{building_hint}"""

    msg = HumanMessage(content=[
        {"type": "text", "text": prompt},
        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"}},
    ])

    try:
        response = llm.invoke([msg]).content.strip()
        print(f"  [CAD p{page_num}] raw response ({len(response)} chars): {response[:300]}")
        if response.startswith("```"):
            response = response.split("```")[1]
            if response.startswith("json"):
                response = response[4:]
        return json.loads(response)
    except json.JSONDecodeError as e:
        print(f"  [CAD p{page_num}] JSON parse error: {e}\n  Response was: {response[:500]}")
        return {}
    except Exception as e:
        print(f"  [CAD p{page_num}] LLM error: {type(e).__name__}: {e}")
        return {}


def run_cad_extraction(job_id: str, files: list[dict], jobs: dict):
    """
    files: list of {"filename": str, "bytes": bytes}
    Accumulates results into jobs[job_id]["result"] keyed by building_id.
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

        # buildings: { "IES-201": { drawing_refs, roof_area_m2, material_callouts, rooms, schedules } }
        buildings: dict = {}
        processed = 0

        for file_info in files:
            filename = file_info["filename"]
            pdf_bytes = file_info["bytes"]
            doc = fitz.open(stream=pdf_bytes, filetype="pdf")
            n_pages = len(doc)
            doc.close()

            prev_building_id = None

            for page_num in range(1, n_pages + 1):
                processed += 1
                job["progress"] = processed
                job["log"].append(f"[{filename}] page {page_num}/{n_pages}…")

                result = extract_cad_page(pdf_bytes, page_num, llm, prev_building_id, filename)

                building_id = result.get("building_id") or prev_building_id
                if not building_id:
                    job["log"][-1] += " — no building ID, skipped"
                    continue

                prev_building_id = building_id

                if building_id not in buildings:
                    buildings[building_id] = {
                        "drawing_refs": [],
                        "roof_area_m2": None,
                        "material_callouts": [],
                        "rooms": [],
                        "schedule_items": [],
                    }

                b = buildings[building_id]

                # drawing ref
                ref = result.get("drawing_ref", "")
                if ref and ref not in b["drawing_refs"]:
                    b["drawing_refs"].append(ref)

                # roof area — take the largest area found on a PLAN page
                if result.get("drawing_type") == "PLAN":
                    for dim in result.get("dimensions_found", []):
                        a = dim.get("area_m2")
                        if a and isinstance(a, (int, float)):
                            if b["roof_area_m2"] is None or a > b["roof_area_m2"]:
                                b["roof_area_m2"] = round(a, 1)

                # material callouts — deduplicate by material_text
                existing_texts = {c["material_text"] for c in b["material_callouts"]}
                for callout in result.get("material_callouts", []):
                    if callout.get("material_text") and callout["material_text"] not in existing_texts:
                        b["material_callouts"].append(callout)
                        existing_texts.add(callout["material_text"])

                # rooms
                for room in result.get("room_labels", []):
                    if room and room not in b["rooms"]:
                        b["rooms"].append(room)

                # schedule items
                for item in result.get("schedule_items", []):
                    if item not in b["schedule_items"]:
                        b["schedule_items"].append(item)

                n_callouts = len(result.get("material_callouts", []))
                job["log"][-1] = (
                    f"[{filename}] page {page_num}/{n_pages} ✅ "
                    f"{building_id} | {result.get('drawing_type','?')} | "
                    f"{n_callouts} callouts"
                )

        job["result"] = buildings
        job["status"] = "done"
        job["log"].append(f"✅ CAD extraction done. Buildings found: {list(buildings.keys())}")

    except Exception as e:
        job["status"] = "error"
        job["log"].append(f"❌ Error: {e}")
