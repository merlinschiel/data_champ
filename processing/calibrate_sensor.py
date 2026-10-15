"""
Sensor Calibration Workflow (neptoon >= 1.2)
============================================
Calibrates N0 from field campaign samples (gravimetric / FDR) with neptoon's own
calibration stage (Desilets 2010 / Köhli 2021 conversion, Schrön 2017 weights).

- Calibration samples are matched to phases by their timestamps (phase windows from
  sensor_metadata.xlsx). Each phase with samples is calibrated separately, using that
  phase's sensor YAML and the merged time series of that phase, i.e. the same data the
  processing step uses.
- Calibration CSV columns are taken from the phase YAML (calibration.key_column_names)
  when the CSV has them, otherwise from the defaults below.
- With --update-yaml only the YAML of the calibrated phase is changed: N0 is written,
  site soil properties (avg_*) are only filled in where the YAML has no value yet.
- Reports, sample weights and plots go to data/.processed/calibration_results/.

Usage:
  python processing/calibrate_sensor.py --sensor 01
  python processing/calibrate_sensor.py --sensor 01 02 --update-yaml
  python processing/calibrate_sensor.py --sensor 01 --process-config processing/process_config/hourly_config.yaml
"""

import argparse
import json
import re
from pathlib import Path

import pandas as pd
import yaml

from log_utils import configure_utf8_console, get_logger
from neptoon_utils import (
    WORK_DIR,
    build_neptoon_sensor_config,
    clean_work_dir,
    phase_configs,
    read_merged_csv_text,
    run_neptoon,
    write_neptoon_input,
)
from sensor_metadata import (
    MERGED_DIR,
    METADATA_FILE,
    PROCESSING_DIR,
    PROJECT_ROOT,
    SENSOR_CONFIG_DIR,
    assign_phase_ids,
    load_sensors_from_excel,
    parse_sensor_filter,
    read_phase_info,
    sensor_aliases,
    sensor_matches,
    sensor_name,
)

configure_utf8_console()
logger = get_logger("calibrate_sensor", "calibration.log")

DEFAULT_PROCESS_CONFIG = PROCESSING_DIR / "process_config" / "default_config.yaml"
RESULTS_DIR = PROJECT_ROOT / "data" / ".processed" / "calibration_results"
CALIBRATION_DIRS = [
    PROJECT_ROOT / "data" / ".calibration",
    PROJECT_ROOT / "data",
    PROCESSING_DIR / "calibration" / "data",
]
SAMPLE_DT_FORMAT = "%Y-%m-%d %H:%M:%S"

# neptoon calibration column key -> default column name in our calibration CSVs
DEFAULT_CALIBRATION_COLUMNS = {
    "date_time": "DateTime_utc",
    "profile_id": "Profile_ID",
    "radial_distance_from_sensor": "Distance_to_CRNS_m",
    "sample_depth": "Profile_Depth_cm",
    "gravimetric_soil_moisture": "SoilMoisture_g_g",
    "bulk_density_of_sample": "DryBulkDensity_g_cm3",
    "soil_organic_carbon": "SoilOrganicCarbon_g_g",
    "lattice_water": "LatticeWater_g_g",
}
REQUIRED_CALIBRATION_KEYS = (
    "date_time", "profile_id", "radial_distance_from_sensor",
    "sample_depth", "gravimetric_soil_moisture", "bulk_density_of_sample",
)
# YAML calibration.key_column_names entries whose name differs from the neptoon key
YAML_KEY_NAMES = {
    "profile_id": "profile_id_column",
}
SITE_PROPERTY_KEYS = {
    "avg_dry_soil_bulk_density": "value_avg_bulk_density",
    "avg_soil_organic_carbon": "value_avg_soil_organic_carbon",
    "avg_lattice_water": "value_avg_lattice_water",
}


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------

def resolve_sensor(sensor_id: str, sensors: dict) -> tuple[str, dict]:
    """Find the metadata sensor for an id (sheet name, sensor name or config folder)."""
    sensor_filter = parse_sensor_filter([sensor_id])
    matches = [(k, v) for k, v in sensors.items() if sensor_matches(sensor_filter, k, v)]
    exact = [(k, v) for k, v in matches if k.lower() == str(sensor_id).lower()]
    if exact:
        return exact[0]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise ValueError(f"Sensor '{sensor_id}' not found in {METADATA_FILE.name}")
    raise ValueError(f"Sensor '{sensor_id}' is ambiguous: {[k for k, _ in matches]}")


def find_calibration_file(sensor_key: str, sensor_data: dict, campaign_path: str = None) -> Path:
    if campaign_path:
        path = Path(campaign_path)
        if not path.exists():
            raise FileNotFoundError(f"Calibration file not found: {path}")
        return path
    for alias in sorted(sensor_aliases(sensor_key, sensor_data) | {str(sensor_key)}):
        for candidate_id in {alias, f"{int(alias):02d}" if alias.isdigit() else alias}:
            for directory in CALIBRATION_DIRS:
                path = directory / f"calibration_{candidate_id}.csv"
                if path.exists():
                    return path
    raise FileNotFoundError(
        f"Calibration data not found for sensor '{sensor_key}'. "
        f"Place it at data/.calibration/calibration_{sensor_key}.csv"
    )


def _yaml_calibration_names(cfg: dict) -> dict:
    names = ((cfg or {}).get("calibration") or {}).get("key_column_names") or {}
    resolved = {}
    for key in DEFAULT_CALIBRATION_COLUMNS:
        value = names.get(YAML_KEY_NAMES.get(key, key))
        if value:
            resolved[key] = str(value)
    return resolved


def resolve_calibration_columns(cfg: dict, csv_columns) -> tuple[dict, list, list]:
    """Pick the CSV column for each neptoon calibration key.

    The YAML name is used when the CSV contains it, otherwise the default name.
    Returns (mapping, notes, missing_required).
    """
    yaml_names = _yaml_calibration_names(cfg)
    csv_columns = set(csv_columns)
    mapping, notes, missing = {}, [], []
    for key, default in DEFAULT_CALIBRATION_COLUMNS.items():
        candidates = [c for c in (yaml_names.get(key), default) if c]
        chosen = next((c for c in candidates if c in csv_columns), None)
        if chosen is None:
            if key in REQUIRED_CALIBRATION_KEYS:
                missing.append(f"{key} (tried {candidates})")
                continue
            chosen = candidates[0]  # optional: neptoon falls back to the sensor_info average
        elif yaml_names.get(key) and chosen != yaml_names[key]:
            notes.append(f"YAML column '{yaml_names[key]}' for {key} not in CSV; using '{chosen}'")
        mapping[key] = chosen
    return mapping, notes, missing


def load_calibration_dataset(calib_file: Path) -> pd.DataFrame:
    logger.info(f"Loading calibration dataset from: {calib_file}")
    df = pd.read_csv(calib_file)
    if df.empty:
        raise ValueError(f"Calibration file has no rows: {calib_file}")
    return df


# ---------------------------------------------------------------------------
# YAML update (text based, keeps comments and formatting)
# ---------------------------------------------------------------------------

def update_sensor_info_values(yaml_path: Path, values: dict) -> None:
    """Set sensor_info.<key> values in a YAML file without reformatting the rest of it."""
    lines = yaml_path.read_text(encoding="utf-8").splitlines(keepends=True)
    start = next((i for i, line in enumerate(lines) if re.match(r"^sensor_info:\s*(#.*)?$", line)), None)
    if start is None:
        raise ValueError(f"No top-level sensor_info block in {yaml_path}")
    end = len(lines)
    for i in range(start + 1, len(lines)):
        stripped = lines[i].strip()
        if stripped and not stripped.startswith("#") and not lines[i][0].isspace():
            end = i
            break
    key_lines = [i for i in range(start + 1, end) if re.match(r"^\s+\w[\w.]*\s*:", lines[i])]
    indent = re.match(r"^(\s+)", lines[key_lines[0]]).group(1) if key_lines else "  "
    newline = "\r\n" if lines[start].endswith("\r\n") else "\n"

    for key, value in values.items():
        rendered = yaml.safe_dump(value, default_flow_style=True).strip()
        rendered = rendered.removesuffix("...").strip()
        pattern = re.compile(rf"^{re.escape(indent)}{re.escape(key)}\s*:(?P<value>[^#\n\r]*)(?P<comment>#.*)?(?P<eol>\r?\n)?$")
        for i in key_lines:
            match = pattern.match(lines[i])
            if match:
                comment = f" {match.group('comment')}" if match.group("comment") else ""
                lines[i] = f"{indent}{key}: {rendered}{comment}{match.group('eol') or ''}"
                break
        else:
            insert_at = (key_lines[-1] + 1) if key_lines else start + 1
            lines.insert(insert_at, f"{indent}{key}: {rendered}{newline}")
            key_lines = [k + 1 if k >= insert_at else k for k in key_lines] + [insert_at]
            end += 1

    new_text = "".join(lines)
    parsed = yaml.safe_load(new_text)
    for key, value in values.items():
        if parsed.get("sensor_info", {}).get(key) != value:
            raise ValueError(f"YAML update check failed for {yaml_path.name}: {key}")
    yaml_path.write_text(new_text, encoding="utf-8")


# ---------------------------------------------------------------------------
# Calibration
# ---------------------------------------------------------------------------

def _calibrate_phase(name, window, cfg, samples, sample_times, text_df, hours_around_calib,
                     proc_cfg_path, work_dir):
    """Calibrate one phase with neptoon. Returns (phase_report, ts_df, results_df, weights_df)."""
    phase_id = window["phase_id"]
    mapping, notes, missing = resolve_calibration_columns(cfg, samples.columns)
    for note in notes:
        logger.info(f"  -> {phase_id}: {note}")
    if missing:
        raise ValueError(f"calibration CSV lacks required columns: {missing}")

    part = text_df[text_df["phase"] == phase_id]
    if part.empty:
        raise ValueError("no merged neutron data in this phase")

    csv_path = work_dir / f"{name}__{phase_id}.csv"
    columns = write_neptoon_input(part, csv_path)

    samples = samples.copy()
    samples[mapping["date_time"]] = sample_times.dt.tz_convert("UTC").dt.strftime(SAMPLE_DT_FORMAT)
    samples_path = work_dir / f"calibration_samples_{phase_id}.csv"
    samples.to_csv(samples_path, index=False)

    calibration_block = {
        "calibrate": True,
        "location": str(samples_path),
        "date_time_format": SAMPLE_DT_FORMAT,
        "key_column_names": mapping,
        "hours_of_data_around_calib": hours_around_calib,
    }
    neptoon_cfg, _ = build_neptoon_sensor_config(cfg, csv_path, columns, calibration=calibration_block)
    proc = run_neptoon(neptoon_cfg, proc_cfg_path, work_dir, until="soil_moisture")

    hub = proc.data_hub
    calibrator = hub.calibrator
    n0 = float(proc.sensor_config.sensor_info.N0)
    results_df = calibrator.return_calibration_results_data_frame()
    try:
        weights_df = calibrator.return_weighting_dataframe()
    except Exception:
        weights_df = None

    sample_averages = {}
    for yaml_key, context_attr in SITE_PROPERTY_KEYS.items():
        value = getattr(calibrator.context, context_attr, None)
        if value is not None:
            try:
                sample_averages[yaml_key] = float(value)
            except (TypeError, ValueError):
                pass

    report = {
        "phase_id": phase_id,
        "location": window.get("location"),
        "config_file": window.get("config_file"),
        "phase_start": str(window.get("start")),
        "phase_end": str(window.get("end")),
        "n_samples": int(len(samples)),
        "calibration_days": sorted({str(t.date()) for t in sample_times}),
        "N0_previous": (cfg.get("sensor_info") or {}).get("N0"),
        "optimal_N0": int(round(n0)),
        "optimal_N0_exact": n0,
        "N0_uncertainty": getattr(proc.sensor_config.sensor_info, "N0_uncertainty", None),
        "sample_averages": sample_averages,
        "calibration_columns": mapping,
        "campaign_results": results_df.to_dict(orient="records"),
    }
    return report, hub.crns_data_frame.copy(), results_df, weights_df


def calibrate_sensor(
    sensor_id: str = None,
    campaign_path: str = None,
    hours_around_calib: int = 6,
    update_yaml: bool = False,
    save_results: bool = True,
    process_config_path: Path = None,
) -> dict:
    """Calibrate every phase of a sensor that has calibration samples.

    Returns the calibration report dict. Raises when no phase could be calibrated.
    """
    if sensor_id is None:
        raise ValueError("sensor_id is required. Pass --sensor <id> or specify sensor_id.")
    proc_cfg_path = Path(process_config_path) if process_config_path else DEFAULT_PROCESS_CONFIG
    with open(proc_cfg_path, "r", encoding="utf-8") as f:
        proc_cfg = yaml.safe_load(f) or {}
    sm_est = (proc_cfg.get("correction_steps") or {}).get("soil_moisture_estimation") or {}
    method = sm_est.get("method", "desilets_etal_2010")

    sensors = load_sensors_from_excel(METADATA_FILE)
    sensor_key, sensor_data = resolve_sensor(sensor_id, sensors)
    name = sensor_name(sensor_key, sensor_data)

    logger.info("==================================================")
    logger.info(f" Starting calibration for sensor '{name}' (metadata sheet '{sensor_key}')")
    logger.info(f" Conversion method: {method}")
    logger.info("==================================================")

    windows = read_phase_info(name)
    merged_csv = MERGED_DIR / f"merged_{name}.csv"
    if windows is None or not merged_csv.exists():
        raise RuntimeError(f"No merged data for '{name}'; run the merge first "
                           f"(auto_append.py --sensor {sensor_key})")

    calib_file = find_calibration_file(sensor_key, sensor_data, campaign_path)
    calib_df = load_calibration_dataset(calib_file)
    configs = phase_configs(windows)

    # date/time column: first YAML-configured name present in the CSV, else the default
    dt_candidates = [_yaml_calibration_names(c).get("date_time") for c in configs.values()]
    dt_candidates = [c for c in dt_candidates if c] + [DEFAULT_CALIBRATION_COLUMNS["date_time"]]
    dt_col = next((c for c in dt_candidates if c in calib_df.columns), None)
    if dt_col is None:
        raise ValueError(f"Calibration CSV has no date/time column (tried {dt_candidates})")
    sample_times = pd.to_datetime(calib_df[dt_col], utc=True, errors="coerce")
    if sample_times.isna().any():
        raise ValueError(f"{int(sample_times.isna().sum())} calibration timestamps in '{dt_col}' could not be parsed")

    sample_phase = assign_phase_ids(sample_times, windows)
    outside = sample_phase.isna()
    if outside.any():
        logger.warning(f"Skipping {int(outside.sum())} sample(s) outside all phase windows: "
                       f"{sorted({str(t.date()) for t in sample_times[outside]})}")

    text_df = read_merged_csv_text(merged_csv)
    if "phase" not in text_df.columns:
        raise RuntimeError(f"{merged_csv.name} has no 'phase' column; re-run the merge with --force")

    work_dir = WORK_DIR / f"{name}_calibration"
    clean_work_dir(work_dir)
    phase_reports, failures = [], {}
    plot_inputs = []
    window_by_id = {w["phase_id"]: w for w in windows}
    try:
        for phase_id in [w["phase_id"] for w in windows]:
            mask = (sample_phase == phase_id).to_numpy()
            if not mask.any():
                continue
            window = window_by_id[phase_id]
            logger.info(f"  -> {phase_id} ({window.get('location')}, {window.get('config_file')}): "
                        f"{int(mask.sum())} samples")
            cfg = configs.get(phase_id)
            if cfg is None:
                failures[phase_id] = f"config '{window.get('config_file')}' not found"
                logger.error(f"  -> {phase_id}: {failures[phase_id]}")
                continue
            try:
                report, ts_df, results_df, weights_df = _calibrate_phase(
                    name, window, cfg, calib_df[mask], sample_times[mask], text_df,
                    hours_around_calib, proc_cfg_path, work_dir,
                )
            except Exception as e:
                failures[phase_id] = str(e)
                logger.error(f"  -> {phase_id}: calibration failed: {e}")
                continue
            phase_reports.append(report)
            plot_inputs.append((report, ts_df, results_df, weights_df))
            logger.info(f"  -> {phase_id}: optimal N0 = {report['optimal_N0']} cph "
                        f"(YAML had {report['N0_previous']})")
            logger.info(f"     Campaign summary:\n{results_df.to_string(index=False)}")
    finally:
        clean_work_dir(work_dir)

    if not phase_reports:
        raise RuntimeError(f"No phase of '{name}' could be calibrated: {failures or 'no samples in any phase'}")

    report_dict = {
        "sensor": name,
        "metadata_sheet": sensor_key,
        "calibration_timestamp": pd.Timestamp.now().isoformat(),
        "method": method,
        "koehli_parameters": sm_est.get("koehli_etal_2021_parameterset") if method == "koehli_etal_2021" else None,
        "calibration_data_file": str(calib_file),
        "process_config": str(proc_cfg_path),
        "phases": phase_reports,
        "failed_phases": failures,
    }

    if save_results:
        _save_results(name, report_dict, plot_inputs)

    if update_yaml:
        report_dict["yaml_updates"] = update_phase_yamls(sensor_key, sensor_data, phase_reports, method)

    return report_dict


def _save_results(name, report_dict, plot_inputs):
    from plots_and_reports import generate_calibration_plots

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    report_file = RESULTS_DIR / f"calibration_report_{name}.json"
    with open(report_file, "w", encoding="utf-8") as f:
        json.dump(report_dict, f, indent=2, default=str)
    logger.info(f"Saved calibration report -> {report_file}")

    for report, ts_df, results_df, weights_df in plot_inputs:
        label = f"{name}_{report['phase_id']}"
        if weights_df is not None and not weights_df.empty:
            weights_file = RESULTS_DIR / f"calibration_sample_weights_{label}.csv"
            weights_df.to_csv(weights_file, index=False)
            logger.info(f"Saved sample weights -> {weights_file}")
        try:
            generate_calibration_plots(
                sensor_id=label,
                ts_df=ts_df,
                results_df=results_df,
                weights_df=weights_df,
                report_dict={**report, "method": report_dict["method"]},
                output_dir=RESULTS_DIR,
            )
        except Exception as plot_err:
            logger.warning(f"Calibration plot generation failed for {label}: {plot_err}")


def update_phase_yamls(sensor_key: str, sensor_data: dict, phase_reports: list,
                       method: str = "desilets_etal_2010") -> dict:
    """Write calibrated N0 into the YAML of each calibrated phase only.

    If the YAML keeps method-specific values (N0_<method>), that key is updated instead
    of N0. Site soil properties are only filled where the YAML has no value. If several
    calibrated phases share one YAML with different N0 values, that YAML is left unchanged.
    """
    by_file = {}
    for report in phase_reports:
        by_file.setdefault(report["config_file"], []).append(report)

    updates = {}
    for config_file, reports in by_file.items():
        yaml_path = SENSOR_CONFIG_DIR / config_file
        n0_values = {r["optimal_N0"] for r in reports}
        if len(n0_values) > 1:
            msg = (f"phases {[r['phase_id'] for r in reports]} share {config_file} but calibrated to "
                   f"different N0 values {sorted(n0_values)}; YAML not updated")
            logger.warning(msg)
            updates[config_file] = msg
            continue
        with open(yaml_path, "r", encoding="utf-8") as f:
            current = (yaml.safe_load(f) or {}).get("sensor_info", {})
        n0_key = f"N0_{method}" if f"N0_{method}" in current else "N0"
        values = {n0_key: reports[0]["optimal_N0"]}
        for key, value in reports[0]["sample_averages"].items():
            if current.get(key) is None:
                values[key] = round(value, 5)
        try:
            update_sensor_info_values(yaml_path, values)
        except Exception as e:
            logger.error(f"Failed to update {config_file}: {e}")
            updates[config_file] = f"failed: {e}"
            continue
        logger.info(f"Updated {config_file}: {values}")
        updates[config_file] = values

    # refresh the copies of the changed YAMLs in the data folders
    try:
        from copy_yamls import sync_sensor_configs
        sync_sensor_configs(sensor_key, sensor_data, log_fn=logger.info)
    except Exception as e:
        logger.warning(f"Could not sync updated YAMLs to data folders: {e}")
    return updates


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------

def _discover_calibration_sensors(sensors: dict) -> list:
    """Sheet names of sensors that have a calibration_<id>.csv in a calibration folder."""
    found = []
    for directory in CALIBRATION_DIRS:
        if not directory.exists():
            continue
        for path in directory.glob("calibration_*.csv"):
            sensor_filter = parse_sensor_filter([path.stem.replace("calibration_", "", 1)])
            for key, data in sensors.items():
                if sensor_matches(sensor_filter, key, data) and key not in found:
                    found.append(key)
    return found


def run_pipeline(sensor=None, update_yaml=False, process_config=None, campaign_path=None,
                 hours_around_calib=6, save_results=True, **kwargs):
    """Calibrate the given sensors (default: every sensor with a calibration file)."""
    sensors = load_sensors_from_excel(METADATA_FILE)
    if sensor:
        sensor_filter = parse_sensor_filter(sensor)
        targets = [k for k, v in sensors.items() if sensor_matches(sensor_filter, k, v)]
        if not targets:
            raise ValueError(f"No sensor in the metadata matches {sorted(sensor_filter)}")
    else:
        targets = _discover_calibration_sensors(sensors)
        if not targets:
            logger.info("No calibration data found and no --sensor specified. Skipping calibration.")
            return {}
    if campaign_path and len(targets) > 1:
        raise ValueError("--campaign can only be used with a single sensor")

    logger.info(f"Calibrating sensor(s): {targets}")
    results, failed = {}, {}
    for key in targets:
        try:
            results[key] = calibrate_sensor(
                sensor_id=key,
                campaign_path=campaign_path,
                hours_around_calib=hours_around_calib,
                update_yaml=update_yaml,
                save_results=save_results,
                process_config_path=Path(process_config) if process_config else None,
            )
        except Exception as e:
            logger.error(f"Failed calibration for sensor '{key}': {e}")
            failed[key] = str(e)
    if failed:
        raise RuntimeError(f"Calibration failed for {len(failed)} sensor(s): {sorted(failed)}")
    return results


def main():
    parser = argparse.ArgumentParser(description="Calibrate CRNS sensor N0 with neptoon, phase by phase.")
    parser.add_argument("--sensor", "-s", type=str, nargs="+", default=None,
                        help="Sensor(s) to calibrate (default: all sensors with a calibration file)")
    parser.add_argument("--campaign", "-c", type=str, default=None,
                        help="Path to calibration CSV file (single sensor only; "
                             "default: data/.calibration/calibration_<sensor>.csv)")
    parser.add_argument("--hours-window", "-w", type=int, default=6,
                        help="Hours of neutron data around each calibration time (default: 6)")
    parser.add_argument("--update-yaml", "-u", action="store_true",
                        help="Write the calibrated N0 into the YAML of each calibrated phase")
    parser.add_argument("--no-save", action="store_true",
                        help="Do not save calibration report files")
    parser.add_argument("--process-config", "-p", type=str, default=None,
                        help="Path to process config YAML (controls conversion method)")
    args = parser.parse_args()

    run_pipeline(
        sensor=args.sensor,
        update_yaml=args.update_yaml,
        process_config=args.process_config,
        campaign_path=args.campaign,
        hours_around_calib=args.hours_window,
        save_results=not args.no_save,
    )


if __name__ == "__main__":
    main()
