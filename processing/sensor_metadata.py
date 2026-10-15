"""
Shared access to the sensor metadata workbook (processing/meta/sensor_metadata.xlsx).

All scripts read sensors and phases through this module so that phase handling,
sensor-name matching and paths are identical everywhere.

Phases
------
Phases are numbered chronologically by Phase_Start_Date: phase1, phase2, ...
Each phase covers [start, end). The effective end is the Phase_End_Date (a date
without time counts as the whole day), cut back to the next phase's start if the two
overlap; an empty end date ("now") runs until the next phase starts (or open-ended for
the last phase). Records outside every phase window are not attributed to any phase.
"""

import json
from datetime import datetime
from functools import lru_cache
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

PROCESSING_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = PROCESSING_DIR.parent
META_DIR = PROCESSING_DIR / "meta"
METADATA_FILE = META_DIR / "sensor_metadata.xlsx"
SENSOR_CONFIG_DIR = PROCESSING_DIR / "sensor_config"
TARGET_DATA_DIR = PROJECT_ROOT / "data"
MERGED_DIR = TARGET_DATA_DIR / ".merged"
MASTER_CONFIG_DIR = MERGED_DIR / "sensor_configs"

# Subfolders of a phase folder that hold neutron logger data (parsed with the phase YAML)
LOGGER_SUBFOLDERS = ("ftp", "sd")
# All subfolders copydata may create inside a phase folder
PHASE_SUBFOLDERS = ("ftp", "sd", "sd_pr2", "sd_theta")


# ---------------------------------------------------------------------------
# Excel loading
# ---------------------------------------------------------------------------

def _get_cell(row, col):
    """Read a cell from an Excel row, return stripped string or None."""
    val = row.get(col, "")
    if pd.notna(val) and str(val).strip():
        return str(val).strip()
    return None


def _parse_date_cell(row, col):
    """Read a date cell, return formatted string or None."""
    raw = row.get(col, "")
    if not pd.notna(raw) or not str(raw).strip():
        return None
    val = raw
    if isinstance(val, (pd.Timestamp, datetime)):
        if val.hour == 0 and val.minute == 0 and val.second == 0:
            val = val.strftime("%Y-%m-%d")
        else:
            val = val.strftime("%Y-%m-%d %H:%M:%S")
    return str(val).strip()


def _parse_folder_cell(row, col):
    """Read a folder cell that may be comma-separated. Returns str, list, or None."""
    val = _get_cell(row, col)
    if not val:
        return None
    if "," in val:
        return [s.strip() for s in val.split(",") if s.strip()]
    return val


def load_sensors_from_excel(filepath: Path = METADATA_FILE) -> dict:
    """Read sensor_metadata.xlsx into {sheet_name: {active, root_folder_name, phase: [...]}}.

    The returned structure is hashed by copydata to detect metadata changes, so keep
    it free of derived values (phase ids/windows come from phase_windows()).
    """
    xls = pd.ExcelFile(filepath)
    sensors = {}

    for sheet_name in xls.sheet_names:
        if sheet_name.lower() == "config_meta":
            continue

        df = pd.read_excel(xls, sheet_name=sheet_name)
        df.columns = df.columns.str.strip()

        # --- sensor-level fields (same for every row) ---
        active_val = df["Active"].iloc[0] if "Active" in df.columns and len(df) > 0 else False
        if isinstance(active_val, str):
            active = active_val.strip().lower() in ("true", "1", "yes")
        else:
            active = bool(active_val)

        if "Root_Folder" in df.columns and len(df) > 0:
            root_folder = str(df["Root_Folder"].iloc[0]).strip()
        else:
            root_folder = sheet_name

        # --- build phase list (one row = one phase) ---
        phases = []
        for _, row in df.iterrows():
            phase = {}

            sd = _parse_date_cell(row, "Phase_Start_Date")
            if sd: phase["start_date"] = sd

            ed = _parse_date_cell(row, "Phase_End_Date")
            if ed: phase["end_date"] = ed

            for excel_col, phase_key in [
                ("Phase_Folder", "folder_name"),
                ("Config", "config_file"),
                ("Phase_Location", "location"),
                ("Phase_Description", "description"),
            ]:
                val = _get_cell(row, excel_col)
                if val: phase[phase_key] = val

            source_lookup = {}
            for excel_col, key in [
                ("FTP_Folder", "ftp_folder"),
                ("Field_Folder", "field_folder"),
                ("Field_PR2_Folder", "field_pr2_folder"),
                ("Field_Theta_Folder", "field_theta_folder"),
            ]:
                val = _parse_folder_cell(row, excel_col)
                if val: source_lookup[key] = val
            if source_lookup:
                phase["source_lookup"] = source_lookup

            phases.append(phase)

        sensors[sheet_name] = {
            "active": active,
            "root_folder_name": root_folder,
            "phase": phases,
        }

    return sensors


# ---------------------------------------------------------------------------
# Phase windows
# ---------------------------------------------------------------------------

def parse_phase_timestamp(value):
    """Parse a metadata date string into a UTC timestamp (None if empty/invalid)."""
    if value is None or str(value).strip().lower() in ("", "now", "today", "open", "nan", "nat", "none"):
        return None
    ts = pd.to_datetime(str(value).strip(), errors="coerce")
    if pd.isna(ts):
        return None
    return ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")


def phase_windows(phases: list, log_fn=None) -> list[dict]:
    """Return chronologically numbered phase windows.

    Each entry: phase_id, start, end (effective, exclusive), end_configured,
    folder_name, config_file, location, description, phase (original dict).
    """
    def log(msg):
        if log_fn:
            log_fn(msg)

    dated = []
    for order, phase in enumerate(phases):
        start = parse_phase_timestamp(phase.get("start_date"))
        if start is None:
            log(f"  -> Phase '{phase.get('folder_name')}' has no valid Phase_Start_Date; ignored.")
            continue
        dated.append((start, order, phase))
    dated.sort(key=lambda item: (item[0], item[1]))

    windows = []
    for number, (start, _, phase) in enumerate(dated, start=1):
        end_configured = parse_phase_timestamp(phase.get("end_date"))
        next_start = dated[number][0] if number < len(dated) else None
        end = end_configured
        if end is not None and ":" not in str(phase.get("end_date")):
            # a date without time means the phase runs through that day
            end = end + pd.Timedelta(days=1)
            if next_start is not None and end > next_start >= end_configured:
                end = next_start
        if next_start is not None and (end is None or end > next_start):
            if end is not None:
                log(f"  -> Phase '{phase.get('folder_name')}' ends {end} after the next phase "
                    f"starts ({next_start}); using {next_start} as its end.")
            end = next_start
        if end is not None and end <= start:
            log(f"  -> Phase '{phase.get('folder_name')}' has end <= start; it covers no time.")
        windows.append({
            "phase_id": f"phase{number}",
            "start": start,
            "end": end,
            "end_configured": end_configured,
            "folder_name": phase.get("folder_name"),
            "config_file": phase.get("config_file"),
            "location": phase.get("location"),
            "description": phase.get("description"),
            "phase": phase,
        })
    return windows


def _to_ns(values) -> np.ndarray:
    return pd.DatetimeIndex(values).tz_convert("UTC").as_unit("ns").asi8


def assign_phase_ids(timestamps: pd.Series, windows: list[dict]) -> pd.Series:
    """Map UTC timestamps to phase ids ('phaseN'); NaN where no phase window applies."""
    result = pd.Series(np.nan, index=timestamps.index, dtype=object)
    if not windows or timestamps.empty:
        return result
    ts = pd.to_datetime(timestamps, utc=True)
    valid = ts.notna().to_numpy()
    if not valid.any():
        return result
    values = _to_ns(ts[valid])
    starts = _to_ns([w["start"] for w in windows])
    max_ns = np.iinfo(np.int64).max
    ends = np.array([
        _to_ns([w["end"]])[0] if w["end"] is not None else max_ns for w in windows
    ], dtype=np.int64)
    idx = np.searchsorted(starts, values, side="right") - 1
    inside = idx >= 0
    inside[inside] = values[inside] < ends[idx[inside]]
    ids = np.array([w["phase_id"] for w in windows], dtype=object)
    labels = np.full(values.shape, np.nan, dtype=object)
    labels[inside] = ids[idx[inside]]
    result.loc[valid] = labels
    return result


# ---------------------------------------------------------------------------
# Phase configs and sensor names
# ---------------------------------------------------------------------------

@lru_cache(maxsize=None)
def _load_yaml_cached(path_str: str, mtime: float):
    with open(path_str, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_phase_config(config_file) -> dict | None:
    """Load a phase YAML from processing/sensor_config/<config_file> (None if missing)."""
    if not config_file:
        return None
    path = SENSOR_CONFIG_DIR / config_file
    if not path.exists():
        return None
    cfg = _load_yaml_cached(str(path), path.stat().st_mtime)
    return cfg if isinstance(cfg, dict) else None


def sensor_name(sensor_key: str, sensor_data: dict) -> str:
    """Sensor name used for merged/processed file names (sensor_info.name of the latest phase)."""
    for window in reversed(phase_windows(sensor_data.get("phase", []))):
        cfg = load_phase_config(window["config_file"])
        name = (cfg or {}).get("sensor_info", {}).get("name")
        if name is not None and str(name).strip():
            return str(name).strip()
    return str(sensor_key)


def _normalise_token(token) -> set[str]:
    token = str(token).strip().lower()
    if not token:
        return set()
    tokens = {token}
    if token.isdigit():
        tokens.add(str(int(token)))
    return tokens


def sensor_aliases(sensor_key: str, sensor_data: dict) -> set[str]:
    """Names a sensor can be addressed by: sheet name, root folder, sensor name and the
    sensor_config folder of its latest phase (older phases may borrow other sensors' configs)."""
    aliases = set()
    aliases |= _normalise_token(sensor_key)
    aliases |= _normalise_token(sensor_data.get("root_folder_name", ""))
    aliases |= _normalise_token(sensor_name(sensor_key, sensor_data))
    for window in reversed(phase_windows(sensor_data.get("phase", []))):
        if window["config_file"]:
            parts = Path(window["config_file"]).parts
            if len(parts) > 1:
                aliases |= _normalise_token(parts[0])
            break
    return aliases


def parse_sensor_filter(values) -> set[str] | None:
    """Turn CLI/hub sensor arguments ('01', '01+02', '01,02', ['01', '02']) into a match set."""
    if not values:
        return None
    if isinstance(values, str):
        values = [values]
    tokens = set()
    for item in values:
        for part in str(item).replace("+", ",").split(","):
            tokens |= _normalise_token(part)
    return tokens or None


def sensor_matches(sensor_filter: set[str] | None, sensor_key: str, sensor_data: dict) -> bool:
    if not sensor_filter:
        return True
    return bool(sensor_filter & sensor_aliases(sensor_key, sensor_data))


# ---------------------------------------------------------------------------
# Phase info sidecar (written by the merge, read by neptoon/calibration)
# ---------------------------------------------------------------------------

def phase_info_path(name: str) -> Path:
    return MASTER_CONFIG_DIR / f"sensor_{name}_phases.json"


def write_phase_info(name: str, sensor_key: str, windows: list[dict]) -> Path:
    MASTER_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "sensor": name,
        "metadata_sheet": sensor_key,
        "phases": [
            {
                "phase_id": w["phase_id"],
                "start": w["start"].isoformat() if w["start"] is not None else None,
                "end": w["end"].isoformat() if w["end"] is not None else None,
                "folder_name": w["folder_name"],
                "config_file": w["config_file"],
                "location": w["location"],
                "description": w["description"],
            }
            for w in windows
        ],
    }
    path = phase_info_path(name)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    return path


def read_phase_info(name: str) -> list[dict] | None:
    """Phase windows as written with the merged CSV (start/end as UTC timestamps)."""
    path = phase_info_path(name)
    if not path.exists():
        return None
    with open(path, "r", encoding="utf-8") as f:
        payload = json.load(f)
    windows = []
    for entry in payload.get("phases", []):
        entry = dict(entry)
        entry["start"] = parse_phase_timestamp(entry.get("start"))
        entry["end"] = parse_phase_timestamp(entry.get("end"))
        windows.append(entry)
    return windows
