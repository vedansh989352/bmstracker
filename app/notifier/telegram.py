"""
ShowPulser – Telegram Notifier
Sends notifications and status reports via python-telegram-bot.
Uses HTML mode for status reports with automatic plain-text fallback.
"""
from __future__ import annotations

import html
from loguru import logger
from telegram import Bot
from telegram.constants import ParseMode

from app.config import settings
from app.models import ChangeEvent, SourceSnapshot
from app.notifier.base import BaseNotifier


_ICON_MAP = {
    "new_theatre": "🏛️",
    "theatre_removed": "❌",
    "new_show": "🚨",
    "show_removed": "🗑️",
    "new_format": "🎞️",
    "booking_open": "🟢",
}

_SOURCE_ICON = {
    "bookmyshow": "🎟 BookMyShow",
    "district": "📍 District",
}


class TelegramNotifier(BaseNotifier):
    """Sends rich notifications via Telegram Bot API."""

    @property
    def name(self) -> str:
        return "telegram"

    async def send(
        self,
        movie_name: str,
        changes: list[ChangeEvent],
        source_url: str = "",
    ) -> bool:
        if not settings.telegram_bot_token or not settings.telegram_chat_id:
            logger.warning("[Telegram] Token or Chat ID not configured. Skipping.")
            return False

        if not changes:
            return True

        message = self._build_message(movie_name, changes, source_url)
        bot = Bot(token=settings.telegram_bot_token)

        try:
            await bot.send_message(
                chat_id=settings.telegram_chat_id,
                text=message,
                parse_mode=ParseMode.MARKDOWN_V2,
                disable_web_page_preview=False,
            )
            logger.info(f"[Telegram] Notification sent for '{movie_name}' ({len(changes)} changes)")
            return True
        except Exception as e:
            logger.warning(f"[Telegram] MarkdownV2 notification failed: {e}. Falling back to plain text...")
            try:
                plain_msg = self._build_plain_changes(movie_name, changes, source_url)
                await bot.send_message(
                    chat_id=settings.telegram_chat_id,
                    text=plain_msg,
                    parse_mode=None,
                    disable_web_page_preview=False,
                )
                logger.info(f"[Telegram] Notification sent as plain text for '{movie_name}'")
                return True
            except Exception as e2:
                logger.error(f"[Telegram] Failed to send notification: {e2}")
                return False

    def _build_message(
        self,
        movie_name: str,
        changes: list[ChangeEvent],
        source_url: str,
    ) -> str:
        lines: list[str] = []
        safe_name = _escape(movie_name)
        lines.append(f"🎬 *{safe_name} — Show Update*")
        lines.append("")

        sources: dict[str, list[ChangeEvent]] = {}
        for c in changes:
            sources.setdefault(c.source, []).append(c)

        for source, src_changes in sources.items():
            src_icon = _SOURCE_ICON.get(source, "📽")
            booking_url = next(
                (c.booking_url for c in src_changes if c.booking_url),
                source_url
            )
            src_label = _escape(self._format_source_label(source, booking_url))
            lines.append(f"{src_icon} *{src_label}*")
            lines.append("")

            grouped = self._group_changes_by_theatre(src_changes)
            for theatre, t_changes in grouped.items():
                lines.append(f"  🏛 *{_escape(theatre)}*")

                for c in t_changes:
                    if c.type == "new_show":
                        lines.append("    🚨 *NEW SHOW ADDED*")
                        if c.before:
                            lines.append(f"    • *Before:* {_escape(c.before)}")
                        if c.after:
                            after_formatted = _escape(c.after)
                            if c.new_items:
                                shows = [s.strip() for s in c.after.split(",")]
                                highlighted = [f"__*{_escape(s)}*__" if s in c.new_items else _escape(s) for s in shows]
                                after_formatted = ", ".join(highlighted)
                            lines.append(f"    • *After:* {after_formatted}")
                        else:
                            lines.append(f"    • {_escape(c.detail)}")

                    elif c.type == "new_theatre":
                        lines.append("    🏛️ *NEW THEATRE ADDED*")
                        if c.before:
                            lines.append(f"    • *Before:* {_escape(c.before)}")
                        if c.after:
                            after_formatted = _escape(c.after)
                            if c.new_items:
                                shows = [s.strip() for s in c.after.split(",")]
                                highlighted = [f"__*{_escape(s)}*__" if s in c.new_items else _escape(s) for s in shows]
                                after_formatted = ", ".join(highlighted)
                            lines.append(f"    • *After:* {after_formatted}")
                        else:
                            lines.append(f"    • {_escape(c.detail)}")

                    elif c.type == "booking_open":
                        lines.append("    🟢 *BOOKING OPEN!*")

                    else:
                        icon = _ICON_MAP.get(c.type, "📌")
                        detail = _escape(c.detail)
                        lines.append(f"    {icon} {detail}")

                booking_url = next(
                    (c.booking_url for c in t_changes if c.booking_url),
                    None,
                )
                if booking_url:
                    lines.append(f"    🔗 [Book Tickets]({_escape_url(booking_url)})")

                lines.append("")

        if source_url:
            lines.append(f"🌐 [View on site]({_escape_url(source_url)})")

        return "\n".join(lines)

    def _build_plain_changes(self, movie_name: str, changes: list[ChangeEvent], source_url: str) -> str:
        lines = [f"🎬 {movie_name} — Show Update\n"]
        for c in changes:
            lines.append(f"• [{c.source.upper()}] {c.theatre}: {c.detail}")
            if c.booking_url:
                lines.append(f"  Link: {c.booking_url}")
        if source_url:
            lines.append(f"\n🌐 {source_url}")
        return "\n".join(lines)

    # ── Status Report (HTML Mode with Plain-Text Fallback) ───────────────────
    async def send_status_report(
        self,
        movie_name: str,
        snapshots: list[SourceSnapshot],
    ) -> bool:
        if not settings.telegram_bot_token or not settings.telegram_chat_id:
            logger.warning("[Telegram] Token or Chat ID not configured. Skipping.")
            return False

        if not snapshots:
            return True

        bot = Bot(token=settings.telegram_bot_token)
        html_message = self._build_status_message_html(movie_name, snapshots)

        try:
            await bot.send_message(
                chat_id=settings.telegram_chat_id,
                text=html_message,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
            logger.info(f"[Telegram] Status report sent for '{movie_name}' (HTML mode)")
            return True
        except Exception as e:
            logger.warning(f"[Telegram] HTML status report failed: {e}. Retrying as plain text...")
            try:
                plain_message = self._build_status_message_plain(movie_name, snapshots)
                await bot.send_message(
                    chat_id=settings.telegram_chat_id,
                    text=plain_message,
                    parse_mode=None,
                    disable_web_page_preview=True,
                )
                logger.info(f"[Telegram] Status report sent as plain text for '{movie_name}'")
                return True
            except Exception as e2:
                logger.error(f"[Telegram] Failed to send status report even in plain text: {e2}")
                return False

    def _build_status_message_html(
        self,
        movie_name: str,
        snapshots: list[SourceSnapshot],
    ) -> str:
        lines: list[str] = []
        safe_header = html.escape(f"{movie_name} — Current Status Report")
        lines.append(f"📊 <b>{safe_header}</b>\n")

        for snap in snapshots:
            src_icon = _SOURCE_ICON.get(snap.source, "📽")
            raw_label = self._format_source_label(snap.source, snap.url)
            src_label = html.escape(raw_label)
            lines.append(f"{src_icon} <b>{src_label}</b>")
            if snap.url:
                lines.append(f"🌐 <a href=\"{snap.url}\">View Source Link</a>")
            lines.append("")

            if not snap.theatres:
                lines.append("  ⚠️ <i>No theatres currently listing shows.</i>\n")
                continue

            for t in snap.theatres:
                safe_theatre = html.escape(t.theatre)
                lines.append(f"  🏛 <b>{safe_theatre}</b>")
                status_icon = "🟢" if t.booking_open else "🔴"
                status_lbl = "Booking Open" if t.booking_open else "Booking Not Open"
                lines.append(f"    {status_icon} {status_lbl}")
                if t.shows:
                    safe_shows = html.escape(", ".join(t.shows))
                    lines.append(f"    🕐 Shows: {safe_shows}")
                if t.formats:
                    safe_formats = html.escape(", ".join(t.formats))
                    lines.append(f"    🎞 Formats: {safe_formats}")
                if t.booking_url:
                    lines.append(f"    🔗 <a href=\"{t.booking_url}\">Book Tickets</a>")
                lines.append("")

        return "\n".join(lines)

    def _build_status_message_plain(
        self,
        movie_name: str,
        snapshots: list[SourceSnapshot],
    ) -> str:
        lines: list[str] = [f"📊 {movie_name} — Current Status Report\n"]

        for snap in snapshots:
            lines.append(f"[{snap.source.upper()}] {snap.url}")
            if not snap.theatres:
                lines.append("  ⚠️ No theatres currently listing shows.\n")
                continue

            for t in snap.theatres:
                lines.append(f"  🏛 {t.theatre}")
                lines.append("    🟢 Booking Open" if t.booking_open else "    🔴 Booking Not Open")
                if t.shows:
                    lines.append(f"    🕐 Shows: {', '.join(t.shows)}")
                if t.formats:
                    lines.append(f"    🎞 Formats: {', '.join(t.formats)}")
                if t.booking_url:
                    lines.append(f"    🔗 {t.booking_url}")
                lines.append("")

        return "\n".join(lines)


def _escape(text: str) -> str:
    """Escape special characters for Telegram MarkdownV2."""
    special = r"\_*[]()~`>#+-=|{}.!"
    return "".join(f"\\{c}" if c in special else c for c in str(text))


def _escape_url(url: str) -> str:
    """Escape parentheses in URLs for MarkdownV2 links."""
    return url.replace("(", "%28").replace(")", "%29")
