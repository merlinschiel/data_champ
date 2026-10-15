"""
Neptoon Processing Pipeline
===========================
Runs neptoon (QA, corrections, soil moisture, aggregation) on the merged time series
written by auto_append.py (data/.merged/merged_<sensor>.csv).

Modes:
  1. Default (unified): one neptoon pass over the whole merged series, using the master
     YAML (= the latest phase's sensor config). Avoids phase-boundary artifacts, but
     applies the latest site parameters (N0, soil properties, location) to all phases;
     a warning is logged when phases differ in those parameters.
  2. Phase-by-phase (--by-phase): the merged series is split by its 'phase' column and
     every phase is processed with its own YAML from the metadata, then stitched.

Outputs: data/.processed/processed_<sensor>.csv (with a 'phase' column), figures and
a JSON processing report per sensor.
"""

import json
from datetime import datetime
from pathlib import Path

import pandas as pd
import yaml

from log_utils import configure_utf8_console, get_logger
from merge_utils import write_csv_atomic
from neptoon_utils import (
    ROW_COLUMN,
    WORK_DIR,
    NeptoonRunError,
    build_neptoon_sensor_config,
    clean_work_dir,
    differing_site_parameters,
    phase_configs,
    read_merged_csv_text,
    run_neptoon,
    tag_phases,
    write_neptoon_input,
)
from sensor_metadata import (
    MASTER_CONFIG_DIR,
    MERGED_DIR,
    METADATA_FILE,
    PROCESSING_DIR,
    TARGET_DATA_DIR,
    load_phase_config,
    load_sensors_from_excel,
    parse_sensor_filter,
    phase_windows,
    read_phase_info,
    sensor_matches,
    sensor_name,
)

configure_utf8_console()
logger = get_logger("neptoon_pipeline", "neptoon_pipeline.log")

PROCESSED_DIR = TARGET_DATA_DIR / ".processed"
PROCESS_CONFIG_DIR = PROCESSING_DIR / "process_config"
DEFAULT_PROCESS_CONFIG = PROCESS_CONFIG_DIR / "default_config.yaml"


def _process_unified(name, text_df, windows, proc_cfg_path, work_dir):
    """One neptoon run over the full merged series with the master YAML."""
    master_yaml = MASTER_CONFIG_DIR / f"sensor_{name}_master.yaml"
    with open(master_yaml, "r", encoding="utf-8") as f:
        master_cfg = yaml.safe_load(f)
    csv_path = work_dir / f"{name}__all.csv"
    columns = write_neptoon_input(text_df, csv_path)
    neptoon_cfg, _ = build_neptoon_sensor_config(master_cfg, csv_path, columns)
    proc = run_neptoon(neptoon_cfg, proc_cfg_path, work_dir)
    df = proc.data_hub.crns_data_frame.drop(columns=[ROW_COLUMN], errors="ignore")
    return tag_phases(df, windows)


def _process_by_phase(name, text_df, windows, proc_cfg_path, work_dir):
    """Run neptoon per phase with that phase's YAML; returns (combined_df, failures)."""
    configs = phase_configs(windows)
    results, failures = [], []
    for w in windows:
        phase_id = w["phase_id"]
        part = text_df[text_df["phase"] == phase_id]
        if part.empty:
            continue
        if phase_id not in configs:
            failures.append(f"{phase_id}: config '{w.get('config_file')}' not found")
            logger.warning(f"  -> {failures[-1]}")
            continue
        logger.info(f"  -> {phase_id} ({w.get('location')}, {w.get('config_file')}): {len(part)} rows")
        csv_path = work_dir / f"{name}__{phase_id}.csv"
        columns = write_neptoon_input(part, csv_path)
        neptoon_cfg, _ = build_neptoon_sensor_config(configs[phase_id], csv_path, columns)
        try:
            proc = run_neptoon(neptoon_cfg, proc_cfg_path, work_dir)
        except Exception as e:
            failures.append(f"{phase_id}: {e}")
            logger.warning(f"  -> {phase_id} failed: {e}")
            continue
        df = proc.data_hub.crns_data_frame.drop(columns=[ROW_COLUMN], errors="ignore").copy()
        df["phase"] = phase_id
        results.append(df)

    if not results:
        raise NeptoonRunError("no phase produced data: " + "; ".join(failures))
    combined = pd.concat(results).sort_index()
    # aggregation bins at a phase boundary can appear in both phases; keep the later phase
    combined = combined[~combined.index.duplicated(keep="last")]
    return combined, failures


class SensorSkipped(Exception):
    """The sensor is not set up for neptoon processing (e.g. no neutron columns)."""


def _has_neutron_columns(cfg: dict) -> bool:
    kci = ((cfg or {}).get("time_series_data") or {}).get("key_column_info") or {}
    return bool(kci.get("epithermal_neutron_columns"))


def process_sensor(sensor_key, sensor_data, proc_cfg_path, by_phase=False, generate_plots=True):
    """Process one metadata sensor. Returns the report dict; raises on failure."""
    name = sensor_name(sensor_key, sensor_data)
    latest_cfg = next((load_phase_config(w["config_file"])
                       for w in reversed(phase_windows(sensor_data.get("phase", [])))
                       if load_phase_config(w["config_file"])), None)
    if not _has_neutron_columns(latest_cfg):
        raise SensorSkipped("no epithermal_neutron_columns in its sensor config")
    merged_csv = MERGED_DIR / f"merged_{name}.csv"
    master_yaml = MASTER_CONFIG_DIR / f"sensor_{name}_master.yaml"

    windows = read_phase_info(name)
    if windows is None or not merged_csv.exists() or not master_yaml.exists():
        raise RuntimeError(f"No merged data for '{name}'; run the merge first "
                           f"(auto_append.py --sensor {sensor_key})")

    text_df = read_merged_csv_text(merged_csv)
    if "phase" not in text_df.columns:
        raise RuntimeError(f"{merged_csv.name} has no 'phase' column; re-run the merge "
                           f"(auto_append.py --sensor {sensor_key} --force)")

    present = set(text_df["phase"].unique())
    active_windows = [w for w in windows if w["phase_id"] in present]
    work_dir = WORK_DIR / name
    clean_work_dir(work_dir)

    mode = "phase_by_phase" if by_phase else "unified_merged"
    failures = []
    try:
        if not by_phase:
            diffs = differing_site_parameters(active_windows)
            if diffs:
                logger.warning(
                    f"  -> Phases of '{name}' differ in {sorted(diffs)}; unified mode applies the "
                    f"latest phase values to the whole series. Use --by-phase for phase-specific values.")
            out_df = _process_unified(name, text_df, windows, proc_cfg_path, work_dir)
        else:
            out_df, failures = _process_by_phase(name, text_df, windows, proc_cfg_path, work_dir)
    finally:
        clean_work_dir(work_dir)

    if out_df is None or out_df.empty:
        raise NeptoonRunError(f"No data processed for '{name}'")

    out_df = out_df[["phase"] + [c for c in out_df.columns if c != "phase"]]

    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    out_file = PROCESSED_DIR / f"processed_{name}.csv"
    to_save = out_df.copy()
    to_save.index = pd.DatetimeIndex(to_save.index).strftime("%Y-%m-%dT%H:%M:%SZ")
    write_csv_atomic(to_save.reset_index(names="Date_Time"), out_file)

    logger.info(f"  -> PROCESSED TIMESERIES: {out_file.name} ({len(out_df)} rows, "
                f"{out_df.index.min()} to {out_df.index.max()}, mode: {mode})")
    sm_valid = out_df["smv"].notna().mean() if "smv" in out_df.columns else 0.0
    logger.info(f"  -> Valid soil moisture values: {sm_valid:.1%}")

    if generate_plots:
        from plots_and_reports import generate_neptoon_figures
        sensor_fig_dir = PROCESSED_DIR / "figures" / str(name)
        try:
            latest_cfg = next((load_phase_config(w["config_file"]) for w in reversed(active_windows)
                               if load_phase_config(w["config_file"])), {})
            fig_files = generate_neptoon_figures(
                df=out_df.drop(columns=["phase"]),
                sensor_name=str(name),
                output_dir=sensor_fig_dir,
                sensor_info_dict=(latest_cfg or {}).get("sensor_info", {}),
                create_pdf_report=True,
            )
            logger.info(f"  -> Generated {len(fig_files)} figures/reports in {sensor_fig_dir}")
        except Exception as fig_err:
            logger.warning(f"  -> Plot generation failed for '{name}': {fig_err}")

    report = {
        "sensor": name,
        "metadata_sheet": sensor_key,
        "timestamp": datetime.now().isoformat(),
        "mode": mode,
        "phases_configured": len(windows),
        "phases_with_data": sorted(present, key=lambda p: int(p.replace("phase", "") or 0)),
        "phase_failures": failures,
        "total_rows": len(out_df),
        "valid_soil_moisture_fraction": round(float(sm_valid), 4),
        "date_range": [str(out_df.index.min()), str(out_df.index.max())],
        "output_file": str(out_file),
        "process_config": str(proc_cfg_path),
        "output_columns": list(out_df.columns),
    }
    proc_log_dir = PROCESSED_DIR / "processing_log"
    proc_log_dir.mkdir(parents=True, exist_ok=True)
    with open(proc_log_dir / f"{name}_report.json", "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    return report


def run_pipeline(sensor=None, process_config=None, generate_plots=True, by_phase=False):
    """Run neptoon processing for the selected (default: all) sensors in the metadata.

    Raises RuntimeError listing the sensors that failed.
    """
    proc_cfg_path = Path(process_config) if process_config else DEFAULT_PROCESS_CONFIG
    if not proc_cfg_path.exists():
        raise FileNotFoundError(f"Process config not found: {proc_cfg_path}")
    logger.info(f"Using process config: {proc_cfg_path}")
    logger.info(f"Processing mode: {'phase-by-phase' if by_phase else 'unified merged (single pass)'}")

    sensor_filter = parse_sensor_filter(sensor)
    if sensor_filter:
        logger.info(f"Filtering neptoon pipeline to sensor(s): {sorted(sensor_filter)}")

    sensors = load_sensors_from_excel(METADATA_FILE)
    selected = {k: v for k, v in sensors.items() if sensor_matches(sensor_filter, k, v)}
    if not selected:
        raise ValueError(f"No sensor in the metadata matches {sorted(sensor_filter or [])}")

    failed = {}
    for sensor_key, sensor_data in selected.items():
        logger.info(f"=== Processing sensor: '{sensor_key}' ===")
        try:
            process_sensor(sensor_key, sensor_data, proc_cfg_path,
                           by_phase=by_phase, generate_plots=generate_plots)
        except SensorSkipped as e:
            logger.info(f"  -> Skipped '{sensor_key}': {e}")
        except Exception as e:
            logger.error(f"  -> Processing failed for '{sensor_key}': {e}")
            failed[sensor_key] = str(e)

    if failed:
        raise RuntimeError(f"Neptoon processing failed for {len(failed)} sensor(s): {sorted(failed)}")


def main():
    import argparse
    parser = argparse.ArgumentParser(
        description="CRNS Neptoon Processing Pipeline — QA, corrections, soil moisture, aggregation."
    )
    parser.add_argument("--sensor", "-s", type=str, nargs="+", default=None,
                        help="Process only specific sensor(s)")
    parser.add_argument("--process-config", type=str, default=None,
                        help="Path to process config YAML (default: processing/process_config/default_config.yaml)")
    parser.add_argument("--by-phase", action="store_true",
                        help="Process each phase with its own YAML and stitch the results "
                             "(default is a single unified pass)")
    parser.add_argument("--no-plots", action="store_true",
                        help="Disable generation of figures and PDF reports")
    args, _ = parser.parse_known_args()
    run_pipeline(
        sensor=args.sensor,
        process_config=args.process_config,
        generate_plots=not args.no_plots,
        by_phase=args.by_phase,
    )


if __name__ == "__main__":
    main()
