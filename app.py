import json
import os
import sqlite3
import time
from datetime import datetime, timezone
from urllib.parse import urlencode

import requests
from flask import Flask, g, jsonify, redirect, render_template_string, request

app = Flask(__name__)

DB_PATH = os.environ.get("LEADS_DB", os.path.join(os.path.dirname(__file__), "leads.db"))
REQUIRED_FIELDS = [f for f in os.environ.get("REQUIRED_FIELDS", "fullName,email,zip").split(",") if f]
OPTIONAL_FIELDS = [f for f in os.environ.get("OPTIONAL_FIELDS", "phone").split(",") if f]
# Recognized tracking fields passed through by the ad platform (e.g. Taboola click id).
# Never flagged as extra, never reported missing.
PASSTHROUGH_FIELDS = [f for f in os.environ.get("PASSTHROUGH_FIELDS", "tblci").split(",") if f]

S2S_CONVERSION_URL = os.environ.get("S2S_CONVERSION_URL", "https://trc.taboola.com/actions-handler/log/3/s2s-action")
S2S_CONVERSION_NAME = os.environ.get("S2S_CONVERSION_NAME", "LeadGenTest")
S2S_TIMEOUT_SECONDS = 5


def db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("""
            CREATE TABLE IF NOT EXISTS leads (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                received_at  TEXT NOT NULL,
                remote_ip    TEXT,
                origin       TEXT,
                referer      TEXT,
                user_agent   TEXT,
                content_type TEXT,
                payload      TEXT,
                validation   TEXT,
                query_params TEXT,
                s2s          TEXT
            )
        """)
        # existing db file from an older schema: add the new columns in place
        for column in ("query_params", "s2s"):
            try:
                g.db.execute(f"ALTER TABLE leads ADD COLUMN {column} TEXT")
            except sqlite3.OperationalError:
                pass
    return g.db


@app.teardown_appcontext
def close_db(exc):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


@app.after_request
def cors(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    resp.headers["Access-Control-Max-Age"] = "86400"
    return resp


def parse_body():
    ctype = (request.content_type or "").lower()
    if "json" in ctype:
        data = request.get_json(silent=True)
        return (data if isinstance(data, dict) else {"_raw": request.get_data(as_text=True)}), ctype
    if request.form:
        return {k: v for k, v in request.form.items()}, ctype or "application/x-www-form-urlencoded"
    raw = request.get_data(as_text=True)
    data = None
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        pass
    return (data if isinstance(data, dict) else {"_raw": raw}), ctype or "unknown"


def validate(payload):
    present = set(payload.keys())
    missing_required = [f for f in REQUIRED_FIELDS if f not in present]
    missing_optional = [f for f in OPTIONAL_FIELDS if f not in present]
    known = set(REQUIRED_FIELDS) | set(OPTIONAL_FIELDS) | set(PASSTHROUGH_FIELDS)
    extra = sorted(present - known - {"_raw"})
    return {
        "ok": not missing_required,
        "missing_required": missing_required,
        "missing_optional": missing_optional,
        "extra": extra,
    }


@app.route("/leads", methods=["POST", "OPTIONS"])
def leads():
    if request.method == "OPTIONS":
        return "", 204

    simulate = request.args.get("simulate")
    if simulate == "timeout":
        time.sleep(20)
    elif simulate and simulate.isdigit():
        return jsonify({"status": "error", "simulated": int(simulate)}), int(simulate)
    elif simulate == "reject":
        return jsonify({"status": "error", "reason": "simulated rejection"}), 200

    payload, ctype = parse_body()
    verdict = validate(payload)
    query_params = request.args.to_dict(flat=True)
    tblci = payload.get("tblci") if isinstance(payload.get("tblci"), str) else None
    s2s_result = fire_s2s_conversion(tblci)

    conn = db()
    cur = conn.execute(
        "INSERT INTO leads (received_at, remote_ip, origin, referer, user_agent, content_type, payload, validation, query_params, s2s) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            datetime.now(timezone.utc).isoformat(timespec="seconds"),
            request.headers.get("X-Forwarded-For", request.remote_addr),
            request.headers.get("Origin"),
            request.headers.get("Referer"),
            request.headers.get("User-Agent"),
            ctype,
            json.dumps(payload, ensure_ascii=False),
            json.dumps(verdict),
            json.dumps(query_params, ensure_ascii=False),
            json.dumps(s2s_result) if s2s_result else None,
        ),
    )
    conn.commit()

    if request.args.get("redir") == "true":
        thank_you_url = "/thanks" + ("?" + urlencode({"tblci": tblci}) if tblci else "")
        # a real HTML form submit should navigate; a fetch/XHR JSON client
        # gets the url in the response and navigates itself
        if "json" not in ctype:
            return redirect(thank_you_url, code=303)
        return jsonify({"status": "ok", "id": cur.lastrowid, "validation": verdict,
                        "thankYouUrl": thank_you_url, "s2s": s2s_result}), 200

    return jsonify({"status": "ok", "id": cur.lastrowid, "validation": verdict, "s2s": s2s_result}), 200


def fire_s2s_conversion(tblci):
    if not tblci:
        return None
    params = {"click-id": tblci, "name": S2S_CONVERSION_NAME}
    url = S2S_CONVERSION_URL + "?" + urlencode(params)
    try:
        resp = requests.get(url, timeout=S2S_TIMEOUT_SECONDS)
        return {"url": url, "status": resp.status_code, "ok": resp.ok}
    except requests.RequestException as e:
        return {"url": url, "status": None, "ok": False, "error": type(e).__name__}


@app.route("/leads.json")
def leads_json():
    limit = min(int(request.args.get("limit", 100)), 1000)
    rows = db().execute("SELECT * FROM leads ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["payload"] = json.loads(d["payload"])
        d["validation"] = json.loads(d["validation"])
        d["query_params"] = json.loads(d["query_params"]) if d.get("query_params") else {}
        d["s2s"] = json.loads(d["s2s"]) if d.get("s2s") else None
        out.append(d)
    return jsonify(out)


@app.route("/clear", methods=["POST"])
def clear():
    db().execute("DELETE FROM leads")
    db().commit()
    return jsonify({"status": "cleared"})


@app.route("/health")
def health():
    return jsonify({"ok": True})


THANKS = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Thank you</title>
<style>
  body { font: 16px/1.5 -apple-system, Segoe UI, sans-serif; margin: 0; background: #f7f8fa; color: #1a1a2e;
         display: flex; align-items: center; justify-content: center; min-height: 100vh; }
  .card { background: #fff; border: 1px solid #e3e5ea; border-radius: 12px; padding: 40px 48px; text-align: center;
          box-shadow: 0 2px 10px rgba(0,0,0,.05); max-width: 560px; }
  .check { font-size: 44px; }
  h1 { font-size: 22px; margin: 12px 0 6px; }
  p { color: #555; margin: 6px 0; }
  code { background: #eef1f6; padding: 2px 8px; border-radius: 6px; font-size: 13px; word-break: break-all; }
  .muted { color: #999; font-size: 13px; margin-top: 18px; }
</style>
</head>
<body>
<div class="card">
  <div class="check">&#9989;</div>
  <h1>Thank you!</h1>
  <p>Your details were submitted successfully.</p>
  {% if tblci %}<p class="muted">click id: <code>{{ tblci }}</code></p>{% endif %}
</div>
</body>
</html>"""


@app.route("/thanks")
def thanks():
    return render_template_string(THANKS, tblci=request.args.get("tblci"))


DASHBOARD = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Lead Catcher</title>
<style>
  body { font: 14px/1.45 -apple-system, Segoe UI, sans-serif; margin: 24px; color: #1a1a2e; }
  h1 { font-size: 20px; } h1 code { background:#eef; padding:2px 6px; border-radius:4px; }
  table { border-collapse: collapse; width: 100%; margin-top: 12px; }
  th, td { border: 1px solid #ddd; padding: 6px 10px; text-align: left; vertical-align: top; }
  th { background: #f4f4f8; position: sticky; top: 0; }
  tr.bad td { background: #fff2f2; }
  .pill { display:inline-block; padding:1px 8px; border-radius:10px; font-size:12px; }
  .ok  { background:#d9f2e3; color:#116633; }
  .bad { background:#fbd9d9; color:#992222; }
  pre { margin:0; white-space: pre-wrap; word-break: break-all; font-size:12px; }
  button { padding:6px 14px; border:1px solid #bbb; border-radius:6px; background:#fff; cursor:pointer; }
  button:hover { background:#f4f4f8; }
  .muted { color:#888; font-size:12px; }
</style>
</head>
<body>
<h1>Lead Catcher &mdash; POST to <code>/leads</code></h1>
<p class="muted">Auto-refreshes every 3s. Required: {{required}} &middot; Optional: {{optional}} &middot;
   Simulations: <code>?simulate=500</code> <code>?simulate=timeout</code> <code>?simulate=reject</code></p>
<button onclick="clearLeads()">Clear all</button>
<span id="count" class="muted"></span>
<table id="tbl">
  <thead><tr>
    <th>#</th><th>Received (UTC)</th><th>Validation</th><th>fullName</th><th>email</th><th>phone</th><th>zip</th>
    <th>Query params</th><th>S2S conversion</th>
    <th>Content-Type</th><th>Origin / IP</th><th>Raw payload</th>
  </tr></thead>
  <tbody></tbody>
</table>
<script>
async function refresh() {
  const res = await fetch('/leads.json');
  const leads = await res.json();
  document.getElementById('count').textContent = ' ' + leads.length + ' lead(s)';
  const tb = document.querySelector('#tbl tbody');
  tb.innerHTML = '';
  for (const l of leads) {
    const v = l.validation, p = l.payload;
    const tr = document.createElement('tr');
    if (!v.ok) tr.className = 'bad';
    const vtext = v.ok ? '<span class="pill ok">ok</span>'
      : '<span class="pill bad">missing: ' + v.missing_required.join(', ') + '</span>';
    const extra = (v.extra && v.extra.length) ? ' <span class="pill bad">extra: ' + v.extra.join(', ') + '</span>' : '';
    const qp = l.query_params && Object.keys(l.query_params).length
      ? '<pre>' + esc(JSON.stringify(l.query_params)) + '</pre>' : '';
    let s2s = '';
    if (l.s2s) {
      s2s = l.s2s.ok ? '<span class="pill ok">fired: ' + l.s2s.status + '</span>'
                     : '<span class="pill bad">failed: ' + esc(l.s2s.error || l.s2s.status) + '</span>';
    }
    tr.innerHTML =
      '<td>' + l.id + '</td>' +
      '<td>' + l.received_at + '</td>' +
      '<td>' + vtext + extra + '</td>' +
      '<td>' + esc(p.fullName) + '</td>' +
      '<td>' + esc(p.email) + '</td>' +
      '<td>' + esc(p.phone) + '</td>' +
      '<td>' + esc(p.zip) + '</td>' +
      '<td>' + qp + '</td>' +
      '<td>' + s2s + '</td>' +
      '<td>' + esc(l.content_type) + '</td>' +
      '<td>' + esc(l.origin || '') + '<br>' + esc(l.remote_ip) + '</td>' +
      '<td><pre>' + esc(JSON.stringify(p)) + '</pre></td>';
    tb.appendChild(tr);
  }
}
function esc(s) {
  if (s === undefined || s === null) return '';
  return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}
async function clearLeads() {
  await fetch('/clear', {method: 'POST'});
  refresh();
}
refresh();
setInterval(refresh, 3000);
</script>
</body>
</html>"""


@app.route("/")
def dashboard():
    return render_template_string(
        DASHBOARD,
        required=", ".join(REQUIRED_FIELDS),
        optional=", ".join(OPTIONAL_FIELDS + PASSTHROUGH_FIELDS),
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
