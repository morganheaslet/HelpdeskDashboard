"""
SmileBack CSAT reviews -> sb.reviews.

SmileBack's API reference is a Swagger page behind the app login (Account >
API Credentials), so several details below come from third-party
integrations rather than the vendor and are written defensively:
- Auth: OAuth2 password grant against {SMILEBACK_BASE_URL}/token/ with the
  client id/secret from that page plus a SmileBack user's login. Use a
  dedicated service user; store the password and secret in Key Vault.
- Reviews: GET {base}/v3/reviews/?modified_since=...&include_unrated=true.
  Pagination style is unconfirmed, so _get_pages() follows a `next` URL if
  the response is a dict that has one, and stops otherwise.
- rating: documented only as Positive/Neutral/Negative; _rating_score()
  accepts words or +1/0/-1.
- ticket.id: assumed to be the ConnectWise ticket id. The first sync logs one
  sample review — check that its ticket id matches a real CW ticket number.
"""
import logging
import os
from datetime import datetime, timedelta, timezone

import requests

from shared import db

ENTITY = "sb_reviews"
BACKFILL_START = datetime(2025, 1, 1)
OVERLAP = timedelta(minutes=10)


class SmileBackClient:
    def __init__(self):
        self.base = os.environ.get("SMILEBACK_BASE_URL", "https://app.smileback.io/api").rstrip("/")
        self.client_id = os.environ.get("SMILEBACK_CLIENT_ID", "")
        self.client_secret = os.environ.get("SMILEBACK_CLIENT_SECRET", "")
        self.username = os.environ.get("SMILEBACK_USERNAME", "")
        self.password = os.environ.get("SMILEBACK_PASSWORD", "")
        self._token = None

    def configured(self):
        return all([self.client_id, self.client_secret, self.username, self.password])

    def _auth(self):
        resp = requests.post(f"{self.base}/token/", data={
            "grant_type": "password", "client_id": self.client_id, "client_secret": self.client_secret,
            "username": self.username, "password": self.password, "scope": "read read_recent",
        }, timeout=30)
        resp.raise_for_status()
        self._token = resp.json()["access_token"]

    def _get_pages(self, url, params):
        if not self._token:
            self._auth()
        while url:
            resp = requests.get(url, params=params, headers={"Authorization": f"Bearer {self._token}"}, timeout=60)
            resp.raise_for_status()
            body = resp.json()
            if isinstance(body, list):
                yield from body
                return
            yield from body.get("results") or body.get("data") or []
            url, params = body.get("next"), None   # `next` already carries the query

    def reviews_modified_since(self, since):
        return self._get_pages(f"{self.base}/v3/reviews/", {
            "modified_since": since.strftime("%Y-%m-%dT%H:%M:%SZ"), "include_unrated": "true",
        })


def _rating_score(raw):
    if raw is None or raw == "":
        return None
    s = str(raw).strip().lower()
    if s in ("positive", "+1", "1", "good", "happy"):
        return 1
    if s in ("neutral", "0", "ok"):
        return 0
    if s in ("negative", "-1", "bad", "unhappy"):
        return -1
    return None


def _s(v, n):
    if v is None:
        return None
    v = str(v).strip()
    return v[:n] if v else None


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def review_row(r, now):
    ticket = r.get("ticket") or {}
    contact = r.get("contact") or {}
    company = r.get("company") or {}
    agents = ticket.get("agents") or []
    if isinstance(agents, list):
        agents = ", ".join(a.get("name", "") if isinstance(a, dict) else str(a) for a in agents)
    tags = r.get("tags") or []
    if isinstance(tags, list):
        tags = ", ".join(t.get("name", "") if isinstance(t, dict) else str(t) for t in tags)
    return {
        "review_id": r["id"],
        "rating_raw": _s(r.get("rating"), 30),
        "rating_score": _rating_score(r.get("rating")),
        "comment": _s(r.get("comment"), 2000),
        "status": _s(r.get("status"), 50),
        "tags": _s(tags, 500),
        "rated_on": db.parse_ts(r.get("rated_on")),
        "viewed_on": db.parse_ts(r.get("viewed_on")),
        "last_modified": db.parse_ts(r.get("last_modified")),
        "ticket_id": _int(ticket.get("id")),
        "ticket_title": _s(ticket.get("title"), 500),
        "board_name": _s((ticket.get("segment") or {}).get("name"), 100),
        "ticket_closed_on": db.parse_ts(ticket.get("closed_on")),
        "agents": _s(agents, 500),
        "contact_name": _s(contact.get("name"), 200),
        "contact_email": _s(contact.get("email"), 200),
        "company_name": _s(company.get("name"), 200),
        "permalink": _s(r.get("permalink"), 400),
        "synced_at": now,
    }


def sync_smileback(conn, deadline):
    client = SmileBackClient()
    if not client.configured():
        logging.info("SmileBack not configured — skipping sb.reviews")
        return 0
    state = db.get_state(conn, ENTITY)
    since = (state["cursor_utc"] or BACKFILL_START) - (OVERLAP if state["cursor_utc"] else timedelta(0))
    now = db.utcnow()
    rows, newest, logged_sample = [], state["cursor_utc"], bool(state["backfill_complete"])
    for r in client.reviews_modified_since(since):
        if not logged_sample:
            logging.info("SmileBack sample review (verify ticket.id is the CW ticket number): %s", r)
            logged_sample = True
        row = review_row(r, now)
        rows.append(row)
        if row["last_modified"] and (newest is None or row["last_modified"] > newest):
            newest = row["last_modified"]
        if len(rows) >= 1000:
            db.upsert(conn, "sb.reviews", ["review_id"], rows)
            conn.commit()
            rows.clear()
        if datetime.now(timezone.utc) >= deadline:
            # Pages may not be ordered by last_modified, so a partial pull
            # can't safely advance the cursor — the next run repeats it (MERGE
            # makes that harmless).
            db.upsert(conn, "sb.reviews", ["review_id"], rows)
            conn.commit()
            logging.warning("SmileBack sync hit its time budget — will resume from the same cursor next run")
            return 0
    n = db.upsert(conn, "sb.reviews", ["review_id"], rows)
    db.set_state(conn, ENTITY, cursor_utc=newest or now, backfill_complete=True)
    conn.commit()
    return n
