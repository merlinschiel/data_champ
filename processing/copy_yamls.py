"""
Copy the phase config YAMLs from processing/sensor_config into the data phase folders
(data/<sensor>/<phase>/ftp|sd) without touching data files.

Only the YAML named in the metadata belongs in a phase's ftp/ and sd/ folders. Any
other YAML found in a phase's subfolders (old configs, copies in sd_pr2/sd_theta) is
deleted so it can no longer be picked up as a config.
"""

import argparse
import filecmp
import shutil
from pathlib import Path

from sensor_metadata import (
    LOGGER_SUBFOLDERS,
    METADATA_FILE,
    PHASE_SUBFOLDERS,
    SENSOR_CONFIG_DIR,
    TARGET_DATA_DIR,
    load_sensors_from_excel,
    parse_sensor_filter,
    sensor_matches,
)

# metadata source column -> phase subfolder that receives the YAML copy
SOURCE_SUBFOLDERS = {"ftp_folder": "ftp", "field_folder": "sd"}


def delete_stale_yamls(phase_dir: Path, config_name: str | None, log_fn=print) -> int:
    """Delete YAMLs that are not the phase's current config from the phase folder."""
    deleted = 0
    for sub in PHASE_SUBFOLDERS:
        sub_dir = phase_dir / sub
        if not sub_dir.is_dir():
            continue
        for path in list(sub_dir.glob("*.yaml")) + list(sub_dir.glob("*.yml")):
            if sub in LOGGER_SUBFOLDERS and path.name == config_name:
                continue
            path.unlink()
            log_fn(f"  Deleted stale config {path.relative_to(TARGET_DATA_DIR)}")
            deleted += 1
    return deleted


def sync_sensor_configs(sensor_key: str, sensor_data: dict, create_dirs: bool = False,
                        log_fn=print) -> dict:
    """Copy each phase's config YAML into its ftp/ and sd/ folders and delete stale YAMLs.

    create_dirs: also create ftp/ and sd/ for phases that list an FTP or field source.
    Returns counts {'copied': n, 'deleted': m}.
    """
    counts = {"copied": 0, "deleted": 0}
    root_folder = sensor_data.get("root_folder_name")
    if not root_folder:
        return counts
    sensor_dir = TARGET_DATA_DIR / root_folder

    for phase in sensor_data.get("phase", []):
        phase_folder = phase.get("folder_name")
        config_file = phase.get("config_file")
        if not phase_folder:
            continue
        phase_dir = sensor_dir / phase_folder
        config_name = Path(config_file).name if config_file else None
        config_src = SENSOR_CONFIG_DIR / config_file if config_file else None

        if config_src is not None and config_src.exists():
            source_lookup = phase.get("source_lookup", {})
            for lookup_key, sub in SOURCE_SUBFOLDERS.items():
                dest_dir = phase_dir / sub
                if create_dirs and lookup_key in source_lookup:
                    dest_dir.mkdir(parents=True, exist_ok=True)
                if not dest_dir.is_dir():
                    continue
                dest = dest_dir / config_name
                if not dest.exists() or not filecmp.cmp(config_src, dest, shallow=False):
                    shutil.copy2(config_src, dest)
                    counts["copied"] += 1
        elif config_file:
            log_fn(f"  Warning: config '{config_file}' not found at {config_src}")

        if phase_dir.is_dir():
            counts["deleted"] += delete_stale_yamls(phase_dir, config_name, log_fn=log_fn)
    return counts


def main():
    parser = argparse.ArgumentParser(description="Copy sensor config YAMLs to data folders without copying data files")
    parser.add_argument("--sensor", type=str, nargs="+", default=None,
                        help="Specific sensors (e.g., 26, 38, or 2+21)")
    args = parser.parse_args()

    if not METADATA_FILE.exists():
        print(f"Error: {METADATA_FILE} not found.")
        return

    sensors = load_sensors_from_excel(METADATA_FILE)
    sensor_filter = parse_sensor_filter(args.sensor)

    totals = {"copied": 0, "deleted": 0}
    for sensor_key, sensor_data in sensors.items():
        if not sensor_matches(sensor_filter, sensor_key, sensor_data):
            continue
        if not sensor_data.get("active", False):
            continue
        counts = sync_sensor_configs(sensor_key, sensor_data, create_dirs=True)
        for k in totals:
            totals[k] += counts[k]

    print(f"\nDone. Copied {totals['copied']} YAML file(s), deleted {totals['deleted']} stale YAML(s).")


if __name__ == "__main__":
    main()
