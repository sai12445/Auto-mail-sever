"""
Auto Mail Server (SQLite edition).

Storage is now a real database (mailserver.db) for saved subjects, messages,
recipient lists, and file metadata, plus a recent-sends log. Saved file bytes
live in saved_files/. Any existing store.json is imported automatically.

Run:  python3 app.py   then open  http://127.0.0.1:5000
"""

import os
import json
import time
import uuid
import sqlite3
import smtplib
import mimetypes
import threading
from collections import Counter
from email.message import EmailMessage
from email.utils import formataddr

from flask import Flask, request, jsonify, render_template_string, g
from werkzeug.utils import secure_filename

import config

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("DATA_DIR", HERE)
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, "mailserver.db")
STORE_PATH = os.path.join(DATA_DIR, "store.json")
SAVED_DIR = os.path.join(DATA_DIR, "saved_files")
SCHED_DIR = os.path.join(DATA_DIR, "scheduled_files")
os.makedirs(SCHED_DIR, exist_ok=True)
os.makedirs(SAVED_DIR, exist_ok=True)
_lock = threading.Lock()


# ---------------- database ----------------
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init_db():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS subjects(id INTEGER PRIMARY KEY AUTOINCREMENT,
            text TEXT UNIQUE, created REAL);
        CREATE TABLE IF NOT EXISTS bodies(id INTEGER PRIMARY KEY AUTOINCREMENT,
            label TEXT UNIQUE, body TEXT, is_html INTEGER, created REAL);
        CREATE TABLE IF NOT EXISTS lists(id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE, emails TEXT, names TEXT, created REAL);
        CREATE TABLE IF NOT EXISTS files(fid TEXT PRIMARY KEY,
            name TEXT, stored TEXT, created REAL);
        CREATE TABLE IF NOT EXISTS trash(id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT, payload TEXT, ts REAL);
        CREATE TABLE IF NOT EXISTS sent_log(id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL, email TEXT, sender_name TEXT, subject TEXT, ok INTEGER, msg TEXT);
        CREATE TABLE IF NOT EXISTS notes(id INTEGER PRIMARY KEY, content TEXT, updated REAL);
        CREATE TABLE IF NOT EXISTS day_notes(day TEXT PRIMARY KEY, content TEXT, updated REAL);
        CREATE TABLE IF NOT EXISTS day_note_versions(id INTEGER PRIMARY KEY AUTOINCREMENT,
            day TEXT, content TEXT, ts REAL);
        CREATE TABLE IF NOT EXISTS scheduled(id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_at REAL, payload TEXT, status TEXT, created REAL, result TEXT);
        """)


def migrate_from_json():
    """One-time import of an existing store.json into the database."""
    if not os.path.isfile(STORE_PATH):
        return
    try:
        with open(STORE_PATH, encoding="utf-8") as f:
            s = json.load(f)
    except Exception:
        return
    now = time.time()
    with _lock, db() as c:
        for text in s.get("subjects", []):
            c.execute("INSERT OR IGNORE INTO subjects(text,created) VALUES(?,?)", (text, now))
        for label, obj in s.get("bodies", {}).items():
            c.execute("INSERT OR IGNORE INTO bodies(label,body,is_html,created) VALUES(?,?,?,?)",
                      (label, obj.get("body", ""), 1 if obj.get("is_html") else 0, now))
        for name, obj in s.get("lists", {}).items():
            c.execute("INSERT OR IGNORE INTO lists(name,emails,names,created) VALUES(?,?,?,?)",
                      (name, obj.get("emails", ""), obj.get("names", ""), now))
        for fid, obj in s.get("files", {}).items():
            c.execute("INSERT OR IGNORE INTO files(fid,name,stored,created) VALUES(?,?,?,?)",
                      (fid, obj.get("name", fid), obj.get("stored", ""), now))
    os.rename(STORE_PATH, STORE_PATH + ".imported")


init_db()
migrate_from_json()


# ---------------- read helpers ----------------
def all_subjects():
    out = list(config.SUBJECTS)
    with db() as c:
        for r in c.execute("SELECT text FROM subjects ORDER BY created"):
            if r["text"] not in out:
                out.append(r["text"])
    return out


def all_bodies():
    out = {}
    for label, text in config.BODIES.items():
        out[label] = {"body": text, "is_html": label in config.BODIES_HTML}
    with db() as c:
        for r in c.execute("SELECT label,body,is_html FROM bodies ORDER BY created"):
            out[r["label"]] = {"body": r["body"], "is_html": bool(r["is_html"])}
    return out


def all_attachments():
    out = [{"type": "preset", "id": str(i), "name": os.path.basename(p)}
           for i, p in enumerate(config.ATTACHMENTS)]
    with db() as c:
        for r in c.execute("SELECT fid,name FROM files ORDER BY created"):
            out.append({"type": "file", "id": r["fid"], "name": r["name"]})
    return out


def all_lists():
    out = {}
    with db() as c:
        for r in c.execute("SELECT name,emails,names FROM lists ORDER BY created"):
            out[r["name"]] = {"emails": r["emails"], "names": r["names"]}
    return out


def trash_add(c, kind, payload):
    c.execute("INSERT INTO trash(kind,payload,ts) VALUES(?,?,?)",
              (kind, json.dumps(payload), time.time()))


# ---------------- email ----------------
def build_connection():
    if config.SECURITY == "ssl":
        s = smtplib.SMTP_SSL(config.SMTP_HOST, config.SMTP_PORT, timeout=30)
    else:
        s = smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=30)
        s.ehlo()
        if config.SECURITY == "tls":
            s.starttls()
            s.ehlo()
    s.login(config.USERNAME, config.PASSWORD)
    return s


def make_message(to_addr, subject, body, is_html, attachments, sender_name=""):
    msg = EmailMessage()
    from_addr = config.FROM_ADDR or config.USERNAME
    name = sender_name or config.FROM_NAME
    msg["From"] = formataddr((name, from_addr)) if name else from_addr
    msg["To"] = to_addr
    msg["Subject"] = subject
    if is_html:
        msg.set_content("This email needs an HTML-capable client.")
        msg.add_alternative(body, subtype="html")
    else:
        msg.set_content(body)
    for fname, fbytes in attachments:
        ctype, _ = mimetypes.guess_type(fname)
        ctype = ctype or "application/octet-stream"
        maintype, subtype = ctype.split("/", 1)
        msg.add_attachment(fbytes, maintype=maintype, subtype=subtype, filename=fname)
    return msg


# ---------------- routes ----------------
@app.route("/")
def index():
    return render_template_string(PAGE, sender=config.FROM_ADDR or config.USERNAME)


@app.route("/options")
def options():
    return jsonify({"subjects": all_subjects(), "bodies": all_bodies(),
                    "attachments": all_attachments(), "lists": all_lists()})


@app.route("/save_subject", methods=["POST"])
def save_subject():
    s = (request.form.get("subject", "") or "").strip()
    if not s:
        return jsonify({"error": "Type a subject first."}), 400
    if s not in config.SUBJECTS:
        with _lock, db() as c:
            c.execute("INSERT OR IGNORE INTO subjects(text,created) VALUES(?,?)", (s, time.time()))
    return jsonify({"ok": True})


@app.route("/delete_subject", methods=["POST"])
def delete_subject():
    s = (request.form.get("subject", "") or "").strip()
    with _lock, db() as c:
        row = c.execute("SELECT text FROM subjects WHERE text=?", (s,)).fetchone()
        if row:
            c.execute("DELETE FROM subjects WHERE text=?", (s,))
            trash_add(c, "subject", {"text": s})
            return jsonify({"ok": True})
    return jsonify({"error": "Only your saved subjects can be deleted "
                             "(built-in ones live in config.py)."}), 400


@app.route("/save_body", methods=["POST"])
def save_body():
    label = (request.form.get("label", "") or "").strip()
    body = request.form.get("body", "") or ""
    is_html = 1 if request.form.get("is_html") == "on" else 0
    if not label:
        return jsonify({"error": "Give the body a name."}), 400
    if not body.strip():
        return jsonify({"error": "Body is empty."}), 400
    with _lock, db() as c:
        c.execute("INSERT INTO bodies(label,body,is_html,created) VALUES(?,?,?,?) "
                  "ON CONFLICT(label) DO UPDATE SET body=excluded.body,is_html=excluded.is_html",
                  (label, body, is_html, time.time()))
    return jsonify({"ok": True})


@app.route("/delete_body", methods=["POST"])
def delete_body():
    label = (request.form.get("label", "") or "").strip()
    with _lock, db() as c:
        row = c.execute("SELECT label,body,is_html FROM bodies WHERE label=?", (label,)).fetchone()
        if row:
            c.execute("DELETE FROM bodies WHERE label=?", (label,))
            trash_add(c, "body", {"label": row["label"], "body": row["body"],
                                  "is_html": bool(row["is_html"])})
            return jsonify({"ok": True})
    return jsonify({"error": "Only your saved bodies can be deleted "
                             "(built-in ones live in config.py)."}), 400


@app.route("/save_list", methods=["POST"])
def save_list():
    name = (request.form.get("name", "") or "").strip()
    emails = request.form.get("emails", "") or ""
    names = request.form.get("sender_names", "") or ""
    if not name:
        return jsonify({"error": "Give the list a name."}), 400
    if not emails.strip():
        return jsonify({"error": "No emails to save."}), 400
    with _lock, db() as c:
        c.execute("INSERT INTO lists(name,emails,names,created) VALUES(?,?,?,?) "
                  "ON CONFLICT(name) DO UPDATE SET emails=excluded.emails,names=excluded.names",
                  (name, emails, names, time.time()))
    return jsonify({"ok": True})


@app.route("/delete_list", methods=["POST"])
def delete_list():
    name = (request.form.get("name", "") or "").strip()
    with _lock, db() as c:
        row = c.execute("SELECT name,emails,names FROM lists WHERE name=?", (name,)).fetchone()
        if row:
            c.execute("DELETE FROM lists WHERE name=?", (name,))
            trash_add(c, "list", {"name": row["name"], "emails": row["emails"], "names": row["names"]})
            return jsonify({"ok": True})
    return jsonify({"error": "List not found."}), 400


@app.route("/save_file", methods=["POST"])
def save_file():
    up = request.files.get("file")
    if not up or not up.filename:
        return jsonify({"error": "Choose a file first."}), 400
    fid = uuid.uuid4().hex[:12]
    safe = secure_filename(up.filename) or "file"
    stored = os.path.join(SAVED_DIR, fid + "_" + safe)
    up.save(stored)
    with _lock, db() as c:
        c.execute("INSERT INTO files(fid,name,stored,created) VALUES(?,?,?,?)",
                  (fid, up.filename, stored, time.time()))
    return jsonify({"ok": True})


@app.route("/delete_file", methods=["POST"])
def delete_file():
    fid = (request.form.get("fid", "") or "").strip()
    with _lock, db() as c:
        row = c.execute("SELECT fid,name,stored FROM files WHERE fid=?", (fid,)).fetchone()
        if row:
            c.execute("DELETE FROM files WHERE fid=?", (fid,))
            trash_add(c, "file", {"fid": row["fid"], "name": row["name"], "stored": row["stored"]})
            return jsonify({"ok": True})
    return jsonify({"error": "File not found."}), 400


@app.route("/trash")
def trash():
    groups = {"subject": [], "body": [], "list": [], "file": []}
    with db() as c:
        for r in c.execute("SELECT id,kind,payload FROM trash ORDER BY ts DESC"):
            p = json.loads(r["payload"])
            label = p.get("text") or p.get("label") or p.get("name")
            groups.get(r["kind"], []).append({"i": r["id"], "label": label})
    return jsonify({"subjects": groups["subject"], "bodies": groups["body"],
                    "lists": groups["list"], "files": groups["file"]})


def _restore_row(c, row):
    kind = row["kind"]
    p = json.loads(row["payload"])
    if kind == "subject":
        if p["text"] not in config.SUBJECTS:
            c.execute("INSERT OR IGNORE INTO subjects(text,created) VALUES(?,?)", (p["text"], time.time()))
    elif kind == "body":
        c.execute("INSERT INTO bodies(label,body,is_html,created) VALUES(?,?,?,?) "
                  "ON CONFLICT(label) DO UPDATE SET body=excluded.body,is_html=excluded.is_html",
                  (p["label"], p["body"], 1 if p.get("is_html") else 0, time.time()))
    elif kind == "list":
        c.execute("INSERT INTO lists(name,emails,names,created) VALUES(?,?,?,?) "
                  "ON CONFLICT(name) DO UPDATE SET emails=excluded.emails,names=excluded.names",
                  (p["name"], p.get("emails", ""), p.get("names", ""), time.time()))
    elif kind == "file":
        c.execute("INSERT OR REPLACE INTO files(fid,name,stored,created) VALUES(?,?,?,?)",
                  (p["fid"], p["name"], p["stored"], time.time()))
    c.execute("DELETE FROM trash WHERE id=?", (row["id"],))


@app.route("/restore", methods=["POST"])
def restore():
    try:
        tid = int(request.form.get("id", ""))
    except ValueError:
        return jsonify({"error": "bad id"}), 400
    with _lock, db() as c:
        row = c.execute("SELECT * FROM trash WHERE id=?", (tid,)).fetchone()
        if not row:
            return jsonify({"error": "Item no longer in recently-deleted."}), 400
        _restore_row(c, row)
    return jsonify({"ok": True})


@app.route("/undo", methods=["POST"])
def undo():
    kind = (request.form.get("kind", "") or "").strip()
    with _lock, db() as c:
        row = c.execute("SELECT * FROM trash WHERE kind=? ORDER BY ts DESC LIMIT 1", (kind,)).fetchone()
        if not row:
            return jsonify({"error": "Nothing to undo."}), 400
        _restore_row(c, row)
    return jsonify({"ok": True})


@app.route("/purge_trash", methods=["POST"])
def purge_trash():
    with _lock, db() as c:
        for r in c.execute("SELECT payload FROM trash WHERE kind='file'"):
            p = json.loads(r["payload"])
            sp = p.get("stored", "")
            if sp and os.path.isfile(sp):
                try:
                    os.remove(sp)
                except OSError:
                    pass
        c.execute("DELETE FROM trash")
    return jsonify({"ok": True})


@app.route("/history")
def history():
    rows = []
    with db() as c:
        for r in c.execute("SELECT ts,email,sender_name,subject,ok,msg FROM sent_log "
                           "ORDER BY id DESC LIMIT 60"):
            rows.append({"ts": r["ts"], "email": r["email"], "sender_name": r["sender_name"],
                         "subject": r["subject"], "ok": bool(r["ok"]), "msg": r["msg"]})
    return jsonify({"rows": rows})


@app.route("/note")
def get_note():
    with db() as c:
        r = c.execute("SELECT content FROM notes WHERE id=1").fetchone()
    return jsonify({"content": r["content"] if r else ""})


@app.route("/save_note", methods=["POST"])
def save_note():
    content = request.form.get("content", "") or ""
    with _lock, db() as c:
        c.execute("INSERT INTO notes(id,content,updated) VALUES(1,?,?) "
                  "ON CONFLICT(id) DO UPDATE SET content=excluded.content,updated=excluded.updated",
                  (content, time.time()))
    return jsonify({"ok": True})


@app.route("/day_note")
def day_note():
    day = (request.args.get("day", "") or "").strip()
    with db() as c:
        r = c.execute("SELECT content FROM day_notes WHERE day=?", (day,)).fetchone()
    return jsonify({"day": day, "content": r["content"] if r else ""})


@app.route("/save_day_note", methods=["POST"])
def save_day_note():
    day = (request.form.get("day", "") or "").strip()
    content = request.form.get("content", "") or ""
    if not day:
        return jsonify({"error": "no day"}), 400
    with _lock, db() as c:
        cur = c.execute("SELECT content FROM day_notes WHERE day=?", (day,)).fetchone()
        prev = cur["content"] if cur else ""
        # keep a recoverable version of the previous text whenever it changes
        if prev and prev.strip() and prev != content:
            c.execute("INSERT INTO day_note_versions(day,content,ts) VALUES(?,?,?)",
                      (day, prev, time.time()))
            c.execute("DELETE FROM day_note_versions WHERE day=? AND id NOT IN "
                      "(SELECT id FROM day_note_versions WHERE day=? ORDER BY id DESC LIMIT 15)",
                      (day, day))
        if content.strip():
            c.execute("INSERT INTO day_notes(day,content,updated) VALUES(?,?,?) "
                      "ON CONFLICT(day) DO UPDATE SET content=excluded.content,updated=excluded.updated",
                      (day, content, time.time()))
        else:
            c.execute("DELETE FROM day_notes WHERE day=?", (day,))
    return jsonify({"ok": True})


@app.route("/undo_day_note", methods=["POST"])
def undo_day_note():
    day = (request.form.get("day", "") or "").strip()
    with _lock, db() as c:
        v = c.execute("SELECT id,content FROM day_note_versions WHERE day=? "
                      "ORDER BY id DESC LIMIT 1", (day,)).fetchone()
        if not v:
            return jsonify({"error": "Nothing to undo for this day."}), 400
        content = v["content"]
        c.execute("DELETE FROM day_note_versions WHERE id=?", (v["id"],))
        c.execute("INSERT INTO day_notes(day,content,updated) VALUES(?,?,?) "
                  "ON CONFLICT(day) DO UPDATE SET content=excluded.content,updated=excluded.updated",
                  (day, content, time.time()))
    return jsonify({"ok": True, "content": content})


@app.route("/note_days")
def note_days():
    with db() as c:
        days = [r["day"] for r in c.execute("SELECT day FROM day_notes WHERE TRIM(content)<>''")]
    return jsonify({"days": days})


@app.route("/check_recipients", methods=["POST"])
def check_recipients():
    raw = request.form.get("emails", "") or ""
    emails = [e.strip() for e in raw.replace(",", " ").split() if e.strip()]
    with db() as c:
        sent_info = {}
        for r in c.execute("SELECT LOWER(email) e, COUNT(*) ct, MAX(ts) last FROM sent_log "
                           "WHERE ok=1 GROUP BY LOWER(email)"):
            sent_info[r["e"]] = (r["ct"], r["last"])
    counts = Counter(e.lower() for e in emails)
    items = []
    for e in emails:
        info = sent_info.get(e.lower())
        items.append({"email": e,
                      "sent_before": info[0] if info else 0,
                      "last_ts": info[1] if info else None,
                      "dup": counts[e.lower()] > 1})
    return jsonify({"items": items})


def _build_shared(preset_idxs, saved_fids, extra_uploads, files_rows, per_ref, exclude_fids=frozenset()):
    """Return list of (filename, bytes) that go to everyone."""
    def referenced(name):
        nl = name.lower()
        return any(tok == nl or tok in nl for tok in per_ref)
    shared = []
    for idx in preset_idxs:
        try:
            path = config.ATTACHMENTS[int(idx)]
        except (ValueError, IndexError):
            continue
        if os.path.isfile(path):
            with open(path, "rb") as fp:
                shared.append((os.path.basename(path), fp.read()))
    fid_map = {fid: (name, stored) for fid, name, stored in files_rows}
    for fid in saved_fids:
        if fid in exclude_fids:
            continue
        nm = fid_map.get(fid)
        if nm and os.path.isfile(nm[1]) and not referenced(nm[0]):
            with open(nm[1], "rb") as fp:
                shared.append((nm[0], fp.read()))
    for origname, path in extra_uploads:   # already-on-disk one-off uploads
        if os.path.isfile(path):
            with open(path, "rb") as fp:
                shared.append((origname, fp.read()))
    return shared


def dispatch_send(subject, body, greetings_text, is_html, default_name,
                  emails_text, names_text, recipient_files_text, shared, files_rows, matrix=None):
    """Send to all recipients. Returns (response_dict, http_code)."""
    targets = [e.strip() for e in (emails_text or "").replace(",", " ").split() if e.strip()]
    if not targets:
        return {"error": "Enter at least one email address."}, 400
    name_lines = [n.strip() for n in (names_text or "").splitlines()]
    greeting_lines = [g.strip() for g in (greetings_text or "").splitlines()]
    one_for_all = len(greeting_lines) == 1
    sep = "<br><br>" if is_html else "\n\n"
    recipient_file_lines = [l.strip() for l in (recipient_files_text or "").splitlines()]
    fid_map = {fid: (name, stored) for fid, name, stored in files_rows}
    _cache = {}

    def _read(stored):
        if stored not in _cache:
            with open(stored, "rb") as fp:
                _cache[stored] = fp.read()
        return _cache[stored]

    def load_named(token):
        t = token.strip().lower()
        if not t:
            return None
        cand = None
        for fid, name, stored in files_rows:
            if name.lower() == t:
                cand = (name, stored); break
        if not cand:
            for fid, name, stored in files_rows:
                if fid.lower() == t:
                    cand = (name, stored); break
        if not cand:
            for fid, name, stored in files_rows:
                if t in name.lower():
                    cand = (name, stored); break
        if not cand or not os.path.isfile(cand[1]):
            return None
        return (cand[0], _read(cand[1]))

    def load_fid(fid):
        nm = fid_map.get(fid)
        if not nm or not os.path.isfile(nm[1]):
            return None
        return (nm[0], _read(nm[1]))

    try:
        server = build_connection()
    except Exception as e:
        return {"error": f"Could not log in to SMTP: {e}"}, 500

    results, log_rows = [], []
    for i, addr in enumerate(targets):
        if "@" not in addr:
            results.append({"email": addr, "ok": False, "msg": "not a valid email"})
            log_rows.append((time.time(), addr, "", subject, 0, "not a valid email"))
            continue
        this_name = name_lines[i] if i < len(name_lines) and name_lines[i] else default_name
        if one_for_all:
            g_i = greeting_lines[0]
        else:
            g_i = greeting_lines[i] if i < len(greeting_lines) and greeting_lines[i] else ""
        full_body = (g_i + sep + body) if g_i else body
        per = list(shared)
        note = ""
        if i < len(recipient_file_lines) and recipient_file_lines[i]:
            hit = load_named(recipient_file_lines[i])
            if hit:
                per.append(hit)
            else:
                note = f" (no saved file matched '{recipient_file_lines[i]}')"
        if matrix and i < len(matrix):
            for fid in matrix[i]:
                hitf = load_fid(fid)
                if hitf:
                    per.append(hitf)
        try:
            server.send_message(make_message(addr, subject, full_body, is_html, per, this_name))
            msg = (f"sent as {this_name}" if this_name else "sent") + note
            results.append({"email": addr, "ok": True, "msg": msg})
            log_rows.append((time.time(), addr, this_name, subject, 1, msg))
        except Exception as e:
            results.append({"email": addr, "ok": False, "msg": str(e)})
            log_rows.append((time.time(), addr, this_name, subject, 0, str(e)))
    try:
        server.quit()
    except Exception:
        pass
    with _lock, db() as c:
        c.executemany("INSERT INTO sent_log(ts,email,sender_name,subject,ok,msg) "
                      "VALUES(?,?,?,?,?,?)", log_rows)
    return {"results": results}, 200


def _files_rows():
    with db() as c:
        return [(r["fid"], r["name"], r["stored"])
                for r in c.execute("SELECT fid,name,stored FROM files")]


@app.route("/known_recipients")
def known_recipients():
    seen = []
    seen_set = set()
    with db() as c:
        for r in c.execute("SELECT email, MAX(ts) m FROM sent_log GROUP BY LOWER(email) ORDER BY m DESC"):
            e = (r["email"] or "").strip()
            if e and e.lower() not in seen_set:
                seen_set.add(e.lower()); seen.append(e)
        for r in c.execute("SELECT emails FROM lists"):
            for e in (r["emails"] or "").replace(",", " ").split():
                e = e.strip()
                if e and "@" in e and e.lower() not in seen_set:
                    seen_set.add(e.lower()); seen.append(e)
    return jsonify({"emails": seen})


@app.route("/send", methods=["POST"])
def send():
    f = request.form
    subject = (f.get("subject", "") or "").strip()
    body = f.get("body", "") or ""
    if not subject:
        return jsonify({"error": "Subject is empty."}), 400
    if not body.strip():
        return jsonify({"error": "Body is empty."}), 400
    recipient_files_text = f.get("recipient_files", "") or ""
    per_ref = set(l.strip().lower() for l in recipient_files_text.splitlines() if l.strip())
    try:
        matrix = json.loads(f.get("matrix", "[]") or "[]")
    except Exception:
        matrix = []
    exclude_fids = set(fid for row in matrix for fid in row)
    files_rows = _files_rows()
    uploads = []
    for up in request.files.getlist("uploads"):
        if up and up.filename:
            uploads.append((up.filename, up.read()))
    shared = _build_shared(request.form.getlist("preset_attachments"),
                           request.form.getlist("saved_files"),
                           [], files_rows, per_ref, exclude_fids)
    shared += uploads
    out, code = dispatch_send(subject, body, f.get("greetings", "") or "",
                              f.get("is_html") == "on",
                              (f.get("sender_name", "") or "").strip(),
                              f.get("emails", "") or "", f.get("sender_names", "") or "",
                              recipient_files_text, shared, files_rows, matrix)
    return jsonify(out), code


@app.route("/schedule", methods=["POST"])
def schedule():
    f = request.form
    subject = (f.get("subject", "") or "").strip()
    body = f.get("body", "") or ""
    if not subject:
        return jsonify({"error": "Subject is empty."}), 400
    if not body.strip():
        return jsonify({"error": "Body is empty."}), 400
    if not (f.get("emails", "") or "").strip():
        return jsonify({"error": "Enter at least one email address."}), 400
    try:
        run_at = float(f.get("run_at", ""))
    except ValueError:
        return jsonify({"error": "Pick a date & time."}), 400
    if run_at < time.time() - 120:
        return jsonify({"error": "That time is in the past."}), 400
    extra = []
    for up in request.files.getlist("uploads"):
        if up and up.filename:
            fid = uuid.uuid4().hex[:12]
            safe = secure_filename(up.filename) or "file"
            p = os.path.join(SCHED_DIR, fid + "_" + safe)
            up.save(p)
            extra.append([up.filename, p])
    payload = {
        "subject": subject, "body": body, "is_html": f.get("is_html") == "on",
        "default_name": (f.get("sender_name", "") or "").strip(),
        "emails": f.get("emails", "") or "", "names": f.get("sender_names", "") or "",
        "greetings": f.get("greetings", "") or "",
        "recipient_files": f.get("recipient_files", "") or "",
        "preset_attachments": f.getlist("preset_attachments"),
        "saved_files": f.getlist("saved_files"),
        "matrix": f.get("matrix", "") or "[]",
        "extra_uploads": extra,
    }
    with _lock, db() as c:
        c.execute("INSERT INTO scheduled(run_at,payload,status,created,result) "
                  "VALUES(?,?,?,?,?)", (run_at, json.dumps(payload), "pending", time.time(), ""))
    return jsonify({"ok": True})


@app.route("/scheduled")
def scheduled_list():
    out = []
    with db() as c:
        for r in c.execute("SELECT id,run_at,payload,status,result FROM scheduled "
                           "ORDER BY (status='pending') DESC, run_at DESC LIMIT 40"):
            p = json.loads(r["payload"])
            n = len([e for e in (p.get("emails", "") or "").replace(",", " ").split() if e.strip()])
            out.append({"id": r["id"], "run_at": r["run_at"], "subject": p.get("subject", ""),
                        "count": n, "status": r["status"], "result": r["result"]})
    return jsonify({"jobs": out})


@app.route("/cancel_scheduled", methods=["POST"])
def cancel_scheduled():
    sid = request.form.get("id", "")
    with _lock, db() as c:
        r = c.execute("SELECT status FROM scheduled WHERE id=?", (sid,)).fetchone()
        if r and r["status"] == "pending":
            c.execute("UPDATE scheduled SET status='cancelled' WHERE id=?", (sid,))
            return jsonify({"ok": True})
    return jsonify({"error": "Only pending jobs can be cancelled."}), 400


def _run_scheduled(row):
    p = json.loads(row["payload"])
    per_ref = set(l.strip().lower() for l in (p.get("recipient_files", "") or "").splitlines() if l.strip())
    try:
        matrix = json.loads(p.get("matrix", "[]") or "[]")
    except Exception:
        matrix = []
    exclude_fids = set(fid for r in matrix for fid in r)
    files_rows = _files_rows()
    shared = _build_shared(p.get("preset_attachments", []), p.get("saved_files", []),
                           p.get("extra_uploads", []), files_rows, per_ref, exclude_fids)
    out, code = dispatch_send(p["subject"], p["body"], p.get("greetings", ""),
                              p.get("is_html", False), p.get("default_name", ""),
                              p.get("emails", ""), p.get("names", ""),
                              p.get("recipient_files", ""), shared, files_rows, matrix)
    if "error" in out:
        return "error", out["error"]
    ok = sum(1 for r in out["results"] if r["ok"])
    return "sent", f"{ok} sent, {len(out['results']) - ok} failed"


def scheduler_loop():
    while True:
        try:
            now = time.time()
            with _lock, db() as c:
                rows = c.execute("SELECT * FROM scheduled WHERE status='pending' AND run_at<=? "
                                 "ORDER BY run_at", (now,)).fetchall()
                for r in rows:
                    c.execute("UPDATE scheduled SET status='running' WHERE id=?", (r["id"],))
            for r in rows:
                try:
                    status, result = _run_scheduled(r)
                except Exception as e:
                    status, result = "error", str(e)
                with _lock, db() as c:
                    c.execute("UPDATE scheduled SET status=?,result=? WHERE id=?",
                              (status, result, r["id"]))
        except Exception:
            pass
        time.sleep(15)


threading.Thread(target=scheduler_loop, daemon=True).start()


PAGE = r"""
<!DOCTYPE html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Auto Mail Server</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<style>
  @import url('https://fonts.googleapis.com/css2?family=Fraunces:ital,opsz,wght@0,9..144,400;0,9..144,500;0,9..144,600;1,9..144,500&family=Inter:wght@400;450;500;600&display=swap');
  :root{--paper:#EEF0F3;--card:#FFFFFF;--ink:#1B1E27;--muted:#6A7080;--line:#E5E8EE;
    --line-strong:#D3D8E0;--accent:#2C3A8C;--accent-press:#222d6e;--accent-soft:#EBEEF9;
    --seal:#A9803F;--ok:#1E7F5C;--bad:#C0392B;--radius:12px;}
  *{box-sizing:border-box}
  body{margin:0;font-family:'Inter',system-ui,Segoe UI,Roboto,sans-serif;font-size:15px;
    line-height:1.55;color:var(--ink);min-height:100vh;background:
    radial-gradient(1200px 600px at 50% -10%, #F7F8FA 0%, var(--paper) 60%);padding:32px 22px}
  .app{max-width:1180px;margin:0 auto}
  .grid{display:grid;grid-template-columns:1fr;gap:24px;align-items:start}
  @media(min-width:980px){.grid{grid-template-columns:minmax(0,1.7fr) minmax(300px,1fr)}}
  .side{align-self:start}
  @media(min-width:980px){.side{position:sticky;top:32px}}
  .sidehead{font-family:'Fraunces',serif;font-weight:500;margin:0;font-size:20px}
  .notepad-ta{width:100%;min-height:360px;margin-top:12px;line-height:1.6;background:#FCFCFD}
  .sidesep{border:none;border-top:1px solid var(--line);margin:18px 0}
  .calhead{display:flex;align-items:center;justify-content:space-between;margin-bottom:10px}
  .calhead .t{font-family:'Fraunces',serif;font-size:17px;font-weight:500}
  .calnav{border:1px solid var(--line-strong);background:#fff;border-radius:8px;cursor:pointer;
    width:30px;height:30px;color:var(--ink);font-size:16px;line-height:1}
  .calnav:hover{border-color:var(--accent);color:var(--accent)}
  .calgrid{display:grid;grid-template-columns:repeat(7,1fr);gap:3px}
  .calw{text-align:center;color:var(--muted);font-weight:600;font-size:10px;text-transform:uppercase;padding:4px 0}
  .cald{text-align:center;font-size:12.5px;padding:7px 0;border-radius:8px;cursor:pointer}
  .cald:hover{background:var(--accent-soft)}
  .cald.today{background:var(--accent);color:#fff;font-weight:600}
  .cald.empty{visibility:hidden;cursor:default}
  @keyframes fadein{from{opacity:0;transform:translateY(5px)}to{opacity:1;transform:none}}
  .calgrid{animation:fadein .28s ease}
  .cald{transition:background .15s,transform .1s}
  .cald:hover{transform:scale(1.1)}
  .cald.sel{box-shadow:inset 0 0 0 2px var(--accent);font-weight:600}
  .cald.has-note{position:relative}
  .cald.has-note::after{content:'';position:absolute;left:50%;bottom:3px;transform:translateX(-50%);
    width:4px;height:4px;border-radius:50%;background:var(--seal)}
  .cald.today.has-note::after{background:#fff}
  .fadein{animation:fadein .25s ease}
  @media (prefers-reduced-motion:reduce){.calgrid,.fadein,.card{animation:none}.cald:hover{transform:none}}
  .card{background:var(--card);border:1px solid var(--line);border-radius:20px;width:100%;
    padding:36px 36px 30px;
    box-shadow:0 1px 2px rgba(20,24,40,.04),0 28px 60px -34px rgba(20,24,40,.32);
    animation:rise .55s cubic-bezier(.2,.7,.2,1) both}
  @keyframes rise{from{opacity:0;transform:translateY(14px)}to{opacity:1;transform:none}}
  @media (prefers-reduced-motion:reduce){.card{animation:none}}
  .head{display:flex;gap:16px;align-items:center;padding-bottom:22px;border-bottom:1px solid var(--line);margin-bottom:4px}
  .seal{flex:none;width:54px;height:54px;border-radius:50%;border:1.5px solid var(--seal);
    display:flex;align-items:center;justify-content:center;color:var(--seal);
    box-shadow:inset 0 0 0 3px #fff,inset 0 0 0 4px rgba(169,128,63,.22)}
  .seal svg{width:25px;height:25px}
  .kicker{font-size:11px;letter-spacing:.18em;text-transform:uppercase;color:var(--seal);font-weight:600;margin-bottom:4px}
  h1{font-family:'Fraunces',Georgia,serif;font-weight:500;font-size:28px;letter-spacing:-.01em;margin:0}
  .meta{color:var(--muted);font-size:13px;margin:4px 0 0}
  .meta b{color:var(--ink);font-weight:600}
  section{padding:22px 0;border-bottom:1px solid var(--line)}
  section:last-of-type{border-bottom:0;padding-bottom:4px}
  .eyebrow{font-size:11px;letter-spacing:.15em;text-transform:uppercase;color:var(--muted);
    font-weight:600;margin-bottom:10px;display:flex;align-items:baseline;gap:9px}
  .eyebrow .n{font-family:'Fraunces',serif;font-style:italic;font-weight:500;font-size:14px;
    color:var(--seal);letter-spacing:0;text-transform:none}
  .help{font-size:12.5px;color:var(--muted);margin:0 0 10px}
  .sublabel{display:block;font-size:12px;color:var(--muted);margin:0 0 6px;font-weight:500}
  input,select,textarea{width:100%;background:#fff;color:var(--ink);border:1px solid var(--line-strong);
    border-radius:var(--radius);padding:12px 13px;font:inherit;transition:border-color .15s,box-shadow .15s}
  textarea{min-height:118px;resize:vertical;line-height:1.5}
  input:focus,select:focus,textarea:focus{outline:none;border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-soft)}
  input::placeholder,textarea::placeholder{color:#9aa0ad}
  select{appearance:none;-webkit-appearance:none;cursor:pointer;
    background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='8'%3E%3Cpath fill='none' stroke='%236A7080' stroke-width='1.5' d='M1 1.5l5 5 5-5'/%3E%3C/svg%3E");
    background-repeat:no-repeat;background-position:right 14px center;padding-right:34px}
  .pickrow{display:flex;gap:8px;align-items:center}
  .pickrow select{flex:1}
  .mini{background:#fff;border:1px solid var(--line-strong);color:var(--ink);border-radius:10px;
    padding:12px 14px;font:inherit;font-size:13px;font-weight:500;cursor:pointer;white-space:nowrap;
    transition:border-color .15s,background .15s,color .15s}
  .mini:hover{border-color:var(--accent);color:var(--accent)}
  .mini.del:hover{border-color:var(--bad);color:var(--bad)}
  .field{margin-top:12px}
  .check{display:flex;align-items:center;gap:8px;margin-top:10px}
  .check input{width:auto;accent-color:var(--accent)}
  .check label{margin:0;font-size:13px;color:var(--muted)}
  .att{border:1px solid var(--line);border-radius:var(--radius);padding:12px 14px;background:#FBFBFD}
  .att .row{display:flex;align-items:center;gap:9px;padding:5px 0}
  .att .row label{font-size:14px;flex:1;display:flex;align-items:center;gap:9px;margin:0;cursor:pointer}
  .att .row input[type=checkbox]{width:auto;accent-color:var(--accent)}
  .xdel{border:none;background:transparent;color:var(--muted);cursor:pointer;font-size:14px;padding:2px 6px;border-radius:6px;line-height:1}
  .xdel:hover{color:var(--bad);background:#fbecea}
  .filerow{display:flex;gap:8px;align-items:center;margin-top:10px;flex-wrap:wrap}
  input[type=file]{font-size:13px;border:none;padding:6px 0;background:transparent;flex:1;min-width:160px}
  .small{font-size:12px;color:var(--muted);margin:8px 0 0}
  .matrix{overflow-x:auto}
  table.mtx{border-collapse:collapse;font-size:13px;width:100%;margin-top:4px}
  table.mtx th,table.mtx td{border:1px solid var(--line);padding:6px 8px;text-align:center}
  table.mtx th{background:#FBFBFD;color:var(--muted);font-weight:600;font-size:12px}
  table.mtx td.mtx-email{text-align:left;max-width:190px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:12.5px}
  table.mtx input{accent-color:var(--accent)}
  .mtx-legend{margin-top:8px;line-height:1.7}
  .sgbox{position:absolute;top:100%;left:0;right:0;margin-top:4px;background:#fff;
    border:1px solid var(--line-strong);border-radius:10px;z-index:30;max-height:200px;overflow:auto;
    box-shadow:0 14px 30px -14px rgba(20,24,40,.4);display:none}
  .sg{padding:9px 12px;font-size:13px;cursor:pointer}
  .sg:hover,.sg.active{background:var(--accent-soft)}
  button.send{width:100%;margin-top:26px;background:var(--accent);color:#fff;border:0;border-radius:var(--radius);
    padding:15px;font:inherit;font-weight:600;letter-spacing:.01em;cursor:pointer;
    transition:background .15s,transform .05s;box-shadow:0 10px 22px -12px rgba(44,58,140,.75)}
  button.send:hover{background:var(--accent-press)}
  button.send:active{transform:translateY(1px)}
  button.send:disabled{opacity:.55;cursor:not-allowed;box-shadow:none}
  .log{margin-top:16px;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:13px}
  .log div{padding:3px 0}
  .ok{color:var(--ok)} .bad{color:var(--bad)}
  #flash{font-size:12.5px;color:var(--ok);margin-top:8px;min-height:16px}
  #recoverBtn{position:fixed;right:24px;bottom:24px;width:50px;height:50px;border-radius:50%;
    background:#fff;border:1px solid var(--line-strong);color:var(--ink);font-size:20px;cursor:pointer;
    box-shadow:0 12px 28px -12px rgba(20,24,40,.5);z-index:50;transition:border-color .15s,transform .15s}
  #recoverBtn:hover{border-color:var(--accent);transform:translateY(-1px)}
  #recoverPanel{position:fixed;right:24px;bottom:86px;width:340px;max-height:64vh;overflow:auto;
    background:#fff;border:1px solid var(--line);border-radius:16px;padding:18px;
    box-shadow:0 26px 60px -22px rgba(20,24,40,.5);display:none;z-index:50}
  #recoverPanel h3{font-family:'Fraunces',serif;font-weight:500;margin:0;font-size:18px}
  .tgroup{font-size:11px;letter-spacing:.12em;text-transform:uppercase;color:var(--muted);font-weight:600;margin:14px 0 2px}
  .trow{display:flex;align-items:center;justify-content:space-between;gap:10px;padding:9px 0;border-top:1px solid var(--line)}
  .trow span{font-size:13px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  @media (max-width:520px){.card{padding:26px 22px}.head{gap:13px}h1{font-size:24px}}
  /* animated sky scene */
  #sky{position:fixed;inset:0;z-index:0;overflow:hidden;pointer-events:none;
    background:linear-gradient(180deg,#bfe3ff 0%,#daefff 38%,#f2f7fb 78%,#fdf6ec 100%)}
  .app{position:relative;z-index:1}
  .sun{position:absolute;top:6%;right:9%;width:84px;height:84px;border-radius:50%;
    background:radial-gradient(circle at 50% 50%,#fff6c2,#ffd86b 72%);
    box-shadow:0 0 50px 16px rgba(255,214,102,.45)}
  .cloudw{position:absolute;will-change:transform;animation:drift linear infinite}
  .cloud{position:relative;width:120px;height:38px;background:#fff;border-radius:40px;opacity:.92;
    filter:drop-shadow(0 12px 14px rgba(80,120,160,.12))}
  .cloud::before,.cloud::after{content:'';position:absolute;background:#fff;border-radius:50%}
  .cloud::before{width:52px;height:52px;top:-22px;left:16px}
  .cloud::after{width:38px;height:38px;top:-14px;left:62px}
  @keyframes drift{from{transform:translateX(-220px)}to{transform:translateX(calc(100vw + 220px))}}
  .mailw{position:absolute;will-change:transform;animation:fly linear infinite}
  @keyframes fly{from{transform:translate(-14vw,4vh)}to{transform:translate(114vw,-10vh)}}
  .mailw svg{width:30px;height:30px;animation:bob 3.2s ease-in-out infinite}
  @keyframes bob{0%,100%{transform:translateY(0) rotate(-6deg)}50%{transform:translateY(-12px) rotate(8deg)}}
  .bird{position:absolute;color:#5b6680;will-change:transform;animation:drift linear infinite}
  .bird svg{width:22px;height:12px;animation:flap 1.1s ease-in-out infinite}
  @keyframes flap{0%,100%{transform:scaleY(1)}50%{transform:scaleY(.5)}}
  .cyclist{position:absolute;bottom:14px;left:0;will-change:transform;animation:ride 28s linear infinite}
  .cyclist svg{width:98px;height:128px}
  @keyframes ride{from{transform:translateX(-14vw)}to{transform:translateX(114vw)}}
  .wheel{transform-box:fill-box;transform-origin:center;animation:spin 1.1s linear infinite}
  @keyframes spin{to{transform:rotate(360deg)}}
  .kite{transform-box:fill-box;transform-origin:50% 100%;animation:kitesway 3.6s ease-in-out infinite}
  @keyframes kitesway{0%,100%{transform:rotate(-5deg)}50%{transform:rotate(6deg)}}
  #sceneToggle{position:fixed;left:24px;bottom:24px;width:46px;height:46px;border-radius:50%;
    background:#fff;border:1px solid var(--line-strong);cursor:pointer;z-index:50;font-size:18px;
    box-shadow:0 10px 26px -12px rgba(20,24,40,.45)}
  #sceneToggle:hover{border-color:var(--accent)}
  body.plain #sky{display:none}
  @media (prefers-reduced-motion:reduce){.cloudw,.mailw,.bird,.cyclist,.wheel,.kite,.mailw svg,.bird svg{animation:none}}
</style></head><body>
<div id="sky" aria-hidden="true">
  <div class="sun"></div>
  <div class="cloudw" style="top:11%;animation-duration:48s"><div class="cloud" style="transform:scale(1)"></div></div>
  <div class="cloudw" style="top:24%;animation-duration:66s;animation-delay:-22s"><div class="cloud" style="transform:scale(.7);opacity:.8"></div></div>
  <div class="cloudw" style="top:6%;animation-duration:84s;animation-delay:-50s"><div class="cloud" style="transform:scale(1.35);opacity:.7"></div></div>
  <div class="cloudw" style="top:38%;animation-duration:58s;animation-delay:-30s"><div class="cloud" style="transform:scale(.55);opacity:.75"></div></div>
  <div class="bird" style="top:18%;animation-duration:30s;animation-delay:-5s"><svg viewBox="0 0 24 12" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M2 8 Q6 2 12 8 Q18 2 22 8"/></svg></div>
  <div class="bird" style="top:30%;animation-duration:38s;animation-delay:-18s"><svg viewBox="0 0 24 12" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M2 8 Q6 2 12 8 Q18 2 22 8"/></svg></div>
  <div class="mailw" style="top:20%;animation-duration:24s"><svg viewBox="0 0 32 24" fill="#fff" stroke="#2C3A8C" stroke-width="1.6" stroke-linejoin="round"><rect x="1" y="3" width="30" height="20" rx="3"/><path d="M2 5 16 15 30 5"/></svg></div>
  <div class="mailw" style="top:46%;animation-duration:32s;animation-delay:-12s"><svg viewBox="0 0 32 24" fill="#fff" stroke="#A9803F" stroke-width="1.6" stroke-linejoin="round"><rect x="1" y="3" width="30" height="20" rx="3"/><path d="M2 5 16 15 30 5"/></svg></div>
  <div class="mailw" style="top:62%;animation-duration:40s;animation-delay:-26s"><svg viewBox="0 0 32 24" fill="#fff" stroke="#2C3A8C" stroke-width="1.6" stroke-linejoin="round"><rect x="1" y="3" width="30" height="20" rx="3"/><path d="M2 5 16 15 30 5"/></svg></div>
  <div class="cyclist"><svg viewBox="0 0 130 170" fill="none" stroke="#2C3A8C" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round">
    <path d="M76 118 Q96 80 108 44" stroke="#9aa3c7" stroke-width="1.4" stroke-dasharray="3 4"/>
    <g class="kite">
      <g transform="rotate(28 108 38)">
        <rect x="92" y="24" width="32" height="22" rx="3" fill="#fff" stroke="#A9803F"/>
        <path d="M93 26 108 37 123 26" stroke="#A9803F"/>
      </g>
      <path d="M108 48 q-3 6 2 10 q5 4 1 10" stroke="#A9803F" stroke-width="1.2"/>
    </g>
    <g class="wheel"><circle cx="28" cy="150" r="14"/><path d="M28 136v28M14 150h28"/></g>
    <g class="wheel"><circle cx="86" cy="150" r="14"/><path d="M86 136v28M72 150h28"/></g>
    <path d="M28 150 54 150 68 124 86 150"/><path d="M54 150 62 124"/><path d="M58 122h14"/>
    <circle cx="68" cy="104" r="5.5" fill="#2C3A8C" stroke="none"/>
    <path d="M68 110 62 128 M62 128 56 148 M64 118 76 118"/>
  </svg></div>
</div>
<button id="sceneToggle" title="Show / hide background scene">🌤</button>
<div class="app"><div class="grid">
<main class="card">
  <header class="head">
    <div class="seal"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"><rect x="2.5" y="5" width="19" height="14" rx="2.5"/><path d="M3.2 6.2 12 13l8.8-6.8"/></svg></div>
    <div>
      <div class="kicker">Outgoing mail</div>
      <h1>Auto Mail Server</h1>
      <p class="meta">Sending from <b>{{sender}}</b></p>
    </div>
  </header>

  <section>
    <div class="eyebrow"><span class="n">01</span> Subject</div>
    <div class="pickrow">
      <select id="subjectPick"></select>
      <button class="mini" id="saveSubject">Save</button>
      <button class="mini del" id="delSubject">Delete</button>
    </div>
    <div class="field"><input id="subject" placeholder="Choose a saved subject, or write your own"></div>
  </section>

  <section>
    <div class="eyebrow"><span class="n">02</span> Message</div>
    <div class="pickrow">
      <select id="bodyPick"></select>
      <button class="mini" id="saveBody">Save</button>
      <button class="mini del" id="delBody">Delete</button>
    </div>
    <div class="field"><textarea id="body" placeholder="Choose a saved message, or write your own"></textarea></div>
    <div class="check"><input type="checkbox" id="is_html"><label for="is_html">This message is written in HTML</label></div>
  </section>

  <section>
    <div class="eyebrow"><span class="n">03</span> Attachments</div>
    <p class="help">Files ticked here are sent to <b>everyone</b>. For a different file per person, leave it unticked and use "Attachment per recipient" below.</p>
    <div class="att" id="attList"></div>
    <div class="filerow">
      <input type="file" id="uploads" multiple>
      <button class="mini" id="saveFile">Save file for reuse</button>
    </div>
  </section>

  <section>
    <div class="eyebrow"><span class="n">04</span> Recipients</div>
    <p class="help">Load a saved list, or enter recipients below and save them as a list.</p>
    <div class="pickrow">
      <select id="listPick"></select>
      <button class="mini" id="saveList">Save list</button>
      <button class="mini del" id="delList">Delete</button>
    </div>
    <div class="field">
      <span class="sublabel">Default sender name &mdash; used for anyone without a name below</span>
      <input id="sender_name" placeholder="Sender name shown to recipients">
    </div>
    <div class="field">
      <span class="sublabel">Email addresses &mdash; one per line (or comma / space separated)</span>
      <div class="sgwrap" style="position:relative">
        <textarea id="emails" placeholder="alice@example.com&#10;bob@example.com" style="min-height:96px"></textarea>
        <div id="emailSuggest" class="sgbox"></div>
      </div>
      <div id="recipientCheck" class="small" style="margin-top:8px"></div>
    </div>
    <div class="field">
      <span class="sublabel">Greeting &mdash; type ONE line to greet everyone the same, OR one line per recipient (same order as emails)</span>
      <textarea id="greetings" placeholder="One line for all:&#10;Dear Hiring Team,&#10;&#10;— or one per person —&#10;Dear Alice,&#10;Dear Bob,&#10;Dear Carol," style="min-height:96px"></textarea>
    </div>
    <div class="field">
      <span class="sublabel">Attachment per recipient &mdash; tick which saved file each person gets. Files chosen here are NOT also sent to everyone.</span>
      <div id="attMatrix" class="matrix"></div>
    </div>
    <div class="small" id="counter">0 emails &middot; 0 names</div>
  </section>

  <div id="flash"></div>
  <button class="send" id="go">Send now</button>
  <div class="field" style="margin-top:14px">
    <span class="sublabel">Or schedule for later (your local date &amp; time)</span>
    <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
      <input type="datetime-local" id="schedAt" style="flex:1;min-width:200px">
      <button class="mini" id="schedBtn">Schedule</button>
    </div>
    <div id="schedStatus" class="small" style="min-height:14px"></div>
  </div>
  <div id="schedList"></div>
  <div class="log" id="log"></div>
  <div style="margin-top:14px"><button class="mini" id="histBtn">Show recent sends</button></div>
  <div id="histList" style="display:none;margin-top:8px"></div>
</main>
<aside class="card side">
  <div id="cal"></div>
  <hr class="sidesep">
  <h3 class="sidehead">Day notes</h3>
  <p class="small" id="noteDate" style="margin-top:2px">Pick a date on the calendar. Each day keeps its own note.</p>
  <textarea id="note" class="notepad-ta" placeholder="Notes for this day…"></textarea>
  <div style="display:flex;align-items:center;gap:10px;margin-top:6px">
    <button class="mini" id="noteUndo">↩ Undo delete</button>
    <span id="noteStatus" class="small" style="margin:0;min-height:14px"></span>
  </div>
</aside>
</div></div>

<button id="recoverBtn" title="Recently deleted">&#8617;</button>
<div id="recoverPanel">
  <h3>Recently deleted</h3>
  <p class="small" style="margin-top:4px">Restore anything you removed by mistake.</p>
  <div id="trashList"></div>
  <button class="mini" id="purgeBtn" style="margin-top:14px">Clear all</button>
</div>

<script>
let BODIES={}, LISTS={}, FILECOLS=[], matrixState={};
const subjectPick=document.getElementById('subjectPick'),subject=document.getElementById('subject'),
      bodyPick=document.getElementById('bodyPick'),body=document.getElementById('body'),
      isHtml=document.getElementById('is_html'),listPick=document.getElementById('listPick'),
      flash=document.getElementById('flash'),go=document.getElementById('go'),log=document.getElementById('log'),
      emailsEl=document.getElementById('emails');
function opt(t){const o=document.createElement('option');o.textContent=t;o.value=t;return o;}
function escapeHtml(s){const d=document.createElement('div');d.textContent=s==null?'':s;return d.innerHTML;}
function flashMsg(m,bad){flash.style.color=bad?'var(--bad)':'var(--ok)';flash.textContent=m;
  setTimeout(()=>{if(flash.textContent===m)flash.textContent='';},2600);}
function showUndo(kind){
  const labels={subject:'Subject',body:'Message',list:'List',file:'File'};
  flash.style.color='var(--muted)';
  flash.innerHTML=labels[kind]+' deleted. <a href="#" id="undoLink" style="color:var(--accent);font-weight:600">Undo</a>';
  const link=document.getElementById('undoLink');const timer=setTimeout(()=>{flash.innerHTML='';},9000);
  link.onclick=async(ev)=>{ev.preventDefault();clearTimeout(timer);
    const fd=new FormData();fd.append('kind',kind);
    const j=await (await fetch('/undo',{method:'POST',body:fd})).json();
    if(j.error){flashMsg(j.error,true);return;}
    await loadOptions();flashMsg('Restored');if(panelOpen)loadTrash();};
}
async function loadOptions(){
  const o=await (await fetch('/options')).json();
  subjectPick.innerHTML='';subjectPick.appendChild(opt('— Write my own —'));subjectPick.firstChild.value='';
  o.subjects.forEach(s=>subjectPick.appendChild(opt(s)));
  BODIES={};bodyPick.innerHTML='';bodyPick.appendChild(opt('— Write my own —'));bodyPick.firstChild.value='';
  Object.keys(o.bodies).forEach(l=>{BODIES[l]=o.bodies[l];bodyPick.appendChild(opt(l));});
  LISTS={};listPick.innerHTML='';listPick.appendChild(opt('— Saved lists —'));listPick.firstChild.value='';
  Object.keys(o.lists).forEach(n=>{LISTS[n]=o.lists[n];listPick.appendChild(opt(n));});
  renderAttachments(o.attachments);
  FILECOLS=o.attachments.filter(a=>a.type==='file');
  buildMatrix();
}

function currentEmails(){return emailsEl.value.replace(/,/g,' ').split(/\s+/).map(s=>s.trim()).filter(Boolean);}

let KNOWN=[];
async function loadKnown(){try{const j=await (await fetch('/known_recipients')).json();KNOWN=j.emails||[];}catch(e){}}
const sgBox=document.getElementById('emailSuggest');
function curLine(){
  const v=emailsEl.value,pos=emailsEl.selectionStart;
  const start=v.lastIndexOf('\n',pos-1)+1;
  let end=v.indexOf('\n',pos);if(end===-1)end=v.length;
  return {start,end,text:v.slice(start,end)};
}
function hideSuggest(){sgBox.style.display='none';}
function showSuggest(){
  const ln=curLine().text.trim().toLowerCase();
  if(ln.length<2){hideSuggest();return;}
  const have=new Set(currentEmails().map(e=>e.toLowerCase()));
  const m=KNOWN.filter(e=>e.toLowerCase().includes(ln)&&e.toLowerCase()!==ln&&!have.has(e.toLowerCase())).slice(0,6);
  if(!m.length){hideSuggest();return;}
  sgBox.innerHTML=m.map(e=>`<div class="sg" data-e="${escapeHtml(e)}">${escapeHtml(e)}</div>`).join('');
  sgBox.style.display='block';
  sgBox.querySelectorAll('.sg').forEach(d=>d.onmousedown=ev=>{ev.preventDefault();applySuggest(d.dataset.e);});
}
function applySuggest(email){
  const {start,end}=curLine(),v=emailsEl.value;
  emailsEl.value=v.slice(0,start)+email+v.slice(end);
  const np=start+email.length;emailsEl.selectionStart=emailsEl.selectionEnd=np;
  hideSuggest();emailsEl.focus();
  updateCounter();scheduleCheck();buildMatrix();
}
emailsEl.addEventListener('keyup',e=>{if(e.key!=='Enter'&&e.key!=='Escape')showSuggest();if(e.key==='Escape')hideSuggest();});
emailsEl.addEventListener('click',showSuggest);
emailsEl.addEventListener('blur',()=>setTimeout(hideSuggest,150));
function buildMatrix(){
  const box=document.getElementById('attMatrix');if(!box)return;
  const emails=currentEmails();
  if(!FILECOLS.length){box.innerHTML='<div class="small" style="margin:0">No saved files yet. Save files under Attachments above to assign them per person.</div>';return;}
  if(!emails.length){box.innerHTML='<div class="small" style="margin:0">Add recipient emails above to choose a file per person.</div>';return;}
  let h='<table class="mtx"><thead><tr><th>Recipient</th>';
  FILECOLS.forEach((f,i)=>h+=`<th title="${escapeHtml(f.name)}">${i+1}</th>`);
  h+='</tr></thead><tbody>';
  emails.forEach(e=>{const key=e.toLowerCase(),set=matrixState[key]||new Set();
    h+=`<tr><td class="mtx-email" title="${escapeHtml(e)}">${escapeHtml(e)}</td>`;
    FILECOLS.forEach(f=>{h+=`<td><input type="checkbox" data-key="${escapeHtml(key)}" data-fid="${f.id}" ${set.has(f.id)?'checked':''}></td>`;});
    h+='</tr>';});
  h+='</tbody></table><div class="small mtx-legend">'+FILECOLS.map((f,i)=>`<b>${i+1}</b> = ${escapeHtml(f.name)}`).join(' &nbsp;·&nbsp; ')+'</div>';
  box.innerHTML=h;
  box.querySelectorAll('input[type=checkbox]').forEach(cb=>cb.onchange=()=>{
    const k=cb.dataset.key;if(!matrixState[k])matrixState[k]=new Set();
    if(cb.checked)matrixState[k].add(cb.dataset.fid);else matrixState[k].delete(cb.dataset.fid);
  });
}
function buildMatrixData(){return currentEmails().map(e=>{const s=matrixState[e.toLowerCase()];return s?[...s]:[];});}
function renderAttachments(list){
  const box=document.getElementById('attList');
  if(!list.length){box.innerHTML='<div class="small" style="margin:0">No saved files yet. Add one below.</div>';return;}
  box.innerHTML='';
  list.forEach(a=>{
    const row=document.createElement('div');row.className='row';
    const lab=document.createElement('label');
    const cb=document.createElement('input');cb.type='checkbox';cb.className='att-c';
    cb.dataset.type=a.type;cb.dataset.id=a.id;
    lab.appendChild(cb);lab.appendChild(document.createTextNode(' '+a.name));row.appendChild(lab);
    if(a.type==='file'){
      const x=document.createElement('button');x.className='xdel';x.textContent='✕';x.title='Delete saved file';
      x.onclick=async()=>{const fd=new FormData();fd.append('fid',a.id);
        const j=await (await fetch('/delete_file',{method:'POST',body:fd})).json();
        if(j.error){flashMsg(j.error,true);return;}
        await loadOptions();showUndo('file');if(panelOpen)loadTrash();};
      row.appendChild(x);
    }
    box.appendChild(row);
  });
}
subjectPick.onchange=()=>{if(subjectPick.value)subject.value=subjectPick.value;};
bodyPick.onchange=()=>{const k=bodyPick.value;if(k&&BODIES[k]){body.value=BODIES[k].body;isHtml.checked=!!BODIES[k].is_html;}};
listPick.onchange=()=>{const k=listPick.value;if(k&&LISTS[k]){emailsEl.value=LISTS[k].emails||'';updateCounter();buildMatrix();scheduleCheck();}};

document.getElementById('saveSubject').onclick=async()=>{
  const s=subject.value.trim();if(!s){flashMsg('Type a subject first',true);return;}
  const fd=new FormData();fd.append('subject',s);
  const j=await (await fetch('/save_subject',{method:'POST',body:fd})).json();
  if(j.error){flashMsg(j.error,true);return;}await loadOptions();subjectPick.value=s;flashMsg('Subject saved');};
document.getElementById('delSubject').onclick=async()=>{
  const s=subjectPick.value;if(!s){flashMsg('Pick a saved subject to delete',true);return;}
  const fd=new FormData();fd.append('subject',s);
  const j=await (await fetch('/delete_subject',{method:'POST',body:fd})).json();
  if(j.error){flashMsg(j.error,true);return;}subject.value='';await loadOptions();showUndo('subject');if(panelOpen)loadTrash();};
document.getElementById('saveBody').onclick=async()=>{
  if(!body.value.trim()){flashMsg('Type a body first',true);return;}
  const label=prompt('Name this message (it shows in the dropdown):');if(!label||!label.trim())return;
  const fd=new FormData();fd.append('label',label.trim());fd.append('body',body.value);
  if(isHtml.checked)fd.append('is_html','on');
  const j=await (await fetch('/save_body',{method:'POST',body:fd})).json();
  if(j.error){flashMsg(j.error,true);return;}await loadOptions();bodyPick.value=label.trim();flashMsg('Message saved');};
document.getElementById('delBody').onclick=async()=>{
  const k=bodyPick.value;if(!k){flashMsg('Pick a saved message to delete',true);return;}
  const fd=new FormData();fd.append('label',k);
  const j=await (await fetch('/delete_body',{method:'POST',body:fd})).json();
  if(j.error){flashMsg(j.error,true);return;}body.value='';await loadOptions();showUndo('body');if(panelOpen)loadTrash();};
document.getElementById('saveList').onclick=async()=>{
  if(!emailsEl.value.trim()){flashMsg('Enter some emails first',true);return;}
  const name=prompt('Name this recipient list (e.g. "March customers"):');if(!name||!name.trim())return;
  const fd=new FormData();fd.append('name',name.trim());fd.append('emails',emailsEl.value);fd.append('sender_names','');
  const j=await (await fetch('/save_list',{method:'POST',body:fd})).json();
  if(j.error){flashMsg(j.error,true);return;}await loadOptions();listPick.value=name.trim();flashMsg('List saved');};
document.getElementById('delList').onclick=async()=>{
  const k=listPick.value;if(!k){flashMsg('Pick a saved list to delete',true);return;}
  const fd=new FormData();fd.append('name',k);
  const j=await (await fetch('/delete_list',{method:'POST',body:fd})).json();
  if(j.error){flashMsg(j.error,true);return;}await loadOptions();showUndo('list');if(panelOpen)loadTrash();};
document.getElementById('saveFile').onclick=async()=>{
  const files=document.getElementById('uploads').files;
  if(!files.length){flashMsg('Choose a file first',true);return;}
  for(const f of files){const fd=new FormData();fd.append('file',f);
    const j=await (await fetch('/save_file',{method:'POST',body:fd})).json();
    if(j.error){flashMsg(j.error,true);return;}}
  document.getElementById('uploads').value='';await loadOptions();flashMsg('File saved for reuse');};

const counter=document.getElementById('counter');
function countEmails(){return emailsEl.value.replace(/,/g,' ').split(/\s+/).filter(Boolean).length;}
function updateCounter(){const e=countEmails();
  counter.textContent=`${e} email${e!=1?'s':''}`;counter.style.color='var(--muted)';}
emailsEl.oninput=()=>{updateCounter();scheduleCheck();buildMatrix();showSuggest();};

let recipTimer=null,lastRepeatCount=0;
function scheduleCheck(){clearTimeout(recipTimer);recipTimer=setTimeout(checkRecipients,450);}
async function checkRecipients(){
  const box=document.getElementById('recipientCheck');
  const raw=emailsEl.value.trim();
  if(!raw){box.innerHTML='';lastRepeatCount=0;return;}
  const fmtDMY=ts=>{const d=new Date(ts*1000),p=n=>String(n).padStart(2,'0');
    return `${p(d.getDate())}-${p(d.getMonth()+1)}-${d.getFullYear()}`;};
  let j;try{const fd=new FormData();fd.append('emails',raw);
    j=await (await fetch('/check_recipients',{method:'POST',body:fd})).json();}catch(e){return;}
  let repeats=0,dups=0;
  const rows=j.items.map(it=>{
    const sb=it.sent_before>0,dup=it.dup,red=sb||dup;
    if(sb)repeats++;if(dup)dups++;
    let tag='';
    if(sb)tag=` — already sent ×${it.sent_before}`+(it.last_ts?` on ${fmtDMY(it.last_ts)}`:'');
    else if(dup)tag=' — duplicate in list';
    return `<div style="color:${red?'var(--bad)':'var(--muted)'}">${red?'⚠ ':'• '}${escapeHtml(it.email)}${tag}</div>`;
  }).join('');
  let head='';
  if(repeats||dups){
    const parts=[];if(repeats)parts.push(repeats+' already emailed before');
    if(dups)parts.push(dups+' duplicate'+(dups>1?'s':'')+' in list');
    head=`<div style="color:var(--bad);font-weight:600;margin-bottom:4px">${parts.join(', ')}</div>`;
  }
  box.innerHTML=head+rows;lastRepeatCount=repeats;
}

const recoverBtn=document.getElementById('recoverBtn'),recoverPanel=document.getElementById('recoverPanel'),
      trashList=document.getElementById('trashList');
let panelOpen=false;
recoverBtn.onclick=()=>{panelOpen=!panelOpen;recoverPanel.style.display=panelOpen?'block':'none';if(panelOpen)loadTrash();};
async function loadTrash(){
  const t=await (await fetch('/trash')).json();let html='';
  const group=(title,items,icon)=>{if(!items.length)return'';
    let h=`<div class="tgroup">${title}</div>`;
    items.forEach(it=>{h+=`<div class="trow"><span title="${escapeHtml(it.label)}">${icon} ${escapeHtml(it.label)}</span>`+
      `<button class="mini" data-i="${it.i}">Restore</button></div>`;});return h;};
  html+=group('Subjects',t.subjects,'📝');html+=group('Messages',t.bodies,'📄');
  html+=group('Lists',t.lists,'👥');html+=group('Files',t.files,'📎');
  if(!html)html='<div class="small">Nothing here. Deleted items show up here.</div>';
  trashList.innerHTML=html;
  trashList.querySelectorAll('button[data-i]').forEach(btn=>btn.onclick=async()=>{
    const fd=new FormData();fd.append('id',btn.dataset.i);
    const j=await (await fetch('/restore',{method:'POST',body:fd})).json();
    if(j.error){flashMsg(j.error,true);return;}
    await loadOptions();await loadTrash();flashMsg('Restored');});
}
document.getElementById('purgeBtn').onclick=async()=>{
  if(!confirm('Permanently clear all recently-deleted items?'))return;
  await fetch('/purge_trash',{method:'POST'});await loadTrash();};

const histBtn=document.getElementById('histBtn'),histList=document.getElementById('histList');
let histOpen=false;
histBtn.onclick=async()=>{histOpen=!histOpen;histList.style.display=histOpen?'block':'none';
  histBtn.textContent=histOpen?'Hide recent sends':'Show recent sends';if(histOpen)loadHistory();};
async function loadHistory(){
  const h=await (await fetch('/history')).json();
  if(!h.rows.length){histList.innerHTML='<div class="small">No sends recorded yet.</div>';return;}
  histList.innerHTML=h.rows.map(r=>{const d=new Date(r.ts*1000).toLocaleString();
    return `<div class="trow"><span title="${escapeHtml(r.subject)}" class="${r.ok?'ok':'bad'}">${r.ok?'✓':'✗'} ${escapeHtml(r.email)}</span>`+
      `<span class="small" style="margin:0">${d}</span></div>`;}).join('');
}

function buildSendForm(){
  const fd=new FormData();
  fd.append('subject',subject.value);fd.append('body',body.value);
  if(isHtml.checked)fd.append('is_html','on');
  fd.append('sender_name',document.getElementById('sender_name').value.trim());
  fd.append('emails',emailsEl.value.trim());fd.append('sender_names','');
  fd.append('greetings',document.getElementById('greetings').value);
  fd.append('matrix',JSON.stringify(buildMatrixData()));
  document.querySelectorAll('#attList .att-c:checked').forEach(c=>{
    if(c.dataset.type==='preset')fd.append('preset_attachments',c.dataset.id);
    else fd.append('saved_files',c.dataset.id);});
  const ups=document.getElementById('uploads').files;
  for(let i=0;i<ups.length;i++)fd.append('uploads',ups[i]);
  return fd;
}
function validCompose(){
  if(!subject.value.trim()){alert('Enter a subject');return false;}
  if(!body.value.trim()){alert('Enter a body');return false;}
  if(!emailsEl.value.trim()){alert('Enter an email');return false;}
  return true;
}

go.onclick=async()=>{
  if(!validCompose())return;
  if(lastRepeatCount>0 && !confirm(lastRepeatCount+' recipient(s) were already emailed before. Send again?'))return;
  const fd=buildSendForm();
  go.disabled=true;go.textContent='Sending...';log.innerHTML='';
  try{
    const j=await (await fetch('/send',{method:'POST',body:fd})).json();
    if(j.error){log.innerHTML='<div class="bad">✗ '+j.error+'</div>';}
    else{log.innerHTML=j.results.map(e=>`<div class="${e.ok?'ok':'bad'}">${e.ok?'✓':'✗'} ${e.email} — ${e.msg}</div>`).join('');
      if(histOpen)loadHistory();}
  }catch(e){log.innerHTML='<div class="bad">✗ '+e+'</div>';}
  go.disabled=false;go.textContent='Send now';checkRecipients();loadKnown();
};

const schedAt=document.getElementById('schedAt'),schedStatus=document.getElementById('schedStatus');
const fmtDT=ts=>{const d=new Date(ts*1000),p=n=>String(n).padStart(2,'0');
  return `${p(d.getDate())}-${p(d.getMonth()+1)}-${d.getFullYear()} ${p(d.getHours())}:${p(d.getMinutes())}`;};
document.getElementById('schedBtn').onclick=async()=>{
  if(!validCompose())return;
  if(!schedAt.value){schedStatus.style.color='var(--bad)';schedStatus.textContent='Pick a date & time first';return;}
  const when=new Date(schedAt.value).getTime()/1000;
  if(when < Date.now()/1000-60){schedStatus.style.color='var(--bad)';schedStatus.textContent='That time is in the past';return;}
  const fd=buildSendForm();fd.append('run_at',when);
  schedStatus.style.color='var(--muted)';schedStatus.textContent='Scheduling…';
  const j=await (await fetch('/schedule',{method:'POST',body:fd})).json();
  if(j.error){schedStatus.style.color='var(--bad)';schedStatus.textContent=j.error;return;}
  schedStatus.style.color='var(--ok)';schedStatus.textContent='Scheduled ✓';schedAt.value='';loadScheduled();
};
async function loadScheduled(){
  const box=document.getElementById('schedList');
  let j;try{j=await (await fetch('/scheduled')).json();}catch(e){return;}
  if(!j.jobs.length){box.innerHTML='';return;}
  box.innerHTML='<div class="tgroup">Scheduled sends</div>'+j.jobs.map(job=>{
    const color=job.status==='pending'?'var(--accent)':job.status==='sent'?'var(--ok)':job.status==='cancelled'?'var(--muted)':'var(--bad)';
    const right=job.status==='pending'?fmtDT(job.run_at):escapeHtml(job.status+(job.result?' · '+job.result:''));
    const cancel=job.status==='pending'?`<button class="mini" data-cancel="${job.id}" style="padding:6px 10px">Cancel</button>`:'';
    return `<div class="trow"><span title="${escapeHtml(job.subject)}"><span style="color:${color}">●</span> ${escapeHtml(job.subject||'(no subject)')} · ${job.count} recip.</span>`+
           `<span class="small" style="margin:0">${right}</span>${cancel}</div>`;
  }).join('');
  box.querySelectorAll('button[data-cancel]').forEach(b=>b.onclick=async()=>{
    const fd=new FormData();fd.append('id',b.dataset.cancel);
    const r=await (await fetch('/cancel_scheduled',{method:'POST',body:fd})).json();
    if(r.error){schedStatus.style.color='var(--bad)';schedStatus.textContent=r.error;return;}
    loadScheduled();});
}
setInterval(loadScheduled,20000);loadScheduled();

const note=document.getElementById('note'),noteStatus=document.getElementById('noteStatus'),
      noteDate=document.getElementById('noteDate');
const MONTHS=['January','February','March','April','May','June','July','August','September','October','November','December'];
const MShort=['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
const WDAYS=['Sunday','Monday','Tuesday','Wednesday','Thursday','Friday','Saturday'];
function iso(y,m,d){return `${y}-${String(m+1).padStart(2,'0')}-${String(d).padStart(2,'0')}`;}
const _t=new Date();
let selDay=iso(_t.getFullYear(),_t.getMonth(),_t.getDate());
let calDate=new Date();
let noteDays=new Set();
let noteTimer=null;

async function loadNoteDays(){try{const j=await (await fetch('/note_days')).json();noteDays=new Set(j.days);}catch(e){}}

function renderCal(){
  const box=document.getElementById('cal');
  const y=calDate.getFullYear(),m=calDate.getMonth();
  const first=new Date(y,m,1).getDay(),days=new Date(y,m+1,0).getDate(),today=new Date();
  let h=`<div class="calhead"><button class="calnav" id="calPrev">‹</button>`+
        `<span class="t">${MONTHS[m]} ${y}</span>`+
        `<button class="calnav" id="calNext">›</button></div><div class="calgrid">`;
  ['Su','Mo','Tu','We','Th','Fr','Sa'].forEach(d=>h+=`<div class="calw">${d}</div>`);
  for(let i=0;i<first;i++)h+='<div class="cald empty"></div>';
  for(let d=1;d<=days;d++){
    const ds=iso(y,m,d);
    const isT=(d===today.getDate()&&m===today.getMonth()&&y===today.getFullYear());
    const cls=['cald'];if(isT)cls.push('today');if(ds===selDay)cls.push('sel');if(noteDays.has(ds))cls.push('has-note');
    h+=`<div class="${cls.join(' ')}" data-ds="${ds}">${d}</div>`;
  }
  box.innerHTML=h+'</div>';
  document.getElementById('calPrev').onclick=()=>{calDate=new Date(y,m-1,1);renderCal();};
  document.getElementById('calNext').onclick=()=>{calDate=new Date(y,m+1,1);renderCal();};
  box.querySelectorAll('.cald[data-ds]').forEach(el=>el.onclick=()=>selectDay(el.dataset.ds));
}

async function selectDay(ds){
  selDay=ds;
  const [Y,M,D]=ds.split('-').map(Number);const dt=new Date(Y,M-1,D);
  noteDate.textContent=`${WDAYS[dt.getDay()]}, ${D} ${MShort[M-1]} ${Y} — saves automatically`;
  try{const j=await (await fetch('/day_note?day='+ds)).json();note.value=j.content||'';}catch(e){note.value='';}
  note.classList.remove('fadein');void note.offsetWidth;note.classList.add('fadein');
  renderCal();
}

note.oninput=()=>{noteStatus.style.color='var(--muted)';noteStatus.textContent='Saving…';
  clearTimeout(noteTimer);noteTimer=setTimeout(saveDayNote,600);};
async function saveDayNote(){
  const fd=new FormData();fd.append('day',selDay);fd.append('content',note.value);
  try{await fetch('/save_day_note',{method:'POST',body:fd});
    if(note.value.trim())noteDays.add(selDay);else noteDays.delete(selDay);
    noteStatus.style.color='var(--ok)';noteStatus.textContent='Saved ✓';
    setTimeout(()=>{if(noteStatus.textContent==='Saved ✓')noteStatus.textContent='';},1500);
    renderCal();
  }catch(e){noteStatus.textContent='';}
}

(async()=>{await loadNoteDays();renderCal();selectDay(selDay);})();

document.getElementById('noteUndo').onclick=async()=>{
  const fd=new FormData();fd.append('day',selDay);
  const j=await (await fetch('/undo_day_note',{method:'POST',body:fd})).json();
  if(j.error){noteStatus.style.color='var(--bad)';noteStatus.textContent=j.error;
    setTimeout(()=>{noteStatus.textContent='';},2200);return;}
  note.value=j.content||'';
  if(note.value.trim())noteDays.add(selDay);else noteDays.delete(selDay);
  renderCal();note.classList.remove('fadein');void note.offsetWidth;note.classList.add('fadein');
  noteStatus.style.color='var(--ok)';noteStatus.textContent='Restored ✓';
  setTimeout(()=>{if(noteStatus.textContent==='Restored ✓')noteStatus.textContent='';},1500);
};

const sceneToggle=document.getElementById('sceneToggle');
if(sceneToggle)sceneToggle.onclick=()=>document.body.classList.toggle('plain');

loadOptions();updateCounter();loadKnown();
</script>
</body></html>
"""

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8894"))
    print(f"\n  Auto Mail Server running →  http://127.0.0.1:{port}\n")
    app.run(host="0.0.0.0", port=port, debug=False)
