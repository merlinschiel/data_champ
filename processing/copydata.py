"""
Data sync step: downloads raw files from FTP servers and HTTP APIs and copies field/SD
card data into data/<sensor>/<phase>/<ftp|sd|sd_pr2|sd_theta>, as defined in
processing/meta/sensor_metadata.xlsx. Also keeps the phase config YAMLs in the data
folders in sync with processing/sensor_config.
"""

import shutil
import os
import re
import json
import hashlib
import ftplib
import io
import argparse
import requests
import pandas as pd
import traceback
from pathlib import Path
from datetime import datetime

from copy_yamls import sync_sensor_configs
from log_utils import get_logger
from sensor_metadata import (
    METADATA_FILE,
    META_DIR,
    PROJECT_ROOT,
    TARGET_DATA_DIR,
    load_sensors_from_excel,
    parse_sensor_filter,
    sensor_matches,
)

# ---------------------------------------------------------------------------
# Configs — all paths relative to project root
# ---------------------------------------------------------------------------

# field/SD data root; Field_Folder patterns are relative to it. Set CRNS_FIELD_DATA_PATH
# to point elsewhere, otherwise the folder next to the project is used.
FIELD_PATH = Path(os.environ.get("CRNS_FIELD_DATA_PATH") or PROJECT_ROOT.parent)

logger = get_logger("copydata", "copydata.log")

FTP_SOURCES_FILE = META_DIR / "ftp_sources.txt"
SYNC_CACHE_FILE = META_DIR / "sync_cache.json"



# ---------------------------------------------------------------------------
# HTTP API downloads
# ---------------------------------------------------------------------------

def download_api_data():
    """Download data from HTTP APIs defined in processing/meta/api_sources.txt."""
    api_config_file = META_DIR / "api_sources.txt"
    if not api_config_file.exists():
        logger.info("No api_sources.txt found")
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
            response = requests.get(url, timeout=(30, 300))
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

def get_remote_file_info(ftp: ftplib.FTP, entry: str) -> tuple:
    """Fetch size and MDTM modification time of a remote file."""
    size = ftp.size(entry)
    mtime = ftp.sendcmd(f"MDTM {entry}")[4:].strip()
    return size, mtime


def ftp_list_files(ftp: ftplib.FTP, host: str, remote_dir: str, _depth: int = 0) -> list[dict]:
    """Return list of {'name': basename, 'path': full_path, 'size': int, 'mtime': str}.
    Recurses into subdirectories. Uses NLST + per-file SIZE/MDTM (the servers in use do
    not support MLSD)."""
    results = []
    if _depth == 0:
        logger.info(f"    Listing FTP dir: {remote_dir}")

    try:
        ftp.sendcmd("TYPE I")
        entries = ftp.nlst(remote_dir)
    except Exception as e:
        if _depth == 0:
            logger.warning(f"    Could not list FTP dir '{remote_dir}' on {host}: {e}")
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

        try:
            size, mtime = get_remote_file_info(ftp, entry)
        except ftplib.all_errors as e:
            logger.error(f"    Could not read size/mtime of '{entry}' on {host}: {e}")
            continue
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

def to_relative_key(dest_key: str) -> str:
    """Cache key of a destination file: its path relative to the project root."""
    return Path(dest_key).relative_to(PROJECT_ROOT).as_posix()


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


def _normalise_mtime(mtime) -> str:
    """FTP MDTM times come as YYYYMMDDHHMMSS[.fff]; compare on whole seconds."""
    text = str(mtime or "").strip()
    match = re.match(r"^(\d{14})", text)
    return match.group(1) if match else text


def is_cached(cache: dict, dest_key: str, src_size: int, src_mtime: str) -> bool:
    """True if the destination file exists and was synced from a source with this size/mtime."""
    r_key = to_relative_key(dest_key)
    entry = cache.get("files", {}).get(r_key)
    if not entry:
        return False
    if not Path(dest_key).exists():
        return False
    return (entry.get("src_size") == src_size
            and _normalise_mtime(entry.get("src_mtime")) == _normalise_mtime(src_mtime))


def update_cache_entry(cache: dict, dest_key: str, src_path: str, src_size: int, src_mtime: str):
    r_key = to_relative_key(dest_key)

    cache.setdefault("files", {})[r_key] = {
        "src_path": str(src_path).replace("\\", "/"),
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
            for matched in base_path.glob(clean_pattern):
                if matched.is_dir():
                    for file_pattern in valid_filenames:
                        files.extend(f for f in matched.rglob(file_pattern)
                                     if f.is_file() and not should_skip_file(f.name))
                elif matched.is_file() and not should_skip_file(matched.name):
                    files.append(matched)
        except OSError as e:
            logger.error(f"Error reading field files for pattern {clean_pattern} in {base_path}: {e}")
    return list(set(files))


def sync_local_file(src_path: Path, dest_path: Path, cache: dict, force=False) -> bool:
    """Copy a local file if changed. Tracked in sync cache."""

    try:
        src_size = src_path.stat().st_size
        src_mtime = str(src_path.stat().st_mtime)
        dest_key = str(dest_path.resolve()).replace("\\", "/")

        if not force and is_cached(cache, dest_key, src_size, src_mtime):
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
    """Delete subdirectories of sensor_dir that no phase references.

    This folder is a copy of the original data, so anything deleted here is re-synced
    from the source if the phase reappears in the metadata.
    """
    valid_folders = {phase.get("folder_name") for phase in phases if phase.get("folder_name")}
    if not sensor_dir.exists():
        return

    for child in sensor_dir.iterdir():
        if not child.is_dir() or child.name in valid_folders:
            continue
        logger.info(f"  Deleting folder no longer in metadata: {child}")
        try:
            shutil.rmtree(child)
        except Exception as e:
            logger.error(f"  Failed to delete {child}: {e}")


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def run_pipeline(sensor=None, force=False, field=False):
    _run_pipeline(force=force, sensor=sensor, field=field)


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


def _run_pipeline(force=False, sensor=None, field=False):
    copy_field = field
    if copy_field:
        logger.info(f"Field sync ENABLED (source: {FIELD_PATH})")
    else:
        logger.info("Field sync DISABLED (use --field to enable)")

    sensor_filter = parse_sensor_filter(sensor)
    if sensor_filter:
        logger.info(f"Filtering to sensors: {sorted(sensor_filter)}")

    if not METADATA_FILE.exists():
        raise FileNotFoundError(f"Could not find {METADATA_FILE}.")
    if not FTP_SOURCES_FILE.exists():
        raise FileNotFoundError(f"Could not find {FTP_SOURCES_FILE}. Create ftp_sources.txt with server credentials.")

    download_api_data()
    logger.info("--- Syncing FTP and Field Data ---")

    sensors = load_sensors_from_excel(METADATA_FILE)
    sources = load_ftp_sources()
    cache = load_sync_cache()

    try:
        for sensor_key, sensor_data in sensors.items():
            if not sensor_matches(sensor_filter, sensor_key, sensor_data):
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

            # Phase config YAMLs: current config into ftp/ and sd/, stale copies deleted
            counts = sync_sensor_configs(sensor_key, sensor_data, log_fn=logger.info)
            if counts["copied"]:
                logger.info(f"  -> Updated {counts['copied']} config YAML copies")

        # Save cache after full run
        save_sync_cache(cache)
        logger.info(f"Sync cache saved ({len(cache.get('files', {}))} entries).")
    finally:
        close_all_ftp()

    logger.info(f"Success! Data structured in: {TARGET_DATA_DIR}")


if __name__ == "__main__":
    main()
