"""
Main Flask application.
All routes live here; agent logic is in agent.py, DB queries in db.py.
"""

import io
import json
import time
import uuid
from datetime import date
from flask import (
    Flask,
    Response,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    stream_with_context,
    url_for,
)

import config
import db
from agent import AgentRunner
from apply_agent import ApplyRunner
from email_tracker import get_tracker

app = Flask(__name__)
app.secret_key = config.SECRET_KEY

# In-memory store for active agent runs keyed by run_id
_active_runs: dict[str, AgentRunner] = {}
# In-memory store for active apply runs keyed by run_id
_active_apply_runs: dict[str, ApplyRunner] = {}


# ---------------------------------------------------------------------------
# Initialise DB on startup (SQLite — no config needed)
# ---------------------------------------------------------------------------
with app.app_context():
    db.init_db()
    # Auto-start email tracker if credentials are configured
    if config.SMTP_USER and config.SMTP_PASSWORD and config.SMTP_USER != "you@gmail.com":
        get_tracker().start()


# ---------------------------------------------------------------------------
# Page routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    stats = db.get_stats()
    recent = db.list_applications()[:5]
    return render_template("index.html", stats=stats, recent=recent)


@app.route("/new-application")
def new_application():
    return render_template("new_application.html")


@app.route("/applications")
def applications():
    apps = db.list_applications()
    return render_template("applications.html", applications=apps)


@app.route("/application/<int:app_id>")
def view_application(app_id):
    app_data = db.get_application(app_id)
    if not app_data:
        return redirect(url_for("applications"))
    events = db.get_events(app_id)
    return render_template("view_application.html", application=app_data, events=events)


@app.route("/settings")
def settings():
    tracker_status = get_tracker().status()
    return render_template("settings.html", tracker=tracker_status)


# ---------------------------------------------------------------------------
# REST API – applications CRUD
# ---------------------------------------------------------------------------

@app.route("/api/applications", methods=["GET"])
def api_list_applications():
    apps = db.list_applications()
    for a in apps:
        # Convert date objects to strings for JSON serialisation
        if isinstance(a.get("applied_date"), date):
            a["applied_date"] = a["applied_date"].isoformat()
        if a.get("created_at"):
            a["created_at"] = str(a["created_at"])
        if a.get("updated_at"):
            a["updated_at"] = str(a["updated_at"])
    return jsonify(apps)


@app.route("/api/applications/<int:app_id>", methods=["GET"])
def api_get_application(app_id):
    app_data = db.get_application(app_id)
    if not app_data:
        return jsonify({"error": "Not found"}), 404
    for key in ("applied_date", "created_at", "updated_at"):
        if app_data.get(key):
            app_data[key] = str(app_data[key])
    return jsonify(app_data)


@app.route("/api/applications/<int:app_id>", methods=["PATCH"])
def api_update_application(app_id):
    data = request.get_json(force=True)
    if not data:
        return jsonify({"error": "No data"}), 400
    ok = db.update_application(app_id, data)
    return jsonify({"success": ok})


@app.route("/api/applications/<int:app_id>", methods=["DELETE"])
def api_delete_application(app_id):
    ok = db.delete_application(app_id)
    return jsonify({"success": ok})


@app.route("/api/stats", methods=["GET"])
def api_stats():
    return jsonify(db.get_stats())


# ---------------------------------------------------------------------------
# Agent run – starts background run, returns run_id
# ---------------------------------------------------------------------------

@app.route("/api/run-agent", methods=["POST"])
def run_agent():
    data = request.get_json(force=True) or {}
    job_url = (data.get("job_url") or "").strip()
    resume_text = (data.get("resume_text") or "").strip()

    if not job_url:
        return jsonify({"error": "job_url is required"}), 400
    if not resume_text:
        return jsonify({"error": "resume_text is required"}), 400

    run_id = str(uuid.uuid4())
    runner = AgentRunner(job_url=job_url, resume_text=resume_text)
    _active_runs[run_id] = runner
    runner.start()

    return jsonify({"run_id": run_id})


# ---------------------------------------------------------------------------
# SSE stream for agent progress
# ---------------------------------------------------------------------------

@app.route("/api/run-agent/stream/<run_id>")
def stream_agent(run_id):
    runner = _active_runs.get(run_id)
    if not runner:
        return jsonify({"error": "run not found"}), 404

    def generate():
        while True:
            try:
                event = runner.event_queue.get(timeout=60)
            except Exception:
                # Timeout – send keep-alive
                yield "data: {\"step\":\"ping\"}\n\n"
                continue

            payload = json.dumps(event)
            yield f"data: {payload}\n\n"

            if event.get("step") in ("complete", "error"):
                # Save to DB on completion
                if event["step"] == "complete":
                    result = runner.get_result()
                    if result:
                        try:
                            app_id = db.create_application(
                                {
                                    "job_url": runner.job_url,
                                    "company": result.get("company"),
                                    "role": result.get("role"),
                                    "jd_text": result.get("jd_text"),
                                    "resume_original": runner.resume_text,
                                    "resume_tailored": result.get("resume_tailored"),
                                    "cover_letter": result.get("cover_letter"),
                                    "analysis": result.get("analysis"),
                                    "ats_score": result.get("ats_score"),
                                    "status": "pending",
                                }
                            )
                            yield f"data: {json.dumps({'step':'saved','app_id':app_id})}\n\n"
                        except Exception as db_err:
                            yield f"data: {json.dumps({'step':'error','message':str(db_err)})}\n\n"
                # Clean up
                _active_runs.pop(run_id, None)
                break

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


# ---------------------------------------------------------------------------
# Individual API endpoints (used when not running full agent)
# ---------------------------------------------------------------------------

@app.route("/api/scrape-job", methods=["POST"])
def scrape_job():
    data = request.get_json(force=True) or {}
    job_url = (data.get("job_url") or "").strip()
    if not job_url:
        return jsonify({"error": "job_url required"}), 400
    from agent import scrape_job_tool
    result = scrape_job_tool.invoke(job_url)
    if result.startswith("ERROR"):
        return jsonify({"error": result}), 500
    return jsonify({"jd_text": result})


@app.route("/api/analyze", methods=["POST"])
def analyze():
    data = request.get_json(force=True) or {}
    resume = data.get("resume", "")
    jd = data.get("jd", "")
    if not resume or not jd:
        return jsonify({"error": "resume and jd required"}), 400
    from agent import analyze_resume_tool
    result = analyze_resume_tool.invoke(json.dumps({"resume": resume, "jd": jd}))
    return jsonify({"analysis": result})


@app.route("/api/generate-resume", methods=["POST"])
def generate_resume():
    data = request.get_json(force=True) or {}
    from agent import rewrite_resume_tool
    result = rewrite_resume_tool.invoke(json.dumps(data))
    return jsonify({"resume_tailored": result})


@app.route("/api/generate-cover-letter", methods=["POST"])
def generate_cover_letter():
    data = request.get_json(force=True) or {}
    from agent import generate_cover_letter_tool
    result = generate_cover_letter_tool.invoke(json.dumps(data))
    return jsonify({"cover_letter": result})


@app.route("/api/parse-resume-pdf", methods=["POST"])
def parse_resume_pdf():
    if "resume_pdf" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400
    f = request.files["resume_pdf"]
    if not f.filename.lower().endswith(".pdf"):
        return jsonify({"error": "Only PDF files are supported"}), 400
    try:
        import pypdf
        reader = pypdf.PdfReader(io.BytesIO(f.read()))
        text = "\n".join(page.extract_text() or "" for page in reader.pages).strip()
        if not text:
            return jsonify({"error": "Could not extract text from PDF. Try a text-based PDF."}), 400
        return jsonify({"resume_text": text})
    except Exception as e:
        return jsonify({"error": f"Failed to parse PDF: {e}"}), 500


@app.route("/api/applications/<int:app_id>/download-resume", methods=["GET"])
def download_resume_pdf(app_id):
    app_data = db.get_application(app_id)
    if not app_data:
        return jsonify({"error": "Not found"}), 404
    resume_text = app_data.get("resume_tailored") or app_data.get("resume_original") or ""
    if not resume_text:
        return jsonify({"error": "No resume available"}), 404

    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib.units import mm
        from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
        from reportlab.lib.enums import TA_LEFT

        buf = io.BytesIO()
        doc = SimpleDocTemplate(buf, pagesize=A4,
                                leftMargin=20*mm, rightMargin=20*mm,
                                topMargin=20*mm, bottomMargin=20*mm)
        styles = getSampleStyleSheet()
        body_style = ParagraphStyle("body", parent=styles["Normal"],
                                    fontSize=10, leading=14, alignment=TA_LEFT)
        heading_style = ParagraphStyle("heading", parent=styles["Heading2"],
                                       fontSize=12, leading=16, spaceBefore=8)

        story = []
        for line in resume_text.splitlines():
            stripped = line.strip()
            if not stripped:
                story.append(Spacer(1, 4))
                continue
            # Heuristic: all-caps short lines are section headings
            if stripped.isupper() and len(stripped) < 60:
                story.append(Paragraph(stripped, heading_style))
            else:
                safe = stripped.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                story.append(Paragraph(safe, body_style))

        doc.build(story)
        buf.seek(0)

        company = (app_data.get("company") or "company").replace(" ", "_")
        role = (app_data.get("role") or "role").replace(" ", "_")
        filename = f"resume_{role}_{company}.pdf"

        return send_file(buf, mimetype="application/pdf",
                         as_attachment=True, download_name=filename)
    except Exception as e:
        return jsonify({"error": f"PDF generation failed: {e}"}), 500


@app.route("/api/send-email", methods=["POST"])
def send_email():
    data = request.get_json(force=True) or {}
    app_id = data.get("app_id")
    to_email = data.get("to_email", "").strip()
    if not to_email:
        return jsonify({"error": "to_email required"}), 400

    app_data = db.get_application(app_id) if app_id else None
    cover = data.get("cover_letter") or (app_data or {}).get("cover_letter", "")
    role = data.get("role") or (app_data or {}).get("role", "the role")
    company = data.get("company") or (app_data or {}).get("company", "the company")

    from agent import send_email_tool
    result = send_email_tool.invoke(
        json.dumps(
            {
                "to_email": to_email,
                "subject": f"Application for {role} at {company}",
                "body": cover,
            }
        )
    )
    if result == "SUCCESS" and app_id:
        db.update_application(app_id, {"status": "applied", "follow_up_sent": True})
    return jsonify({"result": result})


# ---------------------------------------------------------------------------
# Apply agent — starts browser automation in background
# ---------------------------------------------------------------------------

@app.route("/api/apply/<int:app_id>", methods=["POST"])
def api_apply(app_id):
    app_data = db.get_application(app_id)
    if not app_data:
        return jsonify({"error": "Application not found"}), 404
    if not app_data.get("job_url"):
        return jsonify({"error": "No job URL on this application"}), 400

    run_id = str(uuid.uuid4())
    runner = ApplyRunner(app_id=app_id)
    _active_apply_runs[run_id] = runner
    runner.start()
    return jsonify({"run_id": run_id})


@app.route("/api/apply/stream/<run_id>")
def api_apply_stream(run_id):
    runner = _active_apply_runs.get(run_id)
    if not runner:
        return jsonify({"error": "run not found"}), 404

    def generate():
        while True:
            try:
                event = runner.event_queue.get(timeout=120)
            except Exception:
                yield "data: {\"message\":\"ping\"}\n\n"
                continue

            yield f"data: {json.dumps(event)}\n\n"
            if event.get("done"):
                _active_apply_runs.pop(run_id, None)
                break

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---------------------------------------------------------------------------
# Application events
# ---------------------------------------------------------------------------

@app.route("/api/applications/<int:app_id>/events", methods=["GET"])
def api_get_events(app_id):
    events = db.get_events(app_id)
    return jsonify(events)


@app.route("/api/applications/<int:app_id>/events", methods=["POST"])
def api_add_event(app_id):
    data = request.get_json(force=True) or {}
    event_id = db.add_event(
        app_id,
        event_type=data.get("event_type", "note"),
        title=data.get("title", "Note"),
        body=data.get("body", ""),
        source="user",
    )
    return jsonify({"id": event_id})


# ---------------------------------------------------------------------------
# Email tracker control
# ---------------------------------------------------------------------------

@app.route("/api/tracker/status", methods=["GET"])
def api_tracker_status():
    return jsonify(get_tracker().status())


@app.route("/api/tracker/start", methods=["POST"])
def api_tracker_start():
    get_tracker().start()
    return jsonify({"ok": True, "status": get_tracker().status()})


@app.route("/api/tracker/stop", methods=["POST"])
def api_tracker_stop():
    get_tracker().stop()
    return jsonify({"ok": True})


@app.route("/api/tracker/poll", methods=["POST"])
def api_tracker_poll():
    """Manually trigger one poll cycle (for testing)."""
    try:
        get_tracker()._poll()
        return jsonify({"ok": True, "status": get_tracker().status()})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # use_reloader=False avoids the multiprocessing/ctypes import in werkzeug debug mode
    app.run(debug=config.DEBUG, host="0.0.0.0", port=5000, use_reloader=False)
