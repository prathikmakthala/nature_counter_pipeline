# config.py
# Local entry point: edit values below, then run `python config.py`

from pipeline_project import run_once

# --- Your hardcoded values (per your request) ---
MONGO_URI       = ""
SPREADSHEET_ID  = ""

# Where the service account JSON lives on your machine.
# If you'd rather embed the JSON as a GitHub Secret later, leave DRIVE_SA_JSON="" and keep this path.
SA_JSON_PATH    = ""

# Inline JSON is optional (useful for CI). Leave as "" for local runs.
DRIVE_SA_JSON   = ""  # put the *full* JSON string here only if you prefer inline use

# Output file name and mode
# OUTPUT_NAME     = "NC-DA-Journal-Data.xlsx" # No longer needed for sheets
RUN_MODE        = "inc"   # "full" (one-time backfill) or "inc" (incremental append)
SHEET_NAME      = "sheet1"

if __name__ == "__main__":
    cfg = {
        "MONGO_URI": MONGO_URI,
        "SPREADSHEET_ID": SPREADSHEET_ID,
        # "OUTPUT_NAME": OUTPUT_NAME,
        "RUN_MODE": RUN_MODE,
        "SA_JSON_PATH": SA_JSON_PATH,
        "DRIVE_SA_JSON": DRIVE_SA_JSON,
        "SHEET_NAME": SHEET_NAME,
    }
    run_once(cfg)
