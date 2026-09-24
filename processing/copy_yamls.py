import argparse
import shutil
import os
from pathlib import Path
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PROCESSING_DIR = PROJECT_ROOT / "processing"
META_DIR = PROCESSING_DIR / "meta"
METADATA_FILE = META_DIR / "sensor_metadata.xlsx"
SENSOR_CONFIG_DIR = PROCESSING_DIR / "sensor_config"
TARGET_DATA_DIR = PROJECT_ROOT / "data"

def _get_cell(row, col):
    val = row.get(col, "")
    return str(val).strip() if pd.notna(val) and str(val).strip() else None

def _parse_folder_cell(row, col):
    val = _get_cell(row, col)
    if not val:
        return None
    if "," in val:
        return [s.strip() for s in val.split(",") if s.strip()]
    return val

def load_sensors(filepath: Path) -> dict:
    xls = pd.ExcelFile(filepath)
    sensors = {}
    for sheet_name in xls.sheet_names:
        if sheet_name.lower() == "config_meta":
            continue
        df = pd.read_excel(xls, sheet_name=sheet_name)
        df.columns = df.columns.str.strip()
        if df.empty:
            continue
        
        active_val = df["Active"].iloc[0] if "Active" in df.columns else False
        if isinstance(active_val, str):
            active = active_val.strip().lower() in ("true", "1", "yes")
        else:
            active = bool(active_val)
            
        root_folder = str(df["Root_Folder"].iloc[0]).strip() if "Root_Folder" in df.columns else sheet_name
        
        phases = []
        for _, row in df.iterrows():
            phase = {}
            for excel_col, phase_key in [
                ("Phase_Folder", "folder_name"),
                ("Config", "config_file"),
            ]:
                val = _get_cell(row, excel_col)
                if val:
                    phase[phase_key] = val
            
            source_lookup = {}
            for col, key in [
                ("FTP_Folder", "ftp_folder"),
                ("Field_Folder", "field_folder"),
                ("Field_PR2_Folder", "field_pr2_folder"),
                ("Field_Theta_Folder", "field_theta_folder"),
            ]:
                val = _parse_folder_cell(row, col)
                if val:
                    source_lookup[key] = val
            if source_lookup:
                phase["source_lookup"] = source_lookup
            phases.append(phase)
            
        sensors[sheet_name] = {
            "active": active,
            "root_folder_name": root_folder,
            "phase": phases,
        }
    return sensors

def main():
    parser = argparse.ArgumentParser(description="Copy sensor config YAMLs to data folders without copying data files")
    parser.add_argument("--sensor", type=str, nargs="+", default=None, help="Specific sensor keys (e.g., 26, 38, or 2+21)")
    args = parser.parse_args()

    if not METADATA_FILE.exists():
        print(f"Error: {METADATA_FILE} not found.")
        return

    sensors = load_sensors(METADATA_FILE)
    
    only_sensors = None
    if args.sensor:
        expanded = []
        for s in args.sensor:
            expanded.extend([x.strip() for x in s.split("+") if x.strip()])
        only_sensors = expanded

    copied_count = 0

    for sensor_key, sensor_data in sensors.items():
        if only_sensors and sensor_key not in only_sensors:
            continue
        if not sensor_data.get("active", False):
            continue

        root_folder = sensor_data.get("root_folder_name")
        if not root_folder:
            continue

        sensor_dir = TARGET_DATA_DIR / root_folder
        phases = sensor_data.get("phase", [])

        for phase in phases:
            phase_folder = phase.get("folder_name")
            config_file = phase.get("config_file")
            if not phase_folder or not config_file:
                continue

            config_src = SENSOR_CONFIG_DIR / config_file
            if not config_src.exists():
                print(f"Warning: Source config {config_src} not found.")
                continue

            dest_mappings = [
                ("ftp_folder", "ftp"),
                ("field_folder", "sd"),
                ("field_pr2_folder", "sd_pr2"),
                ("field_theta_folder", "sd_theta"),
            ]

            source_lookup = phase.get("source_lookup", {})
            for lookup_key, subfolder in dest_mappings:
                if lookup_key in source_lookup:
                    dest_dir = sensor_dir / phase_folder / subfolder
                    dest_file = dest_dir / Path(config_file).name
                    try:
                        dest_dir.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(config_src, dest_file)
                        copied_count += 1
                    except Exception as e:
                        print(f"Error copying {config_src} to {dest_file}: {e}")

    print(f"\nDone. Copied {copied_count} YAML file(s).")

if __name__ == "__main__":
    main()
