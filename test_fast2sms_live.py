"""
Quick Fast2SMS live test (SMS and Voice Call) using Fast2SMS API.
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from app.notifier.fast2sms import Fast2SMSNotifier
from app.models import ChangeEvent

async def run():
    if sys.stdout.encoding and sys.stdout.encoding.lower() != 'utf-8':
        import io
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

    notifier = Fast2SMSNotifier()

    event = ChangeEvent(
        source="bookmyshow",
        type="booking_open",
        theatre="PVR Palazzo Chennai",
        detail="Booking is now OPEN for Avengers: Doomsday!",
        booking_url="https://in.bookmyshow.com/movies/chennai/avengers-doomsday/buytickets/ET00526306/20261218"
    )

    print("Sending live test SMS/Call via Fast2SMS...")
    success = await notifier.send(
        movie_name="Avengers: Doomsday",
        changes=[event],
        source_url="https://in.bookmyshow.com"
    )

    if success:
        print("[SUCCESS] Fast2SMS alert sent successfully!")
    else:
        print("[FAIL] Failed to send Fast2SMS alert. Check credentials in .env.")

if __name__ == "__main__":
    asyncio.run(run())
