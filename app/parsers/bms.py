"""
ShowPulser – BookMyShow Parser

Strategies (in order):
  1. BMS internal REST API  ← bypasses Cloudflare completely
     GET /api/movies-data/showtimes-by-event?appCode=MOBAND2&...
     Returns structured JSON with ShowDetails > Venues > ShowTimes

  2. window.__INITIAL_STATE__ JSON in page HTML (via Playwright browser)
     Falls through if Cloudflare blocks headless Chromium on datacenter IPs.

  3. DOM fallback via BeautifulSoup
"""
from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime
from typing import Any

from curl_cffi import requests as curl_requests
from bs4 import BeautifulSoup
from loguru import logger

_CURL_IMPERSONATE_PROFILES = ["chrome124", "chrome120", "safari17_0"]

from app.browser import browser_pool
from app.models import ShowEntry, SourceSnapshot
from app.parsers.base import BaseParser
from app.retry import with_retry


_BMS_STATE_RE = re.compile(r"window\.__INITIAL_STATE__\s*=\s*(\{.*)", re.DOTALL)
_TIME_RE = re.compile(r"\b(\d{1,2}:\d{2})\s*(AM|PM)\b", re.I)
_SHOWTIME_BTN_CLASS = "sc-1vhizuf-1"

# BMS internal API endpoint – no Cloudflare protection
_BMS_API_URL = "https://in.bookmyshow.com/api/movies-data/showtimes-by-event"
_BMS_API_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-IN,en;q=0.9",
    "Referer": "https://in.bookmyshow.com/",
    "appCode": "MOBAND2",
    "appVersion": "14710",
}


def _extract_event_and_date(url: str) -> tuple[str, str, str]:
    """Extract eventCode, dateCode, and regionCode from BMS buytickets URL."""
    # 1. Event code
    event_code = ""
    m_ec = re.search(r"/(ET\d+)", url, re.I) or re.search(r"refEventCode=([A-Z0-9]+)", url, re.I) or re.search(r"eventCode=([A-Z0-9]+)", url, re.I)
    if m_ec:
        event_code = m_ec.group(1).upper()

    # 2. Date code (8 digits: YYYYMMDD)
    date_code = ""
    m_dc = re.search(r"/(\d{8})", url) or re.search(r"dateCode=(\d{8})", url)
    if m_dc:
        date_code = m_dc.group(1)
    else:
        date_code = datetime.now().strftime("%Y%m%d")

    # 3. Region code
    region_code = "CHEN"
    m_city = re.search(r"/movies/([^/]+)/", url)
    if m_city:
        city = m_city.group(1).lower()
        if "chennai" in city:
            region_code = "CHEN"
        elif "bangalore" in city or "bengaluru" in city:
            region_code = "BANG"
        elif "mumbai" in city:
            region_code = "MUMBAI"
        elif "delhi" in city or "ncr" in city:
            region_code = "NCR"
        elif "hyderabad" in city:
            region_code = "HYD"

    return event_code, date_code, region_code


class BookMyShowParser(BaseParser):

    @property
    def source_name(self) -> str:
        return "bookmyshow"

    @with_retry(max_attempts=3, base_delay=3.0, backoff=2.0)
    async def fetch(self, url: str, movie_name: str) -> SourceSnapshot:
        logger.info(f"[BMS] Fetching: {url}")

        # ── Strategy 1: BMS internal REST API (no Cloudflare) ─────────────────
        event_code, date_code, region_code = _extract_event_and_date(url)
        if event_code:
            logger.info(f"[BMS] Using API strategy: eventCode={event_code}, dateCode={date_code}, region={region_code}")
            snap = await self._fetch_via_api(event_code, date_code, region_code, url, movie_name)
            if snap.theatres:
                logger.info(f"[BMS API] Got {len(snap.theatres)} theatre(s) via REST API")
                return snap
            logger.warning("[BMS API] REST API returned no theatres, falling back to browser...")

        # ── Strategy 2: Playwright browser ────────────────────────────────────
        html = await self._fetch_via_browser(url)
        if html:
            snap = self._parse_state_json(html, url, movie_name)
            if snap.theatres:
                return snap
            logger.warning("[BMS] __INITIAL_STATE__ gave no theatres – falling back to DOM")
            return self._parse_dom(html, url, movie_name)

        return SourceSnapshot(source="bookmyshow", movie_name=movie_name, url=url, theatres=[])

    # ── Strategy 1: BMS REST API ──────────────────────────────────────────────
    async def _fetch_via_api(
        self,
        event_code: str,
        date_code: str,
        region_code: str,
        source_url: str,
        movie_name: str,
    ) -> SourceSnapshot:
        params = {
            "appCode": "MOBAND2",
            "appVersion": "14710",
            "language": "EN",
            "eventCode": event_code,
            "regionCode": region_code,
            "subRegion": region_code,
            "bmsId": "1.21.0.1",
            "token": "",
            "lat": "",
            "lon": "",
            "myLocation": "",
            "dateCode": date_code,
            "hammerfest": region_code,
        }
        data = None
        for profile in _CURL_IMPERSONATE_PROFILES:
            try:
                resp = await asyncio.to_thread(
                    curl_requests.get,
                    _BMS_API_URL,
                    params=params,
                    headers=_BMS_API_HEADERS,
                    impersonate=profile,
                    timeout=20,
                )
                if resp.status_code == 200:
                    logger.info(f"[BMS API] Got 200 with impersonate={profile}")
                    data = resp.json()
                    break
                else:
                    logger.warning(f"[BMS API] HTTP {resp.status_code} with impersonate={profile}, trying next...")
            except Exception as e:
                logger.warning(f"[BMS API] Request failed with impersonate={profile}: {e}")

        if data is None:
            logger.warning("[BMS API] All impersonate profiles failed")
            return SourceSnapshot(source="bookmyshow", movie_name=movie_name, url=source_url, theatres=[])

        return self._parse_api_response(data, source_url, movie_name)

    def _parse_api_response(self, data: dict, source_url: str, movie_name: str) -> SourceSnapshot:
        theatres: list[ShowEntry] = []
        try:
            raw_details = data.get("ShowDetails", [])
            # Can be either a list of date objects or a dict
            show_details_list = raw_details if isinstance(raw_details, list) else list(raw_details.values())

            for date_data in show_details_list:
                if not isinstance(date_data, dict):
                    continue
                raw_venues = date_data.get("Venues", [])
                # Venues can be a list or a dict
                venues_list = raw_venues if isinstance(raw_venues, list) else list(raw_venues.values())

                for v in venues_list:
                    if not isinstance(v, dict):
                        continue
                    name = (v.get("VenueName") or "").strip()
                    if not name or len(name) < 3:
                        continue
                    if v.get("AllowSales", "Y") == "N" or v.get("isVenueDown"):
                        continue

                    raw_shows = v.get("ShowTimes", [])
                    shows_list = raw_shows if isinstance(raw_shows, list) else list(raw_shows.values())

                    shows: list[str] = []
                    formats: set[str] = set()

                    for st in shows_list:
                        if not isinstance(st, dict):
                            continue
                        show_time = (st.get("ShowTime") or "").strip()
                        if show_time:
                            shows.append(show_time)

                        # Formats from Attributes field
                        attrs = (st.get("Attributes") or "").upper()
                        if attrs:
                            if "IMAX" in attrs:
                                formats.add("IMAX")
                            if "4DX" in attrs or "4 DX" in attrs:
                                formats.add("4DX")
                            if "DOLBY" in attrs or "ATMOS" in attrs:
                                formats.add("DOLBY ATMOS")
                            if "LASER" in attrs:
                                formats.add("LASER")
                            if "PXL" in attrs or "PIXL" in attrs:
                                formats.add("PXL")
                            if "BIGPIX" in attrs or "BIG PIX" in attrs:
                                formats.add("BIGPIX")
                            if "EPIQ" in attrs:
                                formats.add("EPIQ")
                            if "3D" in attrs:
                                formats.add("3D")
                        if st.get("IsAtmosEnabled") == "Y":
                            formats.add("DOLBY ATMOS")

                    url_fmt = _format_from_url(source_url)
                    if url_fmt:
                        formats.add(url_fmt)

                    booking_open = bool(shows)
                    theatres.append(ShowEntry(
                        theatre=name,
                        shows=shows,
                        formats=sorted(formats),
                        booking_open=booking_open,
                        booking_url=source_url if booking_open else None,
                    ))
        except Exception as e:
            logger.warning(f"[BMS API] Error parsing API response: {e}")

        seen: set[str] = set()
        unique_theatres: list[ShowEntry] = []
        for t in theatres:
            k = t.theatre.lower().strip()
            if k not in seen:
                seen.add(k)
                unique_theatres.append(t)

        logger.info(f"[BMS API] Parsed {len(unique_theatres)} theatre(s)")
        return SourceSnapshot(source="bookmyshow", movie_name=movie_name, url=source_url, theatres=unique_theatres)

    # ── Strategy 2: Playwright browser ────────────────────────────────────────
    async def _fetch_via_browser(self, url: str) -> str:
        html = ""
        try:
            async with browser_pool.new_page() as page:
                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
                    await asyncio.sleep(3.0)
                except Exception as e:
                    logger.debug(f"[BMS Browser] Load warning: {e}")
                html = await page.content()
        except Exception as e:
            logger.warning(f"[BMS Browser] Browser fetch failed: {e}")
        return html

    # ── __INITIAL_STATE__ parser ───────────────────────────────────────────────
    def _parse_state_json(self, html: str, url: str, movie_name: str) -> SourceSnapshot:
        m = _BMS_STATE_RE.search(html)
        if not m:
            return SourceSnapshot(source="bookmyshow", movie_name=movie_name, url=url, theatres=[])

        try:
            raw = m.group(1)
            depth = 0
            end = -1
            in_str = False
            escape_next = False
            for i, ch in enumerate(raw):
                if escape_next:
                    escape_next = False
                    continue
                if ch == "\\" and in_str:
                    escape_next = True
                    continue
                if ch == '"':
                    in_str = not in_str
                    continue
                if in_str:
                    continue
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        end = i + 1
                        break
            if end == -1:
                raise ValueError("unclosed JSON")
            state = json.loads(raw[:end])
        except Exception as e:
            logger.warning(f"[BMS] Failed to parse __INITIAL_STATE__: {e}")
            return SourceSnapshot(source="bookmyshow", movie_name=movie_name, url=url, theatres=[])

        theatres: list[ShowEntry] = []

        try:
            sfa = state.get("showtimesFunctionalApi", {})
            for qk, qv in sfa.get("queries", {}).items():
                if not isinstance(qv, dict):
                    continue
                raw_data = qv.get("data", {})
                idata = raw_data.get("data", raw_data) if isinstance(raw_data, dict) else {}
                widgets = idata.get("showtimeWidgets", []) if isinstance(idata, dict) else []
                for w in widgets:
                    if not isinstance(w, dict):
                        continue
                    for g in w.get("data", []):
                        for card in (g.get("data", []) if isinstance(g, dict) else []):
                            if not isinstance(card, dict):
                                continue
                            add = card.get("additionalData", {})
                            name = (add.get("venueName") or card.get("title") or "").strip()
                            if not name or len(name) < 3:
                                continue
                            shows = [
                                st.get("title", "").strip()
                                for st in card.get("showtimes", [])
                                if isinstance(st, dict) and st.get("title")
                            ]
                            formats: set[str] = set()
                            for st in card.get("showtimes", []):
                                if isinstance(st, dict):
                                    for fmt in _extract_formats(st.get("screenAttr", "")):
                                        formats.add(fmt)
                            url_fmt = _format_from_url(url)
                            if url_fmt:
                                formats.add(url_fmt)
                            theatres.append(ShowEntry(
                                theatre=name,
                                shows=shows,
                                formats=sorted(formats),
                                booking_open=bool(shows),
                                booking_url=url if shows else None,
                            ))
        except Exception as e:
            logger.warning(f"[BMS] showtimesFunctionalApi parse error: {e}")

        seen: set[str] = set()
        unique: list[ShowEntry] = []
        for t in theatres:
            k = t.theatre.lower().strip()
            if k not in seen:
                seen.add(k)
                unique.append(t)

        logger.info(f"[BMS JSON] Parsed {len(unique)} theatre(s)")
        return SourceSnapshot(source="bookmyshow", movie_name=movie_name, url=url, theatres=unique)

    # ── DOM fallback ───────────────────────────────────────────────────────────
    def _parse_dom(self, html: str, url: str, movie_name: str) -> SourceSnapshot:
        soup = BeautifulSoup(html, "lxml")
        theatres: list[ShowEntry] = []
        btns = soup.find_all(
            lambda t: t.name in ("a", "button", "div", "span")
            and (
                any(_SHOWTIME_BTN_CLASS in c for c in t.get("class", []))
                or (_TIME_RE.search(t.get_text()) and len(t.get_text().strip()) <= 15)
            )
        )
        seen: set[int] = set()
        containers: list = []
        for btn in btns:
            el = btn.parent
            for _ in range(15):
                if el is None:
                    break
                anchor = el.find("a", href=re.compile(r"/cinemas/|bookmyshow\.com/cinemas/"))
                if anchor and len(anchor.get_text(strip=True)) > 3:
                    eid = id(el)
                    if eid not in seen:
                        seen.add(eid)
                        containers.append(el)
                    break
                el = el.parent
        for ctr in containers:
            anchor = ctr.find("a", href=re.compile(r"/cinemas/|bookmyshow\.com/cinemas/"))
            name = anchor.get_text(strip=True) if anchor else ""
            if not name:
                continue
            shows: list[str] = []
            formats: set[str] = set()
            for btn in ctr.find_all(
                lambda t: t.name in ("a", "button", "div", "span")
                and (
                    any(_SHOWTIME_BTN_CLASS in c for c in t.get("class", []))
                    or (_TIME_RE.search(t.get_text()) and len(t.get_text().strip()) <= 15)
                )
            ):
                text = btn.get_text(separator=" ", strip=True)
                m = _TIME_RE.search(text)
                if m:
                    shows.append(f"{m.group(1)} {m.group(2).upper()}")
                for fmt in _extract_formats(text):
                    formats.add(fmt)
            url_fmt = _format_from_url(url)
            if url_fmt:
                formats.add(url_fmt)
            theatres.append(ShowEntry(
                theatre=name,
                shows=sorted(set(shows)),
                formats=sorted(formats),
                booking_open=bool(shows),
                booking_url=url if shows else None,
            ))
        logger.info(f"[BMS DOM] Parsed {len(theatres)} theatre(s)")
        return SourceSnapshot(source="bookmyshow", movie_name=movie_name, url=url, theatres=theatres)


def _extract_formats(text: str) -> list[str]:
    t = text.upper()
    out = []
    if "IMAX" in t:
        out.append("IMAX")
    elif "4DX" in t:
        out.append("4DX")
    elif "EPIQ" in t:
        out.append("EPIQ")
    elif "DOLBY" in t or "ATMOS" in t:
        out.append("DOLBY ATMOS")
    elif "LASER" in t:
        out.append("LASER")
    if "3D" in t:
        out.append("3D")
    if "4K" in t:
        out.append("4K")
    return out


def _format_from_url(url: str) -> str | None:
    u = url.lower()
    if "epiq" in u:
        return "EPIQ 3D"
    if "imax" in u:
        return "IMAX"
    if "4dx" in u:
        return "4DX"
    return None
