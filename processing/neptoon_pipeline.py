"""
Neptoon Processing Pipeline — Dual-Mode Orchestrator
===================================================
Executes CRNS processing via Neptoon's ProcessWithConfig:

Modes:
  1. Default (Unified Merged): Runs Neptoon in a single continuous pass on the merged
     timeseries (merged_<sensor>.csv). Avoids phase boundary artifacts and multiple NMDB queries.
  2. Phase-by-Phase (--by-phase): Runs Neptoon per phase directory and stitches the results.
     Useful when site characteristics (N0, vegetation, soil properties) changed between phases.
"""

import sys
import os

# Fix Windows console UTF-8 encoding for Rich console unicode characters
os.environ['PYTHONIOENCODING'] = 'utf-8'
if hasattr(sys.stdout, 'reconfigure'):
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass
if hasattr(sys.stderr, 'reconfigure'):
    try:
        sys.stderr.reconfigure(encoding='utf-8')
    except Exception:
        pass

import yaml
import json
import logging
import shutil
from pathlib import Path
from datetime import datetime

from merge_utils import (
    scan_yaml_files,
    group_configs_by,
    neptoon_process_phase,
    concat_processed_phases,
)
from plots_and_reports import generate_neptoon_figures

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

PROCESSING_DIR = Path(__file__).parent.resolve()
META_DIR = PROCESSING_DIR / "meta"
LOG_DIR = META_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOG_DIR / "neptoon_pipeline.log"

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE, mode='w', encoding='utf-8'),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger("neptoon_pipeline")

# Default process config location
PROCESS_CONFIG_DIR = PROCESSING_DIR / "process_config"
DEFAULT_PROCESS_CONFIG = PROCESS_CONFIG_DIR / "default_config.yaml"


def _process_sensor_master(name: str, master_yaml_path: Path, proc_cfg_path: Path, log_fn=logger.info):
    """Run Neptoon ProcessWithConfig directly on sensor master YAML and merged CSV."""
    from neptoon.workflow.process_with_config import ProcessWithConfig

    with open(master_yaml_path, 'r', encoding='utf-8') as f:
        cfg = yaml.safe_load(f)

    temp_master = master_yaml_path.parent / f"_temp_{name}_master.yaml"
    temp_out = master_yaml_path.parent / f"_neptoon_{name}_out"

    try:
        cfg.setdefault('figures', {})['create_figures'] = True
        cfg['figures']['make_all_figures'] = False
        cfg['figures']['custom_list'] = []

        cfg.setdefault('data_storage', {})['create_report'] = False
        cfg['data_storage']['save_location'] = str(temp_out)
        cfg['data_storage']['save_folder'] = str(temp_out)

        with open(temp_master, 'w', encoding='utf-8') as f:
            yaml.dump(cfg, f, default_flow_style=False, sort_keys=False)

        proc = ProcessWithConfig(
            path_to_sensor_config=str(temp_master),
            path_to_process_config=str(proc_cfg_path),
        )

        try:
            proc.run_full_process()
        except Exception as run_err:
            log_fn(f"  -> Neptoon save/figure notice: {run_err}")

        if proc.data_hub is None or proc.data_hub.crns_data_frame is None:
            log_fn(f"  -> No data returned from Neptoon for sensor '{name}'")
            return None

        df = proc.data_hub.crns_data_frame.copy()
        log_fn(f"  -> Neptoon processed {len(df)} rows for '{name}' in unified pass")
        return df

    except Exception as e:
        log_fn(f"  -> Neptoon error for '{name}': {e}")
        return None
    finally:
        if temp_master.exists():
            try:
                temp_master.unlink()
            except Exception:
                pass
        if temp_out.exists():
            try:
                shutil.rmtree(temp_out, ignore_errors=True)
            except Exception:
                pass


def run_pipeline(sensor=None, only_sensors=None, process_config=None, generate_plots=True, by_phase=False):
    """Run Neptoon processing for specified sensor(s) or all sensors.

    Parameters
    ----------
    sensor : str or list, optional
        Sensor name(s) to process. None = all.
    only_sensors : str or list, optional
        Alias for sensor (backward compat).
    process_config : str or Path, optional
        Path to process config YAML. Defaults to processing/process_config/default_config.yaml.
    generate_plots : bool, optional
        Whether to generate Neptoon figures and PDF report (default: True).
    by_phase : bool, optional
        If True, processes phase-by-phase and concatenates results.
        If False (default), runs in unified single pass on merged master timeseries.
    """
    PROJECT_ROOT = PROCESSING_DIR.parent
    TARGET_DATA_DIR = PROJECT_ROOT / "data"
    MERGED_DIR = TARGET_DATA_DIR / ".merged"
    PROCESSED_DIR = TARGET_DATA_DIR / ".processed"
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)

    CALIB_DIR = PROCESSED_DIR / "calibration_results"
    CALIB_DIR.mkdir(parents=True, exist_ok=True)

    PROC_LOG_DIR = PROCESSED_DIR / "processing_log"
    PROC_LOG_DIR.mkdir(parents=True, exist_ok=True)

    FIGURES_DIR = PROCESSED_DIR / "figures"
    FIGURES_DIR.mkdir(parents=True, exist_ok=True)

    # Resolve process config
    if process_config:
        proc_cfg_path = Path(process_config)
    else:
        proc_cfg_path = DEFAULT_PROCESS_CONFIG

    if not proc_cfg_path.exists():
        logger.error(f"Process config not found: {proc_cfg_path}")
        return
    logger.info(f"Using process config: {proc_cfg_path}")
    logger.info(f"Processing mode: {'Phase-by-Phase (--by-phase)' if by_phase else 'Unified Merged (Single-pass)'}")

    # Resolve sensor filter
    target_sensors = sensor or only_sensors
    if target_sensors:
        if isinstance(target_sensors, str):
            target_sensors = [x.strip() for x in target_sensors.replace('+', ',').split(',') if x.strip()]
        elif isinstance(target_sensors, (list, tuple, set)):
            flat = []
            for item in target_sensors:
                if isinstance(item, str):
                    flat.extend([x.strip() for x in item.replace('+', ',').split(',') if x.strip()])
                else:
                    flat.append(str(item))
            target_sensors = flat
        target_sensors = set(str(s) for s in target_sensors)
        logger.info(f"Filtering Neptoon pipeline to sensor(s): {target_sensors}")
    else:
        logger.info("Running Neptoon pipeline for all sensors...")

    # Scan YAMLs and group by sensor name
    yaml_configs = scan_yaml_files(TARGET_DATA_DIR)
    groups = group_configs_by(yaml_configs, 'name')

    if target_sensors:
        groups = {k: v for k, v in groups.items() if k in target_sensors}

    if not groups:
        logger.warning("No matching sensors or YAML configurations found.")
        return

    for name, configs in groups.items():
        logger.info(f"=== Processing sensor: '{name}' ===")
        out_df = None
        phases_count = len(configs)

        if not by_phase:
            # Mode A: Unified pass using master YAML and merged CSV
            master_yaml = MERGED_DIR / "sensor_configs" / f"sensor_{name}_master.yaml"
            merged_csv = MERGED_DIR / f"merged_{name}.csv"

            if not master_yaml.exists() or not merged_csv.exists():
                logger.info(f"  -> Master YAML / merged CSV not found for '{name}'. Running merge first...")
                import auto_append
                auto_append.run_pipeline(sensor=name)

            if master_yaml.exists() and merged_csv.exists():
                logger.info(f"  -> Running Neptoon ProcessWithConfig on unified master series...")
                out_df = _process_sensor_master(name, master_yaml, proc_cfg_path, log_fn=logger.info)
            else:
                logger.warning(f"  -> Could not prepare master dataset for '{name}'. Falling back to phase mode.")
                by_phase = True

        if by_phase or out_df is None or out_df.empty:
            # Mode B: Phase-by-phase processing and concat
            logger.info(f"  -> Running phase-by-phase processing ({len(configs)} phase(s))...")
            dfs = []
            for entry in configs:
                y_path = entry['yaml_path']
                cfg = entry['config']
                phase_label = y_path.parent.relative_to(TARGET_DATA_DIR)
                logger.info(f"  Processing phase: {phase_label}")

                df = neptoon_process_phase(
                    yaml_path=y_path,
                    config=cfg,
                    process_config_path=proc_cfg_path,
                    log_fn=logger.info,
                )
                if df is not None and not df.empty:
                    dfs.append(df)

            if dfs:
                out_df = concat_processed_phases(dfs, log_fn=logger.info)

        if out_df is None or out_df.empty:
            logger.warning(f"  -> No data successfully processed for sensor '{name}'.")
            continue

        # Save processed timeseries
        out_file = PROCESSED_DIR / f"processed_{name}.csv"
        out_df.to_csv(out_file, index_label="Date_Time", date_format='%Y-%m-%dT%H:%M:%SZ')

        logger.info(f"  -> PROCESSED TIMESERIES: {out_file.name}")
        logger.info(f"  -> Total rows: {len(out_df)}")
        logger.info(f"  -> Date range: {out_df.index.min()} to {out_df.index.max()}")

        sm_cols = [c for c in out_df.columns if 'soil_moisture' in c or 'corrected' in c]
        logger.info(f"  -> Output columns: {sm_cols}")

        # Generate Neptoon figures and PDF report
        if generate_plots:
            sensor_fig_dir = FIGURES_DIR / str(name)
            logger.info(f"  -> Generating plots and report for '{name}'...")
            try:
                first_cfg = configs[0]['config'] if configs else {}
                s_info = first_cfg.get('sensor_info', {})
                fig_files = generate_neptoon_figures(
                    df=out_df,
                    sensor_name=str(name),
                    output_dir=sensor_fig_dir,
                    sensor_info_dict=s_info,
                    create_pdf_report=True,
                )
                logger.info(f"  -> Generated {len(fig_files)} figures/reports in {sensor_fig_dir}")
            except Exception as fig_err:
                logger.warning(f"  -> Plot generation notice: {fig_err}")

        # Save processing report
        report = {
            "sensor": name,
            "timestamp": datetime.now().isoformat(),
            "mode": "phase_by_phase" if by_phase else "unified_merged",
            "phases_configured": phases_count,
            "total_rows": len(out_df),
            "date_range": [str(out_df.index.min()), str(out_df.index.max())],
            "output_file": str(out_file),
            "process_config": str(proc_cfg_path),
            "output_columns": list(out_df.columns),
        }
        report_file = PROC_LOG_DIR / f"{name}_report.json"
        try:
            with open(report_file, 'w', encoding='utf-8') as f:
                json.dump(report, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.warning(f"  -> Failed to write report: {e}")


def main():
    import argparse
    parser = argparse.ArgumentParser(
        description="CRNS Neptoon Processing Pipeline"
    )
    parser.add_argument(
        "--sensor", "-s", type=str, nargs="+", default=None,
        help="Process only specific sensor name(s)"
    )
    parser.add_argument(
        "--process-config", type=str, default=None,
        help="Path to process config YAML (default: processing/process_config/default_config.yaml)"
    )
    parser.add_argument(
        "--by-phase", action="store_true",
        help="Process phase-by-phase and concatenate (default is unified master processing)"
    )
    parser.add_argument(
        "--no-plots", action="store_true",
        help="Disable automatic generation of Neptoon figures and PDF reports"
    )
    args, _ = parser.parse_known_args()
    run_pipeline(
        sensor=args.sensor,
        process_config=args.process_config,
        generate_plots=not args.no_plots,
        by_phase=args.by_phase,
    )


if __name__ == "__main__":
    main()
