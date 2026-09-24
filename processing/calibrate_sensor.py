"""
Sensor Calibration Workflow for Neptoon
=======================================

Features:
- Calibrates N0 using field campaign sample data (gravimetric / FDR)
- Calculates Schrön et al. (2017) horizontal and vertical spatial weights
- Extracts footprint radius and weighted field soil moisture
- Optionally writes calibrated N0 back to sensor YAML configuration files
- Saves calibration reports and sample weighting tables

Usage:
  python processing/calibrate_sensor.py --sensor 01
  python processing/calibrate_sensor.py --sensor 01 --update-yaml
  python processing/calibrate_sensor.py --sensor 01 --process-config processing/process_config/hourly_config.yaml
"""

import sys
import os
import shutil
import argparse
import json
import logging
from pathlib import Path
import pandas as pd
import numpy as np
import yaml

os.environ['PYTHONIOENCODING'] = 'utf-8'
if hasattr(sys.stdout, 'reconfigure'):
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass

PROCESSING_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = PROCESSING_DIR.parent
if str(PROCESSING_DIR) not in sys.path:
    sys.path.insert(0, str(PROCESSING_DIR))

from neptoon.calibration import CalibrationConfiguration, CalibrationStation
from neptoon.columns import ColumnInfo
from neptoon.workflow.process_with_config import ProcessWithConfig
from merge_utils import _patch_yaml_for_neptoon, scan_yaml_files, group_configs_by
from plots_and_reports import generate_calibration_plots

LOG_DIR = PROCESSING_DIR / "meta" / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(LOG_DIR / "calibration.log", mode='w', encoding='utf-8'),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger("calibrate_sensor")


def _load_sensor_config(sensor_id: str) -> dict:
    """Load sensor YAML config from processing/sensor_config/<sensor_id>/."""
    cfg_dir = PROCESSING_DIR / "sensor_config" / sensor_id
    if not cfg_dir.exists() and sensor_id.isdigit():
        cfg_dir = PROCESSING_DIR / "sensor_config" / f"{int(sensor_id):02d}"

    if not cfg_dir.exists():
        logger.warning(f"Sensor config directory not found: {cfg_dir}")
        return {}

    yaml_files = sorted(cfg_dir.glob("*.yaml"))
    if not yaml_files:
        return {}

    with open(yaml_files[0], 'r', encoding='utf-8') as f:
        return yaml.safe_load(f) or {}


def _load_sm_estimation_settings(process_config_path: Path = None) -> dict:
    """Extract soil_moisture_estimation settings from process config YAML.

    Returns dict with 'method' and 'koehli_etal_2021_parameterset'.
    """
    cfg_path = process_config_path or (PROCESSING_DIR / "process_config" / "default_config.yaml")
    with open(cfg_path, 'r', encoding='utf-8') as f:
        proc_cfg = yaml.safe_load(f) or {}

    sm_est = proc_cfg.get('correction_steps', {}).get('soil_moisture_estimation', {})
    return {
        'method': sm_est.get('method', 'desilets_etal_2010'),
        'koehli_params': sm_est.get('koehli_etal_2021_parameterset', 'Mar21_mcnp_drf'),
    }


def load_calibration_dataset(sensor_id: str, campaign_path: str = None,
                             required_columns: list = None) -> pd.DataFrame:
    """Load and validate the calibration dataset for a sensor."""
    if campaign_path:
        calib_file = Path(campaign_path)
    else:
        candidates = [
            PROJECT_ROOT / "data" / "calibration" / f"calibration_{sensor_id}.csv",
            PROJECT_ROOT / "data" / "calibration" / f"calibration_{int(sensor_id):02d}.csv" if sensor_id.isdigit() else None,
            PROJECT_ROOT / "data" / f"calibration_{sensor_id}.csv",
            PROCESSING_DIR / "calibration" / "data" / f"calibration_{sensor_id}.csv",
            PROCESSING_DIR / "calibration" / "data" / f"calibration_{int(sensor_id):02d}.csv" if sensor_id.isdigit() else None,
        ]
        calib_file = next((c for c in candidates if c is not None and c.exists()), None)

    if calib_file is None or not calib_file.exists():
        raise FileNotFoundError(
            f"Calibration data not found for sensor '{sensor_id}'. Looked in data/calibration/. "
            f"Please place calibration CSV at data/calibration/calibration_{sensor_id}.csv."
        )

    logger.info(f"Loading calibration dataset from: {calib_file}")
    df = pd.read_csv(calib_file)

    if required_columns:
        missing = [c for c in required_columns if c not in df.columns]
        if missing:
            raise ValueError(
                f"Calibration CSV '{calib_file.name}' missing required columns: {missing}. "
                f"Available columns: {list(df.columns)}. "
                f"Check your calibration CSV or update the column mappings in "
                f"processing/sensor_config/{sensor_id}/*.yaml under 'calibration.key_column_names'."
            )

    return df, calib_file


def get_corrected_timeseries_for_sensor(
    sensor_id: str,
    process_config_path: Path = None,
) -> pd.DataFrame:
    """Obtain corrected neutron timeseries by processing raw phases through Neptoon."""
    if process_config_path is None:
        process_config_path = PROCESSING_DIR / "process_config" / "default_config.yaml"

    target_data_dir = PROJECT_ROOT / "data"
    yaml_configs = scan_yaml_files(target_data_dir)
    groups = group_configs_by(yaml_configs, 'name')

    target_entries = groups.get(sensor_id)
    if not target_entries and sensor_id.isdigit():
        norm_key = str(int(sensor_id))
        target_entries = groups.get(norm_key) or groups.get(f"{int(sensor_id):02d}")

    if not target_entries:
        raise ValueError(f"No phase configurations found for sensor '{sensor_id}' in {target_data_dir}")

    logger.info(f"Preparing corrected timeseries for sensor '{sensor_id}' across {len(target_entries)} phase(s)...")

    corrected_dfs = []
    for entry in target_entries:
        y_path = entry['yaml_path']
        cfg = entry['config']

        patched = _patch_yaml_for_neptoon(y_path, cfg)
        if patched is None:
            continue

        temp_y = y_path.parent / "_temp_calib_cfg.yaml"
        temp_out = y_path.parent / "_temp_calib_out"
        try:
            patched.setdefault('figures', {})['create_figures'] = True
            patched['figures']['make_all_figures'] = False
            patched['figures']['custom_list'] = []
            patched.setdefault('data_storage', {})['create_report'] = False
            patched['data_storage']['save_location'] = str(temp_out)
            patched['data_storage']['save_folder'] = str(temp_out)

            with open(temp_y, 'w', encoding='utf-8') as f:
                yaml.dump(patched, f, default_flow_style=False, sort_keys=False)

            p = ProcessWithConfig(
                path_to_sensor_config=str(temp_y),
                path_to_process_config=str(process_config_path),
            )

            p.data_hub = p._create_data_hub(sensor_config=p.sensor_config)
            p.data_hub = p._attach_nmdb_data(p.data_hub)
            p.data_hub = p._prepare_static_values(p.data_hub)
            p.data_hub = p._prepare_additional_columns(p.data_hub)

            p.data_hub = p._apply_quality_assessment(
                data_hub=p.data_hub,
                sensor_config=p.sensor_config,
                partial_config=p.process_config.neutron_quality_assessment,
                name_of_target="raw_neutrons",
            )
            if hasattr(p.sensor_config, "neutron_quality_assessment") and p.sensor_config.neutron_quality_assessment:
                p.data_hub = p._apply_quality_assessment(
                    data_hub=p.data_hub,
                    sensor_config=p.sensor_config,
                    partial_config=p.sensor_config.neutron_quality_assessment,
                    name_of_target="raw_neutrons",
                )
            p.data_hub = p._apply_quality_assessment(
                data_hub=p.data_hub,
                sensor_config=p.sensor_config,
                partial_config=p.sensor_config.input_data_qa,
                name_of_target=None,
            )

            p.data_hub = p._select_corrections(
                data_hub=p.data_hub,
                process_config=p.process_config,
                sensor_config=p.sensor_config,
            )
            p.data_hub = p._correct_neutrons(p.data_hub)

            if p.data_hub.crns_data_frame is not None and not p.data_hub.crns_data_frame.empty:
                corrected_dfs.append(p.data_hub.crns_data_frame.copy())
        except Exception as err:
            logger.warning(f"  Note processing phase {y_path.parent.name}: {err}")
        finally:
            if temp_y.exists():
                try:
                    temp_y.unlink()
                except Exception:
                    pass

    if not corrected_dfs:
        raise RuntimeError(f"Failed to generate corrected neutron data for sensor '{sensor_id}'")

    full_ts = pd.concat(corrected_dfs)
    full_ts = full_ts[~full_ts.index.duplicated(keep='first')].sort_index()
    logger.info(f"Loaded timeseries for calibration with {len(full_ts)} records ({full_ts.index.min()} to {full_ts.index.max()})")
    return full_ts


def calibrate_sensor(
    sensor_id: str = None,
    campaign_path: str = None,
    hours_around_calib: int = 6,
    update_yaml: bool = False,
    save_results: bool = True,
    process_config_path: Path = None,
) -> dict:
    """Run full calibration for a sensor using Neptoon.

    Parameters
    ----------
    sensor_id : str
        Sensor identifier (e.g. '01')
    campaign_path : str, optional
        Path to calibration CSV.
    hours_around_calib : int
        Window in hours around calibration datetime (default: 6).
    update_yaml : bool
        If True, updates N0 in sensor YAML templates.
    save_results : bool
        If True, saves report JSON and weights table CSV.
    process_config_path : Path, optional
        Path to process config YAML. Defaults to process_config/default_config.yaml.

    Returns
    -------
    dict
        Calibration summary and calculated metrics.
    """
    if sensor_id is None:
        raise ValueError("sensor_id is required. Pass --sensor <id> or specify sensor_id.")

    if process_config_path is None:
        process_config_path = PROCESSING_DIR / "process_config" / "default_config.yaml"

    sm_settings = _load_sm_estimation_settings(process_config_path)
    method = sm_settings['method']
    koehli_params = sm_settings['koehli_params']

    logger.info("==================================================")
    logger.info(f" Starting Calibration for Sensor '{sensor_id}'")
    logger.info(f" Conversion Method: {method}" + (f" ({koehli_params})" if method == "koehli_etal_2021" else ""))
    logger.info("==================================================")

    sensor_cfg = _load_sensor_config(sensor_id)
    s_info_cfg = sensor_cfg.get('sensor_info', {})
    calib_col_cfg = sensor_cfg.get('calibration', {}).get('key_column_names', {})

    dt_col = calib_col_cfg.get('date_time', 'DateTime_utc')
    col_map = {
        'date_time': dt_col,
        'profile_id': calib_col_cfg.get('profile_id', 'Profile_ID'),
        'distance': calib_col_cfg.get('radial_distance_from_sensor', 'Distance_to_CRNS_m'),
        'sample_depth': calib_col_cfg.get('sample_depth', 'Profile_Depth_cm'),
        'soil_moisture': calib_col_cfg.get('gravimetric_soil_moisture', 'SoilMoisture_g_g'),
        'bulk_density': calib_col_cfg.get('bulk_density_of_sample', 'DryBulkDensity_g_cm3'),
        'soc': calib_col_cfg.get('soil_organic_carbon', 'SoilOrganicCarbon_g_g'),
        'lattice_water': calib_col_cfg.get('lattice_water', 'LatticeWater_g_g'),
    }

    required_cols = list(col_map.values())
    calib_df, calib_file_path = load_calibration_dataset(
        sensor_id, campaign_path, required_columns=required_cols
    )

    ts_df = get_corrected_timeseries_for_sensor(sensor_id, process_config_path)

    calib_df['DateTime_parsed'] = pd.to_datetime(calib_df[dt_col], utc=True)
    ts_min, ts_max = ts_df.index.min(), ts_df.index.max()

    valid_mask = (calib_df['DateTime_parsed'] >= ts_min) & (calib_df['DateTime_parsed'] <= ts_max)
    skipped_dates = calib_df.loc[~valid_mask, 'DateTime_parsed'].dt.date.unique()
    if len(skipped_dates) > 0:
        logger.warning(
            f"Skipping calibration date(s) without timeseries coverage ({ts_min.date()} to {ts_max.date()}): {skipped_dates}"
        )
        calib_df = calib_df[valid_mask].copy()

    if calib_df.empty:
        raise ValueError(
            f"No calibration data falls within the sensor timeseries range ({ts_min} to {ts_max})."
        )

    calib_df = calib_df.drop(columns=['DateTime_parsed'])
    unique_calib_dates = pd.to_datetime(calib_df[dt_col]).dt.date.unique()
    logger.info(f"Running calibration for dates: {unique_calib_dates} ({len(calib_df)} samples)")

    calib_config = CalibrationConfiguration(
        neutron_conversion_method=method,
        koehli_parameters=koehli_params if method == "koehli_etal_2021" else "Mar21_mcnp_drf",
        hours_of_data_around_calib=hours_around_calib,
        calib_data_date_time_column_name=col_map['date_time'],
        calib_data_date_time_format="%Y-%m-%d %H:%M",
        profile_id_column=col_map['profile_id'],
        distance_column=col_map['distance'],
        sample_depth_column=col_map['sample_depth'],
        soil_moisture_gravimetric_column=col_map['soil_moisture'],
        bulk_density_of_sample_column=col_map['bulk_density'],
        soil_organic_carbon_column=col_map['soc'],
        lattice_water_column=col_map['lattice_water'],
    )

    from magazine import Magazine
    from neptoon.hub.crns_data_hub import CRNSDataHub
    from neptoon.config.configuration_input import SensorInfo

    Magazine.active = True
    Magazine.clean()

    lat = float(s_info_cfg.get('latitude', 0))
    lon = float(s_info_cfg.get('longitude', 0))
    elev = float(s_info_cfg.get('elevation', 0))
    init_n0 = float(s_info_cfg.get('N0', 1000))

    s_info = SensorInfo(
        name=str(s_info_cfg.get('name', sensor_id)),
        identifier=str(s_info_cfg.get('identifier', sensor_id)),
        country=str(s_info_cfg.get('country', '')),
        install_date=str(s_info_cfg.get('install_date', '2019-01-01')),
        time_zone=int(s_info_cfg.get('time_zone', 0)),
        latitude=lat,
        longitude=lon,
        elevation=elev,
        N0=init_n0,
    )

    hub = CRNSDataHub(
        crns_data_frame=ts_df,
        sensor_info=s_info,
        calibration_samples_data=calib_df,
    )

    hub.calibrate_station(config=calib_config)
    optimal_n0 = float(hub.sensor_info.N0)
    optimal_n0_rounded = int(round(optimal_n0))

    hub.produce_soil_moisture_estimates()
    hub.create_figures(create_all=True)

    cal_station = hub.calibrator
    results_df = cal_station.return_calibration_results_data_frame()
    weights_df = cal_station.return_weighting_dataframe()

    site_bd = float(hub.sensor_info.avg_dry_soil_bulk_density) if hub.sensor_info.avg_dry_soil_bulk_density is not None else float(cal_station.context.list_of_profiles[0].site_avg_bulk_density)
    site_soc = float(hub.sensor_info.avg_soil_organic_carbon) if hub.sensor_info.avg_soil_organic_carbon is not None else float(cal_station.context.list_of_profiles[0].site_avg_organic_carbon)
    site_lw = float(hub.sensor_info.avg_lattice_water) if hub.sensor_info.avg_lattice_water is not None else float(cal_station.context.list_of_profiles[0].site_avg_lattice_water)

    logger.info("\n" + "=" * 54)
    logger.info(f" CALIBRATION RESULT FOR SENSOR '{sensor_id}'")
    logger.info("=" * 54)
    logger.info(f" Optimal N0 : {optimal_n0_rounded} cph (exact: {optimal_n0:.2f})")
    logger.info(f" Method     : {method}")
    logger.info(f" Site Bulk Density (dry)  : {site_bd:.4f} g/cm³")
    logger.info(f" Site Soil Organic Carbon : {site_soc:.5f} g/g")
    logger.info(f" Site Lattice Water       : {site_lw:.5f} g/g")
    logger.info("-" * 54)
    logger.info(f" Campaign Summary:\n{results_df.to_string(index=False)}")
    logger.info("=" * 54)

    out_results_dir = PROJECT_ROOT / "data" / ".processed" / "calibration_results"
    out_results_dir.mkdir(parents=True, exist_ok=True)

    report_dict = {
        "sensor": sensor_id,
        "calibration_timestamp": pd.Timestamp.now().isoformat(),
        "method": method,
        "koehli_parameters": koehli_params if method == "koehli_etal_2021" else None,
        "optimal_N0": optimal_n0_rounded,
        "optimal_N0_exact": float(optimal_n0),
        "site_properties": {
            "dry_soil_bulk_density_g_cm3": site_bd,
            "soil_organic_carbon_g_g": site_soc,
            "lattice_water_g_g": site_lw,
        },
        "campaign_results": results_df.to_dict(orient="records"),
        "calibration_data_file": str(calib_file_path),
    }

    if save_results:
        report_file = out_results_dir / f"calibration_report_{sensor_id}.json"
        with open(report_file, 'w', encoding='utf-8') as f:
            json.dump(report_dict, f, indent=2, default=str)
        logger.info(f"Saved calibration report -> {report_file}")

        if weights_df is not None and not weights_df.empty:
            weights_file = out_results_dir / f"calibration_sample_weights_{sensor_id}.csv"
            weights_df.to_csv(weights_file, index=False)
            logger.info(f"Saved sample weights -> {weights_file}")

        try:
            generate_calibration_plots(
                sensor_id=sensor_id,
                ts_df=ts_df,
                results_df=results_df,
                weights_df=weights_df,
                report_dict=report_dict,
                output_dir=out_results_dir,
            )
        except Exception as plot_err:
            logger.warning(f"Calibration plot generation notice: {plot_err}")

    if update_yaml:
        update_sensor_yaml_n0(sensor_id, optimal_n0_rounded, site_bd, site_soc, site_lw)
        try:
            from copy_yamls import load_sensors, METADATA_FILE, TARGET_DATA_DIR, SENSOR_CONFIG_DIR
            if METADATA_FILE.exists():
                sensors = load_sensors(METADATA_FILE)
                for s_key, s_data in sensors.items():
                    if s_key != sensor_id and str(int(s_key) if s_key.isdigit() else s_key) != str(int(sensor_id) if sensor_id.isdigit() else sensor_id):
                        continue
                    root_folder = s_data.get("root_folder_name")
                    if not root_folder:
                        continue
                    s_dir = TARGET_DATA_DIR / root_folder
                    for phase in s_data.get("phase", []):
                        p_folder = phase.get("folder_name")
                        cfg_file = phase.get("config_file")
                        if not p_folder or not cfg_file:
                            continue
                        src_path = SENSOR_CONFIG_DIR / cfg_file
                        if not src_path.exists():
                            continue
                        for l_key, subf in [("ftp_folder", "ftp"), ("field_folder", "sd"), ("field_pr2_folder", "sd_pr2"), ("field_theta_folder", "sd_theta")]:
                            if l_key in phase.get("source_lookup", {}):
                                d_file = s_dir / p_folder / subf / Path(cfg_file).name
                                if d_file.parent.exists():
                                    shutil.copy2(src_path, d_file)
                logger.info(f"Synced updated YAML configs to data folders for sensor {sensor_id}")
        except Exception as e:
            logger.warning(f"Note syncing YAMLs to data folders: {e}")

    return report_dict


def update_sensor_yaml_n0(
    sensor_id: str,
    n0_value: int,
    bulk_density: float = None,
    soc: float = None,
    lattice_water: float = None,
):
    """Update N0 and site soil properties in sensor YAML files."""
    cfg_dir = PROCESSING_DIR / "sensor_config" / sensor_id
    if not cfg_dir.exists() and sensor_id.isdigit():
        cfg_dir = PROCESSING_DIR / "sensor_config" / f"{int(sensor_id):02d}"

    if not cfg_dir.exists():
        logger.warning(f"Sensor config directory not found: {cfg_dir}")
        return

    yaml_files = list(cfg_dir.glob("*.yaml"))
    for yf in yaml_files:
        try:
            with open(yf, 'r', encoding='utf-8') as f:
                content = yaml.safe_load(f)

            if not isinstance(content, dict) or 'sensor_info' not in content:
                continue

            content['sensor_info']['N0'] = n0_value
            if bulk_density is not None:
                content['sensor_info']['avg_dry_soil_bulk_density'] = bulk_density
            if soc is not None:
                content['sensor_info']['avg_soil_organic_carbon'] = soc
            if lattice_water is not None:
                content['sensor_info']['avg_lattice_water'] = lattice_water

            with open(yf, 'w', encoding='utf-8') as f:
                yaml.dump(content, f, default_flow_style=False, sort_keys=False)
            logger.info(f"Updated N0={n0_value} in {yf.name}")
        except Exception as e:
            logger.error(f"Failed to update {yf.name}: {e}")


def run_pipeline(sensor=None, update_yaml=True, process_config=None, **kwargs):
    """Pipeline integration runner for calibration step."""
    target_sensors = []
    if sensor:
        if isinstance(sensor, str):
            sensor = [sensor]
        for s in sensor:
            if "+" in s:
                target_sensors.extend([x.strip() for x in s.split("+") if x.strip()])
            elif "," in s:
                target_sensors.extend([x.strip() for x in s.split(",") if x.strip()])
            else:
                target_sensors.append(str(s))
    else:
        calib_dirs = [
            PROJECT_ROOT / "data" / "calibration",
            PROCESSING_DIR / "calibration" / "data",
        ]
        for c_dir in calib_dirs:
            if c_dir.exists():
                for cf in c_dir.glob("calibration_*.csv"):
                    stem = cf.stem.replace("calibration_", "")
                    if "_" not in stem:
                        target_sensors.append(stem)

    if not target_sensors:
        logger.info("No calibration data found and no --sensor specified. Skipping calibration.")
        return {}

    target_sensors = sorted(list(set(target_sensors)))
    logger.info(f"Calibrating sensor(s): {target_sensors}")

    results = {}
    for s_id in target_sensors:
        try:
            res = calibrate_sensor(
                sensor_id=s_id,
                update_yaml=update_yaml,
                process_config_path=Path(process_config) if process_config else None,
            )
            results[s_id] = res
        except Exception as e:
            logger.error(f"Failed calibration for sensor '{s_id}': {e}")

    return results


def main():
    parser = argparse.ArgumentParser(description="Calibrate CRNS sensor N0 parameter with Neptoon.")
    parser.add_argument("--sensor", "-s", type=str, required=True,
                        help="Sensor name/ID to calibrate (e.g. 01)")
    parser.add_argument("--campaign", "-c", type=str, default=None,
                        help="Path to calibration CSV file (default: data/calibration/calibration_<sensor>.csv)")
    parser.add_argument("--hours-window", "-w", type=int, default=6,
                        help="Hours around calibration datetime to average (default: 6)")
    parser.add_argument("--update-yaml", "-u", action="store_true",
                        help="Write calibrated N0 and site properties to sensor config YAMLs")
    parser.add_argument("--no-save", action="store_true",
                        help="Do not save calibration report files")
    parser.add_argument("--process-config", "-p", type=str, default=None,
                        help="Path to process config YAML (controls conversion method)")

    args = parser.parse_args()

    calibrate_sensor(
        sensor_id=args.sensor,
        campaign_path=args.campaign,
        hours_around_calib=args.hours_window,
        update_yaml=args.update_yaml,
        save_results=not args.no_save,
        process_config_path=Path(args.process_config) if args.process_config else None,
    )


if __name__ == "__main__":
    main()
