"""
Data merge step: builds data/.merged/merged_<sensor>.csv for every sensor in
processing/meta/sensor_metadata.xlsx.

Each phase folder is parsed with the YAML named in the metadata, records are tagged
with their phase (phase1..phaseN, chronological) and records outside the phase
windows are dropped. Alongside the CSV the master YAML (sensor_configs/
sensor_<name>_master.yaml) and the phase table (sensor_<name>_phases.json) are
written for neptoon processing and calibration.
"""

import json

from log_utils import get_logger
from merge_utils import merge_sensor_phases
from sensor_metadata import (
    MASTER_CONFIG_DIR,
    MERGED_DIR,
    METADATA_FILE,
    load_sensors_from_excel,
    parse_sensor_filter,
    sensor_matches,
)

logger = get_logger("auto_append", "auto_append.log")


def run_pipeline(sensor=None, force=False):
    """Merge all (or the selected) sensors. Raises if any selected sensor fails to merge."""
    MERGED_DIR.mkdir(parents=True, exist_ok=True)
    MASTER_CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    if not METADATA_FILE.exists():
        raise FileNotFoundError(f"Metadata file not found: {METADATA_FILE}")

    sensor_filter = parse_sensor_filter(sensor)
    if sensor_filter:
        logger.info(f"Filtering merge to sensor(s): {sorted(sensor_filter)}")
    if force:
        logger.info("Force mode: parsed-file cache and unchanged-input check bypassed")

    sensors = load_sensors_from_excel(METADATA_FILE)

    mapping_file = MASTER_CONFIG_DIR / "yaml_mapping.json"
    yaml_mapping = {}
    if mapping_file.exists():
        try:
            with open(mapping_file, 'r', encoding='utf-8') as f:
                yaml_mapping = json.load(f)
        except Exception:
            yaml_mapping = {}

    failed = []
    matched = 0
    for sensor_key, sensor_data in sensors.items():
        if not sensor_matches(sensor_filter, sensor_key, sensor_data):
            continue
        matched += 1
        logger.info(f"Processing data for sensor: {sensor_key}")
        try:
            result = merge_sensor_phases(
                sensor_key, sensor_data, force=force,
                log_fn=logger.info, warn_fn=logger.warning,
            )
        except Exception as e:
            logger.exception(f"  -> Merge failed for sensor {sensor_key}: {e}")
            failed.append(sensor_key)
            continue
        if result is None:
            failed.append(sensor_key)
        elif result.get('master_yaml'):
            yaml_mapping[result['merged_csv']] = result['master_yaml']

    if sensor_filter and matched == 0:
        raise ValueError(f"No sensor in the metadata matches {sorted(sensor_filter)}")

    if yaml_mapping:
        with open(mapping_file, 'w', encoding='utf-8') as f:
            json.dump(yaml_mapping, f, indent=4)

    if failed:
        logger.error(f"Merge failed or produced no data for: {failed}")
        raise RuntimeError(f"Merge failed or produced no data for {len(failed)} sensor(s): {failed}")


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Merge sensor raw data phase by phase.")
    parser.add_argument("--sensor", "-s", type=str, nargs="+", default=None,
                        help="Process only specific sensor(s) (sheet name, sensor name or config folder)")
    parser.add_argument("--force", action="store_true",
                        help="Re-parse all raw files and rewrite outputs even if inputs are unchanged")
    args, _ = parser.parse_known_args()
    run_pipeline(sensor=args.sensor, force=args.force)


if __name__ == "__main__":
    main()
