import os
import json
import requests
from datetime import datetime, timedelta

# ── Config ────────────────────────────────────────────────────────────────────
LOOKBACK_DAYS   = 7   # Change to 30 for a monthly digest
MAX_CANVASES    = 20  # Cap so Slack message stays readable
DEBUG           = os.environ.get("BRAZE_DIGEST_DEBUG", "false").lower() in ("1", "true", "yes")


# ── Braze helpers ─────────────────────────────────────────────────────────────
def debug_print(*args, **kwargs):
    if DEBUG:
        print(*args, **kwargs)


def braze_get(path, params=None):
    """Make an authenticated GET request to Braze."""
    headers = {"Authorization": f"Bearer {BRAZE_API_KEY}"}
    url = f"{BRAZE_ENDPOINT}{path}"
    debug_print("Braze GET", url, params)
    response = requests.get(url, headers=headers, params=params, timeout=15)
    response.raise_for_status()
    return response.json()


def get_active_canvases():
    """
    Return list of active Canvases (id + name).
    Relies on include_archived=False to exclude inactive canvases —
    the API does not return a reliable 'enabled' field per canvas object.
    """
    data = braze_get("/canvas/list", params={"include_archived": False})

    # FIX: consolidated, safe canvas extraction — avoids calling .get() on None
    canvases = (
        data.get("canvases")
        or (data.get("data") or {}).get("canvases")
        or []
    )

    debug_print("Active canvas payload keys:", list(data.keys()))
    debug_print("Returned canvas count:", len(canvases))
    debug_print("Returned canvases:", json.dumps(
        [{"id": c.get("id"), "name": c.get("name")} for c in canvases],
        indent=2
    ))

    if not canvases:
        print("Warning: /canvas/list returned no canvases.", flush=True)
        if data:
            print("Payload keys:", list(data.keys()), flush=True)
            print("Payload snippet:", json.dumps(
                {k: data[k] for k in list(data.keys())[:5]}, indent=2
            ), flush=True)

    # FIX: removed the c.get("enabled", True) filter — Braze does not return
    # an 'enabled' field on canvas list objects. Archival exclusion is handled
    # by the include_archived=False param above.
    return canvases[:MAX_CANVASES]


def get_canvas_summary(canvas_id):
    """
    Return conversion event details + counts for a Canvas over the lookback window.
    Braze docs: https://www.braze.com/docs/api/endpoints/export/canvas/get_canvas_analytics_summary/
    """
    end_date   = datetime.utcnow().date()
    start_date = end_date - timedelta(days=LOOKBACK_DAYS)

    response = braze_get("/canvas/data_summary", params={
        "canvas_id":                 canvas_id,
        "ending_at":                 end_date.isoformat(),
        "starting_at":               start_date.isoformat(),
        "include_variant_breakdown": False,
        "include_step_breakdown":    False,
    })

    data = response.get("data")
    if data is None:
        debug_print("data_summary response missing top-level 'data' key, falling back to full response")
        data = response
    return data


# ── Data assembly ─────────────────────────────────────────────────────────────
def build_canvas_rows():
    """
    For each active Canvas, return a dict with:
      - canvas_name
      - events: list of {event_name, count} dicts
    Canvases with no conversion events configured in Braze are excluded.
    """
    canvases = get_active_canvases()
    print(f"Found {len(canvases)} active canvas(es) from Braze", flush=True)
    rows = []

    for canvas in canvases:
        canvas_id   = canvas["id"]
        canvas_name = canvas["name"]

        print(f"Processing canvas: {canvas_name} ({canvas_id})", flush=True)
        try:
            summary = get_canvas_summary(canvas_id)
        except requests.HTTPError as e:
            print(f"  ⚠ Skipping {canvas_name}: {e}", flush=True)
            continue

        debug_print("Canvas summary for", canvas_name, ":", json.dumps(summary, indent=2))

        # Braze returns conversion_behaviors as a list of objects.
        # Each has a 'name' (the event label) and 'total' (conversions in window).
        conversion_behaviors = summary.get("conversion_behaviors", [])

        if not conversion_behaviors:
            print(
                f"  ℹ {canvas_name}: no conversion_behaviors in summary "
                f"(keys: {list(summary.keys())})",
                flush=True
            )

        events = []
        for cb in conversion_behaviors:
            event_name = cb.get("name") or cb.get("description") or "Unnamed event"
            count      = cb.get("total", 0)
            events.append({"event_name": event_name, "count": count})

        # Only include canvases that have at least one conversion event configured
        if events:
            rows.append({
                "canvas_name": canvas_name,
                "events":      events,
            })
        else:
            print(
                f"  ℹ {canvas_name}: skipped — no conversion events configured in Braze.",
                flush=True
            )

    return rows


# ── Slack formatting ──────────────────────────────────────────────────────────
def format_slack_message(rows):
    """
    Build a Slack Block Kit payload.
    One section per Canvas; conversion events listed with their count.
    """
    end_date   = datetime.utcnow().date()
    start_date = end_date - timedelta(days=LOOKBACK_DAYS)
    date_range = f"{start_date.strftime('%-d %b')} – {end_date.strftime('%-d %b %Y')}"

    blocks = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f"📊 Braze Canvas Conversions  |  {date_range}",
                "emoji": True
            }
        },
        {"type": "divider"}
    ]

    if not rows:
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": "_No active Canvases with conversion events found._"
            }
        })
    else:
        for row in rows:
            event_lines = "\n".join(
                f"  ↳ *{e['event_name']}* — {e['count']:,}"
                for e in row["events"]
            )
            blocks.append({
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*{row['canvas_name']}*\n{event_lines}"
                }
            })
            blocks.append({"type": "divider"})

    blocks.append({
        "type": "context",
        "elements": [{
            "type": "mrkdwn",
            "text": f"Data from Braze · {LOOKBACK_DAYS}-day window · Posted automatically"
        }]
    })

    return {"blocks": blocks}


# ── Slack posting ─────────────────────────────────────────────────────────────
def post_to_slack(payload):
    response = requests.post(
        SLACK_WEBHOOK,
        data=json.dumps(payload),
        headers={"Content-Type": "application/json"},
        timeout=10
    )
    response.raise_for_status()
    print("✅ Posted to Slack successfully.")


# ── Main ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    try:
        global BRAZE_API_KEY, BRAZE_ENDPOINT, SLACK_WEBHOOK
        BRAZE_API_KEY  = os.environ["BRAZE_API_KEY"]
        BRAZE_ENDPOINT = os.environ["BRAZE_ENDPOINT"].rstrip("/")  # e.g. https://rest.iad-01.braze.com
        SLACK_WEBHOOK  = os.environ["SLACK_WEBHOOK_URL"]

        print("Starting Braze digest script...", flush=True)
        print(f"Fetching active Canvases from Braze...", flush=True)
        rows = build_canvas_rows()
        print(f"Found {len(rows)} Canvas(es) with conversion events.", flush=True)

        payload = format_slack_message(rows)
        post_to_slack(payload)
    except KeyError as e:
        print(f"❌ Missing environment variable: {e}", flush=True)
        raise
    except Exception as e:
        print(f"❌ Error: {e}", flush=True)
        raise
