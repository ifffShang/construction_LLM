"""
Merger — cross-references CAD extraction results with spec extraction results
to produce tracker rows, quantity basis, and AVL reference entries.

Logic:
  1. Only materials actually extracted from CAD drawings are included.
  2. Each CAD material is fuzzy-matched to the MATERIAL_TAXONOMY for canonical names.
  3. If spec documents explicitly cover that material, the row is enriched with
     spec data (standards, vendors, performance requirements).
  4. If no spec match is found, the row uses taxonomy defaults (or raw CAD data
     when the callout doesn't match any taxonomy entry).
"""

import difflib
from templates import MATERIAL_TAXONOMY, VENDOR_FALLBACKS, STATUS_DEFAULTS


def _normalize(text: str) -> str:
    return text.lower().replace("-", " ").replace("_", " ").strip()


def _match_material(callout_text: str) -> dict | None:
    """
    Fuzzy-match a raw CAD callout string to a canonical material in MATERIAL_TAXONOMY.
    Returns the taxonomy entry or None.
    """
    norm = _normalize(callout_text)
    best_score = 0.0
    best_entry = None

    for entry in MATERIAL_TAXONOMY:
        keyword_hits = sum(1 for kw in entry["keywords"] if kw in norm)
        if keyword_hits == 0:
            continue

        seq_score = difflib.SequenceMatcher(
            None, norm, _normalize(entry["material_name"])
        ).ratio()

        score = keyword_hits * 0.4 + seq_score * 0.6
        if score > best_score:
            best_score = score
            best_entry = entry

    return best_entry if best_score > 0.25 else None


def _vendor_string(vendors: list[dict]) -> str:
    """Format a list of vendor dicts into a pipe-delimited string."""
    if not vendors:
        return ""
    parts = []
    for v in vendors:
        name = v.get("vendor_name", "")
        origin = v.get("origin", "")
        manufacturer = v.get("manufacturer", "")
        if manufacturer and manufacturer.lower() != name.lower():
            parts.append(f"{name} / {manufacturer} ({origin})" if origin else f"{name} / {manufacturer}")
        else:
            parts.append(f"{name} ({origin})" if origin else name)
    return " | ".join(parts)


def _find_spec_material(material_name: str, spec_data: dict) -> dict | None:
    """Search spec_data for any document that has a matching material entry."""
    norm = _normalize(material_name)
    best_score = 0.0
    best_mat = None

    for doc_info in spec_data.values():
        for mat in doc_info.get("materials", []):
            score = difflib.SequenceMatcher(
                None, norm, _normalize(mat.get("material_name", ""))
            ).ratio()
            if score > best_score:
                best_score = score
                best_mat = mat

    return best_mat if best_score > 0.55 else None


def _find_spec_ref(material_name: str, spec_data: dict) -> str:
    """Find which document ref covers this material."""
    norm = _normalize(material_name)
    best_score = 0.0
    best_ref = ""

    for doc_ref, doc_info in spec_data.items():
        for mat in doc_info.get("materials", []):
            score = difflib.SequenceMatcher(
                None, norm, _normalize(mat.get("material_name", ""))
            ).ratio()
            if score > best_score:
                best_score = score
                best_ref = doc_ref

    return best_ref if best_score > 0.55 else ""


def _infer_unit_tag(building_id: str) -> str:
    unit_tag = ""
    unit_map = {"201": "213", "202": "280", "203": "262"}
    for prefix in ("SS-", "IES-", "OMS-"):
        if prefix in building_id:
            suffix = building_id.replace(prefix, "")
            unit_tag = unit_map.get(suffix, suffix)
            break
    return unit_tag


def _make_raw_entry(callout: dict) -> dict:
    """Build a pseudo-taxonomy entry for a CAD callout that matched no taxonomy item."""
    text = callout.get("material_text", "Unknown material")
    location = callout.get("location", "")
    return {
        "material_name": text,
        "system": location or "Unknown",
        "category": location or "Unknown",
        "adnoc_spec_ref": "",
        "international_standard": "",
        "unit": "TBD",
        "default_description": text,
    }


def merge(cad_result: dict, spec_data: dict) -> dict:
    """
    Produces:
      - rows: list of tracker row dicts (one per building x material)
      - quantity_basis: list of { building, roof_area_m2, note }
      - avl_reference: list of { material_name, vendors, notes }
    """
    rows = []
    sn = 1
    all_material_names: list[str] = []

    buildings = sorted(cad_result.keys()) if cad_result else []

    for building_id in buildings:
        b_data = cad_result[building_id]
        drawing_refs = ", ".join(b_data.get("drawing_refs", []))
        roof_area = b_data.get("roof_area_m2")
        unit_tag = _infer_unit_tag(building_id)

        seen_materials: set[str] = set()

        for callout in b_data.get("material_callouts", []):
            callout_text = callout.get("material_text", "")
            if not callout_text:
                continue

            entry = _match_material(callout_text)
            if entry:
                mat_name = entry["material_name"]
            else:
                entry = _make_raw_entry(callout)
                mat_name = entry["material_name"]

            if mat_name in seen_materials:
                continue
            seen_materials.add(mat_name)

            if mat_name not in all_material_names:
                all_material_names.append(mat_name)

            qty_hint = callout.get("quantity_hint")
            if entry["unit"] == "m²" and entry.get("system") == "Roof" and roof_area:
                qty = str(round(roof_area))
            elif qty_hint and isinstance(qty_hint, (int, float)):
                qty = str(round(qty_hint))
            else:
                qty = "TBD"

            adnoc_spec_ref = entry.get("adnoc_spec_ref", "")
            intl_standard = entry.get("international_standard", "")
            description = entry.get("default_description", callout_text)
            vendor_str = ""

            spec_mat = _find_spec_material(mat_name, spec_data) if spec_data else None
            spec_ref_from_doc = _find_spec_ref(mat_name, spec_data) if spec_data else ""

            if spec_mat:
                if spec_ref_from_doc and spec_ref_from_doc not in ("UNKNOWN", ""):
                    if adnoc_spec_ref and spec_ref_from_doc not in adnoc_spec_ref:
                        adnoc_spec_ref = f"{adnoc_spec_ref} / {spec_ref_from_doc}"
                    elif not adnoc_spec_ref:
                        adnoc_spec_ref = spec_ref_from_doc

                if spec_mat.get("international_standards"):
                    standards = spec_mat["international_standards"]
                    intl_standard = " / ".join(standards) if isinstance(standards, list) else str(standards)
                if spec_mat.get("description"):
                    description = spec_mat["description"]
                vendors = spec_mat.get("approved_vendors", [])
                if vendors:
                    vendor_str = _vendor_string(vendors)

            if not vendor_str:
                vendor_str = VENDOR_FALLBACKS.get(mat_name, "TBD")

            if "HOLD" in description.upper() or "HOLD" in mat_name.upper():
                statuses = {
                    "submittal_status": "HOLD",
                    "consultant_approval": "HOLD",
                    "client_approval": "HOLD",
                    "avl_status": "TBD",
                    "po_status": "TBD",
                    "production_status": "Not Started",
                    "delivery_status": "Not Delivered",
                    "site_status": "Not Installed",
                    "installation_status": "Not Started",
                }
            else:
                statuses = dict(STATUS_DEFAULTS)

            rows.append({
                "sn": sn,
                "building": building_id,
                "unit": unit_tag,
                "system": entry.get("system", ""),
                "material_category": entry.get("category", ""),
                "material_name": mat_name,
                "adnoc_spec_ref": adnoc_spec_ref,
                "international_standard": intl_standard,
                "description": description,
                "approved_vendor": vendor_str,
                "brand": statuses.get("brand", STATUS_DEFAULTS["brand"]),
                "origin": statuses.get("origin", STATUS_DEFAULTS["origin"]),
                "unit": entry.get("unit", "TBD"),
                "quantity": qty,
                "drawing_ref": drawing_refs,
                "submittal_status": statuses["submittal_status"],
                "consultant_approval": statuses["consultant_approval"],
                "client_approval": statuses["client_approval"],
                "avl_status": statuses["avl_status"],
                "po_status": statuses["po_status"],
                "production_status": statuses["production_status"],
                "delivery_status": statuses["delivery_status"],
                "site_status": statuses["site_status"],
                "installation_status": statuses["installation_status"],
                "confirmed_in_cad": True,
            })
            sn += 1

    # Quantity Basis
    quantity_basis = []
    for building_id in buildings:
        b_data = cad_result[building_id]
        roof_area = b_data.get("roof_area_m2")
        refs = ", ".join(b_data.get("drawing_refs", []))
        note = (
            f"Roof area takeoff from architectural drawings: {refs}"
            if refs
            else "Area TBD — drawing ref not identified"
        )
        quantity_basis.append({
            "building": building_id,
            "roof_area_m2": str(round(roof_area)) if roof_area else "TBD",
            "note": note,
        })

    # AVL Reference — only for materials that actually appeared in CAD
    avl_reference = []
    for mat_name in all_material_names:
        spec_mat = _find_spec_material(mat_name, spec_data) if spec_data else None
        if spec_mat and spec_mat.get("approved_vendors"):
            vendor_str = _vendor_string(spec_mat["approved_vendors"])
        else:
            vendor_str = VENDOR_FALLBACKS.get(mat_name, "TBD")

        notes = ""
        if spec_mat:
            notes = spec_mat.get("performance_requirements", "")

        avl_reference.append({
            "material_name": mat_name,
            "vendors": vendor_str,
            "notes": notes,
        })

    return {
        "rows": rows,
        "quantity_basis": quantity_basis,
        "avl_reference": avl_reference,
    }
