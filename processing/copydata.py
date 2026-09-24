

import shutil
import os
import json
import hashlib
import ftplib
import io
import argparse
import requests
import pandas as pd
import logging
import traceback
from pathlib import Path
from datetime import datetime

# ---------------------------------------------------------------------------
# Configs — all paths relative to project root
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parent.parent
FIELD_PATH = Path(os.environ.get("CRNS_FIELD_DATA_PATH", "")) if os.environ.get("CRNS_FIELD_DATA_PATH") else None
PROCESSING_DIR = PROJECT_ROOT / "processing"
META_DIR = PROCESSING_DIR / "meta"
LOG_DIR = META_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOG_DIR / "copydata.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, mode='w', encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)

TARGET_DATA_DIR = PROJECT_ROOT / "data"
METADATA_FILE = META_DIR / "sensor_metadata.xlsx"
SENSOR_CONFIG_DIR = PROCESSING_DIR / "sensor_config"
FTP_SOURCES_FILE = META_DIR / "ftp_sources.txt"
SYNC_CACHE_FILE = META_DIR / "sync_cache.json"



# ---------------------------------------------------------------------------
# HTTP API downloads (e.g. Finapp sensors) — config-driven
# ---------------------------------------------------------------------------

def download_api_data():
    """Download data from HTTP APIs defined in processing/meta/api_sources.txt."""
    api_config_file = META_DIR / "api_sources.txt"
    if not api_config_file.exists():
        logger.info("No api_sources.txt found — skipping API downloads.")
        return
    
    try:
        df = pd.read_csv(api_config_file, sep="\t", dtype=str, keep_default_na=False)
        df.columns = df.columns.str.strip()
    except Exception as e:
        logger.error(f"Failed to load api_sources.txt: {e}")
        return
    
    if df.empty:
        return
    
    logger.info(f"--- Downloading API Data ({len(df)} source(s)) ---")
    for _, row in df.iterrows():
        url = str(row.get("url", "")).strip()
        rel_dest = str(row.get("dest", "")).strip()
        if not url or not rel_dest:
            continue
        dest_path = PROJECT_ROOT / rel_dest
        try:
            response = requests.get(url, timeout=30)
            response.raise_for_status()
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            with open(dest_path, "wb") as f:
                f.write(response.content)
            logger.info(f"  Downloaded: {dest_path.name}")
        except Exception as e:
            logger.error(f"  Failed to download from {url}: {e}")



# ---------------------------------------------------------------------------
# FTP source routing
# ---------------------------------------------------------------------------

def load_ftp_sources() -> list[dict]:

    df = pd.read_csv(FTP_SOURCES_FILE, sep="\t", dtype=str, keep_default_na=False)
    df.columns = df.columns.str.strip()
    rows = []
    for _, row in df.iterrows():
        rows.append({
            "host": str(row.get("host", "")).strip(),
            "username": str(row.get("username", "")).strip(),
            "password": str(row.get("password", "")).strip(),
            "remote_dir": str(row.get("remote_dir", "")).strip(),
        })
    return rows


def resolve_ftp_source(ftp_folder: str, sources: list[dict]):
    default = None

    for entry in sources:
        if not entry.get("remote_dir"):
            default = entry
            continue
            
        if ftp_folder.startswith(entry["remote_dir"]):
            return entry["host"], entry["username"], entry["password"], ftp_folder

    if default:
        return default["host"], default["username"], default["password"], ftp_folder.lstrip("/")

    raise ValueError(f"No FTP source found for ftp_folder='{ftp_folder}'")


# ---------------------------------------------------------------------------
# FTP download
# ---------------------------------------------------------------------------
_ftp_connections: dict[tuple, ftplib.FTP] = {}
_ftp_listing_cache: dict[str, list[dict]] = {}  # keyed by "host|user|remote_dir"


def get_ftp_connection(host: str, user: str, pw: str) -> ftplib.FTP:
    key = (host, user)
    conn = _ftp_connections.get(key)
    if conn is not None:
        try:
            conn.voidcmd("NOOP")
            return conn
        except Exception:
            try:
                conn.quit()
            except Exception:
                pass

    logger.info(f"  FTP connect: {user}@{host}")
    conn = ftplib.FTP(host, timeout=30)
    conn.login(user, pw)
    _ftp_connections[key] = conn
    return conn


def close_all_ftp():
    for conn in _ftp_connections.values():
        try:
            conn.quit()
        except Exception:
            pass
    _ftp_connections.clear()
    _ftp_listing_cache.clear()

def get_remote_file_info(ftp: ftplib.FTP, host: str, entry: str) -> tuple:
    """Always fetch fresh size/mtime from FTP server (no disk cache)."""
    size = 0
    mtime = ""
    try:
        size = ftp.size(entry) or 0
    except Exception:
        pass
    try:
        resp = ftp.sendcmd(f"MDTM {entry}")
        mtime = resp[4:].strip()
    except Exception:
        pass
    return size, mtime


def ftp_list_files(ftp: ftplib.FTP, host: str, remote_dir: str, _depth: int = 0) -> list[dict]:
    """Return list of {'name': basename, 'path': full_path, 'size': int, 'mtime': str}.
    Recurses into subdirectories. Uses NLST + per-file SIZE/MDTM."""
    results = []
    if _depth == 0:
        logger.info(f"    Listing FTP dir: {remote_dir}")

    try:
        ftp.sendcmd("TYPE I")
        entries = ftp.nlst(remote_dir)
    except Exception:
        return results

    for entry in entries:
        basename = entry.rsplit("/", 1)[-1]
        if not basename or basename in (".", ".."):
            continue

        if entry.rstrip("/") == remote_dir.rstrip("/"):
            continue

        # Check possible subdirectories
        if "." not in basename:
            is_dir = False
            try:
                current = ftp.pwd()
                ftp.cwd(entry)
                ftp.cwd(current)
                is_dir = True
            except Exception:
                is_dir = False

            if is_dir:
                results.extend(ftp_list_files(ftp, host, entry, _depth + 1))
                continue

        if should_skip_file(basename):
            continue

        size, mtime = get_remote_file_info(ftp, host, entry)
        results.append({"name": basename, "path": entry, "size": size, "mtime": mtime})

    if _depth == 0:
        logger.info(f"    Found {len(results)} files")
    return results


def get_ftp_listing_cached(host: str, user: str, pw: str, remote_dir: str) -> list[dict]:
    cache_key = f"{host}|{user}|{remote_dir}"
    if cache_key in _ftp_listing_cache:
        return _ftp_listing_cache[cache_key]

    ftp = get_ftp_connection(host, user, pw)
    result = ftp_list_files(ftp, host, remote_dir.rstrip("/"))
    _ftp_listing_cache[cache_key] = result
    return result


def ftp_download(ftp: ftplib.FTP, remote_path: str, dest_path: Path, mtime_str: str = ""):
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    buf = io.BytesIO()
    ftp.retrbinary(f"RETR {remote_path}", buf.write)
    data = buf.getvalue()
    buf.close()
    with open(dest_path, "wb") as f:
        f.write(data)
    
    if mtime_str:
        try:
            # mtime_str format is YYYYMMDDHHMMSS
            dt = datetime.strptime(mtime_str[:14], "%Y%m%d%H%M%S")
            ts = dt.timestamp()
            os.utime(dest_path, (ts, ts))
        except Exception as e:
            logger.warning(f"Could not set mtime for {dest_path.name}: {e}")


# ---------------------------------------------------------------------------
# Sync cache
# ---------------------------------------------------------------------------

def to_relative_key(key: str) -> str:
    key_norm = key.replace("\\", "/")
    if key_norm.startswith("data/"):
        return key_norm

    try:
        rel = os.path.relpath(key_norm, PROJECT_ROOT)
        return rel.replace("\\", "/")
    except Exception:
        pass

    if "data/" in key_norm:
        return "data/" + key_norm.split("data/")[-1]
    return key_norm


def to_relative_src(src_path: str) -> str:
    if not src_path:
        return src_path
    
    normalized = src_path.replace("\\", "/")
    if not (":" in normalized or normalized.startswith("/") or normalized.startswith("//")):
        return normalized

    try:
        rel = os.path.relpath(normalized, PROJECT_ROOT)
        return rel.replace("\\", "/")
    except Exception:
        pass
        
    return normalized


def load_sync_cache() -> dict:
    if SYNC_CACHE_FILE.exists():
        try:
            with open(SYNC_CACHE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.critical(f"Failed to load sync cache: {e}")
            raise
            
    return {"metadata_hashes": {}, "files": {}}


def save_sync_cache(cache: dict):
    temp_file = SYNC_CACHE_FILE.with_suffix(".tmp")
    try:
        with open(temp_file, "w", encoding="utf-8") as f:
            json.dump(cache, f, indent=2, ensure_ascii=False)
        if temp_file.exists():
            if SYNC_CACHE_FILE.exists():
                os.remove(SYNC_CACHE_FILE)
            os.rename(temp_file, SYNC_CACHE_FILE)
    except Exception as e:
        logger.error(f"Failed to save sync cache: {e}")
        if temp_file.exists():
            try:
                os.remove(temp_file)
            except Exception:
                pass
        raise


def compute_sensor_hash(sensor_data: dict) -> str:
    raw = json.dumps(sensor_data, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def is_cached(cache: dict, dest_key: str, src_size: int, src_mtime: str) -> bool:
    r_key = to_relative_key(dest_key)
    entry = cache.get("files", {}).get(r_key)
    if not entry:
        return False
    return entry.get("src_size") == src_size and entry.get("src_mtime") == src_mtime


def update_cache_entry(cache: dict, dest_key: str, src_path: str, src_size: int, src_mtime: str):
    r_key = to_relative_key(dest_key)
    r_src = to_relative_src(src_path)

    cache.setdefault("files", {})[r_key] = {
        "src_path": r_src,
        "src_size": src_size,
        "src_mtime": src_mtime,
        "synced_at": datetime.now().isoformat(),
    }


def invalidate_sensor_cache(cache: dict, sensor_root: str):
    prefix = f"data/{sensor_root}/"
    keys_to_drop = [k for k in cache.get("files", {}) if k.startswith(prefix)]
    for k in keys_to_drop:
        del cache["files"][k]
    if keys_to_drop:
        logger.info(f"  Cache invalidated: {len(keys_to_drop)} entries for {sensor_root}")


# ---------------------------------------------------------------------------
# Metadata loading
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


def load_sensors_from_excel(filepath: Path) -> dict:
    """Load sensor metadata from Excel into a dict of {sheet_name: sensor_data}."""
    xls = pd.ExcelFile(filepath)
    sensors = {}

    for sheet_name in xls.sheet_names:
        # Skip the config_meta sheet
        if sheet_name.lower() == "config_meta":
            continue

        df = pd.read_excel(xls, sheet_name=sheet_name)
        # Normalise column names
        df.columns = df.columns.str.strip()

        sensor_key = sheet_name

        # --- sensor-level fields (same for every row) ---
        active_val = df["Active"].iloc[0] if "Active" in df.columns and len(df) > 0 else False
        if isinstance(active_val, str):
            active = active_val.strip().lower() in ("true", "1", "yes")
        else:
            active = bool(active_val)

        root_folder = str(df["Root_Folder"].iloc[0]).strip().lstrip('0') or '0' if "Root_Folder" in df.columns and len(df) > 0 else sensor_key
        # Preserve original string — read_excel may strip leading zeros
        # Try to recover from the raw cell value
        if "Root_Folder" in df.columns and len(df) > 0:
            raw_val = df["Root_Folder"].iloc[0]
            root_folder = str(raw_val).strip()
        else:
            root_folder = sensor_key

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

            # source_lookup  (ftp / field folders — may be comma-separated lists)
            source_lookup = {}
            ftp_val = _parse_folder_cell(row, "FTP_Folder")
            if ftp_val: source_lookup["ftp_folder"] = ftp_val
            field_val = _parse_folder_cell(row, "Field_Folder")
            if field_val: source_lookup["field_folder"] = field_val
            field_pr2_val = _parse_folder_cell(row, "Field_PR2_Folder")
            if field_pr2_val: source_lookup["field_pr2_folder"] = field_pr2_val
            field_theta_val = _parse_folder_cell(row, "Field_Theta_Folder")
            if field_theta_val: source_lookup["field_theta_folder"] = field_theta_val
            if source_lookup:
                phase["source_lookup"] = source_lookup

            phases.append(phase)

        sensors[sensor_key] = {
            "active": active,
            "root_folder_name": root_folder,
            "phase": phases,
        }

    return sensors


# ---------------------------------------------------------------------------
# Get phases
# ---------------------------------------------------------------------------

def get_file_date(filepath: Path, fallback_dir: Path = None):
    mtime = filepath.stat().st_mtime
    dt = datetime.fromtimestamp(mtime)
    if dt.year == 2000:
        # Corrupt timestamp — try parent folder mtime as fallback
        fallback = fallback_dir or filepath.parent
        try:
            folder_mtime = fallback.stat().st_mtime
            folder_dt = datetime.fromtimestamp(folder_mtime)
            logger.warning(
                f"Corrupt timestamp (year 2000) on '{filepath.name}' — "
                f"using folder mtime {folder_dt.strftime('%Y-%m-%d %H:%M:%S')} "
                f"from '{fallback.name}'"
            )
            return folder_dt
        except Exception as e:
            logger.warning(f"Could not read folder mtime for '{filepath.name}': {e}")
    return dt


def get_target_phase_info(file_date, phases):
    sorted_phases = sorted(phases, key=lambda x: x.get("start_date", "1970-01-01"))
    target_phase = None
    for phase in sorted_phases:
        date_str = phase.get("start_date")
        try:
            phase_start = datetime.strptime(date_str, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            try:
                phase_start = datetime.strptime(date_str, "%Y-%m-%d")
            except ValueError:
                continue
        if file_date >= phase_start:
            target_phase = phase
        else:
            break
    return target_phase


def get_target_phase_for_ftp_mtime(mtime_str: str, phases):
    """Parse FTP MDTM timestamp (YYYYMMDDHHmmSS) into datetime for phase lookup."""
    if not mtime_str:
        return None
    try:
        fmt = "%Y%m%d%H%M%S" if len(mtime_str) >= 14 else "%Y%m%d"
        dt = datetime.strptime(mtime_str[:14], fmt)
    except ValueError:
        return None
    return get_target_phase_info(dt, phases)


# ---------------------------------------------------------------------------
# File filtering
# ---------------------------------------------------------------------------

def should_skip_file(filename: str) -> bool:
    f_lower = filename.lower()
    if "cnt" in f_lower:
        return True
    if f_lower.endswith((".zip", ".yaml", ".yml")):
        return True
    if "processed" in f_lower:
        return True
    return False


# ---------------------------------------------------------------------------
# FTP file processing
# ---------------------------------------------------------------------------

def process_sensor_ftp(phases: list, sensor_dir: Path,
                      sources: list[dict], cache: dict, force: bool) -> int:
    """List each unique FTP dir ONCE, then distribute files across phases.
    Returns total count of newly downloaded files."""

    # 1. Collect all unique (ftp_folder → resolved connection) across phases
    ftp_dir_to_phases: dict[str, list[dict]] = {}  # remote_dir → list of phases using it
    ftp_dir_info: dict[str, tuple] = {}  # remote_dir → (host, user, pw)

    for phase in phases:
        ftp_val = phase.get("source_lookup", {}).get("ftp_folder")
        if not ftp_val or not phase.get("folder_name"):
            continue
        ftp_folders = [ftp_val] if isinstance(ftp_val, str) else list(ftp_val)
        for ftp_folder in ftp_folders:
            host, user, pw, remote_dir = resolve_ftp_source(ftp_folder, sources)
            if not user:
                continue
            ftp_dir_info[remote_dir] = (host, user, pw)
            ftp_dir_to_phases.setdefault(remote_dir, []).append(phase)

    # 2. List each unique FTP dir once (cached within the run)
    all_remote_files: dict[str, list[dict]] = {}  # remote_dir → file list
    for remote_dir, (host, user, pw) in ftp_dir_info.items():
        try:
            all_remote_files[remote_dir] = get_ftp_listing_cached(host, user, pw, remote_dir)
        except Exception as e:
            logger.error(f"  FTP error listing {remote_dir} on {host}: {e}")
            all_remote_files[remote_dir] = []

    # 3. For each phase, copy config + distribute matching files
    new_files = 0
    for phase in phases:
        phase_folder_name = phase.get("folder_name")
        if not phase_folder_name:
            continue

        final_dest_dir = sensor_dir / phase_folder_name / "ftp"

        # Gather FTP dirs this phase uses
        ftp_val = phase.get("source_lookup", {}).get("ftp_folder")
        if not ftp_val:
            continue
        ftp_folders = [ftp_val] if isinstance(ftp_val, str) else list(ftp_val)

        for ftp_folder in ftp_folders:
            _, _, _, remote_dir = resolve_ftp_source(ftp_folder, sources)
            remote_files = all_remote_files.get(remote_dir, [])
            host, user, pw = ftp_dir_info.get(remote_dir, ("", "", ""))

            for rf in remote_files:
                if should_skip_file(rf["name"]):
                    continue

                # Phase check via FTP mtime
                target = get_target_phase_for_ftp_mtime(rf["mtime"], phases)
                if not target or target.get("folder_name") != phase_folder_name:
                    continue

                dest_path = final_dest_dir / rf["name"]
                dest_key = str(dest_path.resolve()).replace("\\", "/")

                # Cache check
                if not force and is_cached(cache, dest_key, rf["size"], rf["mtime"]):
                    continue

                # Download
                try:
                    ftp = get_ftp_connection(host, user, pw)
                    ftp_download(ftp, rf["path"], dest_path, rf["mtime"])
                    update_cache_entry(cache, dest_key, rf["path"], rf["size"], rf["mtime"])
                    new_files += 1
                    logger.info(f"    Downloaded: {rf['name']}")
                except Exception as e:
                    logger.error(f"  Failed to download {rf['name']}: {e}")

    return new_files


# ---------------------------------------------------------------------------
# Local (field/SD) file processing
# ---------------------------------------------------------------------------

def gather_files_for_phase(base_path: Path, phase: dict, key: str, valid_filenames=None):
    if valid_filenames is None:
        valid_filenames = ["*"]
    lookup_patterns = []
    phase_val = phase.get("source_lookup", {}).get(key)
    if phase_val:
        if isinstance(phase_val, str):
            lookup_patterns.append(phase_val)
        else:
            lookup_patterns.extend(phase_val)

    lookup_patterns = list(set(lookup_patterns))
    if not lookup_patterns:
        return []

    files = []
    for pattern in lookup_patterns:
        clean_pattern = pattern.strip("/")
        try:
            for matched_dir in base_path.glob(clean_pattern):
                try:
                    if matched_dir.is_dir():
                        for file_pattern in valid_filenames:
                            try:
                                found = list(matched_dir.rglob(file_pattern))
                                for f in found:
                                    try:
                                        if f.is_file() and not should_skip_file(f.name):
                                            files.append(f)
                                    except Exception as e:
                                        logger.error(f"Error accessing file {f}: {e}")
                            except Exception as e:
                                logger.error(f"Error globbing {file_pattern} in {matched_dir}: {e}")
                    elif matched_dir.is_file():
                        if not should_skip_file(matched_dir.name):
                            files.append(matched_dir)
                except Exception as e:
                    logger.error(f"Error processing {matched_dir}: {e}")
        except Exception as e:
            logger.error(f"Error resolving pattern {clean_pattern} in {base_path}: {e}")
    return list(set(files))


def sync_local_file(src_path: Path, dest_path: Path, cache: dict, force=False) -> bool:
    """Copy a local file if changed. Tracked in sync cache."""

    try:
        src_size = src_path.stat().st_size
        src_mtime = str(src_path.stat().st_mtime)
        dest_key = str(dest_path.resolve()).replace("\\", "/")

        if not force and is_cached(cache, dest_key, src_size, src_mtime):
            return False

        if not force and dest_path.exists():
            if dest_path.stat().st_size == src_size:
                # Size match — update cache, skip copy
                update_cache_entry(cache, dest_key, str(src_path), src_size, src_mtime)
                return False

        dest_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_path, dest_path)
        update_cache_entry(cache, dest_key, str(src_path), src_size, src_mtime)
        return True
    except Exception as e:
        logger.error(f"Failed to sync {src_path}: {e}")
        return False


def process_field_files(files, phases, phase_info, sensor_dir, cache, force,
                        dest_subfolder="sd"):
    """Copy local field/SD files into phase folders. Returns count of new copies."""
    phase_folder_name = phase_info.get("folder_name")
    if not phase_folder_name:
        return 0

    final_dest_dir = sensor_dir / phase_folder_name / dest_subfolder

    new_files_copied = 0
    for file_path in files:
        try:
            f_date = get_file_date(file_path, fallback_dir=file_path.parent)
        except OSError as e:
            logger.error(f"Skipping file {file_path.name} due to access error: {e}")
            continue

        target_phase = get_target_phase_info(f_date, phases)
        if not target_phase or target_phase.get("folder_name") != phase_folder_name:
            continue

        dest_path = final_dest_dir / file_path.name
        if sync_local_file(file_path, dest_path, cache, force):
            new_files_copied += 1

    return new_files_copied



# ---------------------------------------------------------------------------
# Stale folder cleanup
# ---------------------------------------------------------------------------

def cleanup_stale_phase_folders(sensor_dir: Path, phases: list) -> None:
    """Delete subdirectories of sensor_dir that are not referenced by any phase."""
    valid_folders = set()
    for phase in phases:
        fn = phase.get("folder_name")
        if fn:
            valid_folders.add(fn)

    if not sensor_dir.exists():
        return

    for child in sensor_dir.iterdir():
        if not child.is_dir():
            continue
        if child.name not in valid_folders:
            logger.info(f"  Removing stale folder: {child}")
            try:
                shutil.rmtree(child)
            except Exception as e:
                logger.error(f"  Failed to remove {child}: {e}")


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def run_pipeline(sensor=None, only_sensors=None, force=False, field=False):
    target_sensors = sensor or only_sensors
    _run_pipeline(force=force, only_sensors=target_sensors, field=field)


def main():
    parser = argparse.ArgumentParser(description="CRNS data sync (FTP + API + field)")
    parser.add_argument("--force", action="store_true", help="Bypass sync cache, re-download everything")
    parser.add_argument("--sensor", type=str, nargs="+", default=None, help="Process only these sensor keys")
    parser.add_argument("--field", action="store_true", help="Enable field data copying")
    args, _ = parser.parse_known_args()

    try:
        run_pipeline(force=args.force, sensor=args.sensor, field=args.field)
    except Exception as e:
        logger.critical(f"Pipeline failed: {e}\n{traceback.format_exc()}")
        raise


def _run_pipeline(force=False, only_sensors=None, field=False):
    # Determine whether field sync is enabled for this run
    copy_field = field
    if copy_field:
        if FIELD_PATH is None or not FIELD_PATH.exists():
            logger.warning("Field sync requested but CRNS_FIELD_DATA_PATH not set or path doesn't exist.")
            logger.warning("Set environment variable CRNS_FIELD_DATA_PATH to your field data root directory.")
            copy_field = False
        else:
            logger.info(f"Field sync ENABLED (source: {FIELD_PATH})")
    else:
        logger.info("Field sync DISABLED (use --field to enable)")

    if only_sensors:
        expanded = []
        for s in only_sensors:
            expanded.extend([x.strip() for x in s.split("+") if x.strip()])
        only_sensors = expanded
        logger.info(f"Filtering to sensors: {only_sensors}")

    download_api_data()
    logger.info("--- Syncing FTP and Field Data ---")

    if not METADATA_FILE.exists():
        logger.error(f"Could not find {METADATA_FILE}.")
        return
    if not FTP_SOURCES_FILE.exists():
        logger.error(f"Could not find {FTP_SOURCES_FILE}. Create ftp_sources.txt with server credentials.")
        return

    sensors = load_sensors_from_excel(METADATA_FILE)
    sources = load_ftp_sources()
    cache = load_sync_cache()

    try:
        for sensor_key, sensor_data in sensors.items():
            if only_sensors and sensor_key not in only_sensors:
                continue
            if not sensor_data.get("active", False):
                logger.info(f"Skipping inactive sensor: {sensor_key}")
                continue

            root_folder = sensor_data.get("root_folder_name")
            if not root_folder:
                continue

            logger.info(f"Processing {sensor_key}...")

            # --- Metadata-change detection ---
            current_hash = compute_sensor_hash(sensor_data)
            prev_hash = cache.get("metadata_hashes", {}).get(sensor_key)
            if prev_hash and prev_hash != current_hash and not force:
                logger.info(f"  Metadata changed for {sensor_key} — invalidating cache")
                invalidate_sensor_cache(cache, root_folder)
            cache.setdefault("metadata_hashes", {})[sensor_key] = current_hash

            sensor_dir = TARGET_DATA_DIR / root_folder
            phases = sensor_data.get("phase", [])

            total_synced = 0

            # 1. FTP files (direct download — lists each unique dir once)
            total_synced += process_sensor_ftp(
                phases, sensor_dir, sources, cache, force
            )

            for phase in phases:
                if not phase.get("folder_name"):
                    continue

                # Always sync config YAML into every existing phase subfolder
                config_filename = phase.get("config_file")
                if config_filename:
                    config_src = SENSOR_CONFIG_DIR / config_filename
                    if config_src.exists():
                        for subfolder in ["ftp", "sd"]:
                            dest_dir = sensor_dir / phase["folder_name"] / subfolder
                            if dest_dir.exists():
                                sync_local_file(
                                    config_src,
                                    dest_dir / Path(config_filename).name,
                                    cache, force=True,
                                )
                    else:
                        logger.warning(f"Config file '{config_filename}' not found at {config_src}")

                # 2. Field (SD) files (local) — only when field sync is enabled
                if copy_field:
                    field_files = gather_files_for_phase(
                        FIELD_PATH, phase, "field_folder"
                    )
                    if field_files:
                        total_synced += process_field_files(
                            field_files, phases, phase, sensor_dir, cache, force
                        )

                    # 3. Auxiliary field probe data (PR2, theta)
                    for aux_key, aux_subfolder in [
                        ("field_pr2_folder", "sd_pr2"),
                        ("field_theta_folder", "sd_theta"),
                    ]:
                        aux_files = gather_files_for_phase(
                            FIELD_PATH, phase, aux_key
                        )
                        if aux_files:
                            total_synced += process_field_files(
                                aux_files, phases, phase, sensor_dir, cache,
                                force, dest_subfolder=aux_subfolder
                            )

            logger.info(f"  -> Synced {total_synced} files.")

            # Remove folders no longer in metadata
            cleanup_stale_phase_folders(sensor_dir, phases)

        # Save cache after full run
        save_sync_cache(cache)
        logger.info(f"Sync cache saved ({len(cache.get('files', {}))} entries).")
    finally:
        close_all_ftp()

    logger.info(f"Success! Data structured in: {TARGET_DATA_DIR}")


if __name__ == "__main__":
    main()
