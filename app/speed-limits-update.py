# TODO
# CREATE EXTENSION postgis; if not already done

import asyncio
import aiohttp
import os
import psycopg2
from psycopg2.extras import execute_values
from collections import Counter

# Database connection
conn = psycopg2.connect(
    dbname=os.getenv("TESLAMATE_DB", "teslamate"),
    user=os.getenv("TESLAMATE_DBUSER", "teslamate"),
    password=os.getenv("TESLAMATE_DBPASSWORD", "your_password"),
    host=os.getenv("TESLAMATE_DBHOST", "your_host")
)
cur = conn.cursor()

# Create table and index if they don’t exist
cur.execute("""
    CREATE TABLE IF NOT EXISTS speed_limits (
        position_id BIGINT PRIMARY KEY REFERENCES positions(id) ON DELETE CASCADE,
        way_id BIGINT,
        speed_limit INTEGER,
        road_name TEXT,
        inferred BOOLEAN DEFAULT FALSE,
        last_updated TIMESTAMP DEFAULT NOW()
    );
""")
cur.execute("""
    CREATE INDEX IF NOT EXISTS idx_speed_limits_way_id ON speed_limits(way_id);
""")
conn.commit()

# Overpass settings with environment variable overrides
OVERPASS_URL = os.getenv("TESLAMATE_OVERPASS_API_URL", "https://overpass.private.coffee/api/interpreter")
CONCURRENCY_LIMIT = int(os.getenv("TESLAMATE_OVERPASS_API_CONCURRENCY", 15))
BATCH_SIZE = int(os.getenv("TESLAMATE_OVERPASS_API_BATCH_SIZE", 45))
DELAY_BETWEEN_BATCHES = float(os.getenv("TESLAMATE_OVERPASS_API_BATCH_DELAY", 0.2))
MAX_RETRIES = int(os.getenv("TESLAMATE_OVERPASS_API_RETRIES", 3))

# Track progress and duplicates
road_stats = Counter()
total_processed = 0

async def fetch_speed_limit(session, pos_id, lat, lon, retry_count=0):
    query = f"""
    [out:json];
    way(around:10,{lat},{lon})["highway"]
      ["highway"!~"footway|path|cycleway|pedestrian"];
    out center;
    """
    try:
        async with session.post(OVERPASS_URL, data=query) as response:
            if response.status == 429:
                if retry_count < MAX_RETRIES:
                    delay = (2 ** retry_count) + 0.1
                    print(f"Position {pos_id}: HTTP 429, retrying in {delay}s (attempt {retry_count + 1}/{MAX_RETRIES})")
                    await asyncio.sleep(delay)
                    return await fetch_speed_limit(session, pos_id, lat, lon, retry_count + 1)
                print(f"Position {pos_id}: Max retries reached for HTTP 429, skipping")
                return pos_id, None, None, None, False
            
            if response.status != 200:
                print(f"Position {pos_id}: Overpass error - HTTP {response.status}")
                return pos_id, None, None, None, False
            
            data = await response.json()
            ways = data.get("elements", [])
            print(f"Position {pos_id}: Found {len(ways)} ways")
            
            speed_limit = None
            way_id = None
            road_name = None
            inferred = False
            
            if ways:
                way = ways[0]
                way_id = way.get("id")
                tags = way.get("tags", {})
                road_name = tags.get("name", tags.get("ref", "Unknown"))
                print(f"Position {pos_id}: Processing way_id {way_id} with tags: {tags}")
                
                if "maxspeed" in tags:
                    speed_limit_str = tags["maxspeed"]
                    try:
                        if "mph" in speed_limit_str.lower():
                            speed_limit = int(round(float(speed_limit_str.replace(" mph", "")) * 1.60934))
                        else:
                            speed_limit = int(speed_limit_str.replace(" km/h", ""))
                        inferred = False
                        print(f"Position {pos_id}: Using explicit maxspeed: {speed_limit} km/h")
                    except ValueError:
                        print(f"Position {pos_id}: Invalid maxspeed format: {speed_limit_str}, falling back to inference")
                
                if not speed_limit:
                    highway_type = tags.get("highway", "").lower()
                    if highway_type == "residential":
                        speed_limit = 50
                        inferred = True
                        print(f"Position {pos_id}: Inferred speed_limit=50 km/h for residential")
                    elif highway_type == "unclassified":
                        speed_limit = 80
                        inferred = True
                        print(f"Position {pos_id}: Inferred speed_limit=80 km/h for unclassified")
                    elif highway_type == "service":
                        speed_limit = 50
                        inferred = True
                        print(f"Position {pos_id}: Inferred speed_limit=50 km/h for service")
                    elif highway_type == "construction":
                        speed_limit = 70
                        inferred = True
                        print(f"Position {pos_id}: Inferred speed_limit=70 km/h for construction")
                    elif highway_type == "tertiary":
                        speed_limit = 50
                        inferred = True
                        print(f"Position {pos_id}: Inferred speed_limit=50 km/h for tertiary")
                    else:
                        speed_limit = 80
                        inferred = True
                        print(f"Position {pos_id}: Inferred default speed_limit=80 km/h for highway type: {highway_type}")
                
                if speed_limit:
                    road_stats[(road_name, speed_limit)] += 1
            
            return pos_id, way_id, speed_limit, road_name, inferred
    
    except Exception as e:
        print(f"Position {pos_id}: Error querying Overpass API: {e}")
        return pos_id, None, None, None, False

async def process_batch(session, positions):
    tasks = [fetch_speed_limit(session, pos_id, lat, lon) for pos_id, lat, lon in positions]
    results = await asyncio.gather(*tasks)
    
    insert_data = [
        (pos_id, way_id, speed_limit, road_name, inferred)
        for pos_id, way_id, speed_limit, road_name, inferred in results
        if speed_limit and way_id
    ]
    
    if insert_data:
        try:
            execute_values(
                cur,
                """
                INSERT INTO speed_limits (position_id, way_id, speed_limit, road_name, inferred)
                VALUES %s
                ON CONFLICT (position_id) DO NOTHING;
                """,
                insert_data
            )
            print(f"Batch inserted {len(insert_data)} speed limits")
        except Exception as e:
            print(f"Batch insert failed: {e}")

async def main():
    semaphore = asyncio.Semaphore(CONCURRENCY_LIMIT)
    
    async with aiohttp.ClientSession() as session:
        while True:
            cur.execute("""
                SELECT p.id, p.latitude, p.longitude
                FROM positions p
                LEFT JOIN speed_limits sl ON p.id = sl.position_id
                WHERE sl.position_id IS NULL
                  AND p.speed IS NOT NULL 
                  AND p.speed > 20
                LIMIT %s;
            """, (BATCH_SIZE,))
            positions = cur.fetchall()
            print(f"Found {len(positions)} positions to process.")
            if not positions:
                break
            
            async with semaphore:
                await process_batch(session, positions)
                conn.commit()
                global total_processed
                total_processed += len(positions)
                print(f"Total processed: {total_processed}/18000000")
                await asyncio.sleep(DELAY_BETWEEN_BATCHES)
    
    print("\nRoad Duplication Stats:")
    for (road_name, speed_limit), count in road_stats.most_common():
        print(f"{road_name}: {speed_limit} km/h - {count} entries")

asyncio.run(main())

cur.close()
conn.close()
