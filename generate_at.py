#!/usr/bin/env python3
"""
RakutenTV AT — EPG generator
Fetches programme data from the Rakuten v3/live_channels API and merges
"""

import hashlib
import re
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import pytz
import requests
from lxml import etree

# ── Configuration ─────────────────────────────────────────────────────────────

TIMEZONE           = pytz.timezone("Europe/Berlin")
DT_FORMAT          = "%Y%m%d%H%M%S %z"
GAP_THRESHOLD_SECS = 60

RETRY_ATTEMPTS     = 4
RETRY_BACKOFF_SECS = 20

API_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/128.0.0.0 Safari/537.36"
    ),
    "Origin": "https://rakuten.tv",
    "Referer": "https://rakuten.tv/",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "de-AT,de;q=0.9",
}

# Values that have historically worked. 250 is now rejected.
PER_PAGE_CANDIDATES = [100, 50, 20]


# ── Helpers ───────────────────────────────────────────────────────────────────

def remove_control_characters(s: str) -> str:
    return "".join(ch for ch in s if unicodedata.category(ch)[0] != "C")


def normalize(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def to_tz_str(val) -> str:
    if isinstance(val, datetime):
        dt = val if val.tzinfo else val.replace(tzinfo=timezone.utc)
    else:
        dt = datetime.fromtimestamp(val, tz=timezone.utc)
    return dt.astimezone(TIMEZONE).strftime(DT_FORMAT)


def fetch_with_retry(url: str, headers: dict | None = None, timeout: int = 30) -> requests.Response:
    last_exc = None
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            resp = requests.get(url, headers=headers or {}, timeout=timeout)
            if resp.status_code == 503 and attempt < RETRY_ATTEMPTS:
                print(f"  [attempt {attempt}/{RETRY_ATTEMPTS}] 503, retrying in {RETRY_BACKOFF_SECS}s ...")
                time.sleep(RETRY_BACKOFF_SECS)
                continue
            if 400 <= resp.status_code < 500:
                print(f"  HTTP {resp.status_code} body: {resp.text[:600]!r}")
                resp.raise_for_status()
            resp.raise_for_status()
            return resp
        except requests.exceptions.RequestException as exc:
            last_exc = exc
            # Don't keep retrying pure 4xx
            if hasattr(exc, "response") and exc.response is not None and 400 <= exc.response.status_code < 500:
                break
            if attempt < RETRY_ATTEMPTS:
                print(f"  [attempt {attempt}/{RETRY_ATTEMPTS}] {exc}, retrying in {RETRY_BACKOFF_SECS}s ...")
                time.sleep(RETRY_BACKOFF_SECS)
    raise last_exc

# ── EPG window ────────────────────────────────────────────────────────────────

def get_epg_window(hours: int = 120):
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    end = (now + timedelta(hours=hours)).replace(hour=0, minute=0, second=0, microsecond=0)
    if end <= now:
        end += timedelta(days=1)
    return now, end

# ── XMLTV ──────────────────────────────────────────────────────────────────────

def build_xmltv(channels: list, programmes: list) -> bytes:
    root = etree.Element("tv")
    root.set("generator-info-name", "Fellfresse")
    root.set("generator-info",  "EPG/RakutenTV-AT")

    for ch in channels:
        channel = etree.SubElement(root, "channel")
        channel.set("id", str(ch["id"]))

        display = etree.SubElement(channel, "display-name")
        lang = (ch.get("language") or "en").rstrip("s").lower()
        display.set("lang", lang)
        display.text = ch["name"]

        if ch.get("icon"):
            icon = etree.SubElement(channel, "icon")
            icon.set("src", ch["icon"])
            icon.text = ""

    for pr in programmes:
        prog = etree.SubElement(root, "programme")
        prog.set("channel", str(pr["channel_id"]))
        prog.set("start",   to_tz_str(pr["starts_at"]))
        prog.set("stop",    to_tz_str(pr["ends_at"]))

        title = etree.SubElement(prog, "title")
        title.set("lang", "en")
        title.text = pr["title"]

        if pr.get("subtitle"):
            sub = etree.SubElement(prog, "sub-title")
            sub.set("lang", "en")
            sub.text = remove_control_characters(pr["subtitle"])

        if pr.get("description"):
            desc = etree.SubElement(prog, "desc")
            desc.set("lang", "en")
            desc.text = remove_control_characters(pr["description"])

        if pr.get("tags"):
            for tag in pr["tags"]:
                cat = etree.SubElement(prog, "category")
                cat.set("lang", "en")
                cat.text = tag.get("name", "")

    return etree.tostring(root, pretty_print=True, encoding="utf-8")

# ── API helpers ───────────────────────────────────────────────────────────────

def build_api_url(epg_start, epg_end, per_page: int, page: int = 1,
                  include_timestamps: bool = True) -> str:
    params = {
        "classification_id": "300",
        "device_identifier": "web",
        "device_stream_audio_quality": "2.0",
        "device_stream_hdr_type": "NONE",
        "device_stream_video_quality": "FHD",
        "epg_duration_minutes": "360",
        "epg_ends_at": epg_end.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "epg_starts_at": epg_start.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "locale": "de",
        "market_code": "at",
        "per_page": str(per_page),
        "page": str(page),
    }
    if include_timestamps:
        params["epg_ends_at_timestamp"] = str(int(epg_end.timestamp()))
        params["epg_starts_at_timestamp"] = str(int(epg_start.timestamp()))

    return "https://gizmo.rakuten.tv/v3/live_channels?" + urlencode(params)


def fetch_one_page(epg_start, epg_end, per_page: int, page: int,
                   include_timestamps: bool) -> list:
    url = build_api_url(epg_start, epg_end, per_page, page, include_timestamps)
    resp = fetch_with_retry(url, headers=API_HEADERS)
    data = resp.json().get("data") or []
    return data


def fetch_epg_data() -> list:
    """
    Try different per_page values and window sizes until something works.
    Also paginates so we still get the full channel list.
    """
    strategies = [
        {"hours": 120, "timestamps": True, "label": "120h + timestamps"},
        {"hours": 96, "timestamps": True, "label": "96h + timestamps"},
        {"hours": 72, "timestamps": True, "label": "72h + timestamps"},
        {"hours": 72, "timestamps": False, "label": "72h (dates only)"},
        {"hours": 48, "timestamps": True,  "label": "48h + timestamps"},
        {"hours": 24, "timestamps": False, "label": "24h (dates only)"},
    ]

    last_exc = None

    for strat in strategies:
        for per_page in PER_PAGE_CANDIDATES:
            epg_start, epg_end = get_epg_window(hours=strat["hours"])
            print(f"\nTrying: {strat['label']}, per_page={per_page}")
            print(f"  window: {epg_start.isoformat()} → {epg_end.isoformat()}")

            try:
                all_channels = []
                page = 1
                while True:
                    chunk = fetch_one_page(
                        epg_start, epg_end, per_page, page, strat["timestamps"]
                    )
                    if not chunk:
                        break
                    all_channels.extend(chunk)
                    print(f"  page {page}: +{len(chunk)} channels (total {len(all_channels)})")
                    # Stop when we receive fewer than requested (last page)
                    if len(chunk) < per_page:
                        break
                    page += 1
                    # Safety limit
                    if page > 10:
                        break

                if all_channels:
                    print(f"  ✓ Success — retrieved {len(all_channels)} channels")
                    return all_channels
                else:
                    print("  empty data array")
            except Exception as exc:
                last_exc = exc
                print(f"  ✗ Failed: {exc}")
                continue

    raise RuntimeError(
        "All EPG fetch strategies failed. Last error: " + str(last_exc)
    ) from last_exc


# ── Main ──────────────────────────────────────────────────────────────────────

def main():

    print("\nFetching EPG data from Rakuten API ...")
    data = fetch_epg_data()
    print(f"\nRetrieved {len(data)} channels\n")

    channels_data  = []
    programme_data = []

    for channel in data:
        ch_name = channel["title"]
        ch_id   = channel["id"]
        print(f"  {ch_name}")

        ch_icon = None
        if channel.get("images"):
            imgs = channel["images"]
            ch_icon = imgs.get("artwork_negative") or imgs.get("artwork")

        ch_language = ch_tags = None
        if channel.get("labels"):
            labels = channel["labels"]
            langs  = labels.get("languages")
            if langs:
                ch_language = langs[0].get("id")
            ch_tags = labels.get("tags")

        for item in channel.get("live_programs", []):
            programme_data.append({
                "title":       item["title"],
                "subtitle":    item.get("subtitle"),
                "description": item.get("description"),
                "starts_at":   datetime.strptime(item["starts_at"], "%Y-%m-%dT%H:%M:%S.000%z"),
                "ends_at":     datetime.strptime(item["ends_at"],   "%Y-%m-%dT%H:%M:%S.000%z"),
                "channel_id":  ch_id,
                "language":    ch_language,
                "tags":        ch_tags,
            })

    # Normalise end times
    programme_data.sort(key=lambda p: (p["channel_id"], p["starts_at"]))
    by_channel = {}
    for p in programme_data:
        by_channel.setdefault(p["channel_id"], []).append(p)

    for plist in by_channel.values():
        for i in range(len(plist) - 1):
            cur, nxt = plist[i], plist[i + 1]
            if nxt["starts_at"] <= cur["ends_at"]:
                cur["ends_at"] = nxt["starts_at"]
            elif (nxt["starts_at"] - cur["ends_at"]).total_seconds() <= GAP_THRESHOLD_SECS:
                cur["ends_at"] = nxt["starts_at"]

    with open("Rakuten_AT_epg.xml", "wb") as f:
        f.write(build_xmltv(channels_data, programme_data))
    print("\nWrote at_epg.xml")

if __name__ == "__main__":
    main()


