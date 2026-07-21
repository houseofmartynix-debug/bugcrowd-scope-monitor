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
  NOTIFY_TWEAKS— if set, also notify scope changes on ALREADY-KNOWN programs
                 (the old firehose). Default OFF: only NEW PROGRAM LAUNCHES notify.
  HANDLES_CAP  — max program handles to retain in state (default 20000)

New-program-launch detection: state tracks `seen_handles` (every program handle
ever observed). A fresh update whose handle is NOT in that set = a newly-launched
engagement → highlighted "NEW PROGRAM" message. Changes on known handles are muted
by default (fresh/pre-dupe programs are the high-EV signal). On the first run after
this upgrade, `seen_handles` is baselined from diff_log.jsonl so existing programs
do not misfire as new.
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

def _envflag(name: str, default: bool = False) -> bool:
    """Tri-state env flag: unset/empty → default; 0/false/no/off → False; else True.
    (The workflow passes '' on plain cron runs, which must fall back to the default.)"""
    v = os.getenv(name)
    if v is None or v.strip() == "":
        return default
    return v.strip().lower() not in ("0", "false", "no", "off")


STATE_FILE = Path(os.getenv("STATE_FILE", "state.json"))
AUDIT_FILE = Path(os.getenv("AUDIT_FILE", "diff_log.jsonl"))
LOOKBACK   = os.getenv("LOOKBACK", "2d")
MAX_NOTIFY = int(os.getenv("MAX_NOTIFY", "50"))
SEEN_CAP   = int(os.getenv("SEEN_CAP", "20000"))
HANDLES_CAP = int(os.getenv("HANDLES_CAP", "20000"))
DRY_RUN    = _envflag("DRY_RUN")
SEED_ONLY  = _envflag("SEED_ONLY")
# Default ON: notify scope changes on already-known programs too (not just launches).
# Mute with NOTIFY_TWEAKS=0 (env) or the notify_tweaks=false workflow_dispatch input.
NOTIFY_TWEAKS = _envflag("NOTIFY_TWEAKS", default=True)

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


# Human labels + icons per bbscope scope category, so a message reads at a glance.
CAT_LABEL = {
    "URL": "🌐 URL", "WILDCARD": "🌟 Wildcard", "API": "🔌 API",
    "ANDROID": "🤖 Android", "IOS": "🍎 iOS", "AI": "🧠 AI/LLM",
    "CIDR": "📡 CIDR/IP", "HARDWARE": "🔧 Hardware", "OTHER": "📦 Lainnya",
    "PROGRAM": "📄 Program",
}
CAT_ORDER = ["WILDCARD", "URL", "API", "AI", "ANDROID", "IOS", "CIDR", "HARDWARE", "OTHER", "PROGRAM"]
TARGETS_PER_CAT = 60  # generous cap so a message stays "lengkap" without runaway length


def cat_label(cat: str) -> str:
    c = (cat or "OTHER").upper()
    return CAT_LABEL.get(c, f"📦 {esc(cat or 'Lainnya')}")


def prog_title(handle: str) -> str:
    """`/engagements/canva` or `https://bugcrowd.com/engagements/x` → `canva` / `x`."""
    src = handle or ""
    if "engagements/" in src:
        src = src.split("engagements/", 1)[1]
    src = src.strip("/").split("/")[0]
    return esc(src or (handle or "?"))


def prog_url(handle: str, program_url: str = "") -> str:
    """Clean https link. Note: program_removed rows double the scheme in program_url
    (`https://bugcrowd.com/https://bugcrowd.com/...`) but their `handle` is already a
    clean absolute URL, so prefer handle when it is absolute."""
    h = handle or ""
    if h.startswith("http"):
        return esc(h)
    if program_url and program_url.startswith("http") and program_url.count("http") == 1:
        return esc(program_url)
    if not h.startswith("/"):
        h = "/" + h
    return "https://bugcrowd.com" + esc(h)


def _fmt_ts(updates: list[dict]) -> str:
    tss = [u.get("timestamp") for u in updates if u.get("timestamp")]
    if not tss:
        return ""
    t = max(tss)
    try:
        dt = datetime.fromisoformat(t.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d %H:%M UTC")
    except Exception:
        return esc(t)


def _target_lines(updates: list[dict]) -> list[str]:
    """Group targets by category (largest first) and render one bullet per target."""
    by: dict[str, list[dict]] = {}
    for u in updates:
        by.setdefault((u.get("category") or "OTHER").upper(), []).append(u)
    order = sorted(by, key=lambda c: (CAT_ORDER.index(c) if c in CAT_ORDER else 99, c))
    lines: list[str] = []
    for cat in order:
        items = by[cat]
        lines.append(f"{cat_label(cat)} ({len(items)})")
        for u in items[:TARGETS_PER_CAT]:
            lines.append(f"  • <code>{esc(u.get('target', ''))}</code>")
        if len(items) > TARGETS_PER_CAT:
            lines.append(f"  … +{len(items) - TARGETS_PER_CAT} lagi")
    return lines


def fmt_new_program(handle: str, updates: list[dict]) -> str:
    """Detailed alert for a newly-launched engagement (handle never seen before)."""
    title = prog_title(handle)
    url = prog_url(handle, (updates[0].get("program_url") if updates else "") or "")
    ins = [u for u in updates
           if u.get("scope_type", "in") != "out" and u.get("change_type") != "removed"
           and (u.get("category") or "").upper() != "PROGRAM"]
    outs = [u for u in updates if u.get("scope_type") == "out"]

    parts = ["🚀 <b>PROGRAM BUGCROWD BARU</b>", f"<b>{title}</b>", "",
             "Program baru muncul di Bugcrowd — surface fresh, kemungkinan besar belum ada yang nge-dupe."]
    if ins:
        parts += ["", f"📍 <b>In-scope</b> ({len(ins)} target):"]
        parts += _target_lines(ins)
    if outs:
        parts += ["", f"⛔ <b>Out-of-scope</b> ({len(outs)}):"]
        parts += _target_lines(outs)
    parts += ["", f"🔗 <a href=\"{url}\">Buka program</a>"]
    ts = _fmt_ts(updates)
    if ts:
        parts.append(f"🕐 {ts}")
    return "\n".join(parts)


def fmt_scope_update(handle: str, updates: list[dict]) -> str:
    """Detailed alert for a scope change on an already-known program."""
    title = prog_title(handle)
    url = prog_url(handle, (updates[0].get("program_url") if updates else "") or "")

    def is_prog(u): return (u.get("category") or "").upper() == "PROGRAM"
    added    = [u for u in updates if u.get("change_type") == "added" and not is_prog(u)]
    removed  = [u for u in updates if u.get("change_type") == "removed" and not is_prog(u)]
    modified = [u for u in updates if u.get("change_type") == "modified" and not is_prog(u)]
    delisted = any(is_prog(u) and u.get("change_type") == "removed" for u in updates)

    # Pure program delist (no asset-level movement) — short, distinct message.
    if delisted and not (added or removed or modified):
        return "\n".join([
            "🗑️ <b>PROGRAM DI-DELIST</b>", f"<b>{title}</b>", "",
            "Program ini dihapus dari Bugcrowd (tidak lagi in-scope).",
            "", f"🔗 <a href=\"{url}\">Detail</a>",
        ])

    parts = [f"🔔 <b>UPDATE SCOPE</b> · <b>{title}</b>"]
    if added:
        ins  = [u for u in added if u.get("scope_type", "in") != "out"]
        outs = [u for u in added if u.get("scope_type") == "out"]
        parts += ["", f"➕ <b>Ditambahkan</b> ({len(added)} target):"]
        if ins:
            parts += _target_lines(ins)
        if outs:
            parts.append("  <i>— sebagian out-of-scope:</i>")
            parts += _target_lines(outs)
    if removed:
        parts += ["", f"➖ <b>Dihapus</b> ({len(removed)} target):"]
        parts += _target_lines(removed)
    if modified:
        parts += ["", f"✏️ <b>Dimodifikasi</b> ({len(modified)} target):"]
        parts += _target_lines(modified)
    if delisted:
        parts += ["", "🗑️ <i>Program juga ditandai delist.</i>"]
    parts += ["", f"🔗 <a href=\"{url}\">Buka program</a>"]
    ts = _fmt_ts(updates)
    if ts:
        parts.append(f"🕐 {ts}")
    return "\n".join(parts)


def baseline_handles_from_audit() -> set[str]:
    """Reconstruct every program handle ever observed from the audit log, so the
    first run after the new-program upgrade does not misfire existing programs."""
    handles: set[str] = set()
    if not AUDIT_FILE.exists():
        return handles
    try:
        with AUDIT_FILE.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    h = json.loads(line).get("handle")
                except ValueError:
                    continue
                if h:
                    handles.add(h)
    except Exception as e:
        log.warning("could not baseline handles from audit: %s", e)
    return handles


TG_LIMIT = 3900  # keep under Telegram's 4096 hard cap with headroom for entities


def tg_send(text: str, token: str, chat_id: str) -> bool:
    if DRY_RUN:
        log.info("DRY_RUN: would send %d chars:\n%s", len(text), text)
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


def tg_send_long(text: str, token: str, chat_id: str) -> bool:
    """Send a detailed message in full, splitting on line boundaries when it exceeds
    Telegram's per-message limit (so nothing is silently truncated)."""
    chunks: list[str] = []
    cur = ""
    for line in text.split("\n"):
        while len(line) > TG_LIMIT:  # pathological single long line — hard-cut
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.append(line[:TG_LIMIT])
            line = line[TG_LIMIT:]
        if cur and len(cur) + 1 + len(line) > TG_LIMIT:
            chunks.append(cur)
            cur = line
        else:
            cur = line if not cur else cur + "\n" + line
    if cur:
        chunks.append(cur)

    ok_all = True
    for i, ch in enumerate(chunks):
        if len(chunks) > 1:
            ch = f"<i>({i + 1}/{len(chunks)})</i>\n{ch}"
        ok_all = tg_send(ch, token, chat_id) and ok_all
        if i < len(chunks) - 1:
            time.sleep(0.4)
    return ok_all


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

    # seen_handles: every program handle ever observed. Absent = first run after the
    # new-program upgrade → baseline from audit log so existing programs don't misfire.
    if "seen_handles" in state:
        seen_handles = set(state.get("seen_handles", []))
        baselined = False
    else:
        seen_handles = baseline_handles_from_audit()
        baselined = True
        log.info("baselined %d handles from audit log (first new-program run)",
                 len(seen_handles))

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

    # Split fresh updates: NEW PROGRAM (handle never seen) vs tweak (known handle).
    new_by_handle: dict[str, list[dict]] = {}
    tweak_by_handle: dict[str, list[dict]] = {}
    for u in fresh:
        h = u.get("handle", "?")
        if h in seen_handles:
            tweak_by_handle.setdefault(h, []).append(u)
        else:
            new_by_handle.setdefault(h, []).append(u)
    # Every handle in this run is now known for future runs.
    seen_handles.update(new_by_handle.keys())
    seen_handles.update(tweak_by_handle.keys())

    log.info("fresh: %d new-program handle(s), %d tweaked handle(s) (notify_tweaks=%s)",
             len(new_by_handle), len(tweak_by_handle), NOTIFY_TWEAKS)

    # Deterministic order so repeated saves produce identical JSON (heartbeat suppression).
    state["seen"] = sorted(seen)[-SEEN_CAP:]
    state["seen_handles"] = sorted(seen_handles)[-HANDLES_CAP:]

    if SEED_ONLY or baselined:
        why = "SEED_ONLY" if SEED_ONLY else "first new-program baseline"
        log.info("%s — state saved, no notifications this run", why)
        save_state(state)
        return 0

    if not fresh:
        save_state(state)
        return 0

    # Notify: NEW PROGRAMS always; tweaks only when NOTIFY_TWEAKS is set.
    notify_plan: list[tuple[str, str, list[dict]]] = [
        ("new", h, g) for h, g in new_by_handle.items()
    ]
    if NOTIFY_TWEAKS:
        notify_plan += [("tweak", h, g) for h, g in tweak_by_handle.items()]
    # New programs first (highest EV).
    notify_plan.sort(key=lambda x: 0 if x[0] == "new" else 1)

    ts = datetime.now(timezone.utc).isoformat()
    audit_rows: list[dict] = []
    sent = 0
    for kind, handle, group in notify_plan:
        if sent >= MAX_NOTIFY:
            log.warning("hit MAX_NOTIFY=%d, remaining handles unsent", MAX_NOTIFY)
            break
        # An unseen handle whose only change is a removal/delist is NOT a launch —
        # only treat it as "new program" when it actually adds a real (non-PROGRAM) asset.
        real_new = kind == "new" and any(
            u.get("change_type") == "added" and (u.get("category") or "").upper() != "PROGRAM"
            for u in group
        )
        msg = fmt_new_program(handle, group) if real_new else fmt_scope_update(handle, group)
        ok = tg_send_long(msg, tg_token, tg_chat_id)
        if ok:
            sent += 1
        for u in group:
            audit_rows.append({
                "ts": ts,
                "platform": "bc",
                "handle": handle,
                "kind": kind,
                "change": u.get("change_type"),
                "scope": u.get("scope_type"),
                "target": u.get("target"),
                "category": u.get("category"),
                "asset_ts": u.get("timestamp"),
                "sent": ok,
            })
        time.sleep(0.35)  # gentle Telegram pacing

    # Audit ALL fresh updates (incl. muted tweaks) so nothing is silently lost.
    audited_handles = {r["handle"] for r in audit_rows}
    for h, g in tweak_by_handle.items():
        if h in audited_handles:
            continue
        for u in g:
            audit_rows.append({
                "ts": ts, "platform": "bc", "handle": h, "kind": "tweak-muted",
                "change": u.get("change_type"), "scope": u.get("scope_type"),
                "target": u.get("target"), "category": u.get("category"),
                "asset_ts": u.get("timestamp"), "sent": False,
            })

    if audit_rows:
        append_audit(audit_rows)
    log.info("sent %d message(s); %d updates audited (%d new-program handles)",
             sent, len(audit_rows), len(new_by_handle))

    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
