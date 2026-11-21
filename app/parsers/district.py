"""
ShowPulser – District Parser
Parses District.in movie and cinema session pages.

Primary strategy: Parse __NEXT_DATA__ JSON embedded in HTML.
Fallback 1: Intercepted API / XHR responses.
Fallback 2: DOM parsing with BeautifulSoup.
"""
from __future__ import annotations

import asyncio
import json
import re
from typing import Any

from bs4 import BeautifulSoup, Tag
from loguru import logger

from app.browser import browser_pool
from app.models import ShowEntry, SourceSnapshot
from app.parsers.base import BaseParser
from app.retry import with_retry


_DISTRICT_API_PATTERNS = [
    re.compile(r"district\.in/api", re.I),
    re.compile(r"api\.district\.in", re.I),
    re.compile(r"zomato\.com.*event", re.I),
]

_TIME_RE = re.compile(r"\b(\d{1,2}:\d{2})\s*(AM|PM)\b", re.I)

_NOISE_RE = re.compile(
    r"(allows?\s+cancellation|non-cancellable|non cancellable|\d+[\d\.\+]*\s*km(?:\s*away)?"
    r"|filling\s+fast|almost\s+full|available)",
    re.I
)


class DistrictParser(BaseParser):

    @property
    def source_name(self) -> str:
        return "district"

    @with_retry(max_attempts=3, base_delay=3.0, backoff=2.0)
    async def fetch(self, url: str, movie_name: str) -> SourceSnapshot:
        logger.info(f"[District] Fetching: {url}")
        intercepted: list[dict] = []

        async def on_response(resp) -> None:
            if any(p.search(resp.url) for p in _DISTRICT_API_PATTERNS):
                try:
                    if "json" in resp.headers.get("content-type", ""):
                        body = await resp.json()
                        intercepted.append({"url": resp.url, "data": body})
                except Exception:
                    pass

        target_date_match = re.search(r"fromdate=(\d{4}-\d{2}-\d{2})", url)
        target_date = target_date_match.group(1) if target_date_match else None

        async with browser_pool.new_page() as page:
            page.on("response", on_response)
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
                await asyncio.sleep(2.5)
            except Exception as e:
                logger.debug(f"[District] Load warning: {e}")
            final_url = page.url
            html = await page.content()

        # Only check redirection away from date if this is a single cinema venue page (-CD)
        if target_date and ("-cd" in url.lower() or "/cinemas/" in url.lower()):
            final_date_match = re.search(r"fromdate=(\d{4}-\d{2}-\d{2})", final_url)
            if final_date_match and final_date_match.group(1) != target_date:
                logger.info(
                    f"[District] Target date {target_date} is not yet open on this cinema page. "
                    f"District redirected to {final_date_match.group(1)}. Treating as no shows yet."
                )
                return SourceSnapshot(source="district", movie_name=movie_name, url=url, theatres=[])

        # Strategy 1: Parse __NEXT_DATA__ JSON embedded in HTML
        snap = self._parse_next_data(html, url, movie_name, target_date)
        if snap.theatres:
            return snap

        # Strategy 2: Intercepted XHR responses
        if intercepted:
            snap = self._parse_xhr(intercepted, url, movie_name)
            if snap.theatres:
                return snap

        # Strategy 3: Fallback DOM parser
        return self._parse_dom(html, url, movie_name)

    # ── Strategy 1: __NEXT_DATA__ JSON ───────────────────────────────────────
    def _parse_next_data(
        self,
        html: str,
        url: str,
        movie_name: str,
        target_date: str | None = None,
    ) -> SourceSnapshot:
        theatres: list[ShowEntry] = []
        try:
            m = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', html, re.DOTALL)
            if not m:
                return SourceSnapshot(source="district", movie_name=movie_name, url=url, theatres=[])

            data = json.loads(m.group(1))
            movies_state = (
                data.get("props", {})
                .get("pageProps", {})
                .get("initialState", {})
                .get("movies", {})
            )

            # 1. Check movieSessions (e.g. on movie detail / listing pages)
            movie_sessions = movies_state.get("movieSessions", {})
            for session_key, session_obj in movie_sessions.items():
                if not isinstance(session_obj, dict):
                    continue
                arranged = (
                    session_obj.get("arrangedSessions")
                    or session_obj.get("cinemas")
                    or session_obj.get("data")
                    or []
                )
                if isinstance(arranged, list):
                    for item in arranged:
                        t = self._parse_district_json_venue(item, url)
                        if t:
                            theatres.append(t)

            # 2. Check cinemaSessions (e.g. on cinema-specific pages like Seven Screen)
            cinema_sessions = movies_state.get("cinemaSessions", {})
            for session_key, session_obj in cinema_sessions.items():
                if not isinstance(session_obj, dict):
                    continue
                arranged = (
                    session_obj.get("arrangedSessions")
                    or session_obj.get("cinemas")
                    or []
                )
                if isinstance(arranged, list):
                    for item in arranged:
                        t = self._parse_district_json_venue(item, url)
                        if t:
                            theatres.append(t)

                # Also check movies directly inside cinema session
                cinema_meta = session_obj.get("meta", {}).get("cinema", {})
                c_name = cinema_meta.get("name") or cinema_meta.get("label") or ""
                movies_list = session_obj.get("movies") or []
                if c_name and movies_list:
                    shows: list[str] = []
                    formats: set[str] = set()
                    for m_item in movies_list:
                        for s in m_item.get("sessions", []):
                            time_str, fmt_set = self._parse_session_time_and_formats(s)
                            if time_str:
                                shows.append(time_str)
                            formats.update(fmt_set)
                    if shows:
                        theatres.append(ShowEntry(
                            theatre=_clean_name(c_name),
                            shows=sorted(shows),
                            formats=sorted(formats),
                            booking_open=True,
                            booking_url=url,
                        ))

            # Deduplicate by canonical theatre name
            seen: set[str] = set()
            unique_theatres: list[ShowEntry] = []
            for t in theatres:
                k = t.theatre.lower().strip()
                if k not in seen:
                    seen.add(k)
                    unique_theatres.append(t)

            if unique_theatres:
                logger.info(f"[District JSON] Extracted {len(unique_theatres)} theatre(s) from __NEXT_DATA__")
                return SourceSnapshot(source="district", movie_name=movie_name, url=url, theatres=unique_theatres)

        except Exception as e:
            logger.warning(f"[District JSON] Error parsing __NEXT_DATA__: {e}")

        return SourceSnapshot(source="district", movie_name=movie_name, url=url, theatres=[])

    def _parse_district_json_venue(self, item: dict, url: str) -> ShowEntry | None:
        if not isinstance(item, dict):
            return None
        c_name = (
            item.get("entityName")
            or item.get("cinemaInfo", {}).get("name")
            or item.get("data", {}).get("name")
            or item.get("name")
            or ""
        )
        c_name = _clean_name(c_name)
        if not c_name or len(c_name) < 3:
            return None

        raw_sessions = item.get("sessions") or item.get("extraSessions") or []
        shows: list[str] = []
        formats: set[str] = set()

        for s in raw_sessions:
            time_str, fmt_set = self._parse_session_time_and_formats(s)
            if time_str:
                shows.append(time_str)
            formats.update(fmt_set)

        url_fmt = _format_from_url(url)
        if url_fmt:
            formats.add(url_fmt)

        booking_open = bool(shows)
        return ShowEntry(
            theatre=c_name,
            shows=shows,
            formats=sorted(formats),
            booking_open=booking_open,
            booking_url=url if booking_open else None,
        )

    def _parse_session_time_and_formats(self, s: dict) -> tuple[str, set[str]]:
        if not isinstance(s, dict):
            return "", set()

        time_str = ""
        iso = s.get("showTimeISO") or s.get("showTimeLocal") or s.get("showTime") or ""
        if iso:
            try:
                # ISO timestamp e.g. "2026-12-18T13:30:00"
                if "T" in iso:
                    t_part = iso.split("T")[1][:5]
                    hh, mm = map(int, t_part.split(":"))
                    period = "AM" if hh < 12 else "PM"
                    hh_12 = hh if 1 <= hh <= 12 else (hh - 12 if hh > 12 else 12)
                    time_str = f"{hh_12:02d}:{mm:02d} {period}"
            except Exception:
                pass

        if not time_str and s.get("showTime"):
            st = str(s.get("showTime"))
            m = _TIME_RE.search(st)
            if m:
                time_str = f"{m.group(1)} {m.group(2).upper()}"
            else:
                time_str = st

        formats = set()
        fmt = s.get("format") or ""
        if fmt:
            formats.add(str(fmt).upper())

        screen = (s.get("screenName") or "") + " " + (s.get("screenAttr") or "")
        screen_upper = screen.upper()
        if "IMAX" in screen_upper:
            formats.add("IMAX")
        if "4DX" in screen_upper:
            formats.add("4DX")
        if "DOLBY" in screen_upper or "ATMOS" in screen_upper:
            formats.add("DOLBY ATMOS")
        if "LASER" in screen_upper:
            formats.add("LASER")
        if "4K" in screen_upper:
            formats.add("4K")
        if "3D" in screen_upper:
            formats.add("3D")

        return time_str, formats

    # ── Strategy 2: XHR ──────────────────────────────────────────────────────
    def _parse_xhr(self, responses, url, movie_name) -> SourceSnapshot:
        theatres = []
        for item in responses:
            theatres.extend(self._extract_xhr(item.get("data", {})))
        seen, unique = set(), []
        for t in theatres:
            k = t.theatre.lower().strip()
            if k not in seen:
                seen.add(k)
                unique.append(t)
        return SourceSnapshot(source="district", movie_name=movie_name, url=url, theatres=unique)

    def _extract_xhr(self, data: Any) -> list[ShowEntry]:
        out = []
        if isinstance(data, list):
            for x in data:
                out.extend(self._extract_xhr(x))
            return out
        if not isinstance(data, dict):
            return out
        venue_keys = {"venueName", "venue", "cinemaName", "theatreName", "eventVenue"}
        if any(k in data for k in venue_keys):
            e = self._xhr_entry(data)
            if e:
                return [e]
        for v in data.values():
            if isinstance(v, (dict, list)):
                out.extend(self._extract_xhr(v))
        return out

    def _xhr_entry(self, node: dict) -> ShowEntry | None:
        venue = node.get("venue", {})
        name = (isinstance(venue, dict) and venue.get("name", "")) or ""
        if not name:
            name = (node.get("venueName") or node.get("cinemaName") or
                    node.get("theatreName") or "").strip()
        if not name:
            return None
        shows = []
        for s in node.get("sessions", node.get("showtimes", node.get("times", []))):
            t = s.get("startTime", "") or s.get("time", "") if isinstance(s, dict) else str(s)
            if t and "T" in t:
                t = t.split("T")[1][:5]
            if t:
                shows.append(t)
        formats = [f.upper() for f in node.get("formats", []) if isinstance(f, str)]
        return ShowEntry(theatre=name, shows=shows, formats=formats,
                         booking_open=bool(shows), booking_url=None)

    # ── Strategy 3: DOM Fallback ─────────────────────────────────────────────
    def _parse_dom(self, html: str, url: str, movie_name: str) -> SourceSnapshot:
        soup = BeautifulSoup(html, "lxml")
        theatres: list[ShowEntry] = []

        session_lis = soup.select("[class*='movieSessions'], [class*='cdpSessions']")
        logger.debug(f"[District DOM] session items found: {len(session_lis)}")

        for li in session_lis:
            venue_name = self._get_venue_name(li)
            if not venue_name or len(venue_name) < 4:
                continue

            shows, formats = self._get_shows_formats(li)

            li_text = li.get_text(strip=True).lower()
            booking_open = bool(shows) and "coming soon" not in li_text

            url_fmt = _format_from_url(url)
            if url_fmt:
                formats.add(url_fmt)

            theatres.append(ShowEntry(
                theatre=venue_name,
                shows=sorted(set(shows)),
                formats=sorted(formats),
                booking_open=booking_open,
                booking_url=url if booking_open else None,
            ))

        logger.info(f"[District DOM] Parsed {len(theatres)} theatre(s)")
        return SourceSnapshot(source="district", movie_name=movie_name, url=url, theatres=theatres)

    def _get_venue_name(self, li: Tag) -> str:
        col1 = li.select_one("[class*='col1']") or li
        details = col1.select_one("[class*='details'], [class*='venue'], [class*='cinemaName'], h3, h4")
        if details:
            parts = []
            for node in details.children:
                if isinstance(node, str):
                    parts.append(node.strip())
                elif hasattr(node, "name") and node.name not in ("span", "small"):
                    parts.append(node.get_text(strip=True))
            name = " ".join(p for p in parts if p)
            name = _clean_name(name)
            if name and len(name) > 3:
                return name
            raw = details.get_text(separator=" ", strip=True)
            cleaned = _clean_name(raw)
            if cleaned and len(cleaned) > 3:
                return cleaned

        raw = col1.get_text(separator=" ", strip=True)
        return _clean_name(raw)

    def _get_shows_formats(self, li: Tag) -> tuple[list[str], set[str]]:
        shows: list[str] = []
        formats: set[str] = set()

        col2 = li.select_one("[class*='col2']")
        if col2:
            timeblocks = col2.select("li[class*='timeblock']")
        else:
            timeblocks = li.select("li[class*='timeblock']")

        for tb in timeblocks:
            text = tb.get_text(separator=" ", strip=True)
            m = _TIME_RE.search(text)
            if m:
                shows.append(f"{m.group(1)} {m.group(2).upper()}")
            text_upper = text.upper()
            if "IMAX" in text_upper:
                formats.add("IMAX")
            if "4DX" in text_upper:
                formats.add("4DX")
            if "DOLBY" in text_upper or "ATMOS" in text_upper:
                formats.add("DOLBY ATMOS")
            if "LASER" in text_upper:
                formats.add("LASER")
            if "4K" in text_upper:
                formats.add("4K")
            if "3D" in text_upper:
                formats.add("3D")
            if "2D" in text_upper:
                formats.add("2D")

        return shows, formats


def _clean_name(text: str) -> str:
    text = _NOISE_RE.sub("", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _format_from_url(url: str) -> str | None:
    u = url.lower()
    if "imax" in u:
        return "IMAX"
    if "4dx" in u:
        return "4DX"
    if "epiq" in u:
        return "EPIQ"
    if "frmtid=rrfdpndypd" in u:
        return "3D"
    return None
