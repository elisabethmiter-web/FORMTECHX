"""FormSign — store forms, send them to clients as a signing packet,
and get confirmation once every form is signed.

Run locally:  ADMIN_PASSWORD=changeme python app.py
"""
import base64
import hashlib
import hmac
import json
import mimetypes
import os
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from functools import wraps

from flask import (Flask, abort, flash, g, redirect, render_template, request,
                   send_file, session, url_for)
from werkzeug.utils import secure_filename

import forms
import mailer
import pdfgen

# ---------------------------------------------------------------- config
DATA_DIR = os.environ.get("DATA_DIR", os.path.join(os.path.dirname(__file__), "data"))
DB_PATH = os.path.join(DATA_DIR, "formsign.db")
UPLOAD_DIR = os.path.join(DATA_DIR, "templates")
SIGNED_DIR = os.path.join(DATA_DIR, "signed")
for d in (DATA_DIR, UPLOAD_DIR, SIGNED_DIR):
    os.makedirs(d, exist_ok=True)

ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
BUSINESS_NAME = os.environ.get("BUSINESS_NAME", "FormSign")
BASE_URL = os.environ.get("BASE_URL", "").rstrip("/")
LINK_DAYS = int(os.environ.get("LINK_EXPIRY_DAYS", "30"))

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY") or secrets.token_hex(32)
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024  # 50 MB per upload
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
if os.environ.get("HTTPS", "1") == "1" and BASE_URL.startswith("https"):
    app.config["SESSION_COOKIE_SECURE"] = True

FIELD_TYPES = ["text", "textarea", "email", "phone", "date", "number", "checkbox", "initials"]


# ---------------------------------------------------------------- database
SCHEMA = """
CREATE TABLE IF NOT EXISTS templates (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('pdf','file','form','specs')),
  description TEXT DEFAULT '',
  file_path TEXT,
  file_sha256 TEXT,
  fields_json TEXT DEFAULT '[]',
  archived INTEGER DEFAULT 0,
  created_at TEXT NOT NULL,
  file_name TEXT,
  file_mime TEXT
);
CREATE TABLE IF NOT EXISTS packets (
  id INTEGER PRIMARY KEY,
  token TEXT UNIQUE NOT NULL,
  client_name TEXT NOT NULL,
  client_email TEXT NOT NULL,
  message TEXT DEFAULT '',
  status TEXT NOT NULL DEFAULT 'draft',
  created_at TEXT NOT NULL,
  sent_at TEXT,
  first_viewed_at TEXT,
  completed_at TEXT,
  expires_at TEXT,
  voided_at TEXT
);
CREATE TABLE IF NOT EXISTS packet_items (
  id INTEGER PRIMARY KEY,
  packet_id INTEGER NOT NULL REFERENCES packets(id),
  template_id INTEGER REFERENCES templates(id),
  position INTEGER NOT NULL,
  template_name TEXT NOT NULL,
  template_kind TEXT NOT NULL,
  template_file TEXT,
  template_sha256 TEXT,
  fields_json TEXT DEFAULT '[]',
  template_body TEXT DEFAULT '',
  answers_json TEXT,
  signer_name TEXT,
  signature_path TEXT,
  signed_at TEXT,
  signed_pdf_path TEXT,
  signed_sha256 TEXT,
  ip TEXT,
  user_agent TEXT,
  file_name TEXT,
  file_mime TEXT,
  requires_signature INTEGER NOT NULL DEFAULT 1,
  adhoc INTEGER NOT NULL DEFAULT 0,
  downloaded_at TEXT
);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY,
  packet_id INTEGER NOT NULL REFERENCES packets(id),
  type TEXT NOT NULL,
  detail TEXT DEFAULT '',
  ip TEXT,
  at TEXT NOT NULL
);
"""


def db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def _rebuild_if(conn, table, outdated):
    """Upgrade a table from an older version of the app, keeping its rows."""
    row = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
    if not row or not outdated(row[0]):
        return
    old_cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.executescript(f"ALTER TABLE {table} RENAME TO {table}_old;")
    conn.executescript(SCHEMA)
    new_cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    cols = ", ".join(c for c in old_cols if c in new_cols)
    conn.executescript(f"INSERT INTO {table} ({cols}) SELECT {cols} FROM {table}_old; DROP TABLE {table}_old;")


def init_db():
    conn = sqlite3.connect(DB_PATH)
    _rebuild_if(conn, "templates", lambda sql: "'file'" not in sql or "file_mime" not in sql)
    _rebuild_if(conn, "packet_items", lambda sql: "template_id INTEGER NOT NULL" in sql or "requires_signature" not in sql)
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()


init_db()


# ---------------------------------------------------------------- helpers
def now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def client_ip():
    fwd = request.headers.get("X-Forwarded-For", "")
    return (fwd.split(",")[0].strip() if fwd else request.remote_addr) or ""


def log_event(packet_id, type_, detail=""):
    db().execute("INSERT INTO events (packet_id, type, detail, ip, at) VALUES (?,?,?,?,?)",
                 (packet_id, type_, detail, client_ip(), now()))


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def signing_url(token):
    base = BASE_URL or request.url_root.rstrip("/")
    return f"{base}/s/{token}"


@app.template_filter("when")
def fmt_when(value):
    if not value:
        return "—"
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return value
    return dt.strftime("%b %d, %Y · %H:%M UTC")


@app.template_filter("day")
def fmt_day(value):
    if not value:
        return "—"
    try:
        return datetime.fromisoformat(value).strftime("%b %d, %Y")
    except ValueError:
        return value


@app.context_processor
def inject_globals():
    return {"business_name": BUSINESS_NAME, "csrf_token": csrf_token,
            "email_enabled": mailer.enabled()}


# ---------------------------------------------------------------- auth + csrf
def csrf_token():
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(24)
    return session["csrf"]


@app.before_request
def check_csrf():
    if request.method == "POST":
        sent = request.form.get("csrf") or request.headers.get("X-CSRF-Token", "")
        if not sent or not hmac.compare_digest(sent, session.get("csrf", "")):
            abort(400, "Your session expired. Reload the page and try again.")


def login_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not session.get("admin"):
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapper


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if not ADMIN_PASSWORD:
        error = "Set the ADMIN_PASSWORD environment variable before signing in."
    elif request.method == "POST":
        if hmac.compare_digest(request.form.get("password", ""), ADMIN_PASSWORD):
            session.clear()
            session["admin"] = True
            session.permanent = True
            nxt = request.args.get("next", "")
            return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else url_for("dashboard"))
        error = "That password is incorrect."
    return render_template("login.html", error=error)


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("login"))


# ---------------------------------------------------------------- packet status
def packet_progress(packet_id):
    row = db().execute(
        "SELECT COUNT(*) total, SUM(signed_at IS NOT NULL) signed FROM packet_items "
        "WHERE packet_id=? AND requires_signature=1",
        (packet_id,)).fetchone()
    return row["signed"] or 0, row["total"] or 0


def effective_status(p):
    if p["voided_at"]:
        return "voided"
    if p["completed_at"]:
        return "completed"
    if p["expires_at"] and p["expires_at"] < now() and p["status"] != "draft":
        return "expired"
    return p["status"]


# ---------------------------------------------------------------- admin: dashboard
@app.route("/")
@login_required
def dashboard():
    rows = db().execute("SELECT * FROM packets ORDER BY created_at DESC").fetchall()
    packets = []
    counts = {"awaiting": 0, "viewed": 0, "completed": 0}
    for p in rows:
        signed, total = packet_progress(p["id"])
        st = effective_status(p)
        if st == "sent":
            counts["awaiting"] += 1
        elif st in ("viewed", "in_progress"):
            counts["viewed"] += 1
        elif st == "completed":
            counts["completed"] += 1
        packets.append({**dict(p), "signed": signed, "total": total, "state": st})
    filt = request.args.get("show", "all")
    if filt == "open":
        packets = [p for p in packets if p["state"] in ("sent", "viewed", "in_progress", "draft")]
    elif filt == "completed":
        packets = [p for p in packets if p["state"] == "completed"]
    has_templates = db().execute("SELECT 1 FROM templates WHERE archived=0 LIMIT 1").fetchone()
    return render_template("dashboard.html", packets=packets, counts=counts, show=filt,
                           has_templates=bool(has_templates))


# ---------------------------------------------------------------- admin: form library
@app.route("/library")
@login_required
def library():
    rows = db().execute("SELECT * FROM templates WHERE archived=0 ORDER BY name COLLATE NOCASE").fetchall()
    templates = [{**dict(t), "field_count": len(json.loads(t["fields_json"] or "[]"))} for t in rows]
    return render_template("library.html", templates=templates)


def save_upload(f):
    """Store any uploaded file. Returns dict with stored name, original name, mime, sha256 and kind
    ('pdf' for a readable PDF we can stamp, 'file' for everything else)."""
    original = os.path.basename(f.filename or "file")[:200] or "file"
    stored = f"{secrets.token_hex(8)}-{secure_filename(original) or 'file'}"
    path = os.path.join(UPLOAD_DIR, stored)
    f.save(path)
    if os.path.getsize(path) == 0:
        os.remove(path)
        raise ValueError(f"“{original}” is empty.")
    with open(path, "rb") as fh:
        head = fh.read(5)
    kind = "pdf" if head == b"%PDF-" and pdfgen.is_readable_pdf(path) else "file"
    mime = "application/pdf" if head == b"%PDF-" else (mimetypes.guess_type(original)[0] or f.mimetype
                                                       or "application/octet-stream")
    return {"stored": stored, "name": original, "mime": mime, "sha256": sha256_file(path), "kind": kind}


INLINE_MIMES = {"application/pdf", "image/png", "image/jpeg", "image/gif", "image/webp"}


def send_stored(stored, mime, download_name, force_download=False):
    """Serve an uploaded file. Only PDFs and common images are shown in the browser; everything
    else is sent as a download so it can never run as a web page."""
    inline = mime in INLINE_MIMES and not force_download
    return send_file(os.path.join(UPLOAD_DIR, stored), mimetype=mime if inline else "application/octet-stream",
                     as_attachment=not inline, download_name=download_name)


@app.template_filter("filetype")
def filetype_label(name, mime=""):
    ext = os.path.splitext(name or "")[1].lower().lstrip(".")
    labels = {"pdf": "PDF", "doc": "Word document", "docx": "Word document", "xls": "Excel spreadsheet",
              "xlsx": "Excel spreadsheet", "csv": "Spreadsheet (CSV)", "ppt": "PowerPoint", "pptx": "PowerPoint",
              "jpg": "Image", "jpeg": "Image", "png": "Image", "gif": "Image", "webp": "Image", "heic": "Image",
              "txt": "Text file", "zip": "ZIP archive", "dwg": "CAD drawing", "dxf": "CAD drawing",
              "rtf": "Text document", "odt": "Document", "pages": "Pages document", "numbers": "Numbers spreadsheet"}
    return labels.get(ext, (ext.upper() + " file") if ext else "File")


@app.route("/library/upload", methods=["POST"])
@login_required
def upload_pdf():
    f = request.files.get("file")
    name = request.form.get("name", "").strip()
    if not f or not f.filename:
        flash("Choose a file to upload.", "error")
        return redirect(url_for("library"))
    try:
        up = save_upload(f)
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("library"))
    fields = parse_fields_from_request()
    db().execute(
        "INSERT INTO templates (name, kind, description, file_path, file_sha256, fields_json, created_at, "
        "file_name, file_mime) VALUES (?,?,?,?,?,?,?,?,?)",
        (name or os.path.splitext(up["name"])[0], up["kind"], request.form.get("description", "").strip(),
         up["stored"], up["sha256"], json.dumps(fields), now(), up["name"], up["mime"]))
    db().commit()
    flash("Added to your library.", "ok")
    return redirect(url_for("library"))


def parse_fields_from_request():
    raw = request.form.get("fields_json", "[]")
    try:
        items = json.loads(raw)
    except ValueError:
        return []
    fields = []
    for i, it in enumerate(items if isinstance(items, list) else []):
        label = str(it.get("label", "")).strip()[:200]
        ftype = it.get("type", "text")
        if not label or ftype not in FIELD_TYPES:
            continue
        fields.append({"id": f"f{i+1}", "label": label, "type": ftype,
                       "required": bool(it.get("required")),
                       "help": str(it.get("help", "")).strip()[:300]})
    return fields


@app.route("/library/specs", methods=["POST"])
@login_required
def add_spec_form():
    d = forms.SPEC_FORM
    db().execute("INSERT INTO templates (name, kind, description, fields_json, created_at) VALUES (?,?,?,?,?)",
                 (request.form.get("name", "").strip() or d["name"], "specs", d["description"], "[]", now()))
    db().commit()
    flash("Spec form added to your library.", "ok")
    return redirect(url_for("library"))


@app.route("/library/new", methods=["GET", "POST"])
@login_required
def new_form():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        fields = parse_fields_from_request()
        if not name or not fields:
            flash("Give the form a name and at least one field.", "error")
            return render_template("form_builder.html", form=None,
                                   fields_json=request.form.get("fields_json", "[]"),
                                   name=name, body=request.form.get("body", ""))
        db().execute(
            "INSERT INTO templates (name, kind, description, fields_json, created_at) VALUES (?,?,?,?,?)",
            (name, "form", request.form.get("body", "").strip(), json.dumps(fields), now()))
        db().commit()
        flash("Form created.", "ok")
        return redirect(url_for("library"))
    return render_template("form_builder.html", form=None, fields_json="[]", name="", body="")


@app.route("/library/<int:tid>/edit", methods=["GET", "POST"])
@login_required
def edit_form(tid):
    t = db().execute("SELECT * FROM templates WHERE id=? AND archived=0", (tid,)).fetchone() or abort(404)
    if request.method == "POST":
        name = request.form.get("name", "").strip() or t["name"]
        fields = parse_fields_from_request()
        if t["kind"] == "form" and not fields:
            flash("A fillable form needs at least one field.", "error")
            return redirect(url_for("edit_form", tid=tid))
        db().execute("UPDATE templates SET name=?, description=?, fields_json=? WHERE id=?",
                     (name, request.form.get("body", "").strip(), json.dumps(fields), tid))
        db().commit()
        flash("Saved. Packets already sent keep the version they were sent with.", "ok")
        return redirect(url_for("library"))
    return render_template("form_builder.html", form=t, fields_json=t["fields_json"],
                           name=t["name"], body=t["description"])


@app.route("/library/<int:tid>/archive", methods=["POST"])
@login_required
def archive_form(tid):
    db().execute("UPDATE templates SET archived=1 WHERE id=?", (tid,))
    db().commit()
    flash("Form removed from the library. Past packets are unaffected.", "ok")
    return redirect(url_for("library"))


@app.route("/library/<int:tid>/file")
@login_required
def template_file(tid):
    t = db().execute("SELECT * FROM templates WHERE id=?", (tid,)).fetchone() or abort(404)
    if not t["file_path"]:
        abort(404)
    return send_stored(t["file_path"], t["file_mime"] or "application/pdf", t["file_name"] or f"{t['name']}.pdf")


# ---------------------------------------------------------------- admin: packets
@app.route("/packets/new", methods=["GET", "POST"])
@login_required
def new_packet():
    templates = db().execute("SELECT * FROM templates WHERE archived=0 ORDER BY name COLLATE NOCASE").fetchall()
    if request.method == "POST":
        name = request.form.get("client_name", "").strip()
        email = request.form.get("client_email", "").strip()
        ids = [int(x) for x in request.form.getlist("template_ids") if x.isdigit()]
        adhoc = [f for f in request.files.getlist("adhoc_files") if f and f.filename]
        if not name or "@" not in email or not (ids or adhoc):
            flash("Add the client's name, a valid email, and at least one form or file.", "error")
            return render_template("packet_new.html", templates=templates, form=request.form,
                                   selected=ids)
        uploads = []
        try:
            for i, f in enumerate(adhoc):
                up = save_upload(f)
                up["sign"] = request.form.get(f"adhoc_sign_{i}") == "on"
                uploads.append(up)
        except ValueError as exc:
            flash(str(exc), "error")
            return render_template("packet_new.html", templates=templates, form=request.form, selected=ids)
        token = secrets.token_urlsafe(24)
        days = int(request.form.get("expiry_days") or LINK_DAYS)
        expires = (datetime.now(timezone.utc) + timedelta(days=days)).replace(microsecond=0).isoformat()
        cur = db().execute(
            "INSERT INTO packets (token, client_name, client_email, message, status, created_at, expires_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (token, name, email, request.form.get("message", "").strip(), "draft", now(), expires))
        pid = cur.lastrowid
        by_id = {t["id"]: t for t in templates}
        for pos, tid in enumerate(ids):
            t = by_id.get(tid)
            if not t:
                continue
            # Snapshot the template so later edits never change what the client signed.
            db().execute(
                "INSERT INTO packet_items (packet_id, template_id, position, template_name, template_kind, "
                "template_file, template_sha256, fields_json, template_body, file_name, file_mime) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (pid, tid, pos, t["name"], t["kind"], t["file_path"], t["file_sha256"], t["fields_json"],
                 t["description"] if t["kind"] == "form" else "", t["file_name"], t["file_mime"]))
        # One-off files: attached to this packet only, never added to the library.
        for k, up in enumerate(uploads):
            db().execute(
                "INSERT INTO packet_items (packet_id, template_id, position, template_name, template_kind, "
                "template_file, template_sha256, fields_json, file_name, file_mime, requires_signature, adhoc) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,1)",
                (pid, None, len(ids) + k, os.path.splitext(up["name"])[0][:120] or up["name"], up["kind"],
                 up["stored"], up["sha256"], "[]", up["name"], up["mime"], 1 if up["sign"] else 0))
        parts = [f"{len(ids)} form(s) from library"] if ids else []
        if uploads:
            parts.append(f"{len(uploads)} one-off file(s)")
        log_event(pid, "created", ", ".join(parts))
        db().commit()
        if request.form.get("action") == "send_email" and mailer.enabled():
            return send_packet_email(pid)
        mark_sent(pid, "link")
        flash("Packet ready. Copy the signing link below and send it to your client.", "ok")
        return redirect(url_for("packet_detail", pid=pid))
    pre = [int(x) for x in request.args.getlist("t") if x.isdigit()]
    return render_template("packet_new.html", templates=templates, form={}, selected=pre)


def mark_sent(pid, how):
    p = db().execute("SELECT * FROM packets WHERE id=?", (pid,)).fetchone()
    if p["status"] == "draft":
        db().execute("UPDATE packets SET status='sent', sent_at=? WHERE id=?", (now(), pid))
    log_event(pid, "sent", "by email" if how == "email" else "link created")
    db().commit()


def send_packet_email(pid):
    p = db().execute("SELECT * FROM packets WHERE id=?", (pid,)).fetchone()
    items = db().execute("SELECT template_name FROM packet_items WHERE packet_id=? ORDER BY position",
                         (pid,)).fetchall()
    try:
        mailer.send_request(p["client_email"], p["client_name"], BUSINESS_NAME,
                            [i["template_name"] for i in items], signing_url(p["token"]),
                            p["message"], p["expires_at"])
        mark_sent(pid, "email")
        flash(f"Sent to {p['client_email']}.", "ok")
    except Exception as exc:  # noqa: BLE001
        log_event(pid, "email_failed", str(exc)[:200])
        db().commit()
        flash(f"The email didn't go out ({exc}). You can still copy the link below.", "error")
        mark_sent(pid, "link")
    return redirect(url_for("packet_detail", pid=pid))


@app.route("/packets/<int:pid>")
@login_required
def packet_detail(pid):
    p = db().execute("SELECT * FROM packets WHERE id=?", (pid,)).fetchone() or abort(404)
    items = db().execute("SELECT * FROM packet_items WHERE packet_id=? ORDER BY position", (pid,)).fetchall()
    events = db().execute("SELECT * FROM events WHERE packet_id=? ORDER BY at DESC, id DESC", (pid,)).fetchall()
    signed, total = packet_progress(pid)
    return render_template("packet_detail.html", p=p, items=items, events=events, signed=signed,
                           total=total, state=effective_status(p), link=signing_url(p["token"]))


@app.route("/packets/<int:pid>/email", methods=["POST"])
@login_required
def email_packet(pid):
    if not mailer.enabled():
        flash("Email isn't set up yet. Add the SMTP settings to turn it on.", "error")
        return redirect(url_for("packet_detail", pid=pid))
    return send_packet_email(pid)


@app.route("/packets/<int:pid>/remind", methods=["POST"])
@login_required
def remind_packet(pid):
    if mailer.enabled():
        return send_packet_email(pid)
    log_event(pid, "reminder", "copied link")
    db().commit()
    return redirect(url_for("packet_detail", pid=pid))


@app.route("/packets/<int:pid>/extend", methods=["POST"])
@login_required
def extend_packet(pid):
    exp = (datetime.now(timezone.utc) + timedelta(days=LINK_DAYS)).replace(microsecond=0).isoformat()
    db().execute("UPDATE packets SET expires_at=? WHERE id=?", (exp, pid))
    log_event(pid, "extended", f"link valid until {exp[:10]}")
    db().commit()
    flash(f"Link extended by {LINK_DAYS} days.", "ok")
    return redirect(url_for("packet_detail", pid=pid))


@app.route("/packets/<int:pid>/void", methods=["POST"])
@login_required
def void_packet(pid):
    db().execute("UPDATE packets SET voided_at=? WHERE id=? AND completed_at IS NULL", (now(), pid))
    log_event(pid, "voided")
    db().commit()
    flash("Packet cancelled. The signing link no longer works.", "ok")
    return redirect(url_for("packet_detail", pid=pid))


@app.route("/packets/<int:pid>/items/<int:iid>/signed.pdf")
@login_required
def admin_signed_pdf(pid, iid):
    it = db().execute("SELECT * FROM packet_items WHERE id=? AND packet_id=?", (iid, pid)).fetchone() or abort(404)
    if not it["signed_pdf_path"]:
        abort(404)
    return send_file(os.path.join(SIGNED_DIR, it["signed_pdf_path"]), mimetype="application/pdf",
                     download_name=f"{secure_filename(it['template_name'])}-signed.pdf")


@app.route("/packets/<int:pid>/items/<int:iid>/original")
@login_required
def admin_original(pid, iid):
    it = db().execute("SELECT * FROM packet_items WHERE id=? AND packet_id=?", (iid, pid)).fetchone() or abort(404)
    if not it["template_file"]:
        abort(404)
    return send_stored(it["template_file"], it["file_mime"] or "application/pdf",
                       it["file_name"] or f"{it['template_name']}.pdf", force_download=True)


@app.route("/packets/<int:pid>/audit.pdf")
@login_required
def admin_audit_pdf(pid):
    p = db().execute("SELECT * FROM packets WHERE id=?", (pid,)).fetchone() or abort(404)
    items = db().execute("SELECT * FROM packet_items WHERE packet_id=? ORDER BY position", (pid,)).fetchall()
    events = db().execute("SELECT * FROM events WHERE packet_id=? ORDER BY at, id", (pid,)).fetchall()
    path = os.path.join(SIGNED_DIR, f"audit-{pid}-{secrets.token_hex(4)}.pdf")
    pdfgen.audit_trail(path, BUSINESS_NAME, dict(p), [dict(i) for i in items], [dict(e) for e in events])
    return send_file(path, mimetype="application/pdf", download_name=f"audit-trail-{pid}.pdf")


# ---------------------------------------------------------------- client side
def load_packet(token):
    p = db().execute("SELECT * FROM packets WHERE token=?", (token,)).fetchone()
    if not p:
        abort(404)
    return p


def client_block(p):
    """Return a template response if the link can't be used, else None."""
    st = effective_status(p)
    if st == "voided":
        return render_template("client_closed.html", p=p, reason="cancelled")
    if st == "expired":
        return render_template("client_closed.html", p=p, reason="expired")
    return None


@app.route("/s/<token>")
def client_packet(token):
    p = load_packet(token)
    blocked = client_block(p)
    if blocked:
        return blocked
    if not p["first_viewed_at"]:
        db().execute("UPDATE packets SET first_viewed_at=?, status=CASE WHEN status IN ('sent','draft') "
                     "THEN 'viewed' ELSE status END WHERE id=?", (now(), p["id"]))
        log_event(p["id"], "viewed", request.headers.get("User-Agent", "")[:200])
        db().commit()
        p = load_packet(token)
    items = db().execute("SELECT * FROM packet_items WHERE packet_id=? ORDER BY position", (p["id"],)).fetchall()
    signed, total = packet_progress(p["id"])
    if p["completed_at"]:
        return redirect(url_for("client_done", token=token))
    sign_items = [i for i in items if i["requires_signature"]]
    info_items = [i for i in items if not i["requires_signature"]]
    next_item = next((i for i in sign_items if not i["signed_at"]), None)
    return render_template("client_packet.html", p=p, items=sign_items, info_items=info_items, signed=signed,
                           total=total, next_item=next_item)


@app.route("/s/<token>/confirm", methods=["POST"])
def client_confirm(token):
    """For packets that only contain files to review (nothing to sign): the client confirms receipt."""
    p = load_packet(token)
    blocked = client_block(p)
    if blocked:
        return blocked
    signed, total = packet_progress(p["id"])
    if total == 0 and not p["completed_at"]:
        log_event(p["id"], "confirmed", "client confirmed receipt of the files")
        db().commit()
        complete_packet(p["id"], reason="client confirmed receipt")
    return redirect(url_for("client_done", token=token))


@app.route("/s/<token>/f/<int:iid>", methods=["GET", "POST"])
def client_form(token, iid):
    p = load_packet(token)
    blocked = client_block(p)
    if blocked:
        return blocked
    it = db().execute("SELECT * FROM packet_items WHERE id=? AND packet_id=?", (iid, p["id"])).fetchone() or abort(404)
    if it["signed_at"] or not it["requires_signature"]:
        return redirect(url_for("client_packet", token=token))
    fields = json.loads(it["fields_json"] or "[]")
    items = db().execute("SELECT id, template_name, signed_at FROM packet_items WHERE packet_id=? "
                         "AND requires_signature=1 ORDER BY position", (p["id"],)).fetchall()
    errors, answers = {}, {}
    body = it["template_body"] or ""
    is_specs = it["template_kind"] == "specs"
    spec_initial = None

    if request.method == "POST":
        for f in fields:
            if f["type"] == "checkbox":
                answers[f["id"]] = request.form.get(f["id"]) == "on"
                if f["required"] and not answers[f["id"]]:
                    errors[f["id"]] = "Please tick this box to continue."
            else:
                val = request.form.get(f["id"], "").strip()[:5000]
                answers[f["id"]] = val
                if f["required"] and not val:
                    errors[f["id"]] = "This field is required."
        if is_specs:
            try:
                spec_initial = json.loads(request.form.get("specs_json") or "{}")
            except ValueError:
                spec_initial = {}
            clean, err = forms.validate("room_specs", spec_initial)
            if err:
                errors["specs"] = err
            else:
                answers = clean
        signer = request.form.get("signer_name", "").strip()[:200]
        sig_data = request.form.get("signature", "")
        if not signer:
            errors["signer_name"] = "Type your full name."
        if request.form.get("consent") != "on":
            errors["consent"] = "Please agree to sign electronically."
        sig_bytes = None
        if sig_data.startswith("data:image/png;base64,"):
            try:
                sig_bytes = base64.b64decode(sig_data.split(",", 1)[1], validate=True)
            except ValueError:
                sig_bytes = None
        if not sig_bytes or len(sig_bytes) < 200 or len(sig_bytes) > 2_000_000:
            errors["signature"] = "Draw or type your signature."

        if not errors:
            ts = now()
            sig_name = f"sig-{p['id']}-{iid}-{secrets.token_hex(4)}.png"
            sig_path = os.path.join(SIGNED_DIR, sig_name)
            with open(sig_path, "wb") as fh:
                fh.write(sig_bytes)
            out_name = f"{p['id']}-{iid}-{secrets.token_hex(6)}.pdf"
            out_path = os.path.join(SIGNED_DIR, out_name)
            meta = {"business": BUSINESS_NAME, "packet_ref": packet_ref(p), "client_name": p["client_name"],
                    "client_email": p["client_email"], "signer_name": signer, "signed_at": ts,
                    "ip": client_ip(), "user_agent": request.headers.get("User-Agent", "")[:300],
                    "form_name": it["template_name"], "original_sha256": it["template_sha256"]}
            source = os.path.join(UPLOAD_DIR, it["template_file"]) if it["template_kind"] == "pdf" else None
            attachment = None
            if it["template_kind"] == "file":
                fpath = os.path.join(UPLOAD_DIR, it["template_file"])
                attachment = {"name": it["file_name"] or it["template_name"], "type": filetype_label(it["file_name"]),
                              "size": os.path.getsize(fpath), "sha256": it["template_sha256"],
                              "image": fpath if (it["file_mime"] or "").startswith("image/") else None}
            pdfgen.signed_document(out_path, source, body, fields, answers, sig_path, meta,
                                   specs=answers if is_specs else None, attachment=attachment)
            digest = sha256_file(out_path)
            db().execute(
                "UPDATE packet_items SET answers_json=?, signer_name=?, signature_path=?, signed_at=?, "
                "signed_pdf_path=?, signed_sha256=?, ip=?, user_agent=? WHERE id=? AND signed_at IS NULL",
                (json.dumps(answers), signer, sig_name, ts, out_name, digest, meta["ip"], meta["user_agent"], iid))
            db().execute("UPDATE packets SET status='in_progress' WHERE id=? AND completed_at IS NULL", (p["id"],))
            log_event(p["id"], "signed", it["template_name"])
            signed, total = packet_progress(p["id"])
            db().commit()
            if signed == total:
                complete_packet(p["id"])
                return redirect(url_for("client_done", token=token))
            nxt = db().execute("SELECT id FROM packet_items WHERE packet_id=? AND signed_at IS NULL "
                               "AND requires_signature=1 ORDER BY position",
                               (p["id"],)).fetchone()
            flash(f"“{it['template_name']}” signed.", "ok")
            return redirect(url_for("client_form", token=token, iid=nxt["id"]))

    return render_template("client_form.html", p=p, it=it, fields=fields, items=items, errors=errors,
                           answers=answers, body=body, is_specs=is_specs, spec_initial=spec_initial,
                           spec_def=forms.SPEC_FORM if is_specs else None,
                           signer_default=request.form.get("signer_name", p["client_name"]))


def packet_ref(p):
    return f"FS-{p['id']:05d}-{p['token'][:6].upper()}"


app.jinja_env.globals["packet_ref"] = packet_ref


def complete_packet(pid, reason="all forms signed"):
    db().execute("UPDATE packets SET status='completed', completed_at=? WHERE id=? AND completed_at IS NULL",
                 (now(), pid))
    log_event(pid, "completed", reason)
    db().commit()
    if mailer.enabled():
        p = db().execute("SELECT * FROM packets WHERE id=?", (pid,)).fetchone()
        items = db().execute("SELECT * FROM packet_items WHERE packet_id=? ORDER BY position", (pid,)).fetchall()
        files = [(f"{secure_filename(i['template_name'])}-signed.pdf", os.path.join(SIGNED_DIR, i["signed_pdf_path"]))
                 for i in items if i["signed_pdf_path"]]
        # Non-PDF originals (Word, Excel, images...) go along so everyone has the actual files.
        files += [(i["file_name"], os.path.join(UPLOAD_DIR, i["template_file"])) for i in items
                  if i["template_kind"] == "file" or (not i["requires_signature"] and i["template_file"])]
        try:
            mailer.send_completed(p["client_email"], p["client_name"], BUSINESS_NAME, packet_ref(p), files,
                                  admin_link=None)
            mailer.send_completed(mailer.admin_email(), p["client_name"], BUSINESS_NAME, packet_ref(p), files,
                                  admin_link=(BASE_URL or "") + url_for("packet_detail", pid=pid))
            log_event(pid, "receipt_emailed", "client and owner")
        except Exception as exc:  # noqa: BLE001
            log_event(pid, "email_failed", str(exc)[:200])
        db().commit()


@app.route("/s/<token>/done")
def client_done(token):
    p = load_packet(token)
    if p["voided_at"]:
        return render_template("client_closed.html", p=p, reason="cancelled")
    if not p["completed_at"]:
        return redirect(url_for("client_packet", token=token))
    items = db().execute("SELECT * FROM packet_items WHERE packet_id=? ORDER BY position", (p["id"],)).fetchall()
    return render_template("client_done.html", p=p, items=items)


@app.route("/s/<token>/doc/<int:iid>")
def client_doc(token, iid):
    p = load_packet(token)
    if client_block(p):
        abort(410)
    it = db().execute("SELECT * FROM packet_items WHERE id=? AND packet_id=?", (iid, p["id"])).fetchone() or abort(404)
    if not it["template_file"]:
        abort(404)
    download = request.args.get("download") == "1"
    if download and not it["downloaded_at"]:
        db().execute("UPDATE packet_items SET downloaded_at=? WHERE id=?", (now(), iid))
        log_event(p["id"], "downloaded", it["file_name"] or it["template_name"])
        db().commit()
    return send_stored(it["template_file"], it["file_mime"] or "application/pdf",
                       it["file_name"] or f"{it['template_name']}.pdf", force_download=download)


@app.route("/s/<token>/signed/<int:iid>")
def client_signed(token, iid):
    p = load_packet(token)
    if p["voided_at"]:
        abort(410)
    it = db().execute("SELECT * FROM packet_items WHERE id=? AND packet_id=?", (iid, p["id"])).fetchone() or abort(404)
    if not it["signed_pdf_path"]:
        abort(404)
    return send_file(os.path.join(SIGNED_DIR, it["signed_pdf_path"]), mimetype="application/pdf",
                     as_attachment=True, download_name=f"{secure_filename(it['template_name'])}-signed.pdf")


@app.errorhandler(404)
def not_found(_e):
    return render_template("error.html", title="Page not found",
                           message="This link doesn't match anything. Check that you copied the whole address."), 404


@app.errorhandler(400)
def bad_request(e):
    return render_template("error.html", title="Something went wrong",
                           message=getattr(e, "description", "Reload the page and try again.")), 400


@app.errorhandler(413)
def too_large(_e):
    return render_template("error.html", title="File too large",
                           message="Uploads are limited to 20 MB."), 413


@app.after_request
def security_headers(resp):
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "same-origin")
    if request.path.startswith("/s/"):
        resp.headers.setdefault("X-Robots-Tag", "noindex")
    return resp


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")), debug=os.environ.get("DEBUG") == "1")
