"""
Helpers for running neptoon (>= 1.2) on the merged CSVs written by auto_append.py.

neptoon never parses raw logger files in this framework: the merge step does the
parsing (SDI fixes, time shifts, secondary detectors, probes) and neptoon reads
merged_<sensor>.csv, either as a whole (unified mode, latest phase config) or split
per phase with each phase's own YAML (phase mode).

neptoon validates configs strictly (unknown keys are errors), so our sensor YAMLs
are reduced to neptoon's schema before use; framework-only keys such as
sensor_info.location, supplementary_files or time_shift are dropped.
"""

import copy
import re
import shutil
import typing
import uuid
from pathlib import Path

import pandas as pd
import yaml
from pydantic import BaseModel

from sensor_metadata import (
    MERGED_DIR,
    assign_phase_ids,
    load_phase_config,
)

MERGED_DT_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
WORK_DIR = MERGED_DIR / "_neptoon_work"

# sensor_info fields that describe the site/detector; differences between phases
# mean a single unified neptoon run would use wrong values for some phases
SITE_PARAMETERS = (
    "N0", "latitude", "longitude", "elevation", "site_cutoff_rigidity",
    "avg_dry_soil_bulk_density", "avg_lattice_water", "avg_soil_organic_carbon",
    "above_ground_biomass", "beta_coefficient",
)


class NeptoonRunError(RuntimeError):
    """neptoon stopped before producing results."""


# ---------------------------------------------------------------------------
# Config reduction to neptoon's schema
# ---------------------------------------------------------------------------

def _model_from_annotation(annotation):
    """Return the pydantic model inside Optional[...]/Union[...] annotations (not dict/list)."""
    origin = typing.get_origin(annotation)
    if origin in (dict, list, tuple, set):
        return None
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return annotation
    for arg in typing.get_args(annotation):
        model = _model_from_annotation(arg)
        if model is not None:
            return model
    return None


def _allowed_keys(model) -> dict:
    keys = {}
    for field_name, field_info in model.model_fields.items():
        names = {field_name}
        if field_info.alias:
            names.add(field_info.alias)
        alias = field_info.validation_alias
        if isinstance(alias, str):
            names.add(alias)
        elif alias is not None and hasattr(alias, "choices"):
            names |= {c for c in alias.choices if isinstance(c, str)}
        for name in names:
            keys[name] = field_info
    return keys


def strip_unknown_keys(data: dict, model, path: str = "", dropped: list | None = None) -> dict:
    """Recursively drop keys that the neptoon pydantic model does not accept."""
    allowed = _allowed_keys(model)
    result = {}
    for key, value in data.items():
        if key not in allowed:
            if dropped is not None:
                dropped.append(f"{path}{key}")
            continue
        sub_model = _model_from_annotation(allowed[key].annotation)
        if sub_model is not None and isinstance(value, dict):
            value = strip_unknown_keys(value, sub_model, f"{path}{key}.", dropped)
        result[key] = value
    return result


# sensor_info keys holding method-specific N0 values in our YAMLs
METHOD_N0_KEYS = {
    "N0_desilets_etal_2010": ("desilets_etal_2010", None),
    "N0_koehli_etal_2021": ("koehli_etal_2021", "Mar21_mcnp_drf"),
}


def _translate_method_specific_n0(cfg: dict) -> None:
    """Map sensor_info.N0_<method> keys to neptoon's soil_moisture_calibrations records,
    so the N0 matching the configured conversion method is used."""
    info = cfg.get("sensor_info") or {}
    entries = {}
    for key, (method, parameter_set) in METHOD_N0_KEYS.items():
        value = info.pop(key, None)
        if value is None:
            continue
        record = {"method": method, "calibration_parameter": {"name": "N0", "value": float(value)}}
        if parameter_set:
            record["parameter_set"] = parameter_set
        entries[method] = record
    if entries and not cfg.get("soil_moisture_calibrations"):
        cfg["soil_moisture_calibrations"] = {"entries": entries}


def build_neptoon_sensor_config(sensor_cfg: dict, csv_path: Path, columns: list,
                                calibration: dict | None = None) -> tuple[dict, list]:
    """Turn one of our sensor YAML dicts into a neptoon config that reads csv_path.

    Returns (config, dropped_keys).
    """
    from neptoon.config.models.sensor import SensorConfig

    cfg = copy.deepcopy(sensor_cfg)
    # outputs/figures are produced by this framework, calibration only on request
    for section in ("figures", "data_storage", "calibration"):
        cfg.pop(section, None)

    ts_data = cfg.setdefault("time_series_data", {}) or {}
    cfg["time_series_data"] = ts_data
    ts_data["path_to_data"] = None
    ts_data.pop("temporal", None)  # aggregation is configured in the process config
    kci = ts_data.setdefault("key_column_info", {}) or {}
    ts_data["key_column_info"] = kci
    kci["date_time_columns"] = ["Date_Time"]
    kci["date_time_format"] = MERGED_DT_FORMAT
    kci["initial_time_zone"] = "utc"
    kci["convert_time_zone_to"] = "utc"

    csv_path = Path(csv_path)
    cfg["raw_data_parse_options"] = {
        "parse_raw_data": True,
        "data_location": str(csv_path.parent.resolve()),
        "recursive": False,
        "prefix": csv_path.stem,
        "suffix": csv_path.suffix,
        "filename_include_regex": f"(^|/){re.escape(csv_path.name)}$",
        "column_names": list(columns),
        "separator": ",",
        "decimal": ".",
        "skip_lines": 1,
        "encoding": "utf-8",
        "skip_initial_space": False,
        "multi_header": False,
        "strip_names": True,
        "remove_prefix": "",
        "starts_with": "",
    }
    if calibration:
        cfg["calibration"] = calibration

    _translate_method_specific_n0(cfg)

    dropped = []
    cfg = strip_unknown_keys(cfg, SensorConfig, "", dropped)
    return cfg, dropped


# ---------------------------------------------------------------------------
# Running neptoon
# ---------------------------------------------------------------------------

def run_neptoon(sensor_cfg: dict, process_config_path: Path, work_dir: Path,
                until: str = "external_soil_moisture"):
    """Run ProcessWithConfig up to (and including) the given stage.

    The output stage (neptoon's own file saving/figures) is not run; results are read
    from proc.data_hub. Raises NeptoonRunError when neptoon stops early.
    """
    from neptoon.workflow.process_with_config import ProcessWithConfig

    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    tmp_yaml = work_dir / f"_neptoon_{uuid.uuid4().hex[:8]}.yaml"
    try:
        with open(tmp_yaml, "w", encoding="utf-8") as f:
            yaml.safe_dump(sensor_cfg, f, sort_keys=False, allow_unicode=True)
        proc = ProcessWithConfig(
            path_to_sensor_config=str(tmp_yaml),
            path_to_process_config=str(process_config_path),
        )
        proc.run_until(until)
    finally:
        tmp_yaml.unlink(missing_ok=True)

    if proc.last_error is not None or proc.exit_reason:
        raise NeptoonRunError(proc.exit_reason or str(proc.last_error))
    if proc.data_hub is None or proc.data_hub.crns_data_frame is None:
        raise NeptoonRunError("neptoon returned no data")
    return proc


# ---------------------------------------------------------------------------
# Merged CSV handling
# ---------------------------------------------------------------------------

def read_merged_csv_text(merged_csv: Path) -> pd.DataFrame:
    """Read a merged CSV as text so it can be split and rewritten without changing values."""
    return pd.read_csv(merged_csv, dtype=str, keep_default_na=False)


ROW_COLUMN = "_row"


def write_neptoon_input(df_text: pd.DataFrame, path: Path) -> list:
    """Write the CSV neptoon reads (without the phase column). Returns its column names.

    A trailing row-number column keeps the last field non-empty: neptoon's parser
    otherwise treats trailing empty fields as a column-count mismatch.
    """
    data = df_text.drop(columns=["phase"], errors="ignore").copy()
    data[ROW_COLUMN] = range(len(data))
    path.parent.mkdir(parents=True, exist_ok=True)
    data.to_csv(path, index=False)
    return list(data.columns)


def clean_work_dir(work_dir: Path) -> None:
    shutil.rmtree(work_dir, ignore_errors=True)


def phase_configs(windows: list) -> dict:
    """{phase_id: sensor YAML dict} for phases whose config exists."""
    configs = {}
    for w in windows:
        cfg = load_phase_config(w.get("config_file"))
        if cfg is not None:
            configs[w["phase_id"]] = cfg
    return configs


def differing_site_parameters(windows: list) -> dict:
    """{parameter: {phase_id: value}} for site parameters that differ between phases."""
    values = {}
    for phase_id, cfg in phase_configs(windows).items():
        info = cfg.get("sensor_info") or {}
        for key in SITE_PARAMETERS:
            values.setdefault(key, {})[phase_id] = info.get(key)
    return {k: v for k, v in values.items() if len({repr(x) for x in v.values()}) > 1}


def tag_phases(df: pd.DataFrame, windows: list) -> pd.DataFrame:
    """Add a 'phase' column to a neptoon output frame (DatetimeIndex) from the phase windows."""
    df = df.copy()
    df["phase"] = assign_phase_ids(pd.Series(df.index, index=df.index), windows).to_numpy()
    return df
