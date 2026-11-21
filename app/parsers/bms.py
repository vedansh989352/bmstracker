"""
ShowPulser – BookMyShow Parser
Strategy: parse window.__INITIAL_STATE__ JSON injected into the HTML.

Data path:
  state["showtimesFunctionalApi"]["queries"][qk]["data"]
  or state["showtimesByEvent"]["showDates"][DATE]
  → showtimeWidgets → groupList / venue-card
  → card["showtimes"] → list of showtime objects
  → venueName from card or static venues dictionary

If no __INITIAL_STATE__ or parse fails, fall back to robust DOM scraping.
"""
from __future__ import annotations

import asyncio
import json
import re
from typing import Any

from bs4 import BeautifulSoup
from loguru import logger

from app.browser import browser_pool
from app.models import ShowEntry, SourceSnapshot
from app.parsers.base import BaseParser
from app.retry import with_retry


_BMS_STATE_RE = re.compile(r"window\.__INITIAL_STATE__\s*=\s*(\{.*)", re.DOTALL)
_TIME_RE = re.compile(r"\b(\d{1,2}:\d{2})\s*(AM|PM)\b", re.I)
_SHOWTIME_BTN_CLASS = "sc-1vhizuf-1"
_FORMAT_KEYWORDS = ["IMAX", "DOLBY", "ATMOS", "4DX", "3D", "LASER", "EPIQ", "4K", "ICE", "MX4D"]


class BookMyShowParser(BaseParser):

    @property
    def source_name(self) -> str:
        return "bookmyshow"

    @with_retry(max_attempts=3, base_delay=3.0, backoff=2.0)
    async def fetch(self, url: str, movie_name: str) -> SourceSnapshot:
        logger.info(f"[BMS] Fetching: {url}")

        async with browser_pool.new_page() as page:
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
                await asyncio.sleep(2.5)
            except Exception as e:
                logger.debug(f"[BMS] Load warning: {e}")
            html = await page.content()

        # Strategy 1: parse __INITIAL_STATE__ JSON
        snap = self._parse_state_json(html, url, movie_name)
        if snap.theatres:
            return snap

        logger.warning("[BMS] __INITIAL_STATE__ gave no theatres – falling back to DOM")
        return self._parse_dom(html, url, movie_name)

    # ── __INITIAL_STATE__ parser ──────────────────────────────────────────────
    def _parse_state_json(self, html: str, url: str, movie_name: str) -> SourceSnapshot:
        m = _BMS_STATE_RE.search(html)
        if not m:
            logger.debug("[BMS] __INITIAL_STATE__ not found in HTML")
            return SourceSnapshot(source="bookmyshow", movie_name=movie_name, url=url, theatres=[])

        try:
            raw = m.group(1)
            # Find the end of the JSON object using balanced brace counting
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
                raise ValueError("Could not find end of JSON object")
            state = json.loads(raw[:end])

        except Exception as e:
            logger.warning(f"[BMS] Failed to parse __INITIAL_STATE__: {e}")
            return SourceSnapshot(source="bookmyshow", movie_name=movie_name, url=url, theatres=[])

        theatres: list[ShowEntry] = []

        # Build a venue lookup map from any static venue data in state
        venue_lookup: dict[str, str] = {}
        try:
            for k, v in state.items():
                if isinstance(v, dict):
                    for subk, subv in v.items():
                        if isinstance(subv, dict) and "venues" in subv and isinstance(subv["venues"], dict):
                            for vcode, vinfo in subv["venues"].items():
                                if isinstance(vinfo, dict) and "venueName" in vinfo:
                                    venue_lookup[vcode] = vinfo["venueName"]
        except Exception:
            pass

        # Strategy 1A: Parse showtimesFunctionalApi (RTK Query format)
        try:
            sfa = state.get("showtimesFunctionalApi", {})
            queries = sfa.get("queries", {})
            for qk, qv in queries.items():
                if not isinstance(qv, dict):
                    continue
                if "fetchPrimaryDynamic" in qk or "fetchStaticShowtimes" in qk:
                    raw_data = qv.get("data", {})
                    idata = raw_data.get("data", {}) if isinstance(raw_data, dict) and "data" in raw_data else raw_data
                    widgets = (
                        (idata.get("showtimeWidgets") if isinstance(idata, dict) else None)
                        or (raw_data.get("showtimeWidgets") if isinstance(raw_data, dict) else None)
                        or []
                    )
                    for w in widgets:
                        if not isinstance(w, dict):
                            continue
                        for g in w.get("data", []):
                            if not isinstance(g, dict):
                                continue
                            cards = g.get("data", []) if isinstance(g.get("data"), list) else [g]
                            for card in cards:
                                if not isinstance(card, dict):
                                    continue
                                add_data = card.get("additionalData", {})
                                venue_code = add_data.get("venueCode", "")
                                name = (
                                    add_data.get("venueName")
                                    or venue_lookup.get(venue_code)
                                    or card.get("title")
                                    or card.get("name")
                                    or ""
                                ).strip()

                                if not name or len(name) < 3:
                                    continue

                                raw_shows = []
                                if "showtimes" in card and isinstance(card["showtimes"], list):
                                    raw_shows.extend(card["showtimes"])
                                if "showtimesSections" in card and isinstance(card["showtimesSections"], list):
                                    for sec in card["showtimesSections"]:
                                        if isinstance(sec, dict) and "showtimes" in sec:
                                            raw_shows.extend(sec["showtimes"])

                                shows: list[str] = []
                                formats: set[str] = set()

                                for st_item in raw_shows:
                                    if not isinstance(st_item, dict):
                                        continue
                                    title = (
                                        st_item.get("title")
                                        or st_item.get("additionalData", {}).get("showTime")
                                        or ""
                                    ).strip()
                                    if title:
                                        shows.append(title)
                                    screen_attr = (
                                        st_item.get("screenAttr")
                                        or st_item.get("additionalData", {}).get("attributes")
                                        or ""
                                    ).strip()
                                    for fmt in _extract_formats(screen_attr):
                                        formats.add(fmt)

                                url_fmt = _format_from_url(url)
                                if url_fmt:
                                    formats.add(url_fmt)

                                booking_open = bool(shows)
                                theatres.append(ShowEntry(
                                    theatre=name,
                                    shows=shows,
                                    formats=sorted(formats),
                                    booking_open=booking_open,
                                    booking_url=url if booking_open else None,
                                ))
        except Exception as e:
            logger.warning(f"[BMS] Error parsing showtimesFunctionalApi: {e}")

        # Strategy 1B: Fallback to showtimesByEvent (Legacy format) if empty
        if not theatres:
            try:
                stbe = state.get("showtimesByEvent", {})
                show_dates = stbe.get("showDates", {})
                for date_key, date_data in show_dates.items():
                    if not isinstance(date_data, dict):
                        continue
                    static_venues: dict = (
                        date_data.get("primaryStatic", {})
                        .get("data", {})
                        .get("venues", {})
                    )
                    dynamic_widgets = (
                        date_data.get("dynamic", {})
                        .get("data", {})
                        .get("showtimeWidgets", [])
                    )
                    gl_widget = next(
                        (w for w in dynamic_widgets if isinstance(w, dict) and w.get("type") == "groupList"),
                        None
                    )
                    if not gl_widget:
                        continue
                    for group in gl_widget.get("data", []):
                        if not isinstance(group, dict):
                            continue
                        for card in group.get("data", []):
                            if not isinstance(card, dict) or card.get("type") != "venue-card":
                                continue
                            venue_code = card.get("additionalData", {}).get("venueCode", "")
                            name = (
                                static_venues.get(venue_code, {}).get("venueName")
                                or venue_lookup.get(venue_code)
                                or card.get("title")
                                or ""
                            ).strip()
                            if not name or len(name) < 3:
                                continue
                            raw_shows = card.get("showtimes", [])
                            shows: list[str] = []
                            formats: set[str] = set()
                            for st in raw_shows:
                                if not isinstance(st, dict):
                                    continue
                                title = st.get("title", "").strip()
                                if title:
                                    shows.append(title)
                                screen_attr = st.get("screenAttr", "").strip()
                                for fmt in _extract_formats(screen_attr):
                                    formats.add(fmt)
                            url_fmt = _format_from_url(url)
                            if url_fmt:
                                formats.add(url_fmt)
                            theatres.append(ShowEntry(
                                theatre=name,
                                shows=shows,
                                formats=sorted(formats),
                                booking_open=bool(shows),
                                booking_url=url if bool(shows) else None,
                            ))
            except Exception as e:
                logger.warning(f"[BMS] Error walking showtimesByEvent: {e}")

        # Deduplicate
        seen: set[str] = set()
        unique_theatres: list[ShowEntry] = []
        for t in theatres:
            k = t.theatre.lower().strip()
            if k not in seen:
                seen.add(k)
                unique_theatres.append(t)

        logger.info(f"[BMS JSON] Parsed {len(unique_theatres)} theatre(s)")
        return SourceSnapshot(source="bookmyshow", movie_name=movie_name, url=url, theatres=unique_theatres)

    # ── DOM fallback ──────────────────────────────────────────────────────────
    def _parse_dom(self, html: str, url: str, movie_name: str) -> SourceSnapshot:
        soup = BeautifulSoup(html, "lxml")
        theatres: list[ShowEntry] = []

        # Find any candidate showtime elements (buttons, links, spans, divs with time text)
        btns = soup.find_all(
            lambda t: t.name in ("a", "button", "div", "span")
            and (
                any(_SHOWTIME_BTN_CLASS in c or "showtime" in c.lower() for c in t.get("class", []))
                or (_TIME_RE.search(t.get_text()) and len(t.get_text().strip()) <= 15)
            )
        )
        logger.debug(f"[BMS DOM] Showtime candidates: {len(btns)}")

        # Group by container (nearest common parent that contains a cinema anchor or venue title)
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

            shows, formats = [], set()
            for btn in ctr.find_all(
                lambda t: t.name in ("a", "button", "div", "span")
                and (
                    any(_SHOWTIME_BTN_CLASS in c or "showtime" in c.lower() for c in t.get("class", []))
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

        # Deduplicate
        seen_names: set[str] = set()
        unique_theatres: list[ShowEntry] = []
        for t in theatres:
            k = t.theatre.lower().strip()
            if k not in seen_names:
                seen_names.add(k)
                unique_theatres.append(t)

        logger.info(f"[BMS DOM] Parsed {len(unique_theatres)} theatre(s)")
        return SourceSnapshot(source="bookmyshow", movie_name=movie_name, url=url, theatres=unique_theatres)


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
