import os
import yaml
import pandas as pd
from pathlib import Path
import logging
import json


from merge_utils import (
    determine_master_config,
    read_all_config_data,
    resolve_datetime_column,
    clean_and_deduplicate,
    _build_master_yaml,
    process_supplementary_files,
)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

PROCESSING_DIR = Path(__file__).parent.resolve()
META_DIR = PROCESSING_DIR / "meta"
LOG_DIR = META_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOG_DIR / "auto_append.log"

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE, mode='w', encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)


def run_pipeline(sensor=None, only_sensors=None):

    # ---------------------------------------------------------------------------
    # Config
    # ---------------------------------------------------------------------------

    TARGET_DATA_DIR = PROCESSING_DIR.parent / "data"
    MERGED_DIR = TARGET_DATA_DIR / ".merged"
    MERGED_DIR.mkdir(parents=True, exist_ok=True)
    
    SENSOR_CONFIGS_DIR = MERGED_DIR / "sensor_configs"
    SENSOR_CONFIGS_DIR.mkdir(parents=True, exist_ok=True)

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
        logger.info(f"Filtering processing to sensor(s): {target_sensors}")
    else:
        target_sensors = None

    # Load existing mapping if present
    mapping_file = SENSOR_CONFIGS_DIR / "yaml_mapping.json"
    yaml_mapping = {}
    if mapping_file.exists():
        try:
            with open(mapping_file, 'r', encoding='utf-8') as f:
                yaml_mapping = json.load(f)
        except Exception:
            yaml_mapping = {}

    # ---------------------------------------------------------------------------
    # Processing
    # ---------------------------------------------------------------------------
    
    yaml_files = [p for p in TARGET_DATA_DIR.rglob("*.yaml") if not any(part.startswith('.merged') for part in p.parts)]
    sensors_data = {}

    for y_path in yaml_files:
        with open(y_path, 'r', encoding='utf-8') as f:
            try:
                config = yaml.safe_load(f)
            except yaml.YAMLError as e:
                logger.error(f"Error parsing {y_path}: {e}")
                continue

        # Group configurations by sensor name
        name = config.get('sensor_info', {}).get('name')
        if not name:
            continue
            
        name = str(name)
        if target_sensors and name not in target_sensors:
            continue

        if name not in sensors_data:
            sensors_data[name] = []

        sensors_data[name].append({
            'yaml_path': y_path,
            'config': config
        })

    # Iterate through sensors
    for name, configs in sensors_data.items():
        logger.info(f"Processing data for Sensor: {name}")

        master_columns, dt_cols, dt_col, dt_format, base_config = \
            determine_master_config(configs)

        all_dfs = read_all_config_data(configs, log_fn=logger.warning)

        if not all_dfs:
            logger.warning(f"  -> No valid data found to merge for sensor {name}")
            continue

        # merge cleaned and parsed df
        valid_dfs = [df for df in all_dfs if not df.empty]

        if valid_dfs:
            merged_df = pd.concat(valid_dfs, ignore_index=True)
        else:
            merged_df = pd.DataFrame()

        # Drop scrap columns from dataframe
        scrap_cols = [c for c in merged_df.columns if 'scrap_col' in c.lower()]
        if scrap_cols:
            merged_df = merged_df.drop(columns=scrap_cols)
            logger.info(f"  -> Dropped scrap columns: {scrap_cols}")

        # Resolve datetime column
        actual_dt_col = resolve_datetime_column(merged_df, dt_cols, dt_col)

        if not actual_dt_col:
            logger.warning(f"  -> No valid datetime column found for sensor {name}.")
            continue

        # Process supplementary files
        merged_df = process_supplementary_files(merged_df, configs, log_fn=logger.info)

        # Add any newly introduced columns to master_columns
        for col in merged_df.columns:
            if col not in master_columns and col != '_origin_priority':
                master_columns.append(col)

        # Clean, deduplicate, format
        merged_df = clean_and_deduplicate(
            merged_df, master_columns, actual_dt_col, dt_format, dt_cols=dt_cols)
        dt_col = 'Date_Time'

        # save merged file and master yaml

        out_file = MERGED_DIR / f"merged_{name}.csv"
        merged_df.to_csv(out_file, index=False)
        if not merged_df.empty:
            start_date = merged_df[dt_col].min()
            end_date = merged_df[dt_col].max()
            logger.info(f"  -> Successfully created {out_file.name} with {len(merged_df)} rows.")
            logger.info(f"  -> Data range: {start_date} to {end_date}")

            # Write generated master YAML
            master_yaml = _build_master_yaml(base_config, list(merged_df.columns), name, configs=configs)
            dest_yaml = SENSOR_CONFIGS_DIR / f"sensor_{name}_master.yaml"
            try:
                with open(dest_yaml, 'w', encoding='utf-8') as f:
                    yaml.dump(master_yaml, f, default_flow_style=False,
                              allow_unicode=True, sort_keys=False)
                yaml_mapping[out_file.name] = dest_yaml.name
                logger.info(f"  -> Wrote master config: {dest_yaml.name}")
            except Exception as e:
                logger.error(f"  -> Failed to write master config: {e}")
        else:
            logger.error(f"  -> DataFrame is empty after filtering for {out_file.name}. Check your raw data columns.")

    if yaml_mapping:
        mapping_file = SENSOR_CONFIGS_DIR / "yaml_mapping.json"
        with open(mapping_file, 'w', encoding='utf-8') as f:
            json.dump(yaml_mapping, f, indent=4)
        logger.info(f"Saved YAML mapping dictionary to {mapping_file}")


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Auto append and merge sensor raw data.")
    parser.add_argument("--sensor", "-s", type=str, nargs="+", default=None, help="Process only specific sensor name(s)")
    args, _ = parser.parse_known_args()
    run_pipeline(sensor=args.sensor)


if __name__ == "__main__":
    main()
