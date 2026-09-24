# How to Run the Pipeline

## Setup

### Option A: Using `uv` (Recommended)

1. **Install `uv`**:
   - **Windows (PowerShell)**:
     ```powershell
     powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
     ```
   - **macOS / Linux**:
     ```bash
     curl -LsSf https://astral.sh/uv/install.sh | sh
     ```
   *(Restart terminal after install)*

2. **Install dependencies**:
   ```bash
   cd data_champ
   uv sync
   ```

3. **Run**:
   ```bash
   uv run run_pipeline.py
   ```

### Option B: Standard Virtual Environment

```bash
# Create environment (first time only)
python -m venv .venv

# Activate
# Windows PowerShell:
.venv\Scripts\Activate.ps1
# Windows CMD:
.venv\Scripts\activate.bat
# Linux / Mac:
source .venv/bin/activate

# Install dependencies
pip install -e .

# Run
python run_pipeline.py
```

> **PowerShell execution policy error?** Run once:
> ```powershell
> Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser
> ```

## Running the Web Interface (CRNS Data Hub)

The easiest way to run and monitor the pipeline, explore datasets, and view live logs is via the interactive **Streamlit Data Hub**:

```bash
# Using uv (recommended)
uv run streamlit run data_hub.py

# Or standard python/pip
streamlit run data_hub.py
```

---

## Pipeline CLI Commands

```bash
# Full pipeline: sync + merge + dashboard
python run_pipeline.py

# Include neptoon processing (QA + corrections + soil moisture) - default single-pass
python run_pipeline.py --process

# Neptoon processing phase-by-phase (for changing site parameters)
python run_pipeline.py --process --by-phase

# Calibrate sensors with field campaign sample data
python run_pipeline.py --calibrate

# Custom Neptoon process configuration (e.g. hourly aggregation)
python run_pipeline.py --process --process-config processing/process_config/hourly_config.yaml

# Process specific sensors only
python run_pipeline.py --sensor 01
python run_pipeline.py --process --sensor 01+02

# Enable field/SD card data sync (requires CRNS_FIELD_DATA_PATH)
python run_pipeline.py --field

# Run individual steps
python processing/copydata.py
python processing/auto_append.py
python processing/calibrate_sensor.py --sensor 01
python processing/neptoon_pipeline.py --sensor 01
python processing/recent_performance.py
```

---

## Automating with Task Scheduler (Windows)

1. Create a scheduled task in Windows Task Scheduler
2. Set trigger (e.g., daily at 8 AM)
3. Action: **Start a program** → `cmd.exe`
4. Arguments: `/c "uv run run_pipeline.py"`
5. Start in: your project directory

For weekly field data sync, create a second task with arguments:
`/c "uv run run_pipeline.py --field"`

---

## Troubleshooting

- **Check log files** in `processing/meta/logs/` for detailed error information
- **Check `last_run.json`** for a quick summary of the last pipeline run
- **FTP connection issues** — verify credentials in `processing/meta/ftp_sources.txt`
- **Missing data** — ensure `sensor_metadata.xlsx` phases and FTP paths are correct
