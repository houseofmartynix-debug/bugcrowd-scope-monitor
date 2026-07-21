# Bugcrowd Scope Monitor

Telegram bot **@bugcrowdbuneng_bot** — Bugcrowd platform-wide scope-change alerts, polled every 10 min via GitHub Actions cron (24/7, no local process).

Sibling of [ywh-scope-monitor](https://github.com/houseofmartynix-debug/ywh-scope-monitor).

## What it does

Every 10 min, GitHub Actions runs `bugcrowd_monitor.py`:

1. Pull recent scope updates from `bbscope.com/api/v1/updates?platform=bc` (Bugcrowd, 2-day lookback)
2. Dedupe against `state.json` (rolling fingerprint set, last 8000)
3. Split fresh updates: **new program launches** (handle never seen) vs **scope changes** on known programs
4. Send a **detailed** message per affected program — full target breakdown grouped by category (URL, wildcard, API, Android, iOS, AI, CIDR, hardware), in-scope vs out-of-scope, counts, direct link, timestamp. Three shapes:
   - 🚀 **PROGRAM BUGCROWD BARU** — a newly-launched engagement + all its in-scope targets
   - 🔔 **UPDATE SCOPE** — assets added / removed / modified on an existing program
   - 🗑️ **PROGRAM DI-DELIST** — a program removed from Bugcrowd
5. Long messages auto-split into numbered parts `(1/2)` so nothing is truncated
6. Append every change to `diff_log.jsonl` (audit trail)
7. Commit state + audit log back to repo (heartbeat-only commits suppressed)

Both **new launches** (fresh / pre-dupe = highest-EV) **and scope changes on known programs** notify by
default. To go back to launches-only, set `NOTIFY_TWEAKS=0` (env) or run the workflow with the
`notify_tweaks=false` dispatch input. `state.json` tracks `seen_handles`; on the first run after the
new-program upgrade it is baselined from `diff_log.jsonl` (silent run) so existing programs don't misfire
as new — genuine launches start firing from the second run onward.

## Why GH Actions (not local systemd)

Local Kali laptop sleeps. GH Actions runs in the cloud — free on public repos, truly 24/7, survives power outages.

## Setup

```bash
gh repo create houseofmartynix-debug/bugcrowd-scope-monitor --public --source=. --push
gh secret set TG_TOKEN     --repo houseofmartynix-debug/bugcrowd-scope-monitor --body "$(cat token.txt)"
gh secret set TG_CHAT_ID   --repo houseofmartynix-debug/bugcrowd-scope-monitor --body "8215972072"
gh workflow run "Bugcrowd Scope Monitor" --repo houseofmartynix-debug/bugcrowd-scope-monitor -f seed_only=true
```

Then let the cron take over.

## Env / tweak knobs

| Var          | Default | Use                                          |
|--------------|---------|----------------------------------------------|
| `TG_TOKEN`   | —       | Telegram bot token (secret)                  |
| `TG_CHAT_ID` | —       | Telegram chat to notify (secret)             |
| `LOOKBACK`   | `2d`    | bbscope `since` query                        |
| `MAX_NOTIFY` | `50`    | Cap program-grouped messages per run         |
| `SEEN_CAP`   | `8000`  | Max fingerprints retained in state.json      |
| `DRY_RUN`    | unset   | Log msgs, don't send to Telegram             |
| `SEED_ONLY`  | unset   | Baseline state, no notifications             |
| `NOTIFY_TWEAKS`| on (default) | Notify scope changes on known programs too; set `0` for launches-only |
| `HANDLES_CAP`| `20000` | Max program handles retained in state.json   |

## Manual ops

- Re-trigger: `gh workflow run "Bugcrowd Scope Monitor" --repo houseofmartynix-debug/bugcrowd-scope-monitor`
- Re-seed (after spam-prone upstream change): `... -f seed_only=true`
- Pause: disable the workflow in GitHub UI (Settings → Actions → workflow → Disable)

## State files

- `state.json` — `{seen: [fp...], version: 1}`. `last_run` is intentionally dropped so heartbeat commits don't bloat history.
- `diff_log.jsonl` — every fresh update row, including whether Telegram delivery succeeded.

## Local test (DRY_RUN)

```bash
TG_TOKEN=$(cat token.txt) TG_CHAT_ID=8215972072 DRY_RUN=1 python bugcrowd_monitor.py
```
