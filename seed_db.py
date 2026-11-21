"""
Seed the SQLite database with the three user movie URLs so they are automatically monitored.
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from app import database as db
from app.models import MovieConfig

async def run():
    # Make sure DB and tables exist
    await db.init_db()
    
    # Get existing movies to avoid duplicate seeding
    existing = await db.get_all_movies()
    existing_names = {m.name for m in existing}
    
    movies_to_seed = [
        MovieConfig(
            name="Avengers: Doomsday (English)",
            city="Chennai",
            bms_url="https://in.bookmyshow.com/movies/chennai/avengers-doomsday/buytickets/ET00526306/20261218?etCodes=*&language=english&refEventCode=ET00526306",
            district_url="https://www.district.in/movies/avengers-doomsday-movie-tickets-in-chennai-MV176735?frmtid=Mm5dpqTqJz&fromdate=2026-12-18",
            interval=300,
            enabled=True
        ),
        MovieConfig(
            name="Seven Screen Cinemas: Kilambakkam (Dune 3 & Doomsday)",
            city="Chennai",
            bms_url="",
            district_url="https://www.district.in/movies/seven-screen-s-cinemas-kilambakkam-chennai-CD1102122?fromdate=2026-12-18",
            interval=300,
            enabled=True
        )
    ]
    
    for movie in movies_to_seed:
        if movie.name not in existing_names:
            movie_id = await db.add_movie(movie)
            print(f"Added movie: {movie.name} with ID {movie_id}")
        else:
            print(f"Movie already exists in DB: {movie.name}")

if __name__ == "__main__":
    asyncio.run(run())
