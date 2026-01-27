# pipeline.py
# Nature Counter: Journals → Google Sheet
# - Core logic only. No hard-coded credentials.
# - Idempotent design: checks destination sheet for existing records before appending.
# - Auto-creates main data sheet if not present.

import os
import json
import logging
import re
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Dict

import pandas as pd
from pymongo import MongoClient
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("nc-pipeline")

DB_NAME = "NC_dev_db"
JOURNALS_COL, USERS_COL, LOCATIONS_COL = "journals", "userdetails", "locations"

US_STATES = set("""
AL AK AZ AR CA CO CT DC DE FL GA HI ID IL IN IA KS KY LA MA MD ME MI MN MO MS MT
NC ND NE NH NJ NM NV NY OH OK OR PA RI SC SD TN TX UT VA VT WA WI WV WY PR GU VI
""".split())

# journal_id is the final column and is used for de-duplication
FINAL_COLS = [
    "Status", "User Name", "User email", "Timestamp", "n_Duration", "End Date Time",
    "n_Name", "City", "State", "Zip", "Country", "n_Place", "n_Lati", "n_Long",
    "n_park_nb", "n_activity", "n_notes", "journal_id"
]
JOURNAL_ID_COL_LETTER = 'R' # Column R is the 18th column, where journal_id resides

def _require(cfg: Dict, key: str) -> str:
    v = cfg.get(key) or os.getenv(key)
    if not v:
        raise SystemExit(f"Missing required setting: {key}")
    return v

def _ensure_sa_file(cfg: Dict) -> str:
    sa_inline = cfg.get("DRIVE_SA_JSON") or os.getenv("DRIVE_SA_JSON")
    sa_path   = cfg.get("SA_JSON_PATH")  or os.getenv("SA_JSON_PATH", "drive-sa.json")
    if sa_inline:
        with open(sa_path, "w") as f:
            f.write(sa_inline)
    if not os.path.exists(sa_path):
        raise SystemExit(f"Service account JSON not found at SA_JSON_PATH: {sa_path}")
    return sa_path

def _google_client(sa_path: str):
    creds = Credentials.from_service_account_file(
        sa_path,
        scopes=["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
    )
    sheets_client = build("sheets", "v4", credentials=creds)
    sa_email = json.load(open(sa_path))["client_email"]
    return sheets_client, sa_email

def ensure_sheet_exists(sheets_client, spreadsheet_id: str, sheet_name: str) -> None:
    """Ensures a given sheet exists in the spreadsheet, creating it if it doesn't."""
    try:
        spreadsheet = sheets_client.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
        sheet_titles = [s['properties']['title'] for s in spreadsheet.get('sheets', [])]

        if sheet_name not in sheet_titles:
            log.info(f"Sheet '{sheet_name}' not found. Creating it...")
            body = {
                'requests': [{
                    'addSheet': {
                        'properties': {'title': sheet_name}
                    }
                }]
            }
            sheets_client.spreadsheets().batchUpdate(spreadsheetId=spreadsheet_id, body=body).execute()
            log.info(f"Sheet '{sheet_name}' created successfully.")
        else:
            log.info(f"Sheet '{sheet_name}' already exists.")

    except HttpError as e:
        log.error(f"An error occurred while ensuring sheet '{sheet_name}' exists: %s", e)
        raise e

def append_to_sheet(sheets_client, spreadsheet_id: str, sheet_name: str, df: pd.DataFrame) -> None:
    try:
        journal_id_col_index = FINAL_COLS.index('journal_id') # 0-based index
        # This function works robustly if FINAL_COLS is used correctly
        # Get all existing journal_ids from the sheet to prevent duplicates.
        range_to_get_ids = f"{sheet_name}!{chr(ord('A') + journal_id_col_index)}2:{chr(ord('A') + journal_id_col_index)}" # Dynamically get column letter for journal_id
        result = sheets_client.spreadsheets().values().get(spreadsheetId=spreadsheet_id, range=range_to_get_ids).execute()
        values = result.get('values', [])
        existing_ids = {item[0] for item in values if item}

        # Filter the DataFrame to only include rows with journal_ids not already in the sheet.
        df_to_append = df[~df['journal_id'].isin(existing_ids)]

        if df_to_append.empty:
            log.info("ℹ️ No new records to append to the sheet.")
            return

        # Check if the sheet is completely empty to decide if we need to add headers
        range_to_check_A1 = f"{sheet_name}!A1:A1"
        result_A1 = sheets_client.spreadsheets().values().get(spreadsheetId=spreadsheet_id, range=range_to_check_A1).execute()
        is_sheet_empty = not result_A1.get('values')

        data_to_write = []
        if is_sheet_empty:
            data_to_write.append(df_to_append.columns.values.tolist())
        
        data_to_write.extend(df_to_append.values.tolist())

        body = {'values': data_to_write}
        sheets_client.spreadsheets().values().append(
            spreadsheetId=spreadsheet_id,
            range=f"{sheet_name}!A1", # Append will find the next empty row
            valueInputOption='USER_ENTERED',
            insertDataOption='INSERT_ROWS',
            body=body
        ).execute()
        log.info("✅ %d new records appended to sheet.", len(df_to_append))
    except HttpError as e:
        log.error("An error occurred writing to the sheet: %s", e)
        raise e

def decide_country(address: str, state: str, loc_country: str) -> str:
    c = (loc_country or "").strip()
    if c:
        if c.upper() in {"US", "USA", "U.S.", "UNITED STATES", "UNITED STATES OF AMERICA"}:
            return "USA"
        return c
    if (state or "").strip().upper() in US_STATES:
        return "USA"
    tokens = re.split(r"[^A-Za-z]+", (address or "").upper())
    tokens = [t for t in tokens if t]
    if any(t in US_STATES for t in tokens):
        return "USA"
    return ""

def agg_pipeline():
    match = {"end_time": {"$ne": None}}
    return [
        {"$match": match},
        {"$addFields": {
            "uid_obj": {"$convert": {"input": "$uid", "to": "objectId", "onError": None, "onNull": None}},
            "loc_obj": {"$convert": {"input": "$locationId", "to": "objectId", "onError": None, "onNull": None}},
        }},
        {"$lookup": {"from": USERS_COL, "let": {"u": "$uid_obj"}, "pipeline": [{"$match": {"$expr": {"$eq": ["$_id", "$$u"]}}}], "as": "u"}},
        {"$unwind": {"path": "$u", "preserveNullAndEmptyArrays": True}},
        {"$lookup": {"from": LOCATIONS_COL, "let": {"l": "$loc_obj"}, "pipeline": [{"$match": {"$expr": {"$eq": ["$_id", "$$l"]}}}], "as": "loc"}},
        {"$unwind": {"path": "$loc", "preserveNullAndEmptyArrays": True}},
        {"$addFields": {
            "lng_from_geojson": {"$cond": [{"$eq": [{"$type": "$loc.coordinates.coordinates"}, "array"]}, {"$arrayElemAt": ["$loc.coordinates.coordinates", 0]}, None]},
            "lat_from_geojson": {"$cond": [{"$eq": [{"$type": "$loc.coordinates.coordinates"}, "array"]}, {"$arrayElemAt": ["$loc.coordinates.coordinates", 1]}, None]},
        }},
        {"$project": {
            "journal_id": {"$toString": "$_id"},
            "Timestamp": "$start_time",
            "End Date Time": "$end_time",
            "n_Duration": {"$round": [{ "$divide": [{ "$subtract": ["$end_time", "$start_time"] }, 60000] }, 0]},
            "User Name": {"$ifNull": ["$u.name", ""]},
            "User email": {"$ifNull": ["$u.email", ""]},
            "n_Name": {"$ifNull": ["$loc.name", ""]},
            "City": {"$ifNull": ["$loc.city", ""]},
            "State": {"$ifNull": ["$loc.stateInitials", {"$ifNull": ["$loc.state", ""]}]} ,
            "Zip": {"$ifNull": ["$loc.zip", ""]},
            "LocCountry": {"$ifNull": ["$loc.country", ""]},
            "Address": {"$ifNull": ["$loc.address", ""]},
            "n_Place": {"$concat": [{"$ifNull": ["$loc.name", ""]}, ", ", {"$ifNull": ["$loc.city", ""]}, " ", {"$ifNull": ["$loc.stateInitials", {"$ifNull": ["$loc.state", ""]}]}]},
            "n_Lati": {"$ifNull": ["$loc.coordinates.lat", {"$ifNull": ["$loc.coordinates.latitude", "$lat_from_geojson"]}]},
            "n_Long": {"$ifNull": ["$loc.coordinates.lng", {"$ifNull": ["$loc.coordinates.longitude", "$lng_from_geojson"]}]},
            "n_park_nb": "$loc.parkNumber",
            "n_activity": {"$ifNull": ["$n_activity", ""]},
            "n_notes": {"$ifNull": ["$n_notes", ""]}
        }},
        {"$sort": {"journal_id": 1}}
    ]

def _to_str_timestamp(x):
    if x is None:
        return ""
    try:
        return x.strftime("%#m/%#d/%y %#I:%M %p")
    except Exception:
        return str(x)

def clean(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame(columns=FINAL_COLS)
    df = df.copy()

    if "Status" not in df.columns:
        df.insert(0, "Status", "")

    address_for_check = df.get("Address", pd.Series([""]*len(df), index=df.index)).astype(str).where(df.get("Address", pd.Series([""]*len(df), index=df.index)).astype(str).str.len() > 0, df.get("n_Place", pd.Series([""]*len(df), index=df.index)).astype(str))
    state_series       = df.get("State", pd.Series([""]*len(df), index=df.index)).astype(str)
    loc_country_series = df.get("LocCountry", pd.Series([""]*len(df), index=df.index)).astype(str)
    df["Country"] = [
        decide_country(addr, st, lc)
        for addr, st, lc in zip(address_for_check, state_series, loc_country_series)
    ]

    df["n_Lati"]  = pd.to_numeric(df.get("n_Lati"), errors="coerce").round(6)
    df["n_Long"]  = pd.to_numeric(df.get("n_Long"), errors="coerce").round(6)
    df["n_Place"] = df.get("n_Place", pd.Series([""]*len(df), index=df.index)).astype(str).str.replace(r"\s{2,}", " ", regex=True).str.strip(" ,")

    df["Timestamp"]     = df["Timestamp"].apply(_to_str_timestamp)
    df["End Date Time"] = df.get("End Date Time", pd.Series([None]*len(df), index=df.index)).apply(_to_str_timestamp)

    if '_id' in df.columns:
        df = df.drop(columns=['_id'])

    for c in FINAL_COLS:
        if c not in df.columns:
            df[c] = ""
    return df[FINAL_COLS]

def fetch(db) -> pd.DataFrame:
    """Fetches all journal documents from the database."""
    docs = list(db[JOURNALS_COL].aggregate(agg_pipeline()))
    return pd.DataFrame(docs)

def send_email_report(cfg: Dict, new_data: pd.DataFrame):
    if new_data.empty:
        log.info("No new data to email.")
        return

    smtp_host = _require(cfg, "SMTP_HOST")
    smtp_port = int(_require(cfg, "SMTP_PORT"))
    smtp_user = _require(cfg, "SMTP_USER")
    smtp_pass = _require(cfg, "SMTP_PASS")
    from_addr = _require(cfg, "EMAIL_FROM")
    to_addrs  = [addr.strip() for addr in _require(cfg, "EMAIL_TO").split(",")]

    subject = f"Nature Counter Daily Report - {pd.to_datetime('today').strftime('%Y-%m-%d')}"

    html_body = f"""
    <html><head><style>
        body {{ font-family: sans-serif; }} table {{ border-collapse: collapse; width: 100%; }}
        th, td {{ border: 1px solid #dddddd; text-align: left; padding: 8px; }}
        tr:nth-child(even) {{ background-color: #f2f2f2; }} th {{ background-color: #4CAF50; color: white; }}
    </style></head><body>
        <h2>Nature Counter Daily Incremental Report</h2>
        <p>Found {len(new_data)} new journal entries.</p>
        {new_data.to_html(index=False, na_rep="")}
    </body></html>
    """

    msg = MIMEMultipart('alternative')
    msg['Subject'] = subject
    msg['From'] = from_addr
    msg['To'] = ", ".join(to_addrs)
    msg.attach(MIMEText(html_body, 'html'))

    try:
        log.info(f"Connecting to SMTP server {smtp_host}:{smtp_port}...")
        with smtplib.SMTP_SSL(smtp_host, smtp_port) as server:
            server.login(smtp_user, smtp_pass)
            server.sendmail(from_addr, to_addrs, msg.as_string())
            log.info(f"Email report sent successfully to: {', '.join(to_addrs)}")
    except Exception as e:
        log.error(f"Failed to send email report. Error: {e}")

def run_once(cfg: Dict = None):
    """
    Runs one end-to-end pass using cfg (dict) or env vars.
    This process is idempotent. It ensures the destination sheet exists,
    then checks for existing journal_ids and only appends records that are not already present.
    """
    cfg = cfg or {}
    mongo_uri      = _require(cfg, "MONGO_URI")
    spreadsheet_id = _require(cfg, "SPREADSHEET_ID")
    sheet_name     = _require(cfg, "SHEET_NAME")

    # Initialize Google client first
    sa_path = _ensure_sa_file(cfg)
    sheets_client, sa_email = _google_client(sa_path)

    # Check connectivity and ensure sheet exists
    try:
        # Check general sheet access
        sheets_client.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
        # Ensure the main data sheet exists (this needs to happen AFTER general sheet access check)
        ensure_sheet_exists(sheets_client, spreadsheet_id, sheet_name)
    except HttpError as e:
        raise SystemExit(f"Google Sheet not accessible or could not be created. Share {spreadsheet_id} with {sa_email} (Editor). Details: {e}")

    # Check for Mongo connectivity
    try:
        client = MongoClient(mongo_uri, tz_aware=True)
        client.admin.command("ping")
        db = client[DB_NAME]
    except Exception as e:
        raise SystemExit(f"Mongo connection failed. Check MONGO_URI. Details: {e}")

    raw_data = fetch(db)
    if raw_data.empty:
        log.info("ℹ️ No data found in source database; nothing to upload.")
        return

    cleaned = clean(raw_data)

    append_to_sheet(sheets_client, spreadsheet_id, sheet_name, cleaned)
    # send_email_report(cfg, cleaned) # Kept commented out as in original

    log.info("✅ Pipeline run finished.")

if __name__ == "__main__":
    # Fallback to env-only run
    cfg_env = {
        "MONGO_URI":       os.getenv("MONGO_URI"),
        "SPREADSHEET_ID":  os.getenv("SPREADSHEET_ID"),
        "SHEET_NAME":      os.getenv("SHEET_NAME", "Sheet1"),
        "SA_JSON_PATH":    os.getenv("SA_JSON_PATH", "drive-sa.json"),
        "DRIVE_SA_JSON":   os.getenv("DRIVE_SA_JSON", ""),
    }
    run_once(cfg_env)