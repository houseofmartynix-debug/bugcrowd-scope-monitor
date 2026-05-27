"""Bugcrowd scope monitor — one-shot poll + diff + Telegram notify.

Designed to run from cron/GitHub Actions (not a long-lived process).
Each run:
  1. Load previous state (state.json: just the seen-fingerprint set, no last_run)
  2. Fetch recent scope updates from bbscope.com (Bugcrowd platform filter)
  3. Dedupe vs seen fingerprints
  4. Group fresh updates per program handle
  5. Send Telegram message per program (HTML format)
  6. Append audit row to diff_log.jsonl
  7. Save new state (sans last_run, so git diff catches real changes only)

Env:
  TG_TOKEN     — Telegram bot token (required)
  TG_CHAT_ID   — chat id to notify (required)
  BC_UA        — override user-agent (optional)
  STATE_FILE   — override state.json path (optional, default: ./state.json)
  AUDIT_FILE   — override diff_log.jsonl path (optional)
  LOOKBACK     — bbscope `since` query (default 2d)
  MAX_NOTIFY   — cap per-program messages per run (default 50)
  SEEN_CAP     — max fingerprints to retain in state (default 8000)
  DRY_RUN      — if set, don't send telegram messages (log instead)
  SEED_ONLY    — if set, snapshot fingerprints without sending anything
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("bc-monitor")

STATE_FILE = Path(os.getenv("STATE_FILE", "state.json"))
AUDIT_FILE = Path(os.getenv("AUDIT_FILE", "diff_log.jsonl"))
LOOKBACK   = os.getenv("LOOKBACK", "2d")
MAX_NOTIFY = int(os.getenv("MAX_NOTIFY", "50"))
SEEN_CAP   = int(os.getenv("SEEN_CAP", "20000"))
DRY_RUN    = bool(os.getenv("DRY_RUN"))
SEED_ONLY  = bool(os.getenv("SEED_ONLY"))

BC_UA = os.getenv("BC_UA", "bugcrowd-scope-monitor/1.0 (GH Actions; researcher: mrcslvknm)")
BBSCOPE_API = "https://bbscope.com/api/v1"
TG_API = "https://api.telegram.org/bot{token}/sendMessage"


def load_state() -> dict:
    if not STATE_FILE.exists():
        return {"seen": [], "version": 1}
    try:
        s = json.loads(STATE_FILE.read_text())
        s.pop("last_run", None)  # heartbeat suppression: never persist last_run
        return s
    except Exception as e:
        log.error("state corrupt, starting fresh: %s", e)
        return {"seen": [], "version": 1}


def save_state(state: dict) -> None:
    state.pop("last_run", None)
    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True))


def fp(u: dict) -> str:
    raw = f"{u.get('platform','')}|{u.get('handle','')}|{u.get('change_type','')}|" \
          f"{u.get('scope_type','')}|{u.get('target','')}|{u.get('timestamp','')}"
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


def bb_fetch(since: str = "2d", per_page: int = 200) -> list[dict]:
    """Pull recent Bugcrowd scope updates from bbscope.com aggregator.

    Terminates early when a full page returns zero new fingerprints — bbscope's
    pagination tends to echo entries past the real tail, so length-based exit
    alone never fires.
    """
    out: list[dict] = []
    page_seen: set[str] = set()
    page = 1
    sess = requests.Session()
    sess.headers["User-Agent"] = BC_UA
    while True:
        try:
            r = sess.get(
                f"{BBSCOPE_API}/updates",
                params={"since": since, "platform": "bc",
                        "per_page": str(per_page), "page": str(page)},
                timeout=30,
            )
        except requests.RequestException as e:
            log.warning("fetch page %d failed: %s", page, e)
            break
        if r.status_code != 200:
            log.warning("page %d HTTP %d, stopping", page, r.status_code)
            break
        try:
            data = r.json()
        except ValueError:
            log.warning("page %d non-JSON", page)
            break
        ups = data.get("updates", [])
        new_this_page = 0
        for u in ups:
            f = fp(u)
            if f not in page_seen:
                page_seen.add(f)
                out.append(u)
                new_this_page += 1
        log.debug("page %d: %d entries (%d new)", page, len(ups), new_this_page)
        if new_this_page == 0:
            log.info("page %d returned 0 new fingerprints, stopping", page)
            break
        if len(ups) < per_page:
            break
        page += 1
        if page > 50:  # hard cap (50 * 200 = 10k max even with weird pagination)
            log.warning("hit page cap at page %d", page)
            break
    return out


def esc(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def fmt_update(u: dict) -> str:
    icon = {"added": "+", "removed": "-", "modified": "~"}.get(u.get("change_type"), "*")
    target = esc(u.get("target", ""))
    cat = esc(u.get("category", "") or "?")
    scope = u.get("scope_type", "in")
    return f"<code>{icon}</code> <code>{target}</code> [{cat}/{scope}]"


def fmt_group(handle: str, updates: list[dict]) -> str:
    title = esc(handle.lstrip("/").replace("engagements/", ""))
    n = len(updates)
    head = f"<b>Bugcrowd</b> · <b>{title}</b> · {n} change{'s' if n != 1 else ''}"
    lines = [fmt_update(u) for u in updates[:25]]
    if n > 25:
        lines.append(f"… +{n - 25} more (see diff_log.jsonl)")
    url = f"https://bugcrowd.com{esc(handle)}"
    return head + "\n" + "\n".join(lines) + f"\n<a href=\"{url}\">view program</a>"


def tg_send(text: str, token: str, chat_id: str) -> bool:
    if DRY_RUN:
        log.info("DRY_RUN: would send %d chars: %s", len(text), text[:120])
        return True
    try:
        r = requests.post(
            TG_API.format(token=token),
            data={"chat_id": chat_id, "text": text[:4090],
                  "parse_mode": "HTML", "disable_web_page_preview": "true"},
            timeout=20,
        )
        if r.status_code == 200:
            return True
        log.warning("tg send HTTP %d: %s", r.status_code, r.text[:200])
    except requests.RequestException as e:
        log.warning("tg send error: %s", e)
    return False


def append_audit(rows: list[dict]) -> None:
    with AUDIT_FILE.open("a") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def main() -> int:
    tg_token   = os.environ.get("TG_TOKEN", "")
    tg_chat_id = os.environ.get("TG_CHAT_ID", "")
    if not tg_token or not tg_chat_id:
        log.error("TG_TOKEN and TG_CHAT_ID required")
        return 2

    state = load_state()
    seen = set(state.get("seen", []))

    log.info("fetching updates (since=%s, seed_only=%s)", LOOKBACK, SEED_ONLY)
    ups = bb_fetch(since=LOOKBACK)
    log.info("fetched %d updates from bbscope.com", len(ups))

    fresh: list[dict] = []
    for u in ups:
        f = fp(u)
        if f not in seen:
            fresh.append(u)
            seen.add(f)

    log.info("fresh updates: %d (after dedupe)", len(fresh))

    # Deterministic order: sort fingerprints so repeated saves produce identical
    # JSON, suppressing heartbeat commits when nothing actually changed.
    state["seen"] = sorted(seen)[-SEEN_CAP:]

    if SEED_ONLY:
        log.info("SEED_ONLY set — state baselined, no notifications")
        save_state(state)
        return 0

    if not fresh:
        save_state(state)
        return 0

    # Group fresh updates per program handle
    by_handle: dict[str, list[dict]] = {}
    for u in fresh:
        by_handle.setdefault(u.get("handle", "?"), []).append(u)

    ts = datetime.now(timezone.utc).isoformat()
    audit_rows: list[dict] = []
    sent = 0
    for handle, group in by_handle.items():
        if sent >= MAX_NOTIFY:
            log.warning("hit MAX_NOTIFY=%d, %d handles remaining unsent", MAX_NOTIFY,
                        len(by_handle) - sent)
            break
        msg = fmt_group(handle, group)
        ok = tg_send(msg, tg_token, tg_chat_id)
        if ok:
            sent += 1
        for u in group:
            audit_rows.append({
                "ts": ts,
                "platform": "bc",
                "handle": handle,
                "change": u.get("change_type"),
                "scope": u.get("scope_type"),
                "target": u.get("target"),
                "category": u.get("category"),
                "asset_ts": u.get("timestamp"),
                "sent": ok,
            })
        time.sleep(0.35)  # gentle Telegram pacing

    if audit_rows:
        append_audit(audit_rows)
    log.info("sent %d/%d program-grouped messages (%d updates audited)",
             sent, len(by_handle), len(audit_rows))

    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
