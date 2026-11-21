"""
ShowPulser – Fast2SMS Notifier (SMS & Automated Voice Call)
Supports specific theatre filtering so SMS and Calls are only triggered
for selected priority theatres, while Telegram alerts for all theatres.

Fast2SMS Setup:
  1. Register at https://www.fast2sms.com/
  2. Get API key from Dev API section
  3. For Voice Calls (OBD), upload an audio file under Voice Files to get a file_id
  4. Configure in .env:
       FAST2SMS_API_KEY=your_dev_api_key
       FAST2SMS_NUMBERS=9876543210
       FAST2SMS_SPECIFIC_THEATRES=Sathyam, Palazzo, Luxe, PVR
       FAST2SMS_VOICE_FILE_ID=12345 (optional, required for OBD calls)
"""
from __future__ import annotations

import httpx
from loguru import logger

from app.config import settings
from app.models import ChangeEvent
from app.notifier.base import BaseNotifier


class Fast2SMSNotifier(BaseNotifier):
    """Sends SMS and triggers Voice Calls via Fast2SMS for specific theatres."""

    @property
    def name(self) -> str:
        return "fast2sms"

    def _matches_specific_theatres(self, theatre_name: str) -> bool:
        """Check if the given theatre name matches any configured specific theatres."""
        raw_filter = getattr(settings, "fast2sms_specific_theatres", "") or ""
        theatre_filter = [t.strip().lower() for t in raw_filter.split(",") if t.strip()]

        # If no specific filter specified or '*', accept all
        if not theatre_filter or "*" in theatre_filter or "all" in theatre_filter:
            return True

        theatre_lower = theatre_name.lower()
        return any(term in theatre_lower for term in theatre_filter)

    def _clean_numbers(self, raw_numbers: str) -> str:
        """Parse and clean comma-separated phone numbers down to 10-digit format."""
        cleaned = []
        for n in raw_numbers.split(","):
            digits = "".join(filter(str.isdigit, n))
            if digits.startswith("91") and len(digits) == 12:
                digits = digits[2:]
            elif digits.startswith("0") and len(digits) == 11:
                digits = digits[1:]
            if len(digits) == 10:
                cleaned.append(digits)
            else:
                logger.warning(f"[Fast2SMS] Skipping invalid 10-digit number: '{n}' (parsed: '{digits}')")
        return ",".join(cleaned)

    async def send(
        self,
        movie_name: str,
        changes: list[ChangeEvent],
        source_url: str = "",
    ) -> bool:
        api_key = getattr(settings, "fast2sms_api_key", "") or ""
        numbers_raw = getattr(settings, "fast2sms_numbers", "") or ""

        if not api_key or not numbers_raw:
            logger.warning("[Fast2SMS] API key or numbers not configured (FAST2SMS_API_KEY / FAST2SMS_NUMBERS). Skipping.")
            return False

        if not changes:
            return True

        # Filter changes for specific theatres
        specific_changes = [c for c in changes if self._matches_specific_theatres(c.theatre)]

        if not specific_changes:
            logger.info(
                f"[Fast2SMS] Changes detected for other theatres, but none matched "
                f"specific theatre filter '{settings.fast2sms_specific_theatres}'. Skipping SMS/Call."
            )
            return True

        # Filter for high-priority change events (new theatre, new show, booking open)
        priority_events = [c for c in specific_changes if c.type in {"booking_open", "new_theatre", "new_show"}]
        if not priority_events:
            priority_events = specific_changes

        numbers = self._clean_numbers(numbers_raw)
        if not numbers:
            logger.warning("[Fast2SMS] No valid 10-digit recipient numbers found.")
            return False

        success = True

        # ── 1. Send SMS ────────────────────────────────────────────────────────
        if getattr(settings, "fast2sms_enable_sms", True):
            sms_ok = await self._send_sms(api_key, numbers, movie_name, priority_events, source_url)
            if not sms_ok:
                success = False

        # ── 2. Trigger Voice Call ──────────────────────────────────────────────
        if getattr(settings, "fast2sms_enable_call", True):
            call_ok = await self._send_voice_call(api_key, numbers, movie_name, priority_events)
            if not call_ok:
                success = False

        return success

    async def _send_sms(
        self,
        api_key: str,
        numbers: str,
        movie_name: str,
        changes: list[ChangeEvent],
        source_url: str,
    ) -> bool:
        """Send SMS via Fast2SMS bulkV2 endpoint."""
        theatres = list({c.theatre for c in changes})
        theatre_summary = ", ".join(theatres[:3])
        if len(theatres) > 3:
            theatre_summary += f" +{len(theatres)-3} more"

        # Check for booking link
        booking_url = next((c.booking_url for c in changes if c.booking_url), source_url)
        
        # Build concise message
        sms_text = (
            f"ALERT: {movie_name} tickets OPEN/UPDATED at {theatre_summary}! "
            f"Book now: {booking_url or 'Check BMS/District app'}"
        )
        if len(sms_text) > 300:
            sms_text = sms_text[:297] + "..."

        url = "https://www.fast2sms.com/dev/bulkV2"
        headers = {
            "authorization": api_key,
            "Content-Type": "application/json",
        }
        payload = {
            "route": getattr(settings, "fast2sms_route", "q") or "q",
            "message": sms_text,
            "numbers": numbers,
        }

        try:
            async with httpx.AsyncClient(timeout=20) as client:
                resp = await client.post(url, headers=headers, json=payload)
                data = resp.json()
                if resp.status_code == 200 and data.get("return") is True:
                    logger.info(f"[Fast2SMS SMS] ✓ Message sent successfully to {numbers}: {data.get('message')}")
                    return True
                else:
                    logger.error(f"[Fast2SMS SMS] ✗ Failed to send SMS: HTTP {resp.status_code} - {data}")
                    return False
        except Exception as e:
            logger.error(f"[Fast2SMS SMS] ✗ Error sending SMS: {e}")
            return False

    async def _send_voice_call(
        self,
        api_key: str,
        numbers: str,
        movie_name: str,
        changes: list[ChangeEvent],
    ) -> bool:
        """Trigger automated Voice Call (OBD) via Fast2SMS voice bulk endpoint."""
        file_id = getattr(settings, "fast2sms_voice_file_id", "")
        if not file_id:
            logger.warning(
                "[Fast2SMS Call] FAST2SMS_VOICE_FILE_ID not set. "
                "Voice call skipped. (Upload an audio recording to Fast2SMS Voice Files to get a file_id)."
            )
            return True

        url = "https://www.fast2sms.com/dev/voice/bulk"
        headers = {
            "authorization": api_key,
            "Content-Type": "application/json",
        }
        payload = {
            "file_id": str(file_id),
            "numbers": numbers,
        }

        try:
            async with httpx.AsyncClient(timeout=20) as client:
                resp = await client.post(url, headers=headers, json=payload)
                data = resp.json()
                if resp.status_code == 200 and data.get("return") is True:
                    logger.info(f"[Fast2SMS Call] ✓ Voice call campaign triggered to {numbers} (file_id: {file_id})")
                    return True
                else:
                    logger.error(f"[Fast2SMS Call] ✗ Failed to trigger voice call: HTTP {resp.status_code} - {data}")
                    return False
        except Exception as e:
            logger.error(f"[Fast2SMS Call] ✗ Error triggering voice call: {e}")
            return False
