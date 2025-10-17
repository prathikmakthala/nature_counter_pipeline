# Nature Counter Pipeline

This repository contains the Nature Counter export pipeline plus an automated GitHub Actions scheduler that executes the incremental sync every day at 9 PM Pacific.

## Running locally
1. Ensure Python 3.11 and the dependencies in `requirements.txt` are available.
2. Provide the required configuration via either environment variables or by editing `pipeline_config.py`:
   - `MONGO_URI`
   - `DRIVE_FOLDER_ID`
   - `SA_JSON_PATH` **or** `DRIVE_SA_JSON`
   - Optional: `OUTPUT_NAME`, `RUN_MODE`
3. Run the pipeline:
   ```bash
   python pipeline_config.py
   ```
   The script logs the selected mode and Drive folder, so you can confirm that the run started before any external calls are made.

## GitHub Actions scheduler
The workflow in `.github/workflows/daily-nc-pipeline.yml` is triggered twice nightly (`04:00` and `05:00` UTC). The job first converts the current time to the America/Los_Angeles timezone and only proceeds when the local hour is `21`, guaranteeing a single run at 9 PM Pacific regardless of daylight saving time.

Provide the following GitHub Secrets so the job can authenticate:
- `MONGO_URI`
- `DRIVE_FOLDER_ID`
- `DRIVE_SA_JSON` (inline service-account JSON)

If any of these secrets are missing, the workflow now posts a warning that lists the absent keys and exits successfully without attempting the pipeline. That keeps the nightly job in a green “skipped” state instead of failing while you finish wiring credentials.

You can also launch the workflow on demand with the **Run workflow** button in the Actions tab (`workflow_dispatch`).

To confirm that the schedule is active, make sure this workflow file exists on the repository's default branch and that GitHub Actions is enabled for the repository. After the next nightly window, open **Actions → Daily Nature Counter Pipeline** and you should see one of the following:
- ✅ **Success** — the workflow hit the 9 PM Pacific window, all secrets were present, and `python -m pipeline_project` ran to completion.
- ⚠️ **Notice: triggered outside the 9 PM Pacific window** — the cron fired at a different local hour (common when you manually click “Run workflow”). The pipeline itself is not attempted in this case.
- ⚠️ **Notice: repository secrets are incomplete** — at least one of `MONGO_URI`, `DRIVE_FOLDER_ID`, or `DRIVE_SA_JSON` is missing. Add them in the repository settings and the next nightly run will proceed.
- ❌ **Failure** — the pipeline started but encountered an error (for example, MongoDB connectivity or Drive permission issues). Expand the failing step in the logs to see the precise exception.

## Verifying execution
Logs from either a local run or the GitHub Action include entries such as:
```
INFO | Starting Nature Counter pipeline in inc mode (destination folder: <folder-id>)
INFO | Fetched <N> rows from Mongo
```
These confirm that the pipeline started successfully and reached MongoDB.
