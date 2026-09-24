"""
Shared utilities for merging CRNS raw data files.
Used by auto_append.py and the data hub.
"""

import sys
import os

# Fix Windows console UTF-8 encoding for Rich / Neptoon unicode characters
os.environ['PYTHONIOENCODING'] = 'utf-8'
if hasattr(sys.stdout, 'reconfigure'):
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass
if hasattr(sys.stderr, 'reconfigure'):
    try:
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

import copy
import io
import re
import warnings
import yaml
import pandas as pd
from pathlib import Path
import hashlib
import json
import shutil


SKIP_PATTERNS = ('hvr', 'npmdiag', 'status')


def _fix_sdi_column_drop(content: str, unique_cols: list, log_fn=None, file_path=None) -> str:
    """Automatically detects SDI sensor column drops (e.g., PR2 6-to-3, SMT 5-to-3, PR2-4 4-to-3) in raw lines and pads missing columns with -99."""
    if not content or 'sdi' not in content.lower():
        return content

    # Determine expected value count for each SDI block from unique_cols
    sdi_col_indices = [i for i, col in enumerate(unique_cols)] if unique_cols else []
    sdi_col_indices = [i for i in sdi_col_indices if 'sdi' in str(unique_cols[i]).lower()]

    expected_counts = []
    if sdi_col_indices and unique_cols:
        for idx, sdi_idx in enumerate(sdi_col_indices):
            if idx + 1 < len(sdi_col_indices):
                expected_counts.append(sdi_col_indices[idx + 1] - sdi_idx - 1)
            else:
                expected_counts.append(len(unique_cols) - sdi_idx - 1)

    lines = content.splitlines(keepends=True)
    cleaned_lines = []
    corrections = 0

    for line in lines:
        stripped = line.rstrip('\r\n')
        # Skip empty lines or comment lines
        if not stripped or stripped.startswith('//') or stripped.startswith('#'):
            cleaned_lines.append(line)
            continue

        sep_char = ',' if ',' in line else ('\t' if '\t' in line else None)
        if not sep_char:
            cleaned_lines.append(line)
            continue

        parts = stripped.split(sep_char)
        raw_sdi_indices = [i for i, p in enumerate(parts) if re.match(r'^sdi[\w-]*$', p.strip(), re.IGNORECASE)]

        if not raw_sdi_indices:
            cleaned_lines.append(line)
            continue

        line_modified = False

        if expected_counts:
            num_raw = len(raw_sdi_indices)
            num_exp = len(expected_counts)

            # Process SDI blocks right-to-left so list insertion doesn't invalidate earlier indices
            for k in range(num_raw - 1, -1, -1):
                sdi_pos = raw_sdi_indices[k]
                exp_count = expected_counts[k] if (num_raw == num_exp or k < num_exp) else expected_counts[-1]

                val_start = sdi_pos + 1
                val_end = raw_sdi_indices[k + 1] if k + 1 < num_raw else len(parts)
                actual_count = val_end - val_start

                if actual_count < exp_count:
                    missing = exp_count - actual_count
                    parts[val_end:val_end] = ['-99'] * missing
                    line_modified = True
        elif len(unique_cols) if unique_cols else None:
            # Fallback when unique_cols provided but no SDI in unique_cols
            num_expected = len(unique_cols)
            if len(parts) < num_expected:
                missing = num_expected - len(parts)
                parts.extend(['-99'] * missing)
                line_modified = True

        if line_modified:
            corrections += 1
            new_line = sep_char.join(parts) + ('\r\n' if line.endswith('\r\n') else ('\n' if line.endswith('\n') else ''))
            cleaned_lines.append(new_line)
        else:
            cleaned_lines.append(line)

    return "".join(cleaned_lines)
# remove duplicate columns
def make_unique_columns(cols):

    counts = {}
    unique_cols = []
    for col in cols:
        if col in counts:
            counts[col] += 1
            unique_cols.append(f"{col}_{counts[col]}")
        else:
            counts[col] = 0
            unique_cols.append(col)
    return unique_cols

# build master column list from union of all yaml configs
def determine_master_config(configs):

    seen = set()
    master_columns = []
    dt_cols = ['Date_Time']
    dt_col = 'Date_Time'
    dt_format = None
    base_config = None  # first config used as template for generated yaml

    for c in configs:
        parse_opts = c['config'].get('raw_data_parse_options', {})
        raw_cols = parse_opts.get('column_names', [])
        unique_cols = make_unique_columns(raw_cols)

        # keep first config as template
        if base_config is None:
            base_config = c['config']

        # union columns preserving first-seen order
        for col in unique_cols:
            if col not in seen:
                seen.add(col)
                master_columns.append(col)

        # Add supplementary file columns in order of appearance to keep YAML order
        supp_files = c['config'].get('supplementary_files', [])
        if isinstance(supp_files, list):
            for sf in supp_files:
                sf_type = sf.get('type', '')
                if sf_type in ('profile_data', 'field_probe_data'):
                    col_names = sf.get('column_names', [])
                    if col_names:
                        col_prefix = sf.get('column_prefix', '')
                        dt_col_prof = sf.get('datetime_column', 'datetime')
                        for idx, col in enumerate(col_names):
                            if col == dt_col_prof or (idx == 0 and col.lower() in ('datetime', 'date_time', 'date time(utc)')):
                                continue
                            final_col = f"{col_prefix}_{col}" if col_prefix else col
                            if final_col not in seen:
                                seen.add(final_col)
                                master_columns.append(final_col)

        try:
            ts_data = c['config'].get('time_series_data', {})
            dt_cols = ts_data['key_column_info']['date_time_columns']
            dt_col = " ".join(dt_cols)
            fmt = ts_data['key_column_info'].get('date_time_format')
            if fmt:
                dt_format = fmt
        except (KeyError, IndexError, TypeError):
            pass

    # filter out scrap columns
    master_columns = [c for c in master_columns
                      if 'scrap_col' not in c.lower()]

    return master_columns, dt_cols, dt_col, dt_format, base_config

# find data based on prefix and suffix in yaml file
def find_data_files(data_dir: Path, parse_opts: dict):

    prefixes = parse_opts.get('prefix', ['*'])
    suffixes = parse_opts.get('suffix', [''])
    if isinstance(prefixes, str):
        prefixes = [prefixes]
    if isinstance(suffixes, str):
        suffixes = [suffixes]
    n = max(len(prefixes), len(suffixes))
    if len(prefixes) < n:
        prefixes.extend(['*'] * (n - len(prefixes)))
    if len(suffixes) < n:
        suffixes.extend([''] * (n - len(suffixes)))

    data_files = []
    for pref, suff in zip(prefixes, suffixes):
        if suff:
            found = list(data_dir.glob(f"{pref}{suff}"))
            if not found and not suff.endswith('*'):
                found = list(data_dir.glob(f"{pref}{suff}*"))
            data_files.extend(found)
        else:
            found = list(data_dir.glob(f"{pref}*"))
            data_files.extend(
                [f for f in found if f.is_file() and f.suffix.lower() != '.yaml'])

    # Deduplicate while preserving order
    return list(dict.fromkeys(data_files))

# read raw files after cleaning
def read_raw_file(file_path: Path, sep, skip_lines, unique_cols, encoding, decimal, skip_space, log_fn=None):

    try:
        with open(file_path, 'r', encoding=encoding) as raw_file:
            content = raw_file.read()

        if '|' in content or sep in ('\t', '\\t'):
            cleaned_lines = []
            for line in content.splitlines():
                if '|' in line:
                    line = line.split('|')[0]
                if sep in ('\t', '\\t'):
                    line = line.rstrip('\n\r') + '\n'
                else:
                    line = line.rstrip('\t\n\r') + '\n'
                cleaned_lines.append(line)
            content = "".join(cleaned_lines)

        content = _fix_sdi_column_drop(content, unique_cols)
        data_input = io.StringIO(content)

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter('always')
            df = pd.read_csv(
                data_input,
                sep=sep,
                skiprows=skip_lines,
                names=unique_cols,
                index_col=False,
                encoding=encoding,
                decimal=decimal,
                skipinitialspace=skip_space,
                engine='python',
                on_bad_lines='skip'
            )
            if log_fn:
                for w in caught:
                    if issubclass(w.category, pd.errors.ParserWarning):
                        log_fn(f"  -> Column mismatch in {file_path} "
                               f"(yaml expects {len(unique_cols)} cols): {w.message}")
        return df

    except Exception as e:
        if log_fn:
            log_fn(f"  -> Error reading {file_path}: {e}")
        return None

# read all config data for a sensor group
def read_all_config_data(configs, log_fn=None, use_cache=True):
    if not configs:
        return []

    # Get the sensor name from the first config
    sensor_name = str(configs[0]['config'].get('sensor_info', {}).get('name', 'generic'))
    
    # Define cache paths
    processing_dir = Path(__file__).resolve().parent
    project_root = processing_dir.parent
    cache_dir = project_root / "data" / ".merged" / "parsed_cache"
    cache_index_file = cache_dir / f"{sensor_name}_index.json"
    cache_subdir = cache_dir / sensor_name
    
    current_config_hash = ""
    cache_index = {"config_hash": "", "files": {}}
    cache_dirty = False
    
    if use_cache:
        hasher = hashlib.sha256()
        try:
            hasher.update(Path(__file__).read_bytes())
        except Exception:
            pass
        for c in configs:
            y_path = c.get('yaml_path')
            if y_path:
                try:
                    hasher.update(str(y_path).encode('utf-8'))
                    hasher.update(y_path.read_bytes())
                except Exception:
                    pass
        current_config_hash = hasher.hexdigest()[:16]

        if cache_index_file.exists():
            try:
                with open(cache_index_file, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                    if loaded.get("config_hash") == current_config_hash:
                        cache_index = loaded
                    else:
                        if log_fn:
                            log_fn(f"  YAML config changed for {sensor_name} — invalidating parsed merge cache.")
                        # Clean up old pickle directory if config changed
                        if cache_subdir.exists():
                            shutil.rmtree(cache_subdir)
                        cache_dirty = True
            except Exception as e:
                if log_fn:
                    log_fn(f"  Error reading parsed merge cache index for {sensor_name}: {e}")
                cache_dirty = True

    all_dfs = []

    for c in configs:
        y_path = c['yaml_path']
        data_dir = y_path.parent
        origin = data_dir.name.lower()

        parse_opts = c['config'].get('raw_data_parse_options', {})
        raw_cols = parse_opts.get('column_names', [])
        if not raw_cols:
            continue

        sep = parse_opts.get('separator', ',')
        skip_lines = parse_opts.get('skip_lines', 0)
        encoding = parse_opts.get('encoding', 'utf-8')
        decimal = parse_opts.get('decimal', '.')
        skip_space = parse_opts.get('skip_initial_space', True)
        unique_cols = make_unique_columns(raw_cols)

        data_files = find_data_files(data_dir, parse_opts)

        config_dfs = []
        for df_path in data_files:
            if any(pat in df_path.name.lower() for pat in SKIP_PATTERNS):
                continue

            df = None
            if use_cache:
                try:
                    rel_path = df_path.relative_to(project_root).as_posix()
                except Exception:
                    rel_path = str(df_path).replace("\\", "/")

                try:
                    stat = df_path.stat()
                    src_size = stat.st_size
                    src_mtime = stat.st_mtime
                except Exception:
                    src_size = 0
                    src_mtime = 0.0

                entry = cache_index.get("files", {}).get(rel_path)
                if entry and entry.get("size") == src_size and entry.get("mtime") == src_mtime:
                    cache_file = cache_dir / entry.get("pkl")
                    if cache_file.exists():
                        try:
                            df = pd.read_pickle(cache_file)
                        except Exception as e:
                            if log_fn:
                                log_fn(f"  -> Error reading cached pickle {cache_file}: {e}")
                            df = None

            if df is None:
                # Cache miss, bypass, or read failure — parse raw file
                df = read_raw_file(df_path, sep, skip_lines, unique_cols,
                                   encoding, decimal, skip_space, log_fn)
                if df is not None and use_cache:
                    # Save to cache
                    try:
                        cache_subdir.mkdir(parents=True, exist_ok=True)
                        # Generate a short unique name for the pickle file based on filename hash
                        df_hash = hashlib.md5(rel_path.encode('utf-8')).hexdigest()[:16]
                        cache_filename = f"{df_hash}.pkl"
                        cache_file = cache_subdir / cache_filename
                        
                        df.to_pickle(cache_file)
                        
                        cache_index.setdefault("files", {})[rel_path] = {
                            "size": src_size,
                            "mtime": src_mtime,
                            "pkl": f"{sensor_name}/{cache_filename}"
                        }
                        cache_dirty = True
                    except Exception as e:
                        if log_fn:
                            log_fn(f"  -> Failed to cache parsed DataFrame for {df_path}: {e}")

            if df is not None:
                # Resolve and parse datetime column using its config-specific format before appending
                try:
                    ts_data = c['config'].get('time_series_data', {})
                    cfg_dt_cols = ts_data['key_column_info']['date_time_columns']
                    cfg_dt_col = " ".join(cfg_dt_cols)
                    cfg_dt_format = ts_data['key_column_info'].get('date_time_format')
                except (KeyError, IndexError, TypeError):
                    cfg_dt_cols = ['Date_Time']
                    cfg_dt_col = 'Date_Time'
                    cfg_dt_format = None
                
                resolved_col = resolve_datetime_column(df, cfg_dt_cols, cfg_dt_col)
                if resolved_col:
                    df['Date_Time'] = pd.to_datetime(df[resolved_col], format=cfg_dt_format, errors='coerce', utc=True)
                
                df['_origin_priority'] = 1 if origin == 'sd' else 2
                config_dfs.append(df)

        # Apply time_shift options if defined in config
        time_shift_cfg = parse_opts.get('time_shift') or c['config'].get('time_series_data', {}).get('time_shift')
        if time_shift_cfg and isinstance(time_shift_cfg, dict) and time_shift_cfg.get('enabled', True):
            mode = time_shift_cfg.get('mode', 'offset')
            if mode in ('reconstruct_sequence', 'linear_sequence', 'interval'):
                start_str = time_shift_cfg.get('start_date') or time_shift_cfg.get('target_start')
                interval_str = time_shift_cfg.get('interval', '20min')
                if start_str and config_dfs:
                    start_ts = pd.to_datetime(start_str, utc=True)
                    freq = pd.Timedelta(interval_str)
                    
                    total_rows = 0
                    current_ts = start_ts
                    for d in config_dfs:
                        if not d.empty:
                            if 'RecordNum' in d.columns:
                                try:
                                    d.sort_values(by='RecordNum', inplace=True)
                                except Exception:
                                    pass
                            n_rows = len(d)
                            d['Date_Time'] = [current_ts + i * freq for i in range(n_rows)]
                            current_ts += n_rows * freq
                            total_rows += n_rows
                    if log_fn:
                        log_fn(f"  -> Time shift '{mode}': generated {total_rows} timestamps from {start_ts} with interval {interval_str}")
            else:
                delta = None
                if mode == 'align_start':
                    target_start = time_shift_cfg.get('target_start') or time_shift_cfg.get('start_date')
                    if target_start:
                        target_ts = pd.to_datetime(target_start, utc=True)
                        valid_dts = [d['Date_Time'].dropna().min() for d in config_dfs 
                                     if 'Date_Time' in d.columns and not d['Date_Time'].dropna().empty]
                        if valid_dts:
                            all_min = min(valid_dts)
                            delta = target_ts - all_min
                            if log_fn:
                                log_fn(f"  -> Time shift 'align_start': shifted by {delta} (aligning {all_min} to {target_ts})")
                elif mode == 'offset':
                    offset_val = time_shift_cfg.get('offset')
                    if offset_val is not None:
                        if isinstance(offset_val, (int, float)):
                            delta = pd.Timedelta(seconds=offset_val)
                        else:
                            try:
                                delta = pd.Timedelta(offset_val)
                            except Exception:
                                try:
                                    delta = pd.to_timedelta(offset_val)
                                except Exception:
                                    pass
                    if delta is None:
                        kwargs = {k: time_shift_cfg[k] for k in ['days', 'hours', 'minutes', 'seconds', 'weeks'] if k in time_shift_cfg}
                        if kwargs:
                            delta = pd.Timedelta(**kwargs)
                    if delta is not None and log_fn:
                        log_fn(f"  -> Time shift 'offset': shifted by {delta}")

                if delta is not None:
                    for d in config_dfs:
                        if 'Date_Time' in d.columns:
                            d['Date_Time'] = d['Date_Time'] + delta

        all_dfs.extend(config_dfs)

    if use_cache and cache_dirty:
        cache_index["config_hash"] = current_config_hash
        try:
            cache_dir.mkdir(parents=True, exist_ok=True)
            with open(cache_index_file, "w", encoding="utf-8") as f:
                json.dump(cache_index, f, indent=2, ensure_ascii=False)
        except Exception as e:
            if log_fn:
                log_fn(f"  Failed to save parsed merge cache index for {sensor_name}: {e}")

    return all_dfs



def resolve_datetime_column(merged_df, dt_cols, dt_col):

    if 'Date_Time' in merged_df.columns:
        return 'Date_Time'
    elif len(dt_cols) > 1 and all(col in merged_df.columns for col in dt_cols):
        merged_df[dt_col] = merged_df[dt_cols[0]].astype(str).str.strip()
        for col in dt_cols[1:]:
            merged_df[dt_col] += " " + merged_df[col].astype(str).str.strip()
        return dt_col
    elif dt_cols and dt_cols[0] in merged_df.columns:
        return dt_cols[0]
    return None


def clean_and_deduplicate(merged_df, master_columns, actual_dt_col, dt_format,
                          start_date=None, end_date=None, dt_cols=None):
    """Filter columns, parse datetime, drop duplicates (SD priority), format output.
    
    Returns cleaned DataFrame with 'Date_Time' as the datetime column name.
    """
    dt_col = actual_dt_col

    # Parse datetime, filter, dedup
    if dt_col in merged_df.columns:
        if not pd.api.types.is_datetime64_any_dtype(merged_df[dt_col]):
            parsed = pd.to_datetime(merged_df[dt_col], format=dt_format, errors='coerce', utc=True)
            if parsed.isna().any():
                fallback = pd.to_datetime(merged_df[dt_col], format='mixed', errors='coerce', utc=True)
                parsed = parsed.fillna(fallback)
            merged_df[dt_col] = parsed
        merged_df = merged_df.dropna(subset=[dt_col])

        # Filter faulty timestamps (past < 2010 and future > current_year + 1)
        current_year = pd.Timestamp.now().year
        merged_df = merged_df[
            (merged_df[dt_col].dt.year >= 2010) & 
            (merged_df[dt_col].dt.year <= current_year + 1)
        ]

        # Optional date range filter
        if start_date:
            ts_start = pd.Timestamp(start_date)
            if ts_start.tzinfo is None and hasattr(merged_df[dt_col].dt, 'tz') and merged_df[dt_col].dt.tz is not None:
                ts_start = ts_start.tz_localize('UTC')
            merged_df = merged_df[merged_df[dt_col] >= ts_start]
        if end_date:
            ts_end = pd.Timestamp(end_date)
            if ts_end.tzinfo is None and hasattr(merged_df[dt_col].dt, 'tz') and merged_df[dt_col].dt.tz is not None:
                ts_end = ts_end.tz_localize('UTC')
            merged_df = merged_df[merged_df[dt_col] <= ts_end]

        # Drop duplicates, prioritize sd (lower priority number)
        merged_df = merged_df.sort_values(by=[dt_col, '_origin_priority'])
        merged_df = merged_df.drop_duplicates(subset=[dt_col], keep='first')
        merged_df = merged_df.sort_values(by=[dt_col])

        # Unified datetime column format
        merged_df[dt_col] = merged_df[dt_col].dt.strftime('%Y-%m-%dT%H:%M:%SZ')
        if dt_col != 'Date_Time' and 'Date_Time' in merged_df.columns:
            merged_df = merged_df.drop(columns=['Date_Time'])
        merged_df = merged_df.rename(columns={dt_col: 'Date_Time'})

    # Keep only master columns + Date_Time (excluding raw datetime columns and priority)
    exclude_cols = set()
    if dt_cols:
        if isinstance(dt_cols, (list, tuple)):
            exclude_cols.update(dt_cols)
            if len(dt_cols) > 1:
                exclude_cols.add(" ".join(dt_cols))
        else:
            exclude_cols.add(dt_cols)
    exclude_cols.add(actual_dt_col)
    exclude_cols.add('Date_Time')
    exclude_cols.add('_origin_priority')

    # Build the final column list
    ordered_cols = ['Date_Time']
    for col in master_columns:
        if col not in exclude_cols and col in merged_df.columns:
            ordered_cols.append(col)

    # If no other columns are found, keep everything excluding raw datetime/priority
    if len(ordered_cols) == 1:
        for col in merged_df.columns:
            if col not in exclude_cols:
                ordered_cols.append(col)

    merged_df = merged_df[[c for c in ordered_cols if c in merged_df.columns]]

    return merged_df


# ---------------------------------------------------------------------------
# YAML scanning, grouping, and batch merge (reusable across workflows)
# ---------------------------------------------------------------------------

def scan_yaml_files(data_dir: Path):
    """Scan all .yaml files under *data_dir* (excluding .merged, .processed, hidden and temp dirs)
    and return a dict  {yaml_path: config_dict}."""
    results = {}
    for y_path in data_dir.rglob("*.yaml"):
        if any(part.startswith('.') or part.startswith('_') for part in y_path.parts):
            continue
        if y_path.name.startswith('_'):
            continue
        try:
            with open(y_path, 'r', encoding='utf-8') as f:
                config = yaml.safe_load(f)
            if config and isinstance(config, dict):
                results[y_path] = config
        except Exception:
            continue
    return results


def get_unique_values(yaml_configs: dict, property_name: str):
    """Return sorted unique values for a given sensor_info property."""
    values = set()
    for cfg in yaml_configs.values():
        val = cfg.get('sensor_info', {}).get(property_name)
        if val is not None:
            values.add(str(val))
    return sorted(values)


def group_configs_by(yaml_configs: dict, property_name: str):
    """Group yaml configs by the chosen sensor_info property.
    Returns {value: [{'yaml_path': Path, 'config': dict}, ...]}."""
    groups = {}
    for y_path, cfg in yaml_configs.items():
        val = cfg.get('sensor_info', {}).get(property_name)
        if val is None:
            continue
        val = str(val)
        if val not in groups:
            groups[val] = []
        groups[val].append({'yaml_path': y_path, 'config': cfg})
    return groups


# ---------------------------------------------------------------------------
# Supplementary file processing (secondary detectors, profile data)
# ---------------------------------------------------------------------------

def _merge_secondary_detector(merged_df, data_dir, parse_opts, sf_config,
                               dt_col, dt_format, log_fn=None):
    """Read secondary detector file, sum specified columns into merged_df."""
    prefix = sf_config.get('file_prefix') or ''
    suffix = sf_config.get('file_suffix') or ''

    pat1 = f"{prefix}{suffix}"
    while '**' in pat1:
        pat1 = pat1.replace('**', '*')
    files = list(data_dir.glob(pat1))
    if not files:
        pat2 = f"{prefix}*{suffix}"
        while '**' in pat2:
            pat2 = pat2.replace('**', '*')
        files = list(data_dir.glob(pat2))
    if not files:
        if log_fn:
            log_fn(f"  -> No secondary detector file found: {prefix}*{suffix}")
        return merged_df

    raw_cols = parse_opts.get('column_names', [])
    unique_cols = make_unique_columns(raw_cols)
    sep = parse_opts.get('separator', ',')
    skip_lines = parse_opts.get('skip_lines', 0)
    encoding = parse_opts.get('encoding', 'utf-8')
    decimal = parse_opts.get('decimal', '.')
    skip_space = parse_opts.get('skip_initial_space', True)

    # columns to sum from config
    sum_cols = [op['source_column'] for op in sf_config.get('column_operations', [])
                if op.get('operation') == 'sum']

    for fpath in files:
        if log_fn:
            log_fn(f"  -> Processing secondary detector: {fpath.name}")

        df_sec = read_raw_file(fpath, sep, skip_lines, unique_cols,
                               encoding, decimal, skip_space, log_fn)
        if df_sec is None or df_sec.empty:
            continue

        # parse datetime in both frames
        if dt_col not in df_sec.columns or dt_col not in merged_df.columns:
            continue

        df_sec[dt_col] = pd.to_datetime(df_sec[dt_col], format='mixed', errors='coerce', utc=True).dt.tz_localize(None)
        df_sec = df_sec.dropna(subset=[dt_col])

        was_string = not pd.api.types.is_datetime64_any_dtype(merged_df[dt_col])
        if was_string:
            merged_df[dt_col] = pd.to_datetime(merged_df[dt_col], format='mixed', errors='coerce', utc=True).dt.tz_localize(None)
        elif hasattr(merged_df[dt_col].dt, 'tz') and merged_df[dt_col].dt.tz is not None:
            merged_df[dt_col] = merged_df[dt_col].dt.tz_convert('UTC').dt.tz_localize(None)

        # keep only datetime + sum columns from secondary
        keep = [dt_col] + [c for c in sum_cols if c in df_sec.columns]
        df_sec = df_sec[keep]

        # rename secondary sum columns to avoid clash during merge
        rename_map = {c: f"_sec_{c}" for c in sum_cols if c in df_sec.columns}
        df_sec = df_sec.rename(columns=rename_map)

        merged_df = merged_df.merge(df_sec, on=dt_col, how='left')

        # sum primary + secondary, write back to primary column
        for col in sum_cols:
            sec_name = f"_sec_{col}"
            if sec_name in merged_df.columns and col in merged_df.columns:
                merged_df[col] = (pd.to_numeric(merged_df[col], errors='coerce').fillna(0)
                                  + pd.to_numeric(merged_df[sec_name], errors='coerce').fillna(0))
                merged_df = merged_df.drop(columns=[sec_name])

        # convert datetime back to string if it was string before
        if was_string:
            merged_df[dt_col] = merged_df[dt_col].dt.strftime('%Y-%m-%d %H:%M:%S')

        if log_fn:
            log_fn(f"  -> Summed columns {sum_cols} from {fpath.name}")

    return merged_df


def _merge_profile_data(merged_df, data_dir, sf_config, dt_col, dt_format, log_fn=None):
    """Read profile or field probe data, resample, and merge using combine_first."""
    prefix = sf_config.get('file_prefix') or ''
    suffix = sf_config.get('file_suffix') or ''
    sf_type = sf_config.get('type', 'profile_data')

    pat1 = f"{prefix}{suffix}"
    while '**' in pat1:
        pat1 = pat1.replace('**', '*')
    files = list(data_dir.glob(pat1))
    if not files:
        pat2 = f"{prefix}*{suffix}"
        while '**' in pat2:
            pat2 = pat2.replace('**', '*')
        files = list(data_dir.glob(pat2))
    if not files:
        return merged_df

    popts = sf_config.get('parse_options', {})
    
    # Extract values with fallbacks
    if sf_type == 'field_probe_data':
        sep = sf_config.get('separator', '\t')
        decimal = sf_config.get('decimal', ',')
        skip_lines = sf_config.get('skip_lines', 2)
        encoding = sf_config.get('encoding', 'utf-8')
        dt_fmt_prof = sf_config.get('datetime_format', '%d.%m.%Y %H:%M:%S')
    else:
        sep = popts.get('separator', ',')
        decimal = popts.get('decimal', '.')
        skip_lines = popts.get('skip_lines', 0)
        encoding = popts.get('encoding', 'utf-8')
        dt_fmt_prof = sf_config.get('datetime_format', None)

    col_names = sf_config.get('column_names', None)
    col_prefix = sf_config.get('column_prefix', '')
    dt_col_prof = sf_config.get('datetime_column', 'datetime')
    resample_freq = sf_config.get('resample', '20min')
    resample_method = sf_config.get('resample_method', 'mean')

    probe_dfs = []

    for fpath in files:
        try:
            df_probe = pd.read_csv(
                fpath,
                sep=sep,
                skiprows=skip_lines,
                names=col_names,
                decimal=decimal,
                encoding=encoding,
                on_bad_lines='skip',
                engine='python'
            )
        except Exception as e:
            if log_fn:
                log_fn(f"  -> Error reading {fpath.name}: {e}")
            continue

        if df_probe.empty:
            continue

        # Determine datetime column
        if dt_col_prof in df_probe.columns:
            actual_dt_col_prof = dt_col_prof
        elif col_names and col_names[0] in df_probe.columns:
            actual_dt_col_prof = col_names[0]
        else:
            actual_dt_col_prof = df_probe.columns[0]

        df_probe[actual_dt_col_prof] = pd.to_datetime(
            df_probe[actual_dt_col_prof], format=dt_fmt_prof, errors='coerce', utc=True)
        df_probe = df_probe.dropna(subset=[actual_dt_col_prof])
        df_probe = df_probe.set_index(actual_dt_col_prof)

        # Force numeric, drop non-numeric
        df_probe = df_probe.apply(pd.to_numeric, errors='coerce')
        df_probe = df_probe.select_dtypes(include='number')

        # Add column prefix to avoid clashes between sensors
        if col_prefix:
            df_probe.columns = [f"{col_prefix}_{c}" for c in df_probe.columns]

        # Resample
        if resample_method == 'mean':
            df_probe = df_probe.resample(resample_freq).mean()
        else:
            df_probe = df_probe.resample(resample_freq).first()

        df_probe = df_probe.dropna(how='all')
        df_probe = df_probe.reset_index()
        df_probe = df_probe.rename(columns={actual_dt_col_prof: dt_col})

        probe_dfs.append(df_probe)

    if probe_dfs:
        df_probe_all = pd.concat(probe_dfs, ignore_index=True)
        # Drop duplicates on datetime column in case files overlap in time
        df_probe_all = df_probe_all.drop_duplicates(subset=[dt_col])
        df_probe_all = df_probe_all.set_index(dt_col).sort_index()

        # Parse datetime in merged_df if needed
        was_string = not pd.api.types.is_datetime64_any_dtype(merged_df[dt_col])
        if was_string:
            parsed = pd.to_datetime(merged_df[dt_col], format='%Y-%m-%d %H:%M:%S', errors='coerce', utc=True)
            if parsed.notna().any():
                merged_df[dt_col] = parsed
            else:
                merged_df[dt_col] = pd.to_datetime(merged_df[dt_col], format=dt_format, errors='coerce', utc=True)

        merged_df = merged_df.set_index(dt_col).sort_index()

        # Drop NaT in indexes to avoid ValueError in pd.merge_asof
        df_probe_all = df_probe_all[df_probe_all.index.notna()]
        merged_df = merged_df[merged_df.index.notna()]

        # Ensure both indexes match timezone (UTC)
        if hasattr(df_probe_all.index, 'tz') and df_probe_all.index.tz is not None:
            df_probe_all.index = df_probe_all.index.tz_convert('UTC')
        else:
            df_probe_all.index = df_probe_all.index.tz_localize('UTC')

        if hasattr(merged_df.index, 'tz') and merged_df.index.tz is not None:
            merged_df.index = merged_df.index.tz_convert('UTC')
        else:
            merged_df.index = merged_df.index.tz_localize('UTC')

        temp_df = pd.DataFrame(index=merged_df.index)
        aligned_probe = pd.merge_asof(
            temp_df,
            df_probe_all,
            left_index=True,
            right_index=True,
            direction='nearest',
            tolerance=pd.Timedelta('10min')
        )

        # Combine
        merged_df = merged_df.combine_first(aligned_probe)
        merged_df = merged_df.reset_index()

        if was_string:
            merged_df[dt_col] = merged_df[dt_col].dt.strftime('%Y-%m-%d %H:%M:%S')

    return merged_df



def process_supplementary_files(merged_df, configs, log_fn=None):
    """Process supplementary files defined in YAML configs.

    Call after concat, before clean_and_deduplicate.
    Handles secondary_detector (sum columns) and profile_data (resample + merge).
    """
    processed = set()

    for c in configs:
        supp_files = c['config'].get('supplementary_files', [])
        if not supp_files:
            continue

        data_dir = c['yaml_path'].parent
        parse_opts = c['config'].get('raw_data_parse_options', {})

        # get datetime info from config
        try:
            kci = c['config']['time_series_data']['key_column_info']
            dt_cols_cfg = kci.get('date_time_columns', ['Date_Time'])
            dt_format = kci.get('date_time_format')
        except (KeyError, TypeError):
            dt_cols_cfg = ['Date_Time']
            dt_format = None
        dt_col = " ".join(dt_cols_cfg)

        for sf in supp_files:
            # resolve data directory — field_probe_data may use a sibling subfolder
            sf_data_dir = data_dir
            data_subfolder = sf.get('data_subfolder')
            if data_subfolder:
                # go up from yaml dir (ftp/ or sd/) to phase dir, then into subfolder
                phase_dir = data_dir.parent
                sf_data_dir = phase_dir / data_subfolder
                if not sf_data_dir.exists():
                    if log_fn:
                        log_fn(f"  -> Subfolder not found: {sf_data_dir}")
                    continue

            # avoid processing same supplementary file twice (ftp + sd yamls in the same phase)
            sf_key = (sf.get('file_prefix'), sf.get('type'), str(sf_data_dir.resolve()))
            if sf_key in processed:
                continue
            processed.add(sf_key)

            sf_type = sf.get('type', '')
            if sf_type == 'secondary_detector':
                merged_df = _merge_secondary_detector(
                    merged_df, sf_data_dir, parse_opts, sf, dt_col, dt_format, log_fn)
            elif sf_type in ('profile_data', 'field_probe_data'):
                merged_df = _merge_profile_data(
                    merged_df, sf_data_dir, sf, dt_col, dt_format, log_fn)


    return merged_df


# ---------------------------------------------------------------------------
# Neptoon processing helpers
# ---------------------------------------------------------------------------

def _patch_yaml_for_neptoon(y_path: Path, cfg: dict) -> dict:
    """Patch a sensor YAML dict so neptoon can ingest it.

    - Sets data_location to the yaml's parent directory
    - Resolves wildcard prefix/suffix to concrete strings
    - Returns patched config dict (does NOT mutate original).
    """
    import copy as _copy
    cfg = _copy.deepcopy(cfg)

    parse_opts = cfg.get('raw_data_parse_options', {})
    if not parse_opts:
        return cfg

    data_dir = y_path.parent

    # --- prefix / suffix ---
    pref = parse_opts.get('prefix', '')
    suff = parse_opts.get('suffix', '')
    if isinstance(pref, list):
        pref = pref[0] if pref else ''
    if isinstance(suff, list):
        suff = suff[0] if suff else ''
    pref_str = str(pref)
    suff_str = str(suff)

    # Resolve wildcards by inspecting actual files
    if '*' in pref_str or '?' in pref_str:
        glob_pat = f"{pref_str}*{suff_str}"
        while '**' in glob_pat:
            glob_pat = glob_pat.replace('**', '*')
        try:
            actual_files = list(data_dir.glob(glob_pat))
        except Exception:
            actual_files = []
        if not actual_files:
            actual_files = (
                list(data_dir.glob(f"*{suff_str}")) if suff_str
                else list(data_dir.glob("*"))
            )
        if actual_files:
            sample_name = actual_files[0].name
            clean_pref = pref_str.replace('**/*', '').replace('*', '').replace('?', '')
            if clean_pref and clean_pref.lower() in sample_name.lower():
                idx = sample_name.lower().find(clean_pref.lower())
                pref_str = sample_name[:idx + len(clean_pref)]
            else:
                pref_str = sample_name[:4]
        else:
            pref_str = pref_str.replace('**/*', '').replace('*', '')

    if '*' in suff_str:
        suff_str = suff_str.replace('*', '')

    parse_opts['prefix'] = pref_str
    parse_opts['suffix'] = suff_str
    parse_opts['data_location'] = str(data_dir.resolve())

    return cfg


def neptoon_process_phase(yaml_path: Path, config: dict,
                          process_config_path: Path = None,
                          log_fn=None) -> pd.DataFrame:
    """Process single phase folder through neptoon's ProcessWithConfig.

    - Patches sensor config for neptoon compatibility
    - Writes temp YAML for neptoon ingestion
    - Runs full pipeline: QA -> corrections -> calibration -> aggregation -> SM
    - Returns processed DataFrame with neptoon output columns

    Parameters
    ----------
    yaml_path : Path
        Path to the phase sensor YAML.
    config : dict
        Parsed YAML config dict.
    process_config_path : Path, optional
        Path to shared process config YAML. If None, skips processing.
    log_fn : callable, optional
        Logging callback.

    Returns
    -------
    pd.DataFrame or None
    """
    from neptoon.workflow.process_with_config import ProcessWithConfig

    def log(msg):
        if log_fn:
            log_fn(msg)

    patched = _patch_yaml_for_neptoon(yaml_path, config)
    if patched is None:
        return None

    temp_y = yaml_path.parent / "_temp_neptoon_cfg.yaml"
    temp_out = yaml_path.parent / "_neptoon_out"
    try:
        # Enable figure creation to avoid neptoon save-step errors, but generate no actual figures.
        patched.setdefault('figures', {})['create_figures'] = True
        patched['figures']['make_all_figures'] = False
        patched['figures']['custom_list'] = []

        # Redirect any neptoon saves to a temp dir that we delete
        patched.setdefault('data_storage', {})['create_report'] = False
        patched['data_storage']['save_location'] = str(temp_out)
        patched['data_storage']['save_folder'] = str(temp_out)

        with open(temp_y, 'w', encoding='utf-8') as f:
            yaml.dump(patched, f, default_flow_style=False, sort_keys=False)


        proc = ProcessWithConfig(
            path_to_sensor_config=str(temp_y),
            path_to_process_config=str(process_config_path),
        )

        # run_full_process may raise on save/figure steps — catch and extract DataFrame directly.
        try:
            proc.run_full_process()
        except Exception as run_err:
            log(f"  -> Neptoon save/figure step note: {run_err}")

        # Extract DataFrame from data_hub regardless of save errors
        if proc.data_hub is None or proc.data_hub.crns_data_frame is None:
            log(f"  -> No data from neptoon for {yaml_path.parent.name}")
            return None

        df = proc.data_hub.crns_data_frame.copy()

        # Tag origin for dedup (SD has priority)
        origin = yaml_path.parent.name.lower()
        df['_origin_priority'] = 1 if origin == 'sd' else 2

        log(f"  -> Neptoon processed {len(df)} rows from {yaml_path.parent.name}")
        return df

    except Exception as e:
        log(f"  -> Neptoon error for {yaml_path.parent.name}: {e}")
        return None
    finally:
        if temp_y.exists():
            try:
                temp_y.unlink()
            except Exception:
                pass
        
        import shutil
        if temp_out.exists():
            try:
                shutil.rmtree(temp_out)
            except Exception:
                pass



def concat_processed_phases(phase_dfs: list, log_fn=None) -> pd.DataFrame:
    """Concat neptoon-processed phase DataFrames, handle overlap dedup.

    SD-origin rows take priority over FTP on duplicate timestamps.

    Parameters
    ----------
    phase_dfs : list[pd.DataFrame]
        DataFrames from neptoon_process_phase(), each with _origin_priority.
    log_fn : callable, optional
        Logging callback.

    Returns
    -------
    pd.DataFrame
        Combined timeseries, sorted by time, duplicates removed.
    """
    def log(msg):
        if log_fn:
            log_fn(msg)

    if not phase_dfs:
        return pd.DataFrame()

    combined = pd.concat(phase_dfs)

    # Ensure DatetimeIndex
    if not isinstance(combined.index, pd.DatetimeIndex):
        for col_name in ('Date_Time', 'date_time'):
            if col_name in combined.columns:
                combined[col_name] = pd.to_datetime(combined[col_name], errors='coerce')
                combined = combined.set_index(col_name)
                break

    # Drop invalid timestamps
    combined = combined[combined.index.notna()]
    combined = combined[combined.index.year >= 2010]

    # Dedup: SD priority (lower number = keep)
    if '_origin_priority' in combined.columns:
        combined = combined.sort_values(by=['_origin_priority'])
        combined = combined[~combined.index.duplicated(keep='first')].sort_index()
        combined = combined.drop(columns=['_origin_priority'], errors='ignore')
    else:
        combined = combined[~combined.index.duplicated(keep='first')].sort_index()

    log(f"  -> Combined: {len(combined)} rows, "
        f"{combined.index.min()} to {combined.index.max()}")

    return combined


def _sync_master_sensor_info(master: dict, configs: list = None):
    """Update location, coordinates, and elevation in master from the latest YAML config."""
    if not isinstance(master, dict):
        return master
    if 'sensor_info' not in master:
        master['sensor_info'] = {}

    if configs:
        def get_sort_key(c):
            path = c.get('yaml_path')
            if not path:
                return (0, "")
            try:
                folder_name = path.parent.parent.name
                is_now = 1 if 'now' in folder_name.lower() else 0
                parts = folder_name.split('_')
                start_date = parts[0] if parts else ""
                return (is_now, start_date)
            except Exception:
                return (0, "")

        try:
            sorted_configs = sorted(configs, key=get_sort_key)
            latest_c = sorted_configs[-1].get('config', {})
            latest_si = latest_c.get('sensor_info', {})
            for field in ['location', 'latitude', 'longitude', 'elevation']:
                val = latest_si.get(field)
                if val is not None:
                    master['sensor_info'][field] = val
        except Exception:
            pass

    return master


def _build_master_yaml(base_config, master_columns, group_value, configs=None):
    """Create a synthetic master YAML dict from base config + union columns."""
    master = copy.deepcopy(base_config) if base_config else {}
    master.setdefault('raw_data_parse_options', {})
    master['raw_data_parse_options']['parse_raw_data'] = True
    master['raw_data_parse_options']['column_names'] = list(master_columns)
    master['raw_data_parse_options']['prefix'] = f"merged_{group_value}"
    master['raw_data_parse_options']['suffix'] = ".csv"
    master['raw_data_parse_options']['data_location'] = "../"
    master['raw_data_parse_options']['separator'] = ","
    master['raw_data_parse_options']['skip_lines'] = 1
    master['raw_data_parse_options']['recursive'] = False
    master['raw_data_parse_options']['encoding'] = 'utf-8'

    # update location, coordinates, and elevation from latest config / location_metadata
    _sync_master_sensor_info(master, configs)

    # update time_series_data settings for master config
    if 'time_series_data' not in master:
        master['time_series_data'] = {}
    if 'key_column_info' not in master['time_series_data']:
        master['time_series_data']['key_column_info'] = {}
    master['time_series_data']['key_column_info']['date_time_columns'] = ['Date_Time']
    master['time_series_data']['key_column_info']['date_time_format'] = '%Y-%m-%dT%H:%M:%SZ'
    
    if 'temporal' in master['time_series_data']:
        del master['time_series_data']['temporal']

    return master


def merge_sensor_group(configs, output_dir: Path, group_value: str,
                       start_date=None, end_date=None, log_fn=None):
    """Merge data for one group (list of config dicts).
    Writes merged_<group_value>.csv to output_dir.
    Returns True on success."""

    def log(msg):
        if log_fn:
            log_fn(msg)

    master_columns, dt_cols, dt_col, dt_format, base_config = \
        determine_master_config(configs)

    all_dfs = read_all_config_data(configs, log_fn=log)

    if not all_dfs:
        log(f"  -> No valid data found for '{group_value}'\n")
        return False

    valid_dfs = [df for df in all_dfs if not df.empty]
    merged_df = pd.concat(valid_dfs, ignore_index=True) if valid_dfs else pd.DataFrame()

    # Drop any scrap columns that made it into the dataframe
    scrap_cols = [c for c in merged_df.columns if 'scrap_col' in c.lower()]
    if scrap_cols:
        merged_df = merged_df.drop(columns=scrap_cols)
        log(f"  -> Dropped scrap columns: {scrap_cols}")

    # Resolve datetime column
    actual_dt_col = resolve_datetime_column(merged_df, dt_cols, dt_col)

    if not actual_dt_col:
        log(f"  -> Warning: No valid datetime column found for '{group_value}'.")
        return False

    # Process supplementary files
    merged_df = process_supplementary_files(merged_df, configs, log_fn=log)

    # Add any newly introduced columns to master_columns
    for col in merged_df.columns:
        if col not in master_columns and col != '_origin_priority':
            master_columns.append(col)

    # Clean, deduplicate, format (with optional date range)
    merged_df = clean_and_deduplicate(
        merged_df, master_columns, actual_dt_col, dt_format,
        start_date=start_date, end_date=end_date, dt_cols=dt_cols)
    dt_col = 'Date_Time'

    out_file = output_dir / f"merged_{group_value}.csv"
    merged_df.to_csv(out_file, index=False)

    if not merged_df.empty:
        data_start = merged_df[dt_col].min()
        data_end = merged_df[dt_col].max()
        log(f"  -> Created {out_file.name}  ({len(merged_df)} rows, "
            f"{data_start} to {data_end})")

        # Write generated master YAML
        configs_dir = output_dir / "sensor_configs"
        configs_dir.mkdir(parents=True, exist_ok=True)
        master_yaml = _build_master_yaml(base_config, list(merged_df.columns), group_value, configs=configs)
        dest = configs_dir / f"sensor_{group_value}_master.yaml"
        try:
            with open(dest, 'w', encoding='utf-8') as f:
                yaml.dump(master_yaml, f, default_flow_style=False,
                          allow_unicode=True, sort_keys=False)
            log(f"  -> Wrote master config: {dest.name}")
        except Exception as e:
            log(f"  -> Failed to write master config: {e}")
    else:
        log(f"  -> Warning: DataFrame is empty after filtering for '{group_value}'.")

    return True


def merge_by_yaml_property(data_dir, output_dir, group_by,
                           selected_values=None, start_date=None,
                           end_date=None, log_fn=None):
    """Scan YAMLs, group by any sensor_info property, merge each group.

    Parameters
    ----------
    data_dir : str or Path
        Root directory to scan for YAML configs.
    output_dir : str or Path
        Directory where merged CSVs are written.
    group_by : str
        sensor_info property to group on (e.g. 'location', 'name',
        'country', 'identifier', etc.).
    selected_values : list[str] or None
        Only merge groups matching these values.  None = merge all.
    start_date, end_date : datetime or str or None
        Optional date range filter.
    log_fn : callable or None
        Logging callback  ``log_fn(msg: str)``.

    Returns
    -------
    dict
        ``{group_value: bool}`` — True if merge succeeded for that group.
    """
    data_dir = Path(data_dir)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    def log(msg):
        if log_fn:
            log_fn(msg)

    log(f"Scanning YAMLs in {data_dir} …")
    yaml_configs = scan_yaml_files(data_dir)
    log(f"Found {len(yaml_configs)} YAML configs.")

    groups = group_configs_by(yaml_configs, group_by)
    available = sorted(groups.keys())
    log(f"Unique '{group_by}' values: {available}")

    if selected_values is not None:
        targets = [str(v) for v in selected_values]
    else:
        targets = available

    results = {}
    for val in targets:
        log(f"\nProcessing '{val}' …")
        cfgs = groups.get(val, [])
        if not cfgs:
            log(f"  -> No YAML configs found for '{val}'")
            results[val] = False
            continue
        ok = merge_sensor_group(cfgs, output_dir, val,
                                start_date=start_date,
                                end_date=end_date,
                                log_fn=log)
        results[val] = ok

    success = sum(1 for v in results.values() if v)
    log(f"\nDone — {success}/{len(targets)} merged successfully.\n")
    return results


# ---------------------------------------------------------------------------
# Custom merge utilities — column scanning, N(cph), flexible output
# ---------------------------------------------------------------------------

def _is_count_rate_col(col_name):
    """Check if a column represents a count rate in counts per second."""
    if not col_name:
        return False
    clean = str(col_name).strip().lower().replace(' ', '').replace('_', '')
    return 'countrate' in clean


def scan_available_columns(data_dir, group_by, selected_values):
    """Scan merged data columns for selected groups without loading full data."""
    data_dir = Path(data_dir)
    yaml_configs = scan_yaml_files(data_dir)
    groups = group_configs_by(yaml_configs, group_by)

    all_columns = []
    seen = set()
    neutron_cols = []
    integration_time_cols = []
    configs_by_value = {}

    for val in selected_values:
        cfgs = groups.get(val, [])
        if not cfgs:
            continue
        configs_by_value[val] = cfgs

        for c in cfgs:
            parse_opts = c['config'].get('raw_data_parse_options', {})
            raw_cols = parse_opts.get('column_names', [])
            unique_cols = make_unique_columns(raw_cols)
            for col in unique_cols:
                if col not in seen and 'scrap_col' not in col.lower():
                    seen.add(col)
                    all_columns.append(col)

            # Detect neutron and integration time columns from config
            try:
                kci = c['config']['time_series_data']['key_column_info']
                n_cols = kci.get('epithermal_neutron_columns', [])
                for nc in (n_cols or []):
                    if nc not in neutron_cols:
                        neutron_cols.append(nc)
            except (KeyError, TypeError):
                pass

            # Detect Count_rate in column names as neutron column
            for col in unique_cols:
                if _is_count_rate_col(col) and col not in neutron_cols:
                    neutron_cols.append(col)

            # Heuristic: integration time columns typically named *ET* or *_ET
            for col in unique_cols:
                if col.upper().endswith('ET') or '_ET' in col.upper():
                    if col not in integration_time_cols:
                        integration_time_cols.append(col)

    return all_columns, neutron_cols, integration_time_cols, configs_by_value


def _find_most_recent_yaml_in_timeframe(configs, start_date=None, end_date=None):
    """Find the most recent YAML config within the given timeframe."""
    import re as _re

    scored = []
    for c in configs:
        y_path = c.get('yaml_path')
        if not y_path:
            continue

        # Try to extract date range from parent folder names
        # Walk up to find phase folder (pattern: YYYYMMDD_YYYYMMDD or YYYYMMDD_now)
        phase_start = None
        phase_end = None
        for parent in y_path.parents:
            folder = parent.name
            match = _re.match(r'^(\d{8})_(\d{8}|now)$', folder)
            if match:
                try:
                    phase_start = pd.to_datetime(match.group(1), format='%Y%m%d')
                except Exception:
                    pass
                if match.group(2).lower() == 'now':
                    phase_end = pd.Timestamp.now()
                else:
                    try:
                        phase_end = pd.to_datetime(match.group(2), format='%Y%m%d')
                    except Exception:
                        pass
                break

        scored.append({
            'config': c['config'],
            'yaml_path': y_path,
            'phase_start': phase_start,
            'phase_end': phase_end,
        })

    if not scored:
        return configs[-1]['config'] if configs else {}

    # Filter to configs overlapping the timeframe
    if start_date or end_date:
        ts_start = pd.to_datetime(start_date) if start_date else pd.Timestamp.min
        ts_end = pd.to_datetime(end_date) if end_date else pd.Timestamp.max

        in_range = [s for s in scored
                    if s['phase_start'] is not None
                    and s['phase_end'] is not None
                    and s['phase_start'] <= ts_end
                    and s['phase_end'] >= ts_start]
        if in_range:
            scored = in_range

    # Sort by phase_end descending, pick most recent
    scored.sort(key=lambda s: s['phase_end'] if s['phase_end'] is not None
                else pd.Timestamp.min, reverse=True)
    return scored[0]['config']


def _build_custom_master_yaml(base_config, selected_columns, output_filename, configs=None):
    """Create master YAML containing only selected columns."""
    master = copy.deepcopy(base_config) if base_config else {}
    master.setdefault('raw_data_parse_options', {})
    master['raw_data_parse_options']['parse_raw_data'] = True
    master['raw_data_parse_options']['column_names'] = list(selected_columns)

    # Strip extension for prefix
    stem = Path(output_filename).stem if output_filename else 'custom_merge'
    master['raw_data_parse_options']['prefix'] = stem
    master['raw_data_parse_options']['suffix'] = '.csv'
    master['raw_data_parse_options']['data_location'] = '../'
    master['raw_data_parse_options']['separator'] = ','
    master['raw_data_parse_options']['skip_lines'] = 1
    master['raw_data_parse_options']['recursive'] = False
    master['raw_data_parse_options']['encoding'] = 'utf-8'

    # Update location, coordinates, and elevation from latest config / location_metadata
    _sync_master_sensor_info(master, configs)

    # Time series data for master
    if 'time_series_data' not in master:
        master['time_series_data'] = {}
    if 'key_column_info' not in master['time_series_data']:
        master['time_series_data']['key_column_info'] = {}
    master['time_series_data']['key_column_info']['date_time_columns'] = ['Date_Time']
    master['time_series_data']['key_column_info']['date_time_format'] = '%Y-%m-%dT%H:%M:%SZ'

    if 'temporal' in master['time_series_data']:
        del master['time_series_data']['temporal']

    # Filter key_column_info to only columns present in selected_columns
    if 'key_column_info' in master['time_series_data']:
        kci = master['time_series_data']['key_column_info']
        for col_key in ['epithermal_neutron_columns', 'pressure_columns',
                        'temperature_columns', 'relative_humidity_columns']:
            if col_key in kci and isinstance(kci[col_key], list):
                filtered = [c for c in kci[col_key] if c in selected_columns]
                kci[col_key] = filtered if filtered else None

    return master


def add_neutron_cph_column(df, neutron_col, integration_time_col=None):
    """Add N(cph) column."""
    is_cr = _is_count_rate_col(neutron_col) or not integration_time_col
    cph_col = "N_cph" if is_cr else f"{neutron_col}_cph"
    n_counts = pd.to_numeric(df[neutron_col], errors='coerce')

    if is_cr:
        df[cph_col] = (n_counts * 3600).round(2)
        ref_col = neutron_col
    else:
        int_time = pd.to_numeric(df[integration_time_col], errors='coerce')
        int_time = int_time.where(int_time > 0)
        df[cph_col] = (n_counts / int_time * 3600).round(2)
        ref_col = integration_time_col if integration_time_col in df.columns else neutron_col

    # Reorder: place cph column right after integration_time_col (or neutron_col)
    cols = [c for c in df.columns if c != cph_col]
    try:
        insert_idx = cols.index(ref_col) + 1
    except ValueError:
        insert_idx = len(cols)
    cols.insert(insert_idx, cph_col)
    df = df[cols]
    return df, cph_col


def add_combined_neutron_cph_column(df, cph_pairs):
    """Sum multiple neutron columns into one N_cph column."""
    cph_col = "N_cph"
    valid_pairs = []
    for pair in cph_pairs:
        n = pair[0]
        et = pair[1] if len(pair) > 1 else None
        if n in df.columns:
            if _is_count_rate_col(n) or not et:
                valid_pairs.append((n, None))
            elif et in df.columns:
                valid_pairs.append((n, et))

    if not valid_pairs:
        return df, None

    # Calculate count rate (cps) for each detector, then sum
    rate_series = []
    for n_col, et_col in valid_pairs:
        n_series = pd.to_numeric(df[n_col], errors='coerce')
        if _is_count_rate_col(n_col) or et_col is None:
            rate_series.append(n_series)
        else:
            et_series = pd.to_numeric(df[et_col], errors='coerce')
            et_series = et_series.where(et_series > 0)
            rate_series.append(n_series / et_series)

    if len(rate_series) == 1:
        total_rate = rate_series[0]
    else:
        rate_df = pd.concat(rate_series, axis=1)
        total_rate = rate_df.sum(axis=1, min_count=1)

    df[cph_col] = (total_rate * 3600).round(2)

    # Place after the last neutron or ET column among all pairs
    all_pair_cols = [c for pair in valid_pairs for c in pair if c and c in df.columns]
    cols = [c for c in df.columns if c != cph_col]
    last_idx = -1
    for c in all_pair_cols:
        if c in cols:
            last_idx = max(last_idx, cols.index(c))
    if last_idx >= 0:
        cols.insert(last_idx + 1, cph_col)
    else:
        cols.append(cph_col)
    df = df[cols]
    return df, cph_col


def custom_merge(data_dir, group_by, selected_values,
                 selected_columns=None,
                 start_date=None, end_date=None,
                 output_dir=None, output_filename=None,
                 add_cph=False, cph_pairs=None, combine_cph=False,
                 log_fn=None):
    """Custom merge with column selection, timeframe, N(cph), and flexible output.

    Parameters
    ----------
    data_dir : str or Path
        Root data directory to scan for YAMLs.
    group_by : str
        sensor_info property to group on.
    selected_values : list[str]
        Property values to merge.
    selected_columns : list[str] or None
        Columns to keep. None = keep all.
    start_date, end_date : str or datetime or None
        Timeframe filter.
    output_dir : str or Path or None
        Output directory. Defaults to data/.merged.
    output_filename : str or None
        Output CSV filename. Defaults to merged_<value>.csv.
    add_cph : bool
        If True, add N(cph) columns.
    cph_pairs : list[tuple] or None
        List of (neutron_col, integration_time_col) pairs for N(cph).
    combine_cph : bool
        If True, sum all neutron+ET pairs into single N_cph column.
    log_fn : callable or None
        Logging callback.

    Returns
    -------
    dict  {value: bool}
    """
    data_dir = Path(data_dir)
    if output_dir is None:
        output_dir = data_dir / ".merged"
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    def log(msg):
        if log_fn:
            log_fn(msg)

    log(f"Scanning YAMLs in {data_dir} …")
    yaml_configs = scan_yaml_files(data_dir)
    log(f"Found {len(yaml_configs)} YAML configs.")

    groups = group_configs_by(yaml_configs, group_by)

    results = {}
    for val in selected_values:
        log(f"\nProcessing '{val}' …")
        cfgs = groups.get(val, [])
        if not cfgs:
            log(f"  -> No YAML configs found for '{val}'")
            results[val] = False
            continue

        # Determine master config and columns
        master_columns, dt_cols, dt_col, dt_format, base_config = \
            determine_master_config(cfgs)

        # Read all data (never use cache for custom merge)
        all_dfs = read_all_config_data(cfgs, log_fn=log, use_cache=False)
        if not all_dfs:
            log(f"  -> No valid data for '{val}'")
            results[val] = False
            continue

        valid_dfs = [df for df in all_dfs if not df.empty]
        merged_df = pd.concat(valid_dfs, ignore_index=True) if valid_dfs else pd.DataFrame()

        # Drop scrap columns
        scrap_cols = [c for c in merged_df.columns if 'scrap_col' in c.lower()]
        if scrap_cols:
            merged_df = merged_df.drop(columns=scrap_cols)

        # Resolve datetime
        actual_dt_col = resolve_datetime_column(merged_df, dt_cols, dt_col)
        if not actual_dt_col:
            log(f"  -> No valid datetime column for '{val}'")
            results[val] = False
            continue

        # Process supplementary files
        merged_df = process_supplementary_files(merged_df, cfgs, log_fn=log)

        # Update master columns with any new columns
        for col in merged_df.columns:
            if col not in master_columns and col != '_origin_priority':
                master_columns.append(col)

        # Clean and deduplicate with timeframe
        merged_df = clean_and_deduplicate(
            merged_df, master_columns, actual_dt_col, dt_format,
            start_date=start_date, end_date=end_date, dt_cols=dt_cols)

        if merged_df.empty:
            log(f"  -> Empty after filtering for '{val}'")
            results[val] = False
            continue

        # Add N(cph) columns if requested
        cph_added = []
        if add_cph:
            pairs_to_use = list(cph_pairs) if cph_pairs else []
            if not pairs_to_use:
                # Auto-detect count rate or neutron columns from merged_df
                for col in merged_df.columns:
                    if _is_count_rate_col(col):
                        pairs_to_use.append((col, None))
                if not pairs_to_use:
                    for col in merged_df.columns:
                        if col.upper().startswith('N') and not col.upper().endswith('ET') and col != 'N_cph':
                            et_cand = f"{col}ET"
                            if et_cand in merged_df.columns:
                                pairs_to_use.append((col, et_cand))

            if pairs_to_use:
                if combine_cph:
                    # Sum all neutron columns into single N_cph
                    merged_df, cph_name = add_combined_neutron_cph_column(
                        merged_df, pairs_to_use)
                    if cph_name:
                        cph_added.append((cph_name, pairs_to_use))
                        formula_parts = []
                        for p in pairs_to_use:
                            n = p[0]
                            et = p[1] if len(p) > 1 else None
                            if n in merged_df.columns:
                                if _is_count_rate_col(n) or not et:
                                    formula_parts.append(f"{n}")
                                else:
                                    formula_parts.append(f"({n} / {et})")
                        log(f"  -> Added {cph_name} = ({' + '.join(formula_parts)}) * 3600")
                else:
                    for p in pairs_to_use:
                        n_col = p[0]
                        et_col = p[1] if len(p) > 1 else None
                        if n_col in merged_df.columns and (et_col is None or et_col in merged_df.columns or _is_count_rate_col(n_col)):
                            merged_df, cph_name = add_neutron_cph_column(
                                merged_df, n_col, et_col)
                            cph_added.append((cph_name, [(n_col, et_col)]))
                            if _is_count_rate_col(n_col) or not et_col:
                                log(f"  -> Added {cph_name} = {n_col} * 3600")
                            else:
                                log(f"  -> Added {cph_name} = {n_col} / {et_col} * 3600")

        # Apply column selection preserving exact selected sequence
        if selected_columns:
            keep = []
            for col in selected_columns:
                if col in merged_df.columns and col not in keep:
                    keep.append(col)
            if 'Date_Time' in merged_df.columns and 'Date_Time' not in keep:
                keep.insert(0, 'Date_Time')

            # Insert added cph columns right after their related neutron/ET columns
            for cph_name, pairs in cph_added:
                if cph_name in merged_df.columns and cph_name not in keep:
                    # Find highest index among related neutron/ET columns in keep
                    related = [c for pair in pairs for c in pair if c and c in keep]
                    if related:
                        insert_at = max(keep.index(c) for c in related) + 1
                    else:
                        insert_at = len(keep)
                    keep.insert(insert_at, cph_name)

            merged_df = merged_df[[c for c in keep if c in merged_df.columns]]

        # Write CSV
        fname = output_filename or f"merged_{val}.csv"
        if not fname.endswith('.csv'):
            fname += '.csv'
        out_file = output_dir / fname
        merged_df.to_csv(out_file, index=False)

        dt_col_final = 'Date_Time'
        if not merged_df.empty and dt_col_final in merged_df.columns:
            log(f"  -> Created {out_file.name}  ({len(merged_df)} rows, "
                f"{merged_df[dt_col_final].min()} to {merged_df[dt_col_final].max()})")

        # Build master YAML from most recent YAML in timeframe
        recent_base = _find_most_recent_yaml_in_timeframe(
            cfgs, start_date=start_date, end_date=end_date)
        final_columns = list(merged_df.columns)
        master_yaml = _build_custom_master_yaml(
            recent_base, final_columns, fname, configs=cfgs)

        configs_dir = output_dir / "sensor_configs"
        configs_dir.mkdir(parents=True, exist_ok=True)
        yaml_stem = Path(fname).stem
        dest = configs_dir / f"{yaml_stem}_master.yaml"
        try:
            with open(dest, 'w', encoding='utf-8') as f:
                yaml.dump(master_yaml, f, default_flow_style=False,
                          allow_unicode=True, sort_keys=False)
            log(f"  -> Wrote master config: {dest.name}")
        except Exception as e:
            log(f"  -> Failed to write master config: {e}")

        results[val] = True

    success = sum(1 for v in results.values() if v)
    log(f"\nDone — {success}/{len(selected_values)} merged successfully.\n")
    return results
