import os
import json
import requests
from datetime import datetime, timedelta

# ── Config ────────────────────────────────────────────────────────────────────
LOOKBACK_DAYS   = 7   # Change to 30 for a monthly digest
MAX_CANVASES    = 20  # Cap so Slack message stays readable


# ── Braze helpers ─────────────────────────────────────────────────────────────
def braze_get(path, params=None):
    """Make an authenticated GET request to Braze."""
    headers = {"Authorization": f"Bearer {BRAZE_API_KEY}"}
    url = f"{BRAZE_ENDPOINT}{path}"
    response = requests.get(url, headers=headers, params=params, timeout=15)
    response.raise_for_status()
    return response.json()


def get_active_canvases():
    """Return list of enabled Canvases (id + name)."""
    data = braze_get("/canvas/list", params={"include_archived": False})
    canvases = data.get("canvases", [])
    # Filter to enabled only, cap at MAX_CANVASES
    active = [c for c in canvases if c.get("enabled", True)]
    return active[:MAX_CANVASES]


def get_canvas_summary(canvas_id):
    """
    Return conversion event details + counts for a Canvas over the lookback window.
    Braze docs: https://www.braze.com/docs/api/endpoints/export/canvas/get_canvas_analytics_summary/
    """
    end_date   = datetime.utcnow().date()
    start_date = end_date - timedelta(days=LOOKBACK_DAYS)

    data = braze_get("/canvas/data_summary", params={
        "canvas_id":         canvas_id,
        "ending_at":         end_date.isoformat(),
        "starting_at":       start_date.isoformat(),
        "include_variant_breakdown": False,
        "include_step_breakdown":    False,
    })
    return data.get("data", {})


# ── Data assembly ─────────────────────────────────────────────────────────────
def build_canvas_rows():
    """
    For each active Canvas, return a dict with:
      - name
      - list of {event_name, conversion_count} dicts
    """
    canvases = get_active_canvases()
    rows = []

    for canvas in canvases:
        canvas_id   = canvas["id"]
        canvas_name = canvas["name"]

        try:
            summary = get_canvas_summary(canvas_id)
        except requests.HTTPError as e:
            print(f"  ⚠ Skipping {canvas_name}: {e}")
            continue

        # Braze returns conversion_behaviors as a list of objects
        # Each has a 'name' (the event label) and 'total' (conversions in window)
        conversion_behaviors = summary.get("conversion_behaviors", [])

        events = []
        for cb in conversion_behaviors:
            event_name = cb.get("name") or cb.get("description") or "Unnamed event"
            count      = cb.get("total", 0)
            events.append({"event_name": event_name, "count": count})

        # Only include Canvases that have at least one conversion event defined
        if events:
            rows.append({
                "canvas_name": canvas_name,
                "events":      events,
            })

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
            "text": {"type": "mrkdwn", "text": "_No active Canvases with conversion events found._"}
        })
    else:
        for row in rows:
            # Build event lines, e.g.  "↳ Purchase completed — 142"
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

    # Footer
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
        # Load config from environment into the globals used by helper functions
        global BRAZE_API_KEY, BRAZE_ENDPOINT, SLACK_WEBHOOK
        BRAZE_API_KEY   = os.environ["BRAZE_API_KEY"]
        BRAZE_ENDPOINT  = os.environ["BRAZE_ENDPOINT"].rstrip("/")   # e.g. https://rest.iad-01.braze.com
        SLACK_WEBHOOK   = os.environ["SLACK_WEBHOOK_URL"]

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