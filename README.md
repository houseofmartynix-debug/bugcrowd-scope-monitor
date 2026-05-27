# Bugcrowd Scope Monitor

Telegram bot **@bugcrowdbuneng_bot** — Bugcrowd platform-wide scope-change alerts, polled every 10 min via GitHub Actions cron (24/7, no local process).

Sibling of [ywh-scope-monitor](https://github.com/houseofmartynix-debug/ywh-scope-monitor).

## What it does

Every 10 min, GitHub Actions runs `bugcrowd_monitor.py`:

1. Pull recent scope updates from `bbscope.com/api/v1/updates?platform=bc` (Bugcrowd, 2-day lookback)
2. Dedupe against `state.json` (rolling fingerprint set, last 8000)
3. Group fresh updates by program handle
4. Send one Telegram HTML message per program with adds/removes/modifies
5. Append every change to `diff_log.jsonl` (audit trail)
6. Commit state + audit log back to repo (heartbeat-only commits suppressed)

Notifies on **every** Bugcrowd program change, not just watchlisted — match `mrcslvknm`'s recon habit of scanning the platform for fresh attack surface.

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
