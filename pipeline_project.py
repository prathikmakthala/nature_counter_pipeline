# pipeline.py
# Nature Counter: Journals → Excel → Google Drive
# - Core logic only. No hard-coded credentials.
# - Accepts a config dict; falls back to env vars if cfg is None.
# - Timestamps preserved as strings (isoformat if datetime objs).
# - Country rule:
#     (A) if loc.country present → normalize US variants to "USA"; others unchanged
#     (B) elif state is a US code OR address contains a US state → "USA"
#     (C) else blank
# - Modes: cfg["RUN_MODE"] == "full" (backfill) or "inc" (incremental)

import io
import os
import json
import logging
import re
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional, Tuple, Dict

import pandas as pd
from pymongo import MongoClient
from bson import ObjectId
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("nc-pipeline")

DB_NAME = "NC_dev_db"
JOURNALS_COL, USERS_COL, LOCATIONS_COL = "journals", "userdetails", "locations"

US_STATES = set("""
AL AK AZ AR CA CO CT DC DE FL GA HI ID IL IN IA KS KY LA MA MD ME MI MN MO MS MT
NC ND NE NH NJ NM NV NY OH OK OR PA RI SC SD TN TX UT VA VT WA WI WV WY PR GU VI
""".split())

FINAL_COLS = [
    "Status", "User Name", "User email", "Timestamp", "n_Duration", "End Date Time",
    "n_Name", "City", "State", "Zip", "Country", "n_Place", "n_Lati", "n_Long",
    "n_park_nb", "n_activity", "n_notes"
]

def _require(cfg: Dict, key: str) -> str:
    v = cfg.get(key) or os.getenv(key)
    if not v:
        raise SystemExit(f"Missing required setting: {key}")
    return v

def _ensure_sa_file(cfg: Dict) -> str:
    """
    Returns path to service account JSON.
    If cfg has DRIVE_SA_JSON (string with full JSON), writes it to SA_JSON_PATH.
    Otherwise uses SA_JSON_PATH that must point to an existing file.
    """
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
        scopes=["https://www.googleapis.com/auth/drive", "https://www.googleapis.com/auth/spreadsheets"]
    )
    drive_client = build("drive", "v3", credentials=creds)
    sheets_client = build("sheets", "v4", credentials=creds)
    sa_email = json.load(open(sa_path))["client_email"]
    return drive_client, sheets_client, sa_email

def _escape_q(s: str) -> str:
    return s.replace("'", "\\'")

def find_file_id(drive, name: str, folder: str) -> Optional[str]:
    q = f"name='{_escape_q(name)}' and '{folder}' in parents and trashed=false"
    r = drive.files().list(q=q, fields="files(id)", pageSize=1).execute()
    return r["files"][0]["id"] if r.get("files") else None

def append_to_sheet(sheets_client, spreadsheet_id: str, sheet_name: str, df: pd.DataFrame) -> None:
    try:
        # Check if the sheet is empty to decide if we need to add headers
        result = sheets_client.spreadsheets().values().get(spreadsheetId=spreadsheet_id, range=sheet_name).execute()
        is_empty = not result.get('values')

        data_to_write = []
        if is_empty:
            data_to_write.append(df.columns.values.tolist())
        
        data_to_write.extend(df.values.tolist())

        body = {'values': data_to_write}
        result = sheets_client.spreadsheets().values().append(
            spreadsheetId=spreadsheet_id,
            range=f"{sheet_name}!A1",
            valueInputOption='USER_ENTERED',
            insertDataOption='INSERT_ROWS',
            body=body
        ).execute()
        log.info("Google Sheets API response: %s", result)
        log.info("%d cells appended to sheet.", result.get('updates', {}).get('updatedCells'))
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

def agg_pipeline(match: dict):
    return [
        {"$match": match},
        {"$addFields": {
            "uid_obj": {"$convert": {"input": "$uid", "to": "objectId", "onError": None, "onNull": None}},
            "loc_obj": {"$convert": {"input": "$locationId", "to": "objectId", "onError": None, "onNull": None}},
        }},
        {"$lookup": {"from": USERS_COL, "let": {"u": "$uid_obj"},
                     "pipeline": [{"$match": {"$expr": {"$eq": ["$_id", "$$u"]}}}],
                     "as": "u"}},
        {"$unwind": {"path": "$u", "preserveNullAndEmptyArrays": True}},
        {"$lookup": {"from": LOCATIONS_COL, "let": {"l": "$loc_obj"},
                     "pipeline": [{"$match": {"$expr": {"$eq": ["$_id", "$$l"]}}}],
                     "as": "loc"}},
        {"$unwind": {"path": "$loc", "preserveNullAndEmptyArrays": True}},
        {"$addFields": {
            "lng_from_geojson": {"$cond": [
                {"$eq": [{"$type": "$loc.coordinates.coordinates"}, "array"]},
                {"$arrayElemAt": ["$loc.coordinates.coordinates", 0]},
                None
            ]},
            "lat_from_geojson": {"$cond": [
                {"$eq": [{"$type": "$loc.coordinates.coordinates"}, "array"]},
                {"$arrayElemAt": ["$loc.coordinates.coordinates", 1]},
                None
            ]},
        }},
        {"$project": {
            "journal_id": {"$toString": "$_id"},
            "Timestamp": "$start_time",
            "End Date Time": "$end_time",
            "n_Duration": {
              "$round": [{ "$divide": [{ "$subtract": ["$end_time", "$start_time"] }, 60000] }, 0]
            },

            "User Name": {"$ifNull": ["$u.name", ""]},
            "User email": {"$ifNull": ["$u.email", ""]},

            "n_Name": {"$ifNull": ["$loc.name", ""]},
            "City": {"$ifNull": ["$loc.city", ""]},
            "State": {"$ifNull": ["$loc.stateInitials", {"$ifNull": ["$loc.state", ""]}]} ,
            "Zip": {"$ifNull": ["$loc.zip", ""]},

            "LocCountry": {"$ifNull": ["$loc.country", ""]},
            "Address": {"$ifNull": ["$loc.address", ""]},

            "n_Place": {"$concat": [
                {"$ifNull": ["$loc.name", ""]}, ", ",
                {"$ifNull": ["$loc.city", ""]}, " ",
                {"$ifNull": ["$loc.stateInitials", {"$ifNull": ["$loc.state", ""]}]}
            ]},

            "n_Lati": {"$ifNull": ["$loc.coordinates.lat",
                       {"$ifNull": ["$loc.coordinates.latitude", "$lat_from_geojson"]}]},
            "n_Long": {"$ifNull": ["$loc.coordinates.lng",
                       {"$ifNull": ["$loc.coordinates.longitude", "$lng_from_geojson"]}]},

            "n_park_nb": {"$ifNull": ["$loc.parkNumber", {"$arrayElemAt": ["$loc.category", 0]}]},
            "n_activity": {"$ifNull": ["$n_activity", ""]},
            "n_notes": {"$ifNull": ["$n_notes", ""]}
        }},
        {"$sort": {"journal_id": 1}}
    ]

def _to_str_timestamp(x):
    if x is None:
        return ""
    try:
        return x.isoformat()
    except Exception:
        return str(x)

def clean(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame(columns=FINAL_COLS)
    df = df.copy()

    # Add the blank Status column as the first column, if it doesn't exist
    if "Status" not in df.columns:
        df.insert(0, "Status", "")

    # Retain only necessary processing for `Country`
    address_for_check = df.get("Address", pd.Series([""]*len(df), index=df.index)).astype(str).where(df.get("Address", pd.Series([""]*len(df), index=df.index)).astype(str).str.len() > 0, df.get("n_Place", pd.Series([""]*len(df), index=df.index)).astype(str))
    state_series       = df.get("State", pd.Series([""]*len(df), index=df.index)).astype(str)
    loc_country_series = df.get("LocCountry", pd.Series([""]*len(df), index=df.index)).astype(str)
    df["Country"] = [
        decide_country(addr, st, lc)
        for addr, st, lc in zip(address_for_check, state_series, loc_country_series)
    ]

    # Process numeric and text columns
    df["n_Lati"]  = pd.to_numeric(df.get("n_Lati"), errors="coerce").round(6)
    df["n_Long"]  = pd.to_numeric(df.get("n_Long"), errors="coerce").round(6)
    df["n_Place"] = df.get("n_Place", pd.Series([""]*len(df), index=df.index)).astype(str).str.replace(r"\s{2,}", " ", regex=True).str.strip(" ,")

    # Timestamp conversion
    df["Timestamp"]     = df["Timestamp"].apply(_to_str_timestamp)
    df["End Date Time"] = df.get("End Date Time", pd.Series([None]*len(df), index=df.index)).apply(_to_str_timestamp)

    # Drop internal columns used for joining that shouldn't be in final output
    if '_id' in df.columns:
        df = df.drop(columns=['_id'])

    # Ensure all FINAL_COLS exist, adding empty string for missing ones
    for c in FINAL_COLS:
        if c not in df.columns:
            df[c] = ""
    
    # Deduplicate based on journal_id which is still present at this stage
    if "journal_id" in df.columns:
        df = df.drop_duplicates(subset=["journal_id"], keep="last")

    return df[FINAL_COLS]

def load_watermark_from_file(watermark_file: str) -> Optional[str]:
    try:
        with open(watermark_file, "r") as f:
            return f.read().strip()
    except FileNotFoundError:
        return None

def save_watermark_to_file(watermark_file: str, last_oid: str):
    with open(watermark_file, "w") as f:
        f.write(str(last_oid))

def fetch(db, last_oid: Optional[str]) -> Tuple[pd.DataFrame, Optional[str]]:
    match = {"end_time": {"$ne": None}}
    if last_oid:
        try:
            match["_id"] = {"$gt": ObjectId(last_oid)}
        except Exception:
            log.warning("Invalid last_oid; running full fetch.")
    docs = list(db[JOURNALS_COL].aggregate(agg_pipeline(match)))
    return pd.DataFrame(docs), (docs[-1]["journal_id"] if docs else None)

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
    
    # Format the DataFrame as an HTML table
    html_body = f"""
    <html>
    <head>
        <style>
            body {{ font-family: sans-serif; }}
            table {{ border-collapse: collapse; width: 100%; }}
            th, td {{ border: 1px solid #dddddd; text-align: left; padding: 8px; }}
            tr:nth-child(even) {{ background-color: #f2f2f2; }}
            th {{ background-color: #4CAF50; color: white; }}
        </style>
    </head>
    <body>
        <h2>Nature Counter Daily Incremental Report</h2>
        <p>Found {len(new_data)} new journal entries.</p>
        {new_data.to_html(index=False, na_rep="")}
    </body>
    </html>
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
    Required keys/envs: MONGO_URI, SPREADSHEET_ID, SA_JSON_PATH or DRIVE_SA_JSON
    Optional: SHEET_NAME (default 'Sheet1'), RUN_MODE (full|inc)
    """
    cfg = cfg or {}
    mongo_uri      = _require(cfg, "MONGO_URI")
    spreadsheet_id = _require(cfg, "SPREADSHEET_ID")
    sheet_name     = _require(cfg, "SHEET_NAME")
    run_mode       = (cfg.get("RUN_MODE") or os.getenv("RUN_MODE", "inc")).lower()
    watermark_file = "watermark.txt"

    sa_path = _ensure_sa_file(cfg)
    drive, sheets, sa_email = _google_client(sa_path) # drive client is no longer used but kept for now

    # Connectivity checks
    try:
        client = MongoClient(mongo_uri, tz_aware=True)
        client.admin.command("ping")
    except Exception as e:
        raise SystemExit(f"Mongo connection failed. Check MONGO_URI. Details: {e}")

    try:
        sheets.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
    except HttpError as e:
        raise SystemExit(f"Google Sheet not accessible. Share {spreadsheet_id} with {sa_email} (Editor). Details: {e}")

    db = client[DB_NAME]
    last_oid = None if run_mode == "full" else load_watermark_from_file(watermark_file)

    raw, new_last_oid = fetch(db, last_oid)
    if raw is None or raw.empty:
        log.info("ℹ️ No new data; nothing to upload.")
        # Send an email even if there's no new data, if configured
        if cfg.get("EMAIL_ON_NO_NEW_DATA"):
            send_email_report(cfg, pd.DataFrame())
        return

    cleaned = clean(raw)
    
    append_to_sheet(sheets, spreadsheet_id, sheet_name, cleaned)
    send_email_report(cfg, cleaned)
    
    if new_last_oid:
        save_watermark_to_file(watermark_file, new_last_oid)
        log.info(f"✅ Successfully wrote {len(cleaned)} rows to sheet and saved new watermark: {new_last_oid}")
    else:
        log.info(f"✅ Successfully wrote {len(cleaned)} rows to sheet.")

if __name__ == "__main__":
    # Fallback to env-only run
    cfg_env = {
        "MONGO_URI":       os.getenv("MONGO_URI"),
        "DRIVE_FOLDER_ID": os.getenv("DRIVE_FOLDER_ID"),
        "OUTPUT_NAME":     os.getenv("OUTPUT_NAME", "NC-DA-Journal-Data.xlsx"),
        "RUN_MODE":        os.getenv("RUN_MODE", "inc"),
        "SA_JSON_PATH":    os.getenv("SA_JSON_PATH", "drive-sa.json"),
        "DRIVE_SA_JSON":   os.getenv("DRIVE_SA_JSON", ""),
    }
    run_once(cfg_env)
