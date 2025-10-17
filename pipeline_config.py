# config.py
# Local entry point: edit values below, then run `python config.py`

from pipeline_project import run_once

# --- Your hardcoded values (per your request) ---
MONGO_URI       = "mongodb+srv://dbadmin:natureCounter%401998@nature-counter-server-c.n8xv09r.mongodb.net/NC_dev_db?appName=Nature-Counter-Server-Cluster-1"
DRIVE_FOLDER_ID = "1qOEBKw0lngpPhApOwkDe-t6M8yFTs29r"

# Where the service account JSON lives on your machine.
# If you'd rather embed the JSON as a GitHub Secret later, leave DRIVE_SA_JSON="" and keep this path.
SA_JSON_PATH    = "/Users/prathikmakthala/Downloads/my-pipeline-test-040bbc5bc385.json"

# Inline JSON is optional (useful for CI). Leave as "" for local runs.
DRIVE_SA_JSON   = ""  # put the *full* JSON string here only if you prefer inline use

# Output file name and mode
OUTPUT_NAME     = "NC-DA-Journal-Data.xlsx"
RUN_MODE        = "inc"   # "full" (one-time backfill) or "inc" (incremental append)

if __name__ == "__main__":
    cfg = {
        "MONGO_URI": MONGO_URI,
        "DRIVE_FOLDER_ID": DRIVE_FOLDER_ID,
        "OUTPUT_NAME": OUTPUT_NAME,
        "RUN_MODE": RUN_MODE,
        "SA_JSON_PATH": SA_JSON_PATH,
        "DRIVE_SA_JSON": DRIVE_SA_JSON,
    }
    run_once(cfg)
