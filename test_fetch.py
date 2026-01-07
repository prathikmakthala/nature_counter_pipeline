# test_fetch.py
# A local script to test the data fetching and cleaning logic of the pipeline.

import os
from pymongo import MongoClient
from pipeline_project import fetch, clean, DB_NAME

# --- CONFIGURATION ---
# The only secret needed for this local test.
MONGO_URI = "mongodb+srv://dbadmin:natureCounter%401998@nature-counter-server-c.n8xv09r.mongodb.net/NC_dev_db?appName=Nature-Counter-Server-Cluster-1"
OUTPUT_FILE = "local_output.csv"

def run_local_test():
    """
    Fetches all data from MongoDB, cleans it, and saves it to a local CSV file.
    """
    print("--- Starting Local Pipeline Test ---")
    
    # 1. Connect to MongoDB
    try:
        print("Connecting to MongoDB...")
        client = MongoClient(MONGO_URI, tz_aware=True)
        client.admin.command("ping")
        db = client[DB_NAME]
        print("MongoDB connection successful.")
    except Exception as e:
        print(f"Mongo connection failed. Check MONGO_URI. Details: {e}")
        return

    # 2. Fetch data (run in "full" mode by passing no watermark)
    print("Fetching data from the database...")
    raw_data, _ = fetch(db, last_oid=None)
    if raw_data is None or raw_data.empty:
        print("No data found in the database.")
        return
    print(f"Fetched {len(raw_data)} records.")

    # 3. Clean the data
    print("Cleaning and transforming data...")
    cleaned_data = clean(raw_data)
    print("Data cleaning complete.")

    # 4. Save to local CSV file
    try:
        print(f"Saving output to {OUTPUT_FILE}...")
        cleaned_data.to_csv(OUTPUT_FILE, index=False)
        print(f"✅ Success! Output saved to {os.path.abspath(OUTPUT_FILE)}")
    except Exception as e:
        print(f"Failed to save file. Error: {e}")

if __name__ == "__main__":
    run_local_test()
