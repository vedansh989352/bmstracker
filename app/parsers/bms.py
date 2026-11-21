"""
ShowPulser – BookMyShow Parser

Strategies (in order):
  1. BMS internal REST API  ← NEW: bypasses Cloudflare completely
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

import httpx
from bs4 import BeautifulSoup
from loguru import logger

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


def _extract_event_and_date(url: str) -> tuple[str, str]:
    """Extract eventCode and dateCode from BMS buytickets URL."""
    event_match = re.search(r"/buytickets/[^/]+/(\w+)/(\d{8})", url)
    if event_match:
        return event_match.group(1), event_match.group(2)
    # Alt format: ?refEventCode=ET00526306 & /20261218
    event_match2 = re.search(r"refEventCode=([A-Z0-9]+)", url)
    date_match = re.search(r"/(\d{8})", url)
    ec = event_match2.group(1) if event_match2 else ""
    dc = date_match.group(1) if date_match else datetime.now().strftime("%Y%m%d")
    return ec, dc


class BookMyShowParser(BaseParser):

    @property
    def source_name(self) -> str:
        return "bookmyshow"

    @with_retry(max_attempts=3, base_delay=5.0, backoff=2.0)
    async def fetch(self, url: str, movie_name: str) -> SourceSnapshot:
        logger.info(f"[BMS] Fetching: {url}")

        # ── Strategy 1: BMS internal REST API (no Cloudflare) ─────────────────
        event_code, date_code = _extract_event_and_date(url)
        if event_code:
            snap = await self._fetch_via_api(event_code, date_code, url, movie_name)
            if snap.theatres:
                logger.info(f"[BMS API] ✅ Got {len(snap.theatres)} theatre(s) via REST API")
                return snap
            logger.warning("[BMS API] REST API returned no theatres, falling back to browser...")

        # ── Strategy 2: Playwright browser (may be blocked by Cloudflare on datacenter IPs) ──
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
        source_url: str,
        movie_name: str,
    ) -> SourceSnapshot:
        params = {
            "appCode": "MOBAND2",
            "appVersion": "14710",
            "language": "EN",
            "eventCode": event_code,
            "regionCode": "CHEN",
            "subRegion": "CHEN",
            "bmsId": "1.21.0.1",
            "token": "",
            "lat": "",
            "lon": "",
            "myLocation": "",
            "dateCode": date_code,
            "hammerfest": "CHEN",
        }
        try:
            async with httpx.AsyncClient(timeout=20, headers=_BMS_API_HEADERS, follow_redirects=True) as client:
                resp = await client.get(_BMS_API_URL, params=params)
                if resp.status_code != 200:
                    logger.warning(f"[BMS API] HTTP {resp.status_code}")
                    return SourceSnapshot(source="bookmyshow", movie_name=movie_name, url=source_url, theatres=[])
                data = resp.json()
        except Exception as e:
            logger.warning(f"[BMS API] Request failed: {e}")
            return SourceSnapshot(source="bookmyshow", movie_name=movie_name, url=source_url, theatres=[])

        return self._parse_api_response(data, source_url, movie_name)

    def _parse_api_response(self, data: dict, source_url: str, movie_name: str) -> SourceSnapshot:
        theatres: list[ShowEntry] = []
        try:
            show_details = data.get("ShowDetails", {})
            for date_key, date_data in show_details.items():
                venues = date_data.get("Venues", {})
                if not isinstance(venues, dict):
                    continue
                for venue_code, v in venues.items():
                    if not isinstance(v, dict):
                        continue
                    name = (v.get("VenueName") or "").strip()
                    if not name or len(name) < 3:
                        continue
                    if v.get("AllowSales", "Y") == "N" or v.get("isVenueDown"):
                        continue

                    shows: list[str] = []
                    formats: set[str] = set()

                    for st in v.get("ShowTimes", []):
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
                            if "BIGPIX" in attrs:
                                formats.add("BIGPIX")
                            if "EPIQ" in attrs:
                                formats.add("EPIQ")
                            if "3D" in attrs:
                                formats.add("3D")
                        if st.get("IsAtmosEnabled") == "Y":
                            formats.add("DOLBY ATMOS")

                    # Formats from URL
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

        logger.info(f"[BMS API] Parsed {len(theatres)} theatre(s)")
        return SourceSnapshot(source="bookmyshow", movie_name=movie_name, url=source_url, theatres=theatres)

    # ── Strategy 2: Playwright browser ────────────────────────────────────────
    async def _fetch_via_browser(self, url: str) -> str:
        html = ""
        try:
            async with browser_pool.new_page() as page:
                try:
                    await page.goto(url, wait_until="load", timeout=45_000)
                    await asyncio.sleep(6.0)
                    content_check = await page.content()
                    if "challenges.cloudflare.com" in content_check or "__INITIAL_STATE__" not in content_check:
                        logger.info("[BMS Browser] Cloudflare challenge detected, waiting 10s more...")
                        await asyncio.sleep(10.0)
                except Exception as e:
                    logger.debug(f"[BMS Browser] Load warning: {e}")
                html = await page.content()
        except Exception as e:
            logger.warning(f"[BMS Browser] Browser fetch failed: {e}")
        if "challenges.cloudflare.com" in html and "__INITIAL_STATE__" not in html:
            logger.warning("[BMS Browser] Cloudflare blocked the browser request.")
            return ""
        return html

    # ── __INITIAL_STATE__ parser ───────────────────────────────────────────────
    def _parse_state_json(self, html: str, url: str, movie_name: str) -> SourceSnapshot:
        m = _BMS_STATE_RE.search(html)
        if not m:
            return SourceSnapshot(source="bookmyshow", movie_name=movie_name, url=url, theatres=[])

        try:
            raw = m.group(1)
            depth = 0; end = -1; in_str = False; escape_next = False
            for i, ch in enumerate(raw):
                if escape_next: escape_next = False; continue
                if ch == "\\" and in_str: escape_next = True; continue
                if ch == '"': in_str = not in_str; continue
                if in_str: continue
                if ch == "{": depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0: end = i + 1; break
            if end == -1: raise ValueError("unclosed JSON")
            state = json.loads(raw[:end])
        except Exception as e:
            logger.warning(f"[BMS] Failed to parse __INITIAL_STATE__: {e}")
            return SourceSnapshot(source="bookmyshow", movie_name=movie_name, url=url, theatres=[])

        theatres: list[ShowEntry] = []

        try:
            sfa = state.get("showtimesFunctionalApi", {})
            for qk, qv in sfa.get("queries", {}).items():
                if not isinstance(qv, dict): continue
                raw_data = qv.get("data", {})
                idata = raw_data.get("data", raw_data) if isinstance(raw_data, dict) else {}
                widgets = idata.get("showtimeWidgets", []) if isinstance(idata, dict) else []
                for w in widgets:
                    if not isinstance(w, dict): continue
                    for g in w.get("data", []):
                        for card in (g.get("data", []) if isinstance(g, dict) else []):
                            if not isinstance(card, dict): continue
                            add = card.get("additionalData", {})
                            name = (add.get("venueName") or card.get("title") or "").strip()
                            if not name or len(name) < 3: continue
                            shows = [st.get("title", "").strip() for st in card.get("showtimes", []) if isinstance(st, dict) and st.get("title")]
                            formats: set[str] = set()
                            for st in card.get("showtimes", []):
                                if isinstance(st, dict):
                                    for fmt in _extract_formats(st.get("screenAttr", "")):
                                        formats.add(fmt)
                            url_fmt = _format_from_url(url)
                            if url_fmt: formats.add(url_fmt)
                            theatres.append(ShowEntry(theatre=name, shows=shows, formats=sorted(formats), booking_open=bool(shows), booking_url=url if shows else None))
        except Exception as e:
            logger.warning(f"[BMS] showtimesFunctionalApi parse error: {e}")

        seen = set(); unique = []
        for t in theatres:
            k = t.theatre.lower().strip()
            if k not in seen: seen.add(k); unique.append(t)

        logger.info(f"[BMS JSON] Parsed {len(unique)} theatre(s)")
        return SourceSnapshot(source="bookmyshow", movie_name=movie_name, url=url, theatres=unique)

    # ── DOM fallback ───────────────────────────────────────────────────────────
    def _parse_dom(self, html: str, url: str, movie_name: str) -> SourceSnapshot:
        soup = BeautifulSoup(html, "lxml")
        theatres: list[ShowEntry] = []
        btns = soup.find_all(lambda t: t.name and any(_SHOWTIME_BTN_CLASS in c for c in t.get("class", [])))
        seen: set[int] = set(); containers: list = []
        for btn in btns:
            el = btn.parent
            for _ in range(15):
                if el is None: break
                anchor = el.find("a", href=re.compile(r"/cinemas/|bookmyshow\.com/cinemas/"))
                if anchor and len(anchor.get_text(strip=True)) > 3:
                    eid = id(el)
                    if eid not in seen: seen.add(eid); containers.append(el)
                    break
                el = el.parent
        for ctr in containers:
            anchor = ctr.find("a", href=re.compile(r"/cinemas/|bookmyshow\.com/cinemas/"))
            name = anchor.get_text(strip=True) if anchor else ""
            if not name: continue
            shows, formats = [], set()
            for btn in ctr.find_all(lambda t: t.name and any(_SHOWTIME_BTN_CLASS in c for c in t.get("class", []))):
                text = btn.get_text(separator=" ", strip=True)
                m = _TIME_RE.search(text)
                if m: shows.append(f"{m.group(1)} {m.group(2).upper()}")
                for fmt in _extract_formats(text): formats.add(fmt)
            url_fmt = _format_from_url(url)
            if url_fmt: formats.add(url_fmt)
            theatres.append(ShowEntry(theatre=name, shows=sorted(set(shows)), formats=sorted(formats), booking_open=bool(shows), booking_url=url if shows else None))
        logger.info(f"[BMS DOM] Parsed {len(theatres)} theatre(s)")
        return SourceSnapshot(source="bookmyshow", movie_name=movie_name, url=url, theatres=theatres)


def _extract_formats(text: str) -> list[str]:
    t = text.upper()
    out = []
    if "IMAX" in t: out.append("IMAX")
    elif "4DX" in t: out.append("4DX")
    elif "EPIQ" in t: out.append("EPIQ")
    elif "DOLBY" in t or "ATMOS" in t: out.append("DOLBY ATMOS")
    elif "LASER" in t: out.append("LASER")
    if "3D" in t: out.append("3D")
    if "4K" in t: out.append("4K")
    return out


def _format_from_url(url: str) -> str | None:
    u = url.lower()
    if "epiq" in u: return "EPIQ 3D"
    if "imax" in u: return "IMAX"
    if "4dx" in u: return "4DX"
    return None
