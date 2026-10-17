import json
import os
import re
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from http.cookiejar import MozillaCookieJar
from pathlib import Path
from urllib.parse import urljoin, unquote

import requests

BASE = "https://kickofftv.id"
DATA_PATH = Path("data/matches.json")
STATE_PATH = Path("state/collector-state.json")
COOKIE_PATH = Path("/tmp/kotv-cookies.txt")
TIMEOUT = 20

# Повторна перевірка — кожні 5 хвилин.
RETRY_SECONDS = 5 * 60

# Часовий пояс України.
LOCAL_TZ = ZoneInfo("Europe/Kyiv")

ALLOWED_COMPETITIONS = {
    "English Premier League",
    "Spanish La Liga",
    "Italian Serie A",
    "Bundesliga",
}

session = requests.Session()
session.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Referer": BASE + "/",
})


def load_json(path, default):
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return default


def save_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as f:
        json.dump(value, f, ensure_ascii=False, indent=2)
        f.write("\n")
    temporary.replace(path)


def setup_cookies():
    cookie_text = os.environ.get("KOTV_COOKIES", "").strip()
    if not cookie_text:
        print("KOTV_COOKIES secret is empty; continuing without optional cookies.")
        return

    COOKIE_PATH.write_text(cookie_text + "\n", encoding="utf-8")
    try:
        jar = MozillaCookieJar(str(COOKIE_PATH))
        jar.load(ignore_discard=True, ignore_expires=True)
        session.cookies.update(jar)
        print("Loaded optional KickoffTV cookies.")
    except Exception as exc:
        print("Could not load cookies:", str(exc))


def fetch_json(url):
    response = session.get(url, timeout=TIMEOUT)
    response.raise_for_status()
    return response.json()


def absolute_url(value):
    if not value:
        return ""
    value = unquote(str(value).strip())
    if value.startswith("//"):
        return "https:" + value
    if value.startswith("/"):
        return urljoin(BASE, value)
    if value.startswith("http://") or value.startswith("https://"):
        return value
    return ""


def parse_streams(html):
    found = {"sd": "", "hd": ""}

    patterns = [
        ("sd", r"""data-player-url-sd=["']([^"']+)"""),
        ("hd", r"""data-player-url-hd=["']([^"']+)"""),
    ]

    for quality, pattern in patterns:
        match = re.search(pattern, html, re.IGNORECASE)
        if match:
            value = unquote(match.group(1))
            if ".m3u8" in value.lower():
                found[quality] = absolute_url(value)

    for match in re.finditer(
        r"""data-player-url=["']([^"']+)""",
        html,
        re.IGNORECASE,
    ):
        value = unquote(match.group(1))
        nested = re.search(
            r"""(?:url=)(https?[^&"'<> ]+\.m3u8(?:\?[^&"'<> ]*)?)""",
            value,
            re.IGNORECASE,
        )
        if nested:
            stream = absolute_url(nested.group(1))
            if stream and not found["sd"]:
                found["sd"] = stream

    direct_pattern = (
        r"""https?://[^"'\\\s<>]+?\.m3u8(?:\?[^"'\\\s<>]*)?"""
    )
    for match in re.finditer(direct_pattern, html, re.IGNORECASE):
        stream = absolute_url(match.group(0))
        if stream:
            if not found["sd"]:
                found["sd"] = stream
            elif not found["hd"] and stream != found["sd"]:
                found["hd"] = stream
                break

    return found


def fetch_match_streams(slug):
    url = urljoin(BASE + "/", "match/" + str(slug).lstrip("/"))
    try:
        response = session.get(url, timeout=TIMEOUT)
        response.raise_for_status()
        return parse_streams(response.text), url
    except requests.RequestException as exc:
        print("Match page failed:", slug, str(exc))
        return {"sd": "", "hd": ""}, url


def get_match_time(match):
    value = match.get("match_time")
    try:
        number = int(value)
        if number > 10_000_000_000:
            number //= 1000
        return number
    except (TypeError, ValueError):
        return None


def main():
    setup_cookies()

    # Unix-час порівнюємо в UTC, а дату запиту формуємо за Києвом.
    now = int(time.time())
    today = datetime.now(LOCAL_TZ).date().isoformat()
    livescore_url = f"{BASE}/livescore/?ajax=1&day={today}"

    try:
        payload = fetch_json(livescore_url)
    except Exception as exc:
        print("Could not fetch LiveScore:", str(exc))
        raise

    matches = payload.get("matches", [])
    if not isinstance(matches, list):
        raise RuntimeError("Unexpected LiveScore response: matches is not a list")

    state = load_json(
        STATE_PATH,
        {"checked": {}, "streams": {}},
    )
    checked = state.setdefault("checked", {})
    saved_streams = state.setdefault("streams", {})
    output = []

    scanned = 0
    skipped = 0
    excluded = 0

    for match in matches:
        if not isinstance(match, dict):
            continue

        competition = str(match.get("competition") or "").strip()
        if competition not in ALLOWED_COMPETITIONS:
            excluded += 1
            continue

        status = str(match.get("status") or "").strip()
        status_lower = status.lower()
        is_finished = bool(match.get("is_finished")) or (
            status_lower in {"finished", "cancelled", "canceled"}
        )
        if is_finished:
            continue

        match_id = str(match.get("id") or match.get("slug") or "")
        slug = str(match.get("slug") or "").strip()
        if not match_id or not slug:
            continue

        streams = saved_streams.get(match_id, {})
        if not isinstance(streams, dict):
            streams = {}

        sd = streams.get("sd", "") or ""
        hd = streams.get("hd", "") or ""

        match_time = get_match_time(match)
        is_live = bool(match.get("is_live"))

        # Живі матчі перевіряємо одразу, майбутні — за 6 годин до початку.
        eligible = is_live or (
            match_time is not None
            and now - 30 * 60 <= match_time <= now + 6 * 60 * 60
        )

        last_checked = int(checked.get(match_id, 0) or 0)
        has_stream = bool(sd or hd)
        should_scan = eligible and not has_stream

        # Якщо перевірка не знайшла потік, повторюємо через 5 хвилин.
        if should_scan and now - last_checked < RETRY_SECONDS:
            should_scan = False

        match_url = urljoin(BASE + "/", "match/" + slug.lstrip("/"))

        if should_scan:
            new_streams, match_url = fetch_match_streams(slug)
            scanned += 1
            checked[match_id] = now

            if new_streams.get("sd"):
                sd = new_streams["sd"]
            if new_streams.get("hd"):
                hd = new_streams["hd"]

            if sd or hd:
                saved_streams[match_id] = {
                    "sd": sd,
                    "hd": hd,
                    "slug": slug,
                }
        else:
            skipped += 1

        output.append({
            "id": match.get("id"),
            "slug": slug,
            "competition": competition,
            "home": match.get("home", ""),
            "away": match.get("away", ""),
            "home_logo": absolute_url(match.get("home_logo")),
            "away_logo": absolute_url(match.get("away_logo")),
            "home_score": match.get("home_score"),
            "away_score": match.get("away_score"),
            "status": status,
            "time_label": match.get("time_label", ""),
            "match_time": match_time,
            "match_date": match.get("match_date", ""),
            "is_live": is_live,
            "is_finished": False,
            "streams": {
                "sd": sd,
                "hd": hd,
            },
            "match_url": match_url,
        })

    output.sort(
        key=lambda item: (
            not item["is_live"],
            item["match_time"] or 0,
            str(item["competition"]),
            str(item["home"]),
        )
    )

    result = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "source": BASE,
        "matches_count": len(output),
        "matches": output,
    }

    save_json(DATA_PATH, result)
    save_json(STATE_PATH, state)

    print("Kyiv local date:", today)
    print("LiveScore records:", len(matches))
    print("Matches excluded by competition filter:", excluded)
    print("Matches included:", len(output))
    print("Pages scanned this run:", scanned)
    print("Matches skipped from scanning:", skipped)
    print("Retry interval: 5 minutes")
    print("JSON saved to:", DATA_PATH)
    print("State saved to:", STATE_PATH)


if __name__ == "__main__":
    main()
