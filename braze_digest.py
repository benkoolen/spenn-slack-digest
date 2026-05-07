import os
import json
import requests
from datetime import datetime, timedelta

# ── Config ────────────────────────────────────────────────────────────────────
LOOKBACK_DAYS   = 7          # Change to 30 for a monthly digest
CANVAS_TAG      = "SCHMACK"  # Only fetch canvases with this Braze tag; set to "" to disable
MAX_CANVASES    = 20         # Only used as a fallback if CANVAS_TAG is empty
DEBUG           = os.environ.get("BRAZE_DIGEST_DEBUG", "false").lower() in ("1", "true", "yes")

# Override: if populated, skip /canvas/list entirely and process only these IDs.
# Remove entries (or clear the list) once tag-based filtering is confirmed working.
CANVAS_ID_OVERRIDE = [
    "2b5a571e-2ffe-4210-9744-d5abb9079b03",  # 20260420_PushOptInDrive_AllMarkets_Activation_EM-IAM-CC_V1
]


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
    Return list of active Canvases (id + name), filtered by CANVAS_TAG if set.
    Relies on include_archived=False to exclude inactive canvases —
    the API does not return a reliable 'enabled' field per canvas object.
    """
    params = {
        "include_archived": False,
        "page":             0,
        "per_page":         500,  # Max allowed by Braze — ensures we don't hit the 100-canvas default cap
    }
    if CANVAS_TAG:
        params["tags"] = CANVAS_TAG  # Note: no [] — Braze /canvas/list uses plain 'tags' param

    data = braze_get("/canvas/list", params=params)

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
        tag_msg = f" with tag '{CANVAS_TAG}'" if CANVAS_TAG else ""
        print(f"Warning: /canvas/list returned no canvases{tag_msg}.", flush=True)
        if data:
            print("Payload keys:", list(data.keys()), flush=True)
            print("Payload snippet:", json.dumps(
                {k: data[k] for k in list(data.keys())[:5]}, indent=2
            ), flush=True)

    # If filtering by tag, return all matched canvases; otherwise cap at MAX_CANVASES
    return canvases if CANVAS_TAG else canvases[:MAX_CANVASES]


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
    Canvases with no activity in the lookback window are excluded.
    """
    if CANVAS_ID_OVERRIDE:
        canvases = [{"id": cid, "name": cid} for cid in CANVAS_ID_OVERRIDE]
        print(f"ID override active — bypassing /canvas/list, processing {len(canvases)} canvas(es) directly.", flush=True)
    else:
        canvases = get_active_canvases()

    print(f"Found {len(canvases)} canvas(es) to process.", flush=True)
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

        # Pull stats from total_stats, which is present on Canvas Flow responses.
        # Canvases returning ['notice', 'message'] are unsupported by this endpoint
        # and are skipped cleanly.
        if "notice" in summary or "message" in summary:
            print(
                f"  ⚠ {canvas_name}: API notice — {summary.get('notice', '')} {summary.get('message', '')}".strip(),
                flush=True
            )
            continue

        total_stats = summary.get("total_stats", {})

        # If override is active, use the real canvas name from the summary if available
        if CANVAS_ID_OVERRIDE and canvas_name == canvas_id:
            canvas_name = summary.get("name", canvas_id)

        events = []

        sends = total_stats.get("total_sends") or total_stats.get("sent", 0)
        if sends:
            events.append({"event_name": "Sent", "count": sends})

        unique_recipients = total_stats.get("unique_recipients", 0)
        if unique_recipients:
            events.append({"event_name": "Unique Recipients", "count": unique_recipients})

        opens = total_stats.get("total_opens") or total_stats.get("opens", 0)
        if opens:
            events.append({"event_name": "Opens", "count": opens})

        clicks = total_stats.get("total_clicks") or total_stats.get("clicks", 0)
        if clicks:
            events.append({"event_name": "Clicks", "count": clicks})

        conversions = total_stats.get("total_conversions") or total_stats.get("conversions", 0)
        if conversions:
            events.append({"event_name": "Conversions", "count": conversions})

        revenue = total_stats.get("revenue", 0)
        if revenue:
            events.append({"event_name": "Revenue", "count": revenue})

        if events:
            rows.append({
                "canvas_name": canvas_name,
                "events":      events,
            })
        else:
            print(
                f"  ℹ {canvas_name}: skipped — total_stats empty or all zeros "
                f"(keys: {list(total_stats.keys())})",
                flush=True
            )

    return rows


# ── Slack formatting ──────────────────────────────────────────────────────────
def format_slack_message(rows):
    """
    Build a Slack Block Kit payload.
    One section per Canvas; stats listed with their count.
    """
    end_date   = datetime.utcnow().date()
    start_date = end_date - timedelta(days=LOOKBACK_DAYS)
    date_range = f"{start_date.strftime('%-d %b')} – {end_date.strftime('%-d %b %Y')}"

    blocks = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f"📊 Braze Canvas Digest  |  {date_range}",
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
                "text": "_No active Canvases with data in this period._"
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
        rows = build_canvas_rows()
        print(f"Found {len(rows)} Canvas(es) with data to report.", flush=True)

        payload = format_slack_message(rows)
        post_to_slack(payload)
    except KeyError as e:
        print(f"❌ Missing environment variable: {e}", flush=True)
        raise
    except Exception as e:
        print(f"❌ Error: {e}", flush=True)
        raise
