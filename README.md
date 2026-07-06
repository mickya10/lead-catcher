# lead-catcher

Test receiver for Taboola LeadGen form submissions — the thing `submitUrl`
points at. Accepts the form POST, validates it against the LeadGen field spec,
stores it, and shows a live dashboard.

## Endpoints

| Endpoint | What |
|---|---|
| `POST /leads` | Accepts JSON **or** form-urlencoded body. CORS wide-open (+ OPTIONS preflight). Returns `{status, id, validation}`. |
| `GET /` | Live dashboard (auto-refresh, per-lead validation verdict, raw payload, Clear button). |
| `GET /leads.json` | Machine-readable list (newest first) for test assertions. |
| `POST /clear` | Wipe all stored leads. |
| `GET /health` | `{ok:true}` — pinger target. |

Failure simulation on the submit URL:
`?simulate=500` (any status code) · `?simulate=timeout` (20s hang) · `?simulate=reject` (200 with error body).

Validation: required `fullName,email,zip`, optional `phone` — override with
`REQUIRED_FIELDS` / `OPTIONAL_FIELDS` env vars.

## Run locally

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python app.py           # http://localhost:8080
```

## Deploy (Render free tier)

1. Push this repo to GitHub.
2. Render dashboard → New → Blueprint → pick the repo (`render.yaml` drives it).
3. Note the public URL, e.g. `https://lead-catcher.onrender.com`.

Storage caveat: Render's free tier has an **ephemeral disk** — SQLite survives
idle wake-ups while the instance lives, but resets on deploy/restart. Fine for
a test harness; don't treat it as durable.

## Keep it awake

Free services spin down after ~15 min idle (30–60s cold start). Either:

- Add `https://<app>.onrender.com/health` to an existing external pinger
  (UptimeRobot / cron-job.org, 5-min interval) — most reliable, or
- Enable the bundled GitHub Actions cron: repo → Settings → Variables → add
  `SITE_URL=https://<app>.onrender.com`, then Actions → keepalive → enable.

Note: 24/7 pinging consumes ~all of Render's free monthly hours (~750h) — one
always-awake free service per account just fits; a second one will exhaust it.

## Point LeadGen at it

```sql
UPDATE trc.lead_gen_inventory_creative_component
SET submit_url = 'https://<app>.onrender.com/leads'
WHERE instruction_id = <YOUR_INSTRUCTION_ID>;
```

This is a test tool: don't collect real user PII with it — there's no privacy
policy, retention, or auth.
