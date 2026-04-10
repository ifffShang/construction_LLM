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
import database as db

BASE_DIR = os.path.dirname(__file__)
app = Flask(__name__, static_folder=BASE_DIR)
CORS(app)

jobs: dict = {}

OUTPUTS_DIR = os.path.join(BASE_DIR, "outputs")
PROJECTS_DIR = os.path.join(BASE_DIR, "projects")
os.makedirs(OUTPUTS_DIR, exist_ok=True)
os.makedirs(PROJECTS_DIR, exist_ok=True)

db.init_db()


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
    """Save uploaded files to disk and record them in the database."""
    pdir = os.path.join(PROJECTS_DIR, project_id, file_type)
    os.makedirs(pdir, exist_ok=True)
    for f in files:
        fpath = os.path.join(pdir, f["filename"])
        with open(fpath, "wb") as fp:
            fp.write(f["bytes"])
        db.add_file(
            project_id=project_id,
            file_type=file_type,
            filename=f["filename"],
            stored_path=fpath,
            size_bytes=len(f["bytes"]),
        )


# ── page routes ────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return send_from_directory(BASE_DIR, "home.html")


@app.route("/app/<project_id>")
def project_app(project_id: str):
    project = db.get_project(project_id)
    if not project:
        return "Project not found", 404
    return send_from_directory(BASE_DIR, "app.html")


# ── project API ────────────────────────────────────────────────────────────────

@app.route("/api/projects", methods=["GET"])
def list_projects():
    projects = db.list_projects()
    return jsonify({"projects": projects})


@app.route("/api/projects", methods=["POST"])
def create_project():
    body = request.get_json(force=True)
    name = body.get("name", "").strip()
    if not name:
        return jsonify({"error": "Project name is required"}), 400

    project_id = str(uuid.uuid4())[:12]
    os.makedirs(os.path.join(PROJECTS_DIR, project_id), exist_ok=True)

    project = db.create_project(
        project_id=project_id,
        name=name,
        owner=body.get("owner", ""),
        epcm=body.get("epcm", ""),
        project_no=body.get("project_no", ""),
        contractor=body.get("contractor", ""),
        sub_contractor=body.get("sub_contractor", ""),
    )

    return jsonify({"project_id": project_id, "project": project})


@app.route("/api/projects/<project_id>", methods=["GET"])
def get_project(project_id: str):
    project = db.get_project(project_id)
    if not project:
        return jsonify({"error": "Not found"}), 404
    return jsonify({"project": project})


@app.route("/api/projects/<project_id>", methods=["DELETE"])
def delete_project(project_id: str):
    db.delete_project(project_id)
    pdir = os.path.join(PROJECTS_DIR, project_id)
    if os.path.isdir(pdir):
        shutil.rmtree(pdir, ignore_errors=True)
    return jsonify({"ok": True})


@app.route("/api/projects/<project_id>/files", methods=["GET"])
def list_project_files(project_id: str):
    """List all uploaded files for a project, optionally filtered by type."""
    file_type = request.args.get("type")  # 'cad' or 'spec'
    files = db.list_files(project_id, file_type)
    return jsonify({"files": files})


@app.route("/api/projects/<project_id>/files", methods=["DELETE"])
def delete_project_file(project_id: str):
    """Delete a specific file from a project."""
    body = request.get_json(force=True)
    file_type = body.get("file_type")
    filename  = body.get("filename")
    if not file_type or not filename:
        return jsonify({"error": "file_type and filename required"}), 400

    pdir = os.path.join(PROJECTS_DIR, project_id, file_type, filename)
    if os.path.exists(pdir):
        os.remove(pdir)
    db.delete_file(project_id, file_type, filename)
    return jsonify({"ok": True})


@app.route("/api/projects/<project_id>/jobs", methods=["GET"])
def list_project_jobs(project_id: str):
    """List all persisted jobs for a project."""
    return jsonify({"jobs": db.list_jobs(project_id)})


@app.route("/api/projects/<project_id>/materials", methods=["GET"])
def list_project_materials(project_id: str):
    """All extracted + merged tracker rows for a project.
    Optional query params: building=<id>, material_name=<search>
    """
    building      = request.args.get("building")
    material_name = request.args.get("material_name")
    rows = db.list_materials(project_id=project_id, building=building,
                             material_name=material_name)
    return jsonify({"total": len(rows), "materials": rows})


@app.route("/api/materials", methods=["GET"])
def list_all_materials():
    """All extracted materials across all projects.
    Optional query params: project_id=, building=, material_name=
    """
    project_id    = request.args.get("project_id")
    building      = request.args.get("building")
    material_name = request.args.get("material_name")
    rows = db.list_materials(project_id=project_id, building=building,
                             material_name=material_name)
    return jsonify({"total": len(rows), "materials": rows})


# ── spec library API ───────────────────────────────────────────────────────────

@app.route("/api/spec-library", methods=["GET"])
def list_spec_companies():
    return jsonify({"companies": db.list_spec_companies()})


@app.route("/api/spec-library", methods=["POST"])
def create_spec_company():
    """Create a new company (or reuse existing) and upload its spec files."""
    company_name = request.form.get("company_name", "").strip()
    if not company_name:
        return jsonify({"error": "company_name is required"}), 400

    existing = db.find_spec_company_by_name(company_name)
    if existing:
        company = existing
    else:
        company = db.create_spec_company(company_name)
    company_id = company["id"]

    cdir = os.path.join(db.SPEC_LIB_DIR, str(company_id))
    os.makedirs(cdir, exist_ok=True)

    existing_names = {f["filename"] for f in company.get("files", [])}
    uploaded = request.files.getlist("files")
    for f in uploaded:
        if f and f.filename and f.filename not in existing_names:
            data = f.read()
            fpath = os.path.join(cdir, f.filename)
            with open(fpath, "wb") as fp:
                fp.write(data)
            db.add_spec_company_file(
                company_id=company_id,
                filename=f.filename,
                stored_path=fpath,
                size_bytes=len(data),
            )

    return jsonify({"company": db.get_spec_company(company_id)})


@app.route("/api/spec-library/<int:company_id>", methods=["DELETE"])
def delete_spec_company(company_id: int):
    cdir = os.path.join(db.SPEC_LIB_DIR, str(company_id))
    if os.path.isdir(cdir):
        shutil.rmtree(cdir, ignore_errors=True)
    db.delete_spec_company(company_id)
    return jsonify({"ok": True})


@app.route("/api/spec-library/<int:company_id>/files", methods=["POST"])
def upload_spec_company_files(company_id: int):
    """Add more files to an existing spec company, skipping duplicates."""
    company = db.get_spec_company(company_id)
    if not company:
        return jsonify({"error": "Company not found"}), 404

    existing_names = {f["filename"] for f in company.get("files", [])}
    cdir = os.path.join(db.SPEC_LIB_DIR, str(company_id))
    os.makedirs(cdir, exist_ok=True)

    uploaded = request.files.getlist("files")
    added = 0
    for f in uploaded:
        if f and f.filename and f.filename not in existing_names:
            data = f.read()
            fpath = os.path.join(cdir, f.filename)
            with open(fpath, "wb") as fp:
                fp.write(data)
            db.add_spec_company_file(
                company_id=company_id,
                filename=f.filename,
                stored_path=fpath,
                size_bytes=len(data),
            )
            existing_names.add(f.filename)
            added += 1

    return jsonify({"company": db.get_spec_company(company_id), "added": added})


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
    db.upsert_job(job_id, project_id or None, "cad", "queued", 0, 0, [])
    thread = threading.Thread(
        target=_run_and_persist,
        args=(run_cad_extraction, job_id, files, jobs, project_id or None, "cad"),
        daemon=True,
    )
    thread.start()
    return jsonify({"job_id": job_id})


@app.route("/upload-spec", methods=["POST"])
def upload_spec():
    company_id = request.form.get("company_id", "")
    project_id = request.form.get("project_id", "")

    if company_id:
        files = db.load_spec_company_file_bytes(int(company_id))
    else:
        files = _read_uploaded_files("files")

    if not files:
        return jsonify({"error": "No files provided"}), 400

    if project_id:
        _save_files_to_project(project_id, "spec", files)

    job_id, job = _new_job()
    db.upsert_job(job_id, project_id or None, "spec", "queued", 0, 0, [])
    thread = threading.Thread(
        target=_run_and_persist,
        args=(run_spec_extraction, job_id, files, jobs, project_id or None, "spec"),
        daemon=True,
    )
    thread.start()
    return jsonify({"job_id": job_id})


def _run_and_persist(run_fn, job_id: str, files, jobs_dict: dict,
                     project_id: str | None, job_type: str):
    """Wrapper that runs an extraction function and persists the final state to DB."""
    run_fn(job_id, files, jobs_dict)
    job = jobs_dict.get(job_id, {})
    db.upsert_job(
        job_id=job_id,
        project_id=project_id,
        job_type=job_type,
        status=job.get("status", "done"),
        progress=job.get("progress", 0),
        total=job.get("total", 0),
        log=job.get("log", []),
        result=job.get("result"),
    )


@app.route("/job/<job_id>")
def job_status(job_id: str):
    job = jobs.get(job_id)
    if not job:
        # fall back to DB for jobs from previous server sessions
        job = db.get_job(job_id)
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
    cad_job_id   = body.get("cad_job_id", "")
    spec_job_id  = body.get("spec_job_id", "")
    project_meta = body.get("project_metadata", {})

    cad_job  = jobs.get(cad_job_id) or db.get_job(cad_job_id)
    spec_job = jobs.get(spec_job_id) or (db.get_job(spec_job_id) if spec_job_id else None)

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
    project_id = project_meta.get("project_id") or None
    stats = {
        "total_rows": len(merged["rows"]),
        "buildings":  len(merged["quantity_basis"]),
        "no_spec":  sum(1 for r in merged["rows"] if not r.get("adnoc_spec_ref")),
    }
    jobs[gen_id] = {
        "status": "done",
        "file": output_path,
        "filename": output_filename,
        "stats": stats,
    }
    db.upsert_job(
        job_id=gen_id,
        project_id=project_id,
        job_type="generate",
        status="done",
        progress=0,
        total=0,
        log=[],
        result={"filename": output_filename, "stats": stats},
    )
    if project_id:
        db.save_tracker_rows(project_id, gen_id, merged["rows"])

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
