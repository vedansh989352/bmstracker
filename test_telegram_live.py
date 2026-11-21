"""
Quick Telegram live test using Telegram Bot API.
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from app.notifier.telegram import TelegramNotifier
from app.models import ChangeEvent

async def run():
    if sys.stdout.encoding and sys.stdout.encoding.lower() != 'utf-8':
        import io
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

    notifier = TelegramNotifier()

    event = ChangeEvent(
        source="bookmyshow",
        type="booking_open",
        theatre="SPI Sathyam Cinemas Chennai",
        detail="Booking is now OPEN!",
        booking_url="https://in.bookmyshow.com/movies/chennai/avengers-doomsday/buytickets/ET00526306/20261218"
    )

    print("Sending live test Telegram notification...")
    success = await notifier.send(
        movie_name="Avengers: Doomsday",
        changes=[event],
        source_url="https://in.bookmyshow.com/movies/chennai/avengers-doomsday/buytickets/ET00526306/20261218"
    )

    if success:
        print("[SUCCESS] Telegram message sent successfully!")
    else:
        print("[FAIL] Failed to send Telegram message. Check TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env.")

if __name__ == "__main__":
    asyncio.run(run())
