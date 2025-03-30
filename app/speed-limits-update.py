# TODO
# CREATE EXTENSION postgis; if not already done

import overpy
import os
import psycopg2
from collections import Counter

# Connect to PostgreSQL
conn = psycopg2.connect(
    dbname=os.getenv("TESLAMATE_DB", "teslamate"),
    user=os.getenv("TESLAMATE_DBUSER", "teslamate"),
    password=os.getenv("TESLAMATE_DBPASSWORD", "your_password"),
    host=os.getenv("TESLAMATE_DBHOST", "your_host")
)
cur = conn.cursor()

# Track duplicates
road_stats = Counter()

# Query OSM
api = overpy.Overpass()
while True:
    cur.execute("""
        SELECT p.id, p.latitude, p.longitude
        FROM positions p
        LEFT JOIN speed_limits sl ON p.id = sl.position_id
        WHERE sl.position_id IS NULL
        LIMIT 50;
    """)
    positions = cur.fetchall()
    print(f"Found {len(positions)} positions to process.")
    if not positions:
        break

    for pos_id, lat, lon in positions:
        print(f"\nProcessing position_id={pos_id}, lat={lat}, lon={lon}")
        
        # Query Overpass within 10m, excluding foot and bicycle ways
        query = f"""
        [out:json];
        way(around:10,{lat},{lon})["highway"]
          ["highway"!~"footway|path|bicycle|cycleway|pedestrian"];
        out center;
        """
        try:
            result = api.query(query)
            print(f"Radius 10m - Number of ways: {len(result.ways)}")
            
            speed_limit = None
            way_id = None
            road_name = None
            inferred = False
            
            if result.ways:
                way = result.ways[0]  # Take the first way
                way_id = way.id
                road_name = way.tags.get("name", "Unknown")
                print(f"Processing way_id {way_id} with tags: {way.tags}")
                
                if "maxspeed" in way.tags:
                    speed_limit_str = way.tags["maxspeed"]
                    try:
                        if "mph" in speed_limit_str.lower():
                            speed_limit = int(round(float(speed_limit_str.replace(" mph", "")) * 1.60934))
                        else:
                            speed_limit = int(speed_limit_str.replace(" km/h", ""))
                        inferred = False
                        print(f"Using explicit maxspeed: {speed_limit} km/h")
                    except ValueError:
                        print(f"Invalid maxspeed format: {speed_limit_str}, falling back to inference")
                
                if not speed_limit:
                    highway_type = way.tags.get("highway", "").lower()
                    if highway_type == "residential":
                        speed_limit = 50
                        inferred = True
                        print("Inferred speed_limit=50 km/h for residential")
                    elif highway_type == "unclassified":
                        speed_limit = 80
                        inferred = True
                        print("Inferred speed_limit=80 km/h for unclassified")
                    else:
                        speed_limit = None
                        print(f"No inference for highway type: {highway_type}")
                
                if speed_limit:
                    print(f"Found way_id={way_id}, speed_limit={speed_limit} km/h, road_name={road_name}, inferred={inferred}")
                    road_stats[(road_name, speed_limit)] += 1
            
            if speed_limit and way_id:
                cur.execute(
                    """
                    INSERT INTO speed_limits (position_id, way_id, speed_limit, road_name, inferred)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (position_id) DO NOTHING;
                    """,
                    (pos_id, way_id, speed_limit, road_name, inferred)
                )
                print(f"Inserted speed limit {speed_limit} km/h for position_id {pos_id} (inferred={inferred})")
            else:
                print(f"No speed limit found or inferred for position_id {pos_id} within 10m")
        
        except Exception as e:
            print(f"Error querying Overpass API for position_id {pos_id}: {e}")
    
    conn.commit()

# Print duplication stats
print("\nRoad Duplication Stats:")
for (road_name, speed_limit), count in road_stats.most_common():
    print(f"{road_name}: {speed_limit} km/h - {count} entries")

cur.close()
conn.close()
