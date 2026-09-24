# CRNS Data Harmonization

Automated pipeline for syncing, merging, and processing **Cosmic-Ray Neutron Sensor (CRNS)** data into a unified, analysis-ready format. Built around [neptoon](https://github.com/neptoon-project/neptoon) for soil moisture estimation.

---

## Overview

This framework organizes raw CRNS data from multiple sources (FTP servers, HTTP APIs, SD cards) into a standardized folder structure, merges fragmented files into continuous time series, and optionally processes them through neptoon's full correction and soil moisture estimation pipeline.

Control the pipeline via the CLI (`run_pipeline.py`) or the interactive **CRNS Data Hub** web interface (`data_hub.py`).

```
FTP / API / SD Cards
        │
        ▼
┌─────────────────────┐
│  1. copydata.py     │  Reads sensor_metadata.xlsx to discover sensors/phases.
│     (Data Sync)     │  Downloads files from FTP and optional HTTP APIs into
│                     │  data/<sensor>/<phase>/{ftp|sd}/
└────────┬────────────┘
         │
         ▼
┌─────────────────────┐
│  2. auto_append.py  │  Finds all .yaml configs in the data/ tree.
│    (Data Merge)     │  Parses raw data files according to YAML column mappings.
│                     │  Produces one merged CSV per sensor in data/.merged/
└────────┬────────────┘
         │
         ▼
┌──────────────────────────┐
│  3. neptoon_pipeline.py  │  [Optional] Runs neptoon processing:
│     (Processing)         │  QA → corrections → calibration → soil moisture
└────────┬─────────────────┘
         │
         ▼
┌──────────────────────────┐
│ 4. recent_performance.py │  Reads merged CSVs + YAML configs.
│      (Dashboard)         │  Generates plots and an HTML monitoring dashboard.
└────────┬─────────────────┘
         │
         ▼
┌──────────────────────────┐
│      data_hub.py         │  Interactive Streamlit Web UI:
│    (CRNS Data Hub)       │  Pipeline runner, interactive Plotly data explorer,
└──────────────────────────┘  sensor network map & live log viewer.
```

---

## Quick Start

> See [how_to_run.md](how_to_run.md) for detailed setup instructions, including Windows Task Scheduler automation.

### 1. Install

```bash
# Clone the repository
git clone https://github.com/merlinschiel/data_champ.git
cd data_champ

# Install with uv (recommended)
uv sync

# Or with pip
pip install -e .
```

### 2. Configure

1. **FTP sources** — Edit `processing/meta/ftp_sources.txt` with your FTP server credentials:
   ```
   host	username	password	remote_dir	comment
   ftp.example.com	myuser	mypass		Main server
   ```

2. **Sensor metadata** — Edit `processing/meta/sensor_metadata.xlsx` to define your sensors and phases (see [Metadata](#metadata) section).

3. **Sensor configs** — Create YAML config files in `processing/sensor_config/<sensor_id>/` defining column mappings and parse options (see [YAML Configs](#yaml-sensor-configs)).

4. **HTTP API sources** *(optional)* — Copy `processing/meta/api_sources.txt.example` to `api_sources.txt` and add your API endpoints (e.g. Finapp sensors).

5. **Field data** *(optional)* — Set the `CRNS_FIELD_DATA_PATH` environment variable to your local field data root, or add it to a `.env` file (see `.env.example`).

### 3. Run

#### Option A: Web Interface (CRNS Data Hub)
Launch the interactive Streamlit dashboard and pipeline runner:
```bash
# Using uv (recommended)
uv run streamlit run data_hub.py

# Or directly with streamlit
streamlit run data_hub.py
```

#### Option B: Command Line Interface (CLI)
```bash
# Full pipeline: sync + merge + dashboard
uv run run_pipeline.py

# With neptoon processing
uv run run_pipeline.py --process

# Specific sensors only
uv run run_pipeline.py --sensor 01+02

# Enable field/SD card sync
uv run run_pipeline.py --field

# Individual steps
uv run processing/copydata.py
uv run processing/auto_append.py
uv run processing/recent_performance.py
```

---

## Project Structure

```
data_champ/
├── data_hub.py                      # CRNS Data Hub (Streamlit web interface)
├── run_pipeline.py                  # Main CLI entry point
├── processing/
│   ├── copydata.py                  # Data sync (FTP + API + field)
│   ├── auto_append.py               # Merge raw files per sensor
│   ├── merge_utils.py               # Core merge/parse utilities
│   ├── calibrate_sensor.py          # Neptoon sensor calibration (N0)
│   ├── neptoon_pipeline.py          # Neptoon processing & corrections
│   ├── plots_and_reports.py         # Time series plots & PDF reports
│   ├── recent_performance.py        # HTML monitoring dashboard generator
│   ├── copy_yamls.py                # Utility: copy YAML configs to data folders
│   ├── generate_map.py              # Utility: generate sensor location map
│   ├── meta/
│   │   ├── sensor_metadata.xlsx     # Sensor & phase definitions
│   │   ├── location_metadata.xlsx   # Sensor coordinates (for map)
│   │   ├── ftp_sources.txt          # FTP server credentials
│   │   └── api_sources.txt          # HTTP API download config (user-created)
│   ├── sensor_config/               # YAML configs per sensor
│   └── process_config/              # Neptoon process configs
├── data/                            # Data directory (created at runtime)
│   ├── <sensor_id>/                 # Raw data per sensor
│   │   └── <phase>/                 # Phase subfolder
│   │       ├── ftp/                 # FTP-sourced files + YAML config
│   │       └── sd/                  # Field/SD-sourced files (optional)
│   ├── .merged/                     # Merged CSVs (one per sensor)
│   └── .processed/                  # Neptoon output (optional)
├── handout.pdf                      # Project overview & architectural poster
├── how_to_run.md                    # Detailed run instructions & scheduler setup
├── how_to_update_metadata.md        # Guide for adding/updating sensor phases
├── .env.example                     # Environment variable template
├── pyproject.toml                   # Python project config
└── LICENSE                          # MIT License
```

---

## Metadata

### Sensor Metadata (`processing/meta/sensor_metadata.xlsx`)

Each sheet represents one sensor. Each row is a **phase** (a period with consistent data structure, location, or hardware configuration).

| Column | Description |
|--------|-------------|
| `Active` | `TRUE`/`FALSE` — whether to process this sensor |
| `Root_Folder` | Target folder name under `data/` |
| `Phase_Start_Date` | Start date (`YYYY-MM-DD` or `YYYY-MM-DD HH:MM:SS`) |
| `Phase_End_Date` | End date (blank = ongoing) |
| `Phase_Folder` | Subfolder name for this phase (e.g. `20230101_20230601`) |
| `Config` | Relative path to YAML config (e.g. `01/sensor_01_10col.yaml`) |
| `Phase_Location` | Location name |
| `Phase_Description` | Human-readable description |
| `FTP_Folder` | Remote FTP directory path |
| `Field_Folder` | Local field data glob pattern (requires `--field` flag + `CRNS_FIELD_DATA_PATH`) |

### FTP Sources (`processing/meta/ftp_sources.txt`)

Tab-separated file defining FTP servers:

```
host	username	password	remote_dir	comment
ftp.example.com	user	pass		Main server
ftp2.example.com	user2	pass2	/special/dir	Secondary server
```

- Leave `remote_dir` empty for the default server (sensor FTP paths resolve relative to root).
- Rows with a `remote_dir` are used for sensors whose FTP path starts with that directory.

### HTTP API Sources (`processing/meta/api_sources.txt`)

Tab-separated file defining HTTP API download tasks (e.g. Finapp sensors):

```
url	dest	description
https://data.finapptech.com/api/get-data/12345/token	data/my_sensor/20240101_now/ftp/finapp_data.txt	Finapp sensor at location X
```

---

## YAML Sensor Configs

Each phase needs a YAML config file in `processing/sensor_config/<sensor_id>/`. This file tells the pipeline how to parse raw data files.

**Key sections:**

```yaml
config: sensor
sensor_info:
  name: '01'                    # Must match the sheet name in sensor_metadata.xlsx
  country: Germany
  latitude: 52.46
  longitude: 12.96
  elevation: 113
  N0: 1100                     # Reference neutron count for soil moisture estimation
  location: MyLocation

time_series_data:
  key_column_info:
    epithermal_neutron_columns: [N1, N2]
    pressure_columns: [Internal_press]
    temperature_columns: [Internal_t]
    relative_humidity_columns: [Internal_rh]
    date_time_columns: [Date_Time]
    date_time_format: '%Y/%m/%d %H:%M:%S'

raw_data_parse_options:
  column_names: [RecordNum, Date_Time, Internal_press, ...]
  prefix: ['*Data*']            # File name patterns to match
  suffix: [.txt]
  encoding: utf-8
  separator: ','
  skip_lines: 0
```

**Advanced YAML features:**

- **`supplementary_files`** — Merge data from secondary detectors or profile probes (PR2, SMT100)
- **`time_shift`** — Correct timestamp offsets (`offset`, `align_start`, or `reconstruct_sequence` modes)
- **`calibration`** — Calibration data settings for neptoon processing

See the example configs in `processing/sensor_config/01/` for complete examples.

---

## User Interface & Outputs

### CRNS Data Hub (`data_hub.py`)
Central Streamlit web application providing a full graphical control room:
- **Dashboard**: High-level system overview, sensor network summary, interactive sensor map (Folium), and status table.
- **Pipeline Runner**: Run individual steps or full pipeline with custom flags (calibration, processing, sensor filters) and live streamed log output (`st.status`).
- **Data Explorer**: Interactive multi-parameter time-series plotting with Plotly (secondary Y-axis, date filters, pan/zoom), data inspection, and HTML/PNG export.
- **Logs**: Centralized viewer for all component logs.

Launch with:
```bash
uv run streamlit run data_hub.py
```

### Merged Data (`data/.merged/`)
One CSV per sensor with unified datetime format (`YYYY-MM-DDTHH:MM:SSZ`), `Date_Time` as first column. All phase data concatenated, deduplicated (SD card data takes priority over FTP on timestamp conflicts).

### Processed Data (`data/.processed/`)
Neptoon output with corrections applied: soil moisture (gravimetric & volumetric), corrected neutron counts, measurement depth, and uncertainty estimates.

### Performance Dashboard (`dashboard/`)
Static HTML dashboard generated by `processing/recent_performance.py` with per-sensor diagnostic plots (neutron counts, pressure, temperature, battery, soil probes) and status indicators (active / warning / stale).

---

## Additional Tools

| Tool | Description |
|------|-------------|
| `data_hub.py` | Modern Streamlit web interface for pipeline control and interactive data visualization |
| `processing/plots_and_reports.py` | Generate comprehensive time-series plots and publication-ready PDF reports |
| `processing/generate_map.py` | Generate standalone interactive HTML map of sensor locations from `location_metadata.xlsx` |
| `processing/copy_yamls.py` | Copy YAML config files into data phase folders (useful after config-only changes) |

---

## Updating Sensor Metadata

> See [how_to_update_metadata.md](how_to_update_metadata.md) for a step-by-step walkthrough.

When changes affect a sensor's data structure, source, sensitivity, or location:

1. **Add a new phase row** in `processing/meta/sensor_metadata.xlsx`
   - Set end date on previous phase, add start date for new phase
   - Update phase folder name and FTP/field paths if needed

2. **Create a new YAML config** in `processing/sensor_config/<sensor_id>/`
   - Copy the previous config and make relevant changes
   - Update the `Config` column in the metadata to point to the new file

3. **Update FTP/API sources** if the data is coming from a new server

4. **Test** — Run `python run_pipeline.py --sensor <id>` and check the merged output

---

## Log Files

| File | Script | Description |
|------|--------|-------------|
| `processing/meta/logs/copydata.log` | `copydata.py` | Data sync operations |
| `processing/meta/logs/auto_append.log` | `auto_append.py` | Data merge operations |
| `processing/meta/logs/pipeline.log` | `run_pipeline.py` | Overall pipeline orchestration |
| `processing/meta/logs/neptoon_pipeline.log` | `neptoon_pipeline.py` | Neptoon processing details |
| `last_run.json` | `run_pipeline.py` | Machine-readable run status |

---

## License

MIT — see [LICENSE](LICENSE).
