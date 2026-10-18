import json
import os
import re
import time
from datetime import datetime, timezone
from http.cookiejar import MozillaCookieJar
from pathlib import Path
from urllib.parse import urljoin, unquote, urlparse, parse_qs
from zoneinfo import ZoneInfo

import requests

BASE = "https://kickofftv.id"
DATA_PATH = Path("data/matches.json")
STATE_PATH = Path("state/collector-state.json")
COOKIE_PATH = Path("/tmp/kotv-cookies.txt")
TIMEOUT = 20
RETRY_SECONDS = 5 * 60
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


def perform_login():
    username = os.environ.get("KOTV_USER", "").strip()
    password = os.environ.get("KOTV_PASS", "").strip()

    if not username or not password:
        return False

    print("[AUTH] Attempting automated login for:", username)
    login_url = urljoin(BASE + "/", "member/login.php")
    
    try:
        session.get(BASE + "/login/", timeout=TIMEOUT)
        
        payload = {
            "login": username,
            "password": password,
            "redirect_to": "/",
        }
        response = session.post(login_url, data=payload, timeout=TIMEOUT, allow_redirects=True)
        
        print(f"[AUTH] Login HTTP status: {response.status_code}")
        print(f"[AUTH] PHPSESSID present after login:", any(c.name == "PHPSESSID" for c in session.cookies))
        
        return True
    except Exception as exc:
        print("[AUTH] Login failed with error:", type(exc).__name__, str(exc))
        return False


def setup_cookies():
    if perform_login():
        return

    cookie_text = os.environ.get("KOTV_COOKIES", "")

    if not cookie_text.strip():
        print("[COOKIE] KOTV_COOKIES is empty and no credentials provided.")
        return

    COOKIE_PATH.parent.mkdir(parents=True, exist_ok=True)
    COOKIE_PATH.write_text(
        cookie_text.rstrip() + "\n",
        encoding="utf-8",
    )

    try:
        jar = MozillaCookieJar(str(COOKIE_PATH))
        jar.load(ignore_discard=True, ignore_expires=True)

        if not len(jar):
            print("[COOKIE] File loaded but contains no cookies.")
            return

        session.cookies.update(jar)

        print("[COOKIE] Netscape file parsed successfully.")
        print("[COOKIE] Cookies loaded:", len(jar))
        print(
            "[COOKIE] PHPSESSID present:",
            any(c.name == "PHPSESSID" for c in jar),
        )

    except Exception as exc:
        print("[COOKIE] Netscape parsing failed:", type(exc).__name__)
        print("[COOKIE] Check the KOTV_COOKIES secret format.")


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

    if value.startswith(("http://", "https://")):
        return value

    return ""


def clean_stream_url(raw_value):
    if not raw_value:
        return ""
    
    value = unquote(str(raw_value).strip())

    # Якщо посилання містить шлях до плеєра з параметром url=, витягуємо чистий .m3u8 звідти
    parsed_url = urlparse(value)
    query_params = parse_qs(parsed_url.query)
    if "url" in query_params:
        inner_url = query_params["url"][0]
        if ".m3u8" in inner_url.lower():
            return absolute_url(inner_url)

    # Альтернативний регекс для пошуку вкладеного url=
    nested = re.search(
        r"""(?:[?&]|^)url=(https?[^&"'<> ]+\.m3u8(?:\?[^&"'<> ]*)?)""",
        value,
        re.IGNORECASE,
    )
    if nested:
        return absolute_url(nested.group(1))

    if ".m3u8" in value.lower():
        return absolute_url(value)

    return ""


def parse_streams(html):
    found = {"sd": "", "hd": ""}

    patterns = [
        ("sd", r"""data-player-url-sd\s*=\s*["']([^"']+)"""),
        ("hd", r"""data-player-url-hd\s*=\s*["']([^"']+)"""),
    ]

    for quality, pattern in patterns:
        match = re.search(pattern, html, re.IGNORECASE)
        if match:
            stream = clean_stream_url(match.group(1))
            if stream:
                found[quality] = stream

    for match in re.finditer(
        r"""data-player-url\s*=\s*["']([^"']+)""",
        html,
        re.IGNORECASE,
    ):
        stream = clean_stream_url(match.group(1))
        if stream and not found["sd"]:
            found["sd"] = stream

    direct_pattern = r"""https?://[^"'\\\s<>]+?\.m3u8(?:\?[^"'\\\s<>]*)?"""
    for match in re.finditer(direct_pattern, html, re.IGNORECASE):
        stream = clean_stream_url(match.group(0))
        if not stream:
            continue

        if not found["sd"]:
            found["sd"] = stream
        elif not found["hd"] and stream != found["sd"]:
            found["hd"] = stream
            break

    return found


def fetch_match_streams(slug):
    url = urljoin(BASE + "/", "match/" + str(slug).lstrip("/"))

    try:
        response = session.get(
            url,
            timeout=TIMEOUT,
            allow_redirects=True,
        )

        response.raise_for_status()
        html = response.text
        streams = parse_streams(html)

        return streams, url

    except requests.RequestException as exc:
        print(
            "[DEBUG] Match request failed:",
            slug,
            type(exc).__name__,
        )
        return {"sd": "", "hd": ""}, url


def get_match_time(match):
    value = match.get("match_time")

    try:
        number = int(value)
        if number > 10_000_000_000:
            number //= 1000
        # WIB (UTC+7) переводимо у Київський час (UTC+3 у жовтні) -> різниця 4 години назад
        number -= 4 * 3600
        return number
    except (TypeError, ValueError):
        return None


def main():
    setup_cookies()

    now = int(time.time())
    today = datetime.now(LOCAL_TZ).date().isoformat()
    livescore_url = f"{BASE}/livescore/?ajax=1&day={today}"

    try:
        payload = fetch_json(livescore_url)
    except Exception as exc:
        print("Could not fetch LiveScore:", type(exc).__name__, str(exc))
        raise

    matches = payload.get("matches", [])

    if not isinstance(matches, list):
        raise RuntimeError(
            "Unexpected LiveScore response: matches is not a list"
        )

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
    not_eligible = 0
    retry_wait = 0

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
            status_lower in {"finished", "cancelled", "canceled", "ft", "full time", "final", "ended"}
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

        sd = clean_stream_url(streams.get("sd", ""))
        hd = clean_stream_url(streams.get("hd", ""))

        match_time = get_match_time(match)
        is_live = bool(match.get("is_live"))

        eligible = is_live or (
            match_time is not None
            and now - 30 * 60 <= match_time <= now + 6 * 60 * 60
        )

        last_checked = int(checked.get(match_id, 0) or 0)
        has_stream = bool(sd or hd)
        should_scan = eligible and not has_stream

        if not eligible:
            not_eligible += 1
        elif has_stream:
            skipped += 1
        elif now - last_checked < RETRY_SECONDS:
            should_scan = False
            retry_wait += 1

        match_url = urljoin(
            BASE + "/",
            "match/" + slug.lstrip("/"),
        )

        if should_scan:
            new_streams, match_url = fetch_match_streams(slug)
            scanned += 1
            checked[match_id] = now

            if new_streams.get("sd"):
                sd = clean_stream_url(new_streams["sd"])

            if new_streams.get("hd"):
                hd = clean_stream_url(new_streams["hd"])

            if sd or hd:
                saved_streams[match_id] = {
                    "sd": sd,
                    "hd": hd,
                    "slug": slug,
                }

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
            "is_finished": is_finished,
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

    print()
    print("========== SUMMARY ==========")
    print("Kyiv local date:", today)
    print("LiveScore records:", len(matches))
    print("Matches excluded by competition filter:", excluded)
    print("Matches included:", len(output))
    print("Pages scanned this run:", scanned)
    print("Matches skipped (already have streams):", skipped)
    print("Matches waiting for retry:", retry_wait)
    print("Matches not eligible for scanning:", not_eligible)
    print("Retry interval: 5 minutes")
    print("JSON saved to:", DATA_PATH)
    print("State saved to:", STATE_PATH)


if __name__ == "__main__":
    main()
