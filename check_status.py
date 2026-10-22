"""Checks one AION 2 server on aion2.gaming.tools and keeps a single Discord
embed up to date, in one or more Discord channels (via webhooks)."""

import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# ---- SETTINGS --------------------------------------------------------------
# Server and region come from GitHub secrets (AION_SERVER, AION_REGION) so they
# don't appear in the public repo. Region is one of:
# "Europe", "NA West", "NA East", "SA", "Asia"
SERVER_NAME = os.environ["AION_SERVER"]
REGION = os.environ["AION_REGION"]
CHECK_INTERVAL_MINUTES = 5  # how often your timer (cron-job.org) triggers a check
RECOVERY_MIN_ONLINE = 1     # servers online (after the region hit 0) to say
                            # "servers are coming back online"
NOTIFY_ON_CHANGE = True    # True = also post a short new message on each change
                            # (edits to the embed don't notify anyone)
NOTIFICATION_EXPIRY_HOURS = 1   # delete a notification after this many hours
                                # (0 = keep until the next change replaces it)
# ---------------------------------------------------------------------------

URL = "https://aion2.gaming.tools/server-status"
STATE_FILE = Path("state.json")
HEADERS = {"User-Agent": "aion2-status-discord-bot (personal use; polls every 5 min)"}

# One or more webhook URLs in the DISCORD_WEBHOOK_URL secret, separated by
# commas, spaces or new lines.
WEBHOOKS = [u for u in re.split(r"[,\s]+", os.environ["DISCORD_WEBHOOK_URL"]) if u]
for _u in WEBHOOKS:
    print(f"::add-mask::{_u}")   # make sure each URL is hidden in public logs

COLORS = {"online": 0x57F287, "maintenance": 0xFEE75C, "offline": 0xED4245}
ICONS = {"online": "🟢", "maintenance": "🟠", "offline": "🔴"}
NO_MENTIONS = {"parse": []}


def fetch_server():
    """Returns our server's status plus how many servers in the region are online."""
    resp = requests.get(URL, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    mine, seen = None, set()
    region_online = region_total = 0
    for row in soup.select("tr"):
        cells = [c.get_text(" ", strip=True) for c in row.find_all("td")]
        if len(cells) < 6:
            continue
        # Columns: Favorites | Status | Server | Faction | Region | Population | 24h
        status, server, faction, region = cells[1:5]
        words = server.split()
        if not words or region.lower() != REGION.lower():
            continue
        name = words[0].lower()
        if name in seen:           # guard against the same row appearing twice
            continue
        seen.add(name)
        region_total += 1
        if status.lower() == "online":
            region_online += 1
        if name == SERVER_NAME.lower():
            mine = {"status": status, "faction": faction}

    if mine is None:
        sys.exit("Could not find the configured server/region on the page.")
    mine["region_online"] = region_online
    mine["region_total"] = region_total
    return mine


def build_embed(current, down_since, now, recovering):
    key = current["status"].lower()
    icon = ICONS.get(key, "⚪")
    lines = [f"## {icon} {current['status']}"]
    if recovering:
        lines.append(f"🟡 **Servers are coming back online** · "
                     f"{SERVER_NAME} may not be showing as online yet")
    lines.append(f"[Server Status Information provided by aion2.gaming.tools]({URL})")
    fields = [
        {"name": "Region", "value": REGION, "inline": True},
        {"name": "Faction", "value": current["faction"], "inline": True},
        {"name": "Online in region",
         "value": f"{current['region_online']} / {current['region_total']}", "inline": True},
        {"name": "Last checked", "value": f"<t:{now}:R>", "inline": True},
        {"name": "Next check", "value": f"<t:{now + CHECK_INTERVAL_MINUTES * 60}:R>", "inline": True},
    ]
    if down_since is not None:   # only shown while the server isn't online
        fields.append({"name": "Offline since",
                       "value": f"<t:{down_since}:R>", "inline": True})
    return {
        "title": f"AION 2 · {SERVER_NAME}",
        "url": URL,
        "color": COLORS.get(key, 0x95A5A6),
        "description": "\n".join(lines),
        "fields": fields,
        "footer": {"text": f"Checked about every {CHECK_INTERVAL_MINUTES} minutes"},
    }


def notification_text(current, recovery_started):
    if recovery_started:
        return (f"🟡 Servers are coming back online in {REGION} "
                f"({current['region_online']} of {current['region_total']} up)")
    icon = ICONS.get(current["status"].lower(), "⚪")
    return f"{icon} **{SERVER_NAME}** is now **{current['status']}**"


def hook_key(webhook):
    """Short non-reversible ID so state.json never contains webhook details."""
    return hashlib.sha256(webhook.encode()).hexdigest()[:8]


def upsert_message(webhook, embed, message_id):
    """Edit the existing message, or post a new one. Returns the message ID."""
    payload = {"embeds": [embed], "allowed_mentions": NO_MENTIONS}
    if message_id:
        r = requests.patch(f"{webhook}/messages/{message_id}", json=payload, timeout=30)
        if r.status_code != 404:       # 404 = message was deleted, so post a new one
            r.raise_for_status()
            return message_id
    r = requests.post(webhook, params={"wait": "true"}, json=payload, timeout=30)
    r.raise_for_status()
    return r.json()["id"]


def delete_message(webhook, message_id):
    if not message_id:
        return
    r = requests.delete(f"{webhook}/messages/{message_id}", timeout=30)
    if r.status_code not in (204, 404):   # 404 = already deleted
        r.raise_for_status()


def send_notification(webhook, text):
    r = requests.post(
        webhook,
        params={"wait": "true"},
        json={"content": text, "allowed_mentions": NO_MENTIONS},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()["id"]


def main():
    current = fetch_server()
    print(f"Status: {current['status']} | "
          f"region online: {current['region_online']}/{current['region_total']}")
    prev = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
    now = int(time.time())

    # "Coming back online": the region hit zero online, and now some servers are
    # up while ours still isn't. The flag is cleared once our server is online.
    online_n = current["region_online"]
    mine_online = current["status"].lower() == "online"

    # When did the server stop being online? Kept across status changes
    # (e.g. Offline -> Maintenance) so the timer covers the whole downtime.
    if mine_online:
        down_since = None
    else:
        fallback = None
        if (prev.get("status") or "online").lower() != "online":
            fallback = prev.get("since")       # carry over from the old format
        down_since = prev.get("down_since") or fallback or now
    region_down = prev.get("region_down", False) or online_n == 0
    recovering = region_down and online_n >= RECOVERY_MIN_ONLINE and not mine_online
    if mine_online:
        region_down = False

    embed = build_embed(current, down_since, now, recovering)

    # Per-webhook state, keyed by a hash. Migrate the old single-webhook format.
    hooks_prev = prev.get("hooks", {})
    if "message_id" in prev and not hooks_prev:
        legacy = {k: prev[k] for k in ("message_id", "notify_id", "notify_at") if k in prev}
        legacy["status"] = prev.get("status")
        hooks_prev = {hook_key(WEBHOOKS[0]): legacy}

    hooks_new, failures = {}, []
    for i, webhook in enumerate(WEBHOOKS, start=1):
        key = hook_key(webhook)
        old = dict(hooks_prev.get(key, {}))
        hs = dict(old)
        try:
            # Remove an expired notification
            if (hs.get("notify_id") and NOTIFICATION_EXPIRY_HOURS
                    and now - hs.get("notify_at", now) > NOTIFICATION_EXPIRY_HOURS * 3600):
                delete_message(webhook, hs.pop("notify_id"))
                hs.pop("notify_at", None)

            # Update the embed on every run so the timers stay accurate
            had_status = hs.get("status")
            had_recovering = hs.get("recovering", False)
            hs["message_id"] = upsert_message(webhook, embed, hs.get("message_id"))
            hs["status"] = current["status"]
            hs["recovering"] = recovering

            # Optional notification, only on a real change
            recovery_started = recovering and not had_recovering and bool(had_status)
            changed = bool(had_status) and had_status != current["status"]
            if NOTIFY_ON_CHANGE and (recovery_started or changed):
                delete_message(webhook, hs.pop("notify_id", None))
                hs.pop("notify_at", None)
                hs["notify_id"] = send_notification(
                    webhook, notification_text(current, recovery_started))
                hs["notify_at"] = now
        except requests.RequestException as e:
            # Never print the exception text: it can contain the webhook URL.
            code = getattr(e.response, "status_code", "no response")
            print(f"Webhook #{i} failed ({type(e).__name__}, {code}); will retry next run.")
            failures.append(i)
            hs = old   # keep previous state so this webhook is retried next run
        hooks_new[key] = hs

    state = {"status": current["status"], "down_since": down_since,
             "region_down": region_down, "hooks": hooks_new}
    if state != prev:
        STATE_FILE.write_text(json.dumps(state, indent=2))

    if failures:
        sys.exit(f"{len(failures)} webhook(s) failed.")


if __name__ == "__main__":
    main()
