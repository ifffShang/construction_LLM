"""
Flask server for the Construction Material Tracker.
Port: 8766  (independent of the catalogue extractor on 8765)
"""

import os
import json
import uuid
import shutil
import threading
from datetime import datetime, timezone
from flask import Flask, request, jsonify, send_from_directory, send_file
from flask_cors import CORS
from dotenv import load_dotenv

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "..", ".env"))

from extractor_cad  import run_cad_extraction
from extractor_spec import run_spec_extraction
from merger         import merge
from excel_writer   import write_tracker

BASE_DIR = os.path.dirname(__file__)
app = Flask(__name__, static_folder=BASE_DIR)
CORS(app)

jobs: dict = {}

OUTPUTS_DIR = os.path.join(BASE_DIR, "outputs")
PROJECTS_DIR = os.path.join(BASE_DIR, "projects")
os.makedirs(OUTPUTS_DIR, exist_ok=True)
os.makedirs(PROJECTS_DIR, exist_ok=True)


# ── project helpers ────────────────────────────────────────────────────────────

def _projects_index_path() -> str:
    return os.path.join(PROJECTS_DIR, "_index.json")


def _load_projects() -> list[dict]:
    path = _projects_index_path()
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _save_projects(projects: list[dict]):
    with open(_projects_index_path(), "w", encoding="utf-8") as f:
        json.dump(projects, f, ensure_ascii=False, indent=2)


def _get_project(project_id: str) -> dict | None:
    for p in _load_projects():
        if p["id"] == project_id:
            return p
    return None


def _update_project(project_id: str, updates: dict):
    projects = _load_projects()
    for p in projects:
        if p["id"] == project_id:
            p.update(updates)
            break
    _save_projects(projects)


def _project_dir(project_id: str) -> str:
    return os.path.join(PROJECTS_DIR, project_id)


# ── generic helpers ────────────────────────────────────────────────────────────

def _new_job() -> tuple[str, dict]:
    job_id = str(uuid.uuid4())
    job = {"status": "queued", "progress": 0, "total": 0, "log": [], "result": None}
    jobs[job_id] = job
    return job_id, job


def _read_uploaded_files(request_files_key: str) -> list[dict]:
    uploaded = request.files.getlist(request_files_key)
    result = []
    for f in uploaded:
        if f and f.filename:
            result.append({"filename": f.filename, "bytes": f.read()})
    return result


def _save_files_to_project(project_id: str, file_type: str, files: list[dict]):
    """Save uploaded files to disk and record them in the project index."""
    pdir = os.path.join(_project_dir(project_id), file_type)
    os.makedirs(pdir, exist_ok=True)
    saved_names = []
    for f in files:
        fpath = os.path.join(pdir, f["filename"])
        with open(fpath, "wb") as fp:
            fp.write(f["bytes"])
        saved_names.append(f["filename"])

    project = _get_project(project_id)
    if project:
        key = f"{file_type}_files"
        existing = project.get(key, [])
        for name in saved_names:
            if name not in existing:
                existing.append(name)
        _update_project(project_id, {key: existing})


# ── page routes ────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return send_from_directory(BASE_DIR, "home.html")


@app.route("/app/<project_id>")
def project_app(project_id: str):
    project = _get_project(project_id)
    if not project:
        return "Project not found", 404
    return send_from_directory(BASE_DIR, "app.html")


# ── project API ────────────────────────────────────────────────────────────────

@app.route("/api/projects", methods=["GET"])
def list_projects():
    projects = _load_projects()
    projects.sort(key=lambda p: p.get("created_at", ""), reverse=True)
    return jsonify({"projects": projects})


@app.route("/api/projects", methods=["POST"])
def create_project():
    body = request.get_json(force=True)
    name = body.get("name", "").strip()
    if not name:
        return jsonify({"error": "Project name is required"}), 400

    project_id = str(uuid.uuid4())[:12]
    project = {
        "id": project_id,
        "name": name,
        "owner": body.get("owner", ""),
        "epcm": body.get("epcm", ""),
        "project_no": body.get("project_no", ""),
        "contractor": body.get("contractor", ""),
        "sub_contractor": body.get("sub_contractor", ""),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "cad_files": [],
        "spec_files": [],
    }

    os.makedirs(_project_dir(project_id), exist_ok=True)

    projects = _load_projects()
    projects.append(project)
    _save_projects(projects)

    return jsonify({"project_id": project_id, "project": project})


@app.route("/api/projects/<project_id>", methods=["GET"])
def get_project(project_id: str):
    project = _get_project(project_id)
    if not project:
        return jsonify({"error": "Not found"}), 404
    return jsonify({"project": project})


@app.route("/api/projects/<project_id>", methods=["DELETE"])
def delete_project(project_id: str):
    projects = _load_projects()
    projects = [p for p in projects if p["id"] != project_id]
    _save_projects(projects)
    pdir = _project_dir(project_id)
    if os.path.isdir(pdir):
        shutil.rmtree(pdir, ignore_errors=True)
    return jsonify({"ok": True})


# ── upload / extraction endpoints ──────────────────────────────────────────────

@app.route("/upload-cad", methods=["POST"])
def upload_cad():
    files = _read_uploaded_files("files")
    if not files:
        return jsonify({"error": "No files provided"}), 400

    project_id = request.form.get("project_id", "")
    if project_id:
        _save_files_to_project(project_id, "cad", files)

    job_id, job = _new_job()
    thread = threading.Thread(
        target=run_cad_extraction,
        args=(job_id, files, jobs),
        daemon=True,
    )
    thread.start()
    return jsonify({"job_id": job_id})


@app.route("/upload-spec", methods=["POST"])
def upload_spec():
    files = _read_uploaded_files("files")
    if not files:
        return jsonify({"error": "No files provided"}), 400

    project_id = request.form.get("project_id", "")
    if project_id:
        _save_files_to_project(project_id, "spec", files)

    job_id, job = _new_job()
    thread = threading.Thread(
        target=run_spec_extraction,
        args=(job_id, files, jobs),
        daemon=True,
    )
    thread.start()
    return jsonify({"job_id": job_id})


@app.route("/job/<job_id>")
def job_status(job_id: str):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    return jsonify({
        "status":   job["status"],
        "progress": job["progress"],
        "total":    job["total"],
        "log":      job["log"][-30:],
        "summary":  _job_summary(job),
    })


def _job_summary(job: dict) -> dict:
    """Return a lightweight summary of extraction results for the review panel."""
    result = job.get("result")
    if not result:
        return {}
    if isinstance(result, dict) and all(
        isinstance(v, dict) and "material_callouts" in v for v in result.values()
    ):
        return {
            "type": "cad",
            "buildings": [
                {
                    "id": bid,
                    "roof_area_m2": data.get("roof_area_m2"),
                    "callouts": len(data.get("material_callouts", [])),
                    "rooms": data.get("rooms", []),
                    "drawing_refs": data.get("drawing_refs", []),
                }
                for bid, data in result.items()
            ],
        }
    return {
        "type": "spec",
        "documents": [
            {
                "ref": ref,
                "title": info.get("title", ""),
                "materials": len(info.get("materials", [])),
            }
            for ref, info in result.items()
        ],
    }


@app.route("/generate", methods=["POST"])
def generate():
    body = request.get_json(force=True)
    cad_job_id  = body.get("cad_job_id", "")
    spec_job_id = body.get("spec_job_id", "")
    project_meta = body.get("project_metadata", {})

    cad_job  = jobs.get(cad_job_id)
    spec_job = jobs.get(spec_job_id)

    if not cad_job or cad_job["status"] != "done":
        return jsonify({"error": "CAD extraction not complete"}), 400

    cad_result  = cad_job["result"] or {}
    spec_result = (spec_job["result"] or {}) if spec_job and spec_job["status"] == "done" else {}

    try:
        merged = merge(cad_result, spec_result)
    except Exception as e:
        return jsonify({"error": f"Merge failed: {e}"}), 500

    output_filename = f"Material_Tracker_{str(uuid.uuid4())[:8]}.xlsx"
    output_path = os.path.join(OUTPUTS_DIR, output_filename)

    try:
        write_tracker(
            rows           = merged["rows"],
            quantity_basis = merged["quantity_basis"],
            avl_reference  = merged["avl_reference"],
            output_path    = output_path,
            project_metadata = project_meta,
        )
    except Exception as e:
        return jsonify({"error": f"Excel generation failed: {e}"}), 500

    gen_id = str(uuid.uuid4())
    jobs[gen_id] = {
        "status": "done",
        "file": output_path,
        "filename": output_filename,
        "stats": {
            "total_rows": len(merged["rows"]),
            "buildings":  len(merged["quantity_basis"]),
            "no_spec":  sum(1 for r in merged["rows"] if not r.get("adnoc_spec_ref")),
        },
    }

    return jsonify({
        "gen_id":   gen_id,
        "filename": output_filename,
        "stats":    jobs[gen_id]["stats"],
    })


@app.route("/download/<gen_id>")
def download(gen_id: str):
    job = jobs.get(gen_id)
    if not job or "file" not in job:
        return jsonify({"error": "Not found"}), 404
    return send_file(
        job["file"],
        as_attachment=True,
        download_name=job["filename"],
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


if __name__ == "__main__":
    print("🚀  Construction Material Tracker running on http://localhost:8766")
    app.run(port=8766, debug=False)
