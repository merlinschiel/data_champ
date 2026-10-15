"""
CRNS Data Hub — Central User Interface
=======================================
Streamlit-based hub for the CRNS Data Harmonization project.
Replaces the old manual_append.py tkinter UI.

Features:
  - One-click execution of all pipeline steps (sync, merge, process, calibrate, reports, dashboard)
  - Interactive data visualization with Plotly (merged & processed CSVs)
  - Column selection, date range filtering, and plot export (PNG, HTML)
  - Live log output with st.status for running tasks

Launch:
  streamlit run data_hub.py
"""

import sys
import os
import io
import re
import json
import base64
import subprocess
import traceback
from pathlib import Path
from datetime import datetime

# Path setup — data_hub.py lives at project root
PROJECT_ROOT = Path(__file__).resolve().parent
PROCESSING_DIR = PROJECT_ROOT / "processing"
if str(PROCESSING_DIR) not in sys.path:
    sys.path.insert(0, str(PROCESSING_DIR))

import streamlit as st
import pandas as pd
import plotly.graph_objects as go
import plotly.express as px


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DATA_DIR = PROJECT_ROOT / "data"
MERGED_DIR = DATA_DIR / ".merged"
PROCESSED_DIR = DATA_DIR / ".processed"
PROCESS_CONFIG_DIR = PROCESSING_DIR / "process_config"
SENSOR_CONFIG_DIR = PROCESSING_DIR / "sensor_config"
META_DIR = PROCESSING_DIR / "meta"
LOG_DIR = META_DIR / "logs"
LAST_RUN_FILE = PROJECT_ROOT / "last_run.json"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def discover_sensors():
    """Find all sensor IDs from sensor_config directories."""
    sensors = []
    if SENSOR_CONFIG_DIR.exists():
        for d in sorted(SENSOR_CONFIG_DIR.iterdir()):
            if d.is_dir() and not d.name.startswith(('.', '_')):
                sensors.append(d.name)
    return sensors


def discover_process_configs():
    """Find all process config YAML files."""
    configs = []
    if PROCESS_CONFIG_DIR.exists():
        for f in sorted(PROCESS_CONFIG_DIR.glob("*.yaml")):
            configs.append(f)
    return configs


def discover_csv_files(directory):
    """Find all CSV files in a directory."""
    csvs = {}
    if directory.exists():
        for f in sorted(directory.glob("*.csv")):
            csvs[f.stem] = f
    return csvs


@st.cache_data(ttl=300)
def load_csv_cached(path_str):
    """Load CSV with datetime auto-detection."""
    path = Path(path_str)
    try:
        df = pd.read_csv(path)
        for col in df.columns:
            if 'date' in col.lower() or 'time' in col.lower() or 'datetime' in col.lower():
                try:
                    df[col] = pd.to_datetime(df[col], errors='coerce')
                except Exception:
                    pass
        return df
    except Exception as e:
        return None


def load_last_run():
    """Load last pipeline run summary."""
    if LAST_RUN_FILE.exists():
        try:
            with open(LAST_RUN_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            pass
    return None


def read_log_file(name):
    """Read a log file from logs directory."""
    log_path = LOG_DIR / name
    if log_path.exists():
        try:
            return log_path.read_text(encoding='utf-8', errors='replace')
        except Exception:
            return "Could not read log file."
    return "Log file not found."


def get_embedded_dashboard_html():
    """Load dashboard/performance.html and embed images as base64 data URIs."""
    perf_html_path = PROJECT_ROOT / "dashboard" / "performance.html"
    if not perf_html_path.exists():
        return None

    html_content = perf_html_path.read_text(encoding="utf-8", errors="replace")
    images_dir = PROJECT_ROOT / "dashboard" / "images"

    def replace_img(match):
        img_filename = match.group(1)
        img_path = images_dir / img_filename
        if img_path.exists():
            try:
                img_b64 = base64.b64encode(img_path.read_bytes()).decode("utf-8")
                return f"data:image/png;base64,{img_b64}"
            except Exception:
                pass
        return match.group(0)

    # Replace relative image references
    html_content = re.sub(r'images/([a-zA-Z0-9_\-\.]+\.png)', replace_img, html_content)
    return html_content


def get_sensor_map_html():
    """Load processing/meta/sensor_map.html or auto-generate if missing."""
    map_html_path = META_DIR / "sensor_map.html"
    if not map_html_path.exists():
        try:
            import generate_map
            generate_map.generate_map()
        except Exception:
            pass
    if map_html_path.exists():
        try:
            return map_html_path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            pass
    return None


def get_numeric_columns(df):
    """Get numeric columns from dataframe."""
    return [c for c in df.columns if pd.api.types.is_numeric_dtype(df[c])]


def get_datetime_columns(df):
    """Get datetime columns from dataframe."""
    return [c for c in df.columns if pd.api.types.is_datetime64_any_dtype(df[c])]


def run_step_with_status(label, cmd_args, status_container):
    """Run a pipeline command synchronously inside an st.status block.

    Uses unbuffered Python execution to stream terminal outputs live in real time.
    """
    python_exe = sys.executable
    full_cmd = [python_exe, "-u"] + [str(a) for a in cmd_args]
    cmd_display = " ".join(str(a) for a in full_cmd)

    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"

    start_time = datetime.now()

    with status_container.status(f"Running: {label}...", expanded=True) as status:
        st.markdown(f"**Command:** `{cmd_display}`")
        log_area = st.empty()
        output_lines = []

        try:
            proc = subprocess.Popen(
                full_cmd,
                cwd=str(PROJECT_ROOT),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding='utf-8',
                errors='replace',
                env=env,
                bufsize=1,
            )

            # Stream stdout line by line immediately
            for line in iter(proc.stdout.readline, ''):
                output_lines.append(line)
                display_lines = output_lines[-500:] if len(output_lines) > 500 else output_lines
                log_area.code("".join(display_lines), language="bash")

            proc.wait()
            ret_code = proc.returncode
            duration = (datetime.now() - start_time).total_seconds()
            full_text = "".join(output_lines)

            # Persist in session state for review
            st.session_state["last_run_output"] = {
                "label": label,
                "cmd": cmd_display,
                "exit_code": ret_code,
                "duration": duration,
                "output": full_text,
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }

            if ret_code == 0:
                status.update(
                    label=f"{label} — completed successfully ({duration:.1f}s)",
                    state="complete",
                    expanded=True,
                )
                st.success(f"**{label}** finished with exit code 0 ({duration:.1f}s).")
            else:
                status.update(
                    label=f"{label} — failed (exit code {ret_code}) after {duration:.1f}s",
                    state="error",
                    expanded=True,
                )
                st.error(
                    f"**{label}** failed with exit code {ret_code}.\n\n"
                    f"Check the terminal output above for traceback and error details."
                )

            return ret_code

        except FileNotFoundError as e:
            status.update(label=f"{label} — script not found", state="error")
            st.error(
                f"Could not find script to execute.\n\n"
                f"**Command:** `{cmd_display}`\n\n"
                f"**Error:** {e}"
            )
            return -1

        except PermissionError as e:
            status.update(label=f"{label} — permission denied", state="error")
            st.error(
                f"Permission denied when trying to run script.\n\n"
                f"**Command:** `{cmd_display}`\n\n"
                f"**Error:** {e}"
            )
            return -1

        except Exception as e:
            status.update(label=f"{label} — execution error", state="error")
            st.error(
                f"Unexpected error running {label}.\n\n"
                f"**Error type:** `{type(e).__name__}`\n\n"
                f"**Details:** {e}\n\n"
                f"```\n{traceback.format_exc()}\n```"
            )
            return -1


def render_last_run_console():
    """Display terminal output from the most recent run."""
    if st.session_state.get("last_run_output"):
        run = st.session_state["last_run_output"]
        icon = ":material/check_circle:" if run["exit_code"] == 0 else ":material/error:"
        tag = "Success" if run["exit_code"] == 0 else f"Failed (exit {run['exit_code']})"
        with st.expander(
            f"Terminal console: {run['label']} — {tag} ({run['duration']:.1f}s) at {run['timestamp']}",
            expanded=True,
            icon=icon,
        ):
            st.caption(f"**Command:** `{run['cmd']}`")
            st.code(run["output"] if run["output"] else "(no output)", language="bash")
            if st.button("Clear console output", key="btn_clear_console", icon=":material/delete:"):
                st.session_state["last_run_output"] = None
                st.rerun()


# ---------------------------------------------------------------------------
# Page config
# ---------------------------------------------------------------------------

st.set_page_config(
    page_title="CRNS Data Hub",
    page_icon=":material/satellite_alt:",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ---------------------------------------------------------------------------
# Session state defaults & navigation routing
# ---------------------------------------------------------------------------

PAGES = ["Dashboard", "Pipeline", "Data explorer", "Logs"]
st.session_state.setdefault("last_task_label", None)
st.session_state.setdefault("last_task_exit", None)
st.session_state.setdefault("last_run_output", None)
st.session_state.setdefault("current_page", "Dashboard")

if "_jump_to_page" in st.session_state and st.session_state["_jump_to_page"]:
    st.session_state["current_page"] = st.session_state.pop("_jump_to_page")


# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------

with st.sidebar:
    st.header("CRNS Data Hub")
    st.caption("Central interface for CRNS data processing")
    st.divider()

    nav_index = PAGES.index(st.session_state["current_page"]) if st.session_state["current_page"] in PAGES else 0
    page = st.radio(
        "Navigation",
        PAGES,
        index=nav_index,
        label_visibility="collapsed",
        key="nav_page_radio",
    )
    st.session_state["current_page"] = page

    st.divider()

    # Quick stats
    sensors = discover_sensors()
    merged_csvs = discover_csv_files(MERGED_DIR)
    processed_csvs = discover_csv_files(PROCESSED_DIR)

    st.markdown("**Quick stats**")
    st.metric("Sensors configured", len(sensors), border=True)
    st.metric("Merged datasets", len(merged_csvs), border=True)
    st.metric("Processed datasets", len(processed_csvs), border=True)

    st.divider()
    st.caption(f"Project: `{PROJECT_ROOT.name}`")


# ===========================================================================
# PAGE: Dashboard
# ===========================================================================

if page == "Dashboard":
    st.title("CRNS Data Hub")
    st.markdown("Central command center for the CRNS Data Harmonization pipeline.")

    # --- KPI metrics row ---
    last_run = load_last_run()

    with st.container(horizontal=True):
        st.metric("Sensors", len(sensors), border=True)
        st.metric("Merged files", len(merged_csvs), border=True)
        st.metric("Processed files", len(processed_csvs), border=True)

        if last_run:
            status = last_run.get("overall_status", "UNKNOWN")
            st.metric("Last run status", status, border=True)
        else:
            st.metric("Last run status", "No runs yet", border=True)

    # --- Last run details ---
    if last_run:
        with st.expander("Last pipeline run details", expanded=False, icon=":material/history:"):
            ts = last_run.get("timestamp", "")
            try:
                ts_fmt = datetime.fromisoformat(ts).strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                ts_fmt = ts
            st.caption(f"Ran at: **{ts_fmt}**")

            results = last_run.get("results", [])
            if results:
                run_df = pd.DataFrame(results)
                st.dataframe(
                    run_df,
                    hide_index=True,
                    column_config={
                        "step": st.column_config.TextColumn("Step", width="medium"),
                        "status": st.column_config.TextColumn("Status", width="small"),
                        "duration_s": st.column_config.NumberColumn(
                            "Duration (s)", format="%.1f"
                        ),
                        "error": st.column_config.TextColumn("Error", width="large"),
                    },
                )

    st.divider()

    # --- Sensor Performance & Station Network Map ---
    dash_tab_perf, dash_tab_map = st.tabs(
        ["Sensor performance overview", "Sensor network map"]
    )

    with dash_tab_perf:
        dash_col1, dash_col2 = st.columns([3, 1])
        with dash_col1:
            st.subheader("Sensor performance overview")
        with dash_col2:
            if st.button(
                "Refresh dashboard",
                key="btn_refresh_dash",
                icon=":material/refresh:",
                help="Re-generate HTML dashboard via pipeline",
            ):
                st.session_state["_trigger_action"] = "run_dashboard"
                st.session_state["_jump_to_page"] = "Pipeline"
                st.rerun()

        embedded_html = get_embedded_dashboard_html()
        if embedded_html:
            st.iframe(embedded_html, height=720)
        else:
            st.info("Performance dashboard has not been generated yet.")
            if st.button(
                "Generate Dashboard Now",
                key="btn_gen_dash",
                type="primary",
                icon=":material/dashboard:",
            ):
                st.session_state["_trigger_action"] = "run_dashboard"
                st.session_state["_jump_to_page"] = "Pipeline"
                st.rerun()

    with dash_tab_map:
        map_col1, map_col2, map_col3 = st.columns([3, 1, 1])
        with map_col1:
            st.subheader("Sensor network map")
        with map_col2:
            if st.button(
                "Regenerate map",
                key="btn_refresh_map",
                icon=":material/refresh:",
                help="Re-generate HTML map from location metadata",
            ):
                st.session_state["_trigger_action"] = "run_map"
                st.session_state["_jump_to_page"] = "Pipeline"
                st.rerun()
        with map_col3:
            map_html = get_sensor_map_html()
            if map_html:
                st.download_button(
                    "Download map HTML",
                    data=map_html.encode("utf-8"),
                    file_name="sensor_map.html",
                    mime="text/html",
                    icon=":material/download:",
                )

        map_html = get_sensor_map_html()
        if map_html:
            st.iframe(map_html, height=650)

            # Location & metadata table
            loc_metadata_file = META_DIR / "location_metadata.xlsx"
            if loc_metadata_file.exists():
                try:
                    loc_df = pd.read_excel(loc_metadata_file, sheet_name="Locations")
                    loc_df.columns = loc_df.columns.str.strip()
                    with st.expander(
                        "Location & sensor deployment metadata",
                        expanded=False,
                        icon=":material/table_chart:",
                    ):
                        st.dataframe(loc_df, hide_index=True)
                except Exception:
                    pass
        else:
            st.info("Sensor map has not been generated yet.")
            if st.button(
                "Generate Sensor Map Now",
                key="btn_gen_map",
                type="primary",
                icon=":material/map:",
            ):
                st.session_state["_trigger_action"] = "run_map"
                st.session_state["_jump_to_page"] = "Pipeline"
                st.rerun()

    # Show terminal output if previous run exists
    render_last_run_console()


# ===========================================================================
# PAGE: Pipeline
# ===========================================================================

elif page == "Pipeline":
    st.title("Pipeline runner")
    st.markdown("Run pipeline steps individually or as a full sequence with live terminal output.")

    # Container for live streaming terminal console
    console_slot = st.container()

    # --- Configuration ---
    st.subheader("Configuration")

    cfg_col1, cfg_col2 = st.columns(2)

    with cfg_col1:
        sensor_options = ["All sensors"] + sensors
        selected_sensors = st.multiselect(
            "Sensors to process",
            sensor_options,
            default=["All sensors"],
            help="Select specific sensors or 'All sensors' for the full pipeline",
        )

    with cfg_col2:
        proc_configs = discover_process_configs()
        config_names = [f.stem for f in proc_configs]
        selected_config = st.selectbox(
            "Process config",
            config_names if config_names else ["(none found)"],
            help="Neptoon process configuration YAML to use",
        )

    with st.container(horizontal=True):
        enable_field = st.checkbox("Enable field/SD card sync", value=False)
        force_rerun = st.checkbox("Force re-sync / re-merge (bypass caches)", value=False)
        by_phase = st.checkbox("Process phase-by-phase", value=False)

    st.divider()

    # --- Build args ---
    sensor_args = []
    if "All sensors" not in selected_sensors and selected_sensors:
        sensor_args = ["--sensor"] + selected_sensors

    config_path = ""
    if selected_config and selected_config != "(none found)":
        config_path = str(PROCESS_CONFIG_DIR / f"{selected_config}.yaml")

    # --- Check for quick action trigger ---
    triggered = st.session_state.pop("_trigger_action", None)
    if triggered == "sync_merge":
        cmd = [str(PROJECT_ROOT / "run_pipeline.py")]
        if sensor_args:
            cmd += sensor_args
        if force_rerun:
            cmd += ["--force"]
        if enable_field:
            cmd += ["--field"]
        run_step_with_status("Full pipeline (sync + merge + dashboard)", cmd, console_slot)
    elif triggered == "full_process":
        cmd = [str(PROJECT_ROOT / "run_pipeline.py"), "--process"]
        if sensor_args:
            cmd += sensor_args
        if config_path:
            cmd += ["--process-config", config_path]
        if force_rerun:
            cmd += ["--force"]
        if enable_field:
            cmd += ["--field"]
        if by_phase:
            cmd += ["--by-phase"]
        run_step_with_status("Full pipeline (+ Neptoon processing)", cmd, console_slot)
    elif triggered == "run_dashboard":
        cmd = [str(PROCESSING_DIR / "recent_performance.py")]
        run_step_with_status("Performance dashboard", cmd, console_slot)
    elif triggered == "run_map":
        cmd = [str(PROCESSING_DIR / "generate_map.py")]
        run_step_with_status("Sensor map generation", cmd, console_slot)

    # --- Individual step runners ---
    st.subheader("Run individual steps")

    step_cols = st.columns(3)

    with step_cols[0]:
        st.markdown("**Data sync**")
        st.caption("Sync raw data from FTP/SD sources")
        if st.button("Run sync", key="run_sync",
                      icon=":material/cloud_download:"):
            cmd = [str(PROCESSING_DIR / "copydata.py")]
            if sensor_args:
                cmd += sensor_args
            if force_rerun:
                cmd += ["--force"]
            if enable_field:
                cmd += ["--field"]
            run_step_with_status("Data sync", cmd, console_slot)

    with step_cols[1]:
        st.markdown("**Data merge**")
        st.caption("Parse raw files into merged CSVs")
        if st.button("Run merge", key="run_merge",
                      icon=":material/merge:"):
            cmd = [str(PROCESSING_DIR / "auto_append.py")]
            if sensor_args:
                cmd += ["--sensor"] + [
                    s for s in selected_sensors if s != "All sensors"
                ]
            if force_rerun:
                cmd += ["--force"]
            run_step_with_status("Data merge", cmd, console_slot)

        # --- Custom merging (group-merge with advanced options) ---
        with st.expander("Custom merging", expanded=False,
                          icon=":material/tune:"):
            st.caption("Merge data by sensor_info property with column selection, timeframe, and N(cph)")

            @st.cache_data(ttl=120)
            def _scan_data_yamls():
                """Scan YAMLs in data dir, return string properties & grouped values."""
                from merge_utils import scan_yaml_files
                yaml_configs = scan_yaml_files(DATA_DIR)
                string_props = {}
                for cfg in yaml_configs.values():
                    info = cfg.get('sensor_info', {})
                    for key, val in info.items():
                        if isinstance(val, bool):
                            string_props.setdefault(key, set()).add(str(val))
                        elif isinstance(val, str) and val.strip():
                            string_props.setdefault(key, set()).add(val)
                return {k: sorted(v) for k, v in string_props.items()}

            prop_map = _scan_data_yamls()

            if not prop_map:
                st.warning("No YAML configs with string sensor_info properties found in data folder.")
            else:
                # --- Step 1: Group selection ---
                st.markdown("##### 1. Group selection")
                prop_names = sorted(prop_map.keys())
                default_idx = prop_names.index("location") if "location" in prop_names else 0
                group_prop = st.selectbox(
                    "Group by property",
                    prop_names,
                    index=default_idx,
                    key="cm_group_prop",
                    help="sensor_info property to group sensors on",
                )

                available_values = prop_map.get(group_prop, [])
                selected_values = st.multiselect(
                    f"Select '{group_prop}' values to merge",
                    available_values,
                    default=available_values,
                    key="cm_selected_values",
                )

                # --- Step 2: Timeframe ---
                st.markdown("##### 2. Timeframe")
                from datetime import time as dt_time
                cm_tf_from, cm_tf_to = st.columns(2)
                with cm_tf_from:
                    st.caption("**From**")
                    cm_start_date = st.date_input(
                        "Date##start",
                        value=None,
                        key="cm_start_date",
                        help="Leave empty for no start filter",
                        label_visibility="collapsed",
                    )
                    cm_start_time = st.time_input(
                        "Time##start",
                        value=dt_time(0, 0, 0),
                        key="cm_start_time",
                        step=60,
                        label_visibility="collapsed",
                    )
                with cm_tf_to:
                    st.caption("**To**")
                    cm_end_date = st.date_input(
                        "Date##end",
                        value=None,
                        key="cm_end_date",
                        help="Leave empty for no end filter",
                        label_visibility="collapsed",
                    )
                    cm_end_time = st.time_input(
                        "Time##end",
                        value=dt_time(23, 59, 59),
                        key="cm_end_time",
                        step=60,
                        label_visibility="collapsed",
                    )

                # --- Step 3: Output settings ---
                st.markdown("##### 3. Output settings")
                cm_out_col1, cm_out_col2 = st.columns(2)
                with cm_out_col1:
                    cm_output_dir = st.text_input(
                        "Output directory",
                        value=str(MERGED_DIR),
                        key="cm_output_dir",
                        help="Absolute path to output directory",
                    )
                with cm_out_col2:
                    cm_output_filename = st.text_input(
                        "Output filename",
                        value="",
                        key="cm_output_filename",
                        placeholder="e.g. my_merge.csv (empty = auto)",
                        help="Leave empty for auto-naming (merged_<value>.csv)",
                    )

                # --- Step 4: Column selection ---
                st.markdown("##### 4. Column selection")

                if selected_values:
                    @st.cache_data(ttl=120)
                    def _scan_columns(group_by, values_tuple):
                        from merge_utils import scan_available_columns
                        cols, n_cols, et_cols, _ = scan_available_columns(
                            DATA_DIR, group_by, list(values_tuple))
                        return cols, n_cols, et_cols

                    all_cols, neutron_cols, et_cols = _scan_columns(
                        group_prop, tuple(selected_values))

                    # Always include Date_Time in display
                    display_cols = ['Date_Time'] + [c for c in all_cols if c != 'Date_Time']

                    cm_selected_cols = st.multiselect(
                        "Select columns to include",
                        display_cols,
                        default=display_cols,
                        key="cm_selected_cols",
                        help="Only these columns will be in the final CSV and master YAML",
                    )

                    # --- Step 5: N(cph) option ---
                    st.markdown("##### 5. Neutron counts per hour")

                    cm_add_cph = st.checkbox(
                        "Add N(cph) column",
                        value=False,
                        key="cm_add_cph",
                    )

                    cph_pairs = []
                    if cm_add_cph:
                        # Auto-detect pairs: N1→N1ET, N2→N2ET etc. or Count_rate (cps * 3600)
                        auto_pairs = []
                        for nc in neutron_cols:
                            if 'count_rate' in nc.lower() or 'countrate' in nc.lower():
                                auto_pairs.append((nc, None))
                            else:
                                candidates = [et for et in et_cols if et.startswith(nc)]
                                if candidates:
                                    auto_pairs.append((nc, candidates[0]))

                        if auto_pairs:
                            pairs_str = ', '.join(f"{n}→{et}" if et else n for n, et in auto_pairs)
                            st.info(f"Auto-detected pairs: {pairs_str}")
                            use_auto = st.checkbox(
                                "Use auto-detected pairs",
                                value=True,
                                key="cm_use_auto_cph",
                            )
                            if use_auto:
                                cph_pairs = auto_pairs

                        if not cph_pairs:
                            # Manual pair selection
                            st.caption("Select neutron count and integration time columns manually:")
                            available_numeric = [c for c in all_cols if c not in ('Date_Time',)]
                            cph_n_col1, cph_n_col2 = st.columns(2)
                            with cph_n_col1:
                                cph_n = st.selectbox(
                                    "Neutron count column",
                                    available_numeric,
                                    index=available_numeric.index(neutron_cols[0]) if neutron_cols and neutron_cols[0] in available_numeric else 0,
                                    key="cm_cph_n",
                                )
                            with cph_n_col2:
                                cph_et = st.selectbox(
                                    "Integration time column",
                                    available_numeric,
                                    index=available_numeric.index(et_cols[0]) if et_cols and et_cols[0] in available_numeric else 0,
                                    key="cm_cph_et",
                                )
                            if cph_n and cph_et:
                                cph_pairs = [(cph_n, cph_et)]

                    # --- Run button ---
                    st.divider()
                    if st.button("Run custom merge", key="run_custom_merge",
                                  icon=":material/merge_type:", type="primary"):
                        if not selected_values:
                            st.warning("Select at least one value.")
                        elif not cm_selected_cols:
                            st.warning("Select at least one column.")
                        else:
                            from merge_utils import custom_merge
                            log_lines = []
                            def _log(msg):
                                log_lines.append(msg)

                            with st.status(f"Custom merge by '{group_prop}' ...", expanded=True) as status:
                                log_area = st.empty()
                                try:
                                    out_dir = cm_output_dir.strip() or str(MERGED_DIR)
                                    out_fname = cm_output_filename.strip() or None

                                    # Combine date + time into datetime strings
                                    cm_start_dt = None
                                    cm_end_dt = None
                                    if cm_start_date:
                                        cm_start_dt = f"{cm_start_date} {cm_start_time.strftime('%H:%M:%S')}"
                                    if cm_end_date:
                                        cm_end_dt = f"{cm_end_date} {cm_end_time.strftime('%H:%M:%S')}"

                                    results = custom_merge(
                                        data_dir=DATA_DIR,
                                        group_by=group_prop,
                                        selected_values=selected_values,
                                        selected_columns=cm_selected_cols,
                                        start_date=cm_start_dt,
                                        end_date=cm_end_dt,
                                        output_dir=out_dir,
                                        output_filename=out_fname,
                                        add_cph=cm_add_cph,
                                        cph_pairs=cph_pairs if cph_pairs else None,
                                        combine_cph=True,
                                        log_fn=_log,
                                    )
                                    log_area.code("\n".join(log_lines), language="bash")
                                    ok = sum(1 for v in results.values() if v)
                                    status.update(
                                        label=f"Custom merged {ok}/{len(selected_values)} groups",
                                        state="complete",
                                    )
                                    st.success(f"Custom merged {ok}/{len(selected_values)} groups.")
                                except Exception as exc:
                                    log_area.code("\n".join(log_lines), language="bash")
                                    status.update(
                                        label="Custom merge failed",
                                        state="error",
                                    )
                                    st.error(f"Error: {exc}")
                else:
                    st.info("Select at least one group value to scan columns.")

    with step_cols[2]:
        st.markdown("**Neptoon process**")
        st.caption("QA/QC + corrections + soil moisture")
        if st.button("Run process", key="run_process",
                      icon=":material/science:"):
            cmd = [str(PROCESSING_DIR / "neptoon_pipeline.py")]
            if sensor_args:
                cmd += sensor_args
            if config_path:
                cmd += ["--process-config", config_path]
            if by_phase:
                cmd += ["--by-phase"]
            run_step_with_status("Neptoon process", cmd, console_slot)

    step_cols2 = st.columns(4)

    with step_cols2[0]:
        st.markdown("**Calibration**")
        st.caption("Calculate N0 from field samples")
        if st.button("Run calibrate", key="run_calibrate",
                      icon=":material/target:"):
            cmd = [str(PROCESSING_DIR / "calibrate_sensor.py")]
            if sensor_args:
                cmd += sensor_args
            cmd += ["--update-yaml"]
            run_step_with_status("Calibration", cmd, console_slot)

    with step_cols2[1]:
        st.markdown("**Reports and plots**")
        st.caption("Generate figures + PDF reports")
        if st.button("Run reports", key="run_reports",
                      icon=":material/assessment:"):
            cmd = [str(PROCESSING_DIR / "plots_and_reports.py")]
            if sensor_args:
                cmd += sensor_args
            run_step_with_status("Reports and plots", cmd, console_slot)

    with step_cols2[2]:
        st.markdown("**Performance dashboard**")
        st.caption("Generate monitoring HTML")
        if st.button("Run dashboard", key="run_dashboard",
                      icon=":material/dashboard:"):
            cmd = [str(PROCESSING_DIR / "recent_performance.py")]
            run_step_with_status("Performance dashboard", cmd, console_slot)

    with step_cols2[3]:
        st.markdown("**Sensor map**")
        st.caption("Generate network map HTML")
        if st.button("Run map", key="run_map",
                      icon=":material/map:"):
            cmd = [str(PROCESSING_DIR / "generate_map.py")]
            run_step_with_status("Sensor map generation", cmd, console_slot)

    st.divider()

    # --- Full Pipeline ---
    st.subheader("Run full pipeline")

    with st.container(horizontal=True):
        if st.button("Sync + merge + dashboard", key="run_full_basic",
                      type="secondary", icon=":material/play_arrow:"):
            cmd = [str(PROJECT_ROOT / "run_pipeline.py")]
            if sensor_args:
                cmd += sensor_args
            if force_rerun:
                cmd += ["--force"]
            if enable_field:
                cmd += ["--field"]
            run_step_with_status(
                "Full pipeline (sync + merge + dashboard)", cmd, console_slot
            )

        if st.button("Full pipeline (+ process)", key="run_full_process",
                      type="primary", icon=":material/rocket_launch:"):
            cmd = [str(PROJECT_ROOT / "run_pipeline.py"), "--process"]
            if sensor_args:
                cmd += sensor_args
            if config_path:
                cmd += ["--process-config", config_path]
            if force_rerun:
                cmd += ["--force"]
            if enable_field:
                cmd += ["--field"]
            if by_phase:
                cmd += ["--by-phase"]
            run_step_with_status(
                "Full pipeline (+ Neptoon processing)", cmd, console_slot
            )

    # Show persistent terminal console below if output exists
    render_last_run_console()


# ===========================================================================
# PAGE: Data Explorer
# ===========================================================================

elif page == "Data explorer":
    st.title("Data explorer")
    st.markdown("Browse, visualize, and export CRNS datasets.")

    # --- Dataset selection ---
    sel_col1, sel_col2 = st.columns(2)

    with sel_col1:
        data_source = st.segmented_control(
            "Data source",
            ["Merged (raw)", "Processed"],
            default="Merged (raw)",
        )

    source_dir = MERGED_DIR if data_source == "Merged (raw)" else PROCESSED_DIR
    csv_files = discover_csv_files(source_dir)

    if not csv_files:
        st.warning(f"No CSV files in `{source_dir.relative_to(PROJECT_ROOT)}`.")
        st.info("Run pipeline first to generate data.")
        st.stop()

    with sel_col2:
        selected_file = st.selectbox(
            "Select dataset",
            list(csv_files.keys()),
        )

    file_path = csv_files[selected_file]

    # --- Load data ---
    with st.spinner(f"Loading {file_path.name}..."):
        df = load_csv_cached(str(file_path))

    if df is None:
        st.error(
            f"Failed to load `{file_path.name}`.\n\n"
            f"The file may be corrupted or have an unsupported format. "
            f"Check that it is a valid CSV file."
        )
        st.stop()

    # --- Dataset overview ---
    st.subheader(file_path.name)

    with st.container(horizontal=True):
        st.metric("Rows", f"{len(df):,}", border=True)
        st.metric("Columns", len(df.columns), border=True)

        file_size = file_path.stat().st_size
        if file_size > 1_000_000:
            st.metric("File size", f"{file_size / 1_000_000:.1f} MB", border=True)
        else:
            st.metric("File size", f"{file_size / 1_000:.1f} KB", border=True)

        dt_cols = get_datetime_columns(df)
        if dt_cols:
            date_range_vals = df[dt_cols[0]].dropna()
            if not date_range_vals.empty:
                st.metric(
                    "Date range",
                    f"{date_range_vals.min().strftime('%Y-%m-%d')} to "
                    f"{date_range_vals.max().strftime('%Y-%m-%d')}",
                    border=True,
                )

    # --- Data preview ---
    with st.expander("Data preview", expanded=False, icon=":material/search:"):
        preview_rows = st.slider("Preview rows", 5, 100, 20, key="preview_slider")
        st.dataframe(df.head(preview_rows), hide_index=True)

    # --- Column statistics ---
    with st.expander("Column statistics", expanded=False,
                      icon=":material/functions:"):
        num_cols = get_numeric_columns(df)
        if num_cols:
            st.dataframe(df[num_cols].describe().T)
        else:
            st.info("No numeric columns.")

    st.divider()

    # -------------------------------------------------------------------
    # Plotting
    # -------------------------------------------------------------------

    st.subheader("Interactive plots")

    numeric_cols = get_numeric_columns(df)
    datetime_cols = get_datetime_columns(df)

    if not numeric_cols:
        st.warning("No numeric columns available for plotting.")
        st.stop()

    # Plot config
    plot_cfg_col1, plot_cfg_col2 = st.columns([2, 1])

    with plot_cfg_col1:
        x_axis_options = datetime_cols + numeric_cols + ["(index)"]
        x_axis = st.selectbox(
            "X-axis",
            x_axis_options,
            index=0 if datetime_cols else len(x_axis_options) - 1,
            help="Datetime column for time series, numeric column, or row index",
        )

    with plot_cfg_col2:
        plot_type = st.selectbox(
            "Plot type",
            ["Line", "Scatter", "Bar", "Area", "Histogram", "Box"],
        )

    y_columns = st.multiselect(
        "Y-axis columns (select one or more)",
        numeric_cols,
        default=[numeric_cols[0]] if numeric_cols else [],
        help="Columns to plot on Y-axis",
    )

    # Advanced options
    with st.expander("Plot options", expanded=False, icon=":material/tune:"):
        adv_col1, adv_col2, adv_col3 = st.columns(3)

        with adv_col1:
            plot_title = st.text_input(
                "Plot title", value=f"{selected_file} - {data_source}"
            )
            plot_height = st.slider("Plot height (px)", 300, 900, 550)

        with adv_col2:
            use_secondary_y = st.checkbox(
                "Secondary Y-axis for 2nd+ column", value=False
            )
            show_legend = st.checkbox("Show legend", value=True)

        with adv_col3:
            line_mode = st.selectbox(
                "Line mode",
                ["lines", "lines+markers", "markers"],
                index=0,
            )
            opacity = st.slider("Opacity", 0.1, 1.0, 0.85)

        # Date range filter
        date_filter_col = None
        date_range_filter = None
        if datetime_cols:
            date_filter_col = st.selectbox(
                "Filter by date column", datetime_cols, key="date_filter_col"
            )
            date_series = df[date_filter_col].dropna()
            if not date_series.empty:
                min_date = date_series.min().to_pydatetime()
                max_date = date_series.max().to_pydatetime()
                date_range_filter = st.date_input(
                    "Date range filter",
                    value=(min_date.date(), max_date.date()),
                    min_value=min_date.date(),
                    max_value=max_date.date(),
                    key="date_range_input",
                )

    if not y_columns:
        st.info("Select at least one Y-axis column to plot.")
        st.stop()

    # --- Apply date filter ---
    plot_df = df.copy()
    if date_filter_col and date_range_filter and len(date_range_filter) == 2:
        col_tz = getattr(plot_df[date_filter_col].dt, "tz", None)
        start_dt = pd.Timestamp(date_range_filter[0], tz=col_tz)
        end_dt = pd.Timestamp(date_range_filter[1], tz=col_tz) + pd.Timedelta(days=1)
        mask = (plot_df[date_filter_col] >= start_dt) & (
            plot_df[date_filter_col] < end_dt
        )
        plot_df = plot_df[mask]

    # --- Build x values ---
    if x_axis == "(index)":
        x_values = plot_df.index
        x_label = "Index"
    else:
        x_values = plot_df[x_axis]
        x_label = x_axis

    # --- Build figure ---
    from plotly.subplots import make_subplots

    if use_secondary_y and len(y_columns) > 1:
        fig = make_subplots(specs=[[{"secondary_y": True}]])
    else:
        fig = go.Figure()

    colors = px.colors.qualitative.Set2 + px.colors.qualitative.Dark2

    for i, col in enumerate(y_columns):
        color = colors[i % len(colors)]
        secondary = use_secondary_y and i > 0

        if plot_type == "Line":
            trace = go.Scatter(
                x=x_values,
                y=plot_df[col],
                name=col,
                mode=line_mode,
                opacity=opacity,
                line=dict(color=color, width=1.5),
            )
        elif plot_type == "Scatter":
            trace = go.Scatter(
                x=x_values,
                y=plot_df[col],
                name=col,
                mode="markers",
                opacity=opacity,
                marker=dict(color=color, size=4),
            )
        elif plot_type == "Bar":
            trace = go.Bar(
                x=x_values,
                y=plot_df[col],
                name=col,
                opacity=opacity,
                marker_color=color,
            )
        elif plot_type == "Area":
            trace = go.Scatter(
                x=x_values,
                y=plot_df[col],
                name=col,
                fill="tozeroy",
                opacity=opacity,
                line=dict(color=color, width=1),
            )
        elif plot_type == "Histogram":
            trace = go.Histogram(
                x=plot_df[col],
                name=col,
                opacity=opacity,
                marker_color=color,
            )
        elif plot_type == "Box":
            trace = go.Box(
                y=plot_df[col],
                name=col,
                marker_color=color,
            )
        else:
            trace = go.Scatter(
                x=x_values,
                y=plot_df[col],
                name=col,
                mode="lines",
                opacity=opacity,
                line=dict(color=color),
            )

        if use_secondary_y and len(y_columns) > 1:
            fig.add_trace(trace, secondary_y=secondary)
        else:
            fig.add_trace(trace)

    # Layout
    fig.update_layout(
        title=dict(text=plot_title, font=dict(size=16)),
        height=plot_height,
        template="plotly_dark",
        legend=dict(
            orientation="h",
            yanchor="bottom",
            y=1.02,
            xanchor="right",
            x=1,
        )
        if show_legend
        else dict(visible=False),
        margin=dict(l=60, r=30, t=60, b=50),
        hovermode="x unified",
    )

    if plot_type not in ("Histogram", "Box"):
        fig.update_xaxes(title_text=x_label)

    if use_secondary_y and len(y_columns) > 1:
        fig.update_yaxes(title_text=y_columns[0], secondary_y=False)
        if len(y_columns) > 1:
            fig.update_yaxes(title_text=y_columns[1], secondary_y=True)
    elif len(y_columns) == 1:
        fig.update_yaxes(title_text=y_columns[0])

    # Render with interactive box/lasso selection enabled
    chart_selection = st.plotly_chart(
        fig,
        key="main_plot",
        on_select="rerun",
        selection_mode=["box", "lasso", "points"],
    )
    st.caption("*Tip: Use the **Box Select** or **Lasso Select** tool in the chart toolbar above to select a custom timeframe/subset for CSV export.*")

    # --- Check for chart interactive selection ---
    selected_points = []
    if chart_selection and hasattr(chart_selection, "selection") and chart_selection.selection:
        selected_points = chart_selection.selection.get("points", [])

    selected_df = None
    if selected_points:
        pt_indices = sorted(list(set(
            p["point_index"] for p in selected_points if "point_index" in p and p["point_index"] < len(plot_df)
        )))
        if pt_indices:
            selected_df = plot_df.iloc[pt_indices]

    # --- Export ---
    st.subheader("Export")

    if selected_df is not None and not selected_df.empty:
        if x_axis != "(index)":
            x_min_val = selected_df[x_axis].min()
            x_max_val = selected_df[x_axis].max()
            if hasattr(x_min_val, "strftime"):
                range_str = f"{x_min_val.strftime('%Y-%m-%d %H:%M')} → {x_max_val.strftime('%Y-%m-%d %H:%M')}"
            else:
                range_str = f"{x_min_val} → {x_max_val}"
        else:
            range_str = f"Index {selected_df.index.min()} → {selected_df.index.max()}"

        st.info(
            f"**Plot selection active:** {len(selected_df):,} rows selected ({range_str})"
        )

    with st.container(horizontal=True):
        html_buf = io.StringIO()
        fig.write_html(html_buf, include_plotlyjs="cdn")
        html_bytes = html_buf.getvalue().encode("utf-8")
        st.download_button(
            "Download plot (HTML)",
            data=html_bytes,
            file_name=f"{selected_file}_plot.html",
            mime="text/html",
            icon=":material/code:",
        )

        try:
            png_bytes = fig.to_image(
                format="png", width=1600, height=plot_height, scale=2
            )
            st.download_button(
                "Download plot (PNG)",
                data=png_bytes,
                file_name=f"{selected_file}_plot.png",
                mime="image/png",
                icon=":material/image:",
            )
        except Exception:
            st.button(
                "PNG export needs kaleido",
                disabled=True,
                help="Install kaleido: pip install kaleido",
            )

        if selected_df is not None and not selected_df.empty:
            sel_csv_export = selected_df.to_csv(index=False).encode("utf-8")
            st.download_button(
                f"Download selection ({len(selected_df):,} rows CSV)",
                data=sel_csv_export,
                file_name=f"{selected_file}_selection.csv",
                mime="text/csv",
                type="primary",
                icon=":material/highlight:",
            )

        csv_export = plot_df.to_csv(index=False).encode("utf-8")
        st.download_button(
            "Download filtered data (CSV)",
            data=csv_export,
            file_name=f"{selected_file}_filtered.csv",
            mime="text/csv",
            icon=":material/table_chart:",
        )

    # --- Correlation matrix ---
    if len(y_columns) > 1:
        with st.expander("Column correlation matrix", expanded=False,
                          icon=":material/grid_on:"):
            corr_df = plot_df[y_columns].corr()
            corr_fig = px.imshow(
                corr_df,
                text_auto=".2f",
                color_continuous_scale="RdBu_r",
                zmin=-1,
                zmax=1,
                title="Correlation matrix",
                template="plotly_dark",
            )
            corr_fig.update_layout(height=400)
            st.plotly_chart(corr_fig, key="corr_plot")


# ===========================================================================
# PAGE: Logs
# ===========================================================================

elif page == "Logs":
    st.title("Pipeline logs")
    st.markdown("View log files from previous pipeline runs.")

    log_files = []
    if LOG_DIR.exists():
        log_files = sorted(
            LOG_DIR.glob("*.log"),
            key=lambda f: f.stat().st_mtime,
            reverse=True,
        )

    if not log_files:
        st.info("No log files found.")
        st.stop()

    selected_log = st.selectbox(
        "Select log file",
        [f.name for f in log_files],
    )

    if selected_log:
        log_content = read_log_file(selected_log)
        log_path = LOG_DIR / selected_log

        with st.container(horizontal=True):
            st.caption(f"File: `{log_path.relative_to(PROJECT_ROOT)}`")
            try:
                mod_time = datetime.fromtimestamp(log_path.stat().st_mtime)
                st.caption(
                    f"Last modified: **{mod_time.strftime('%Y-%m-%d %H:%M:%S')}**"
                )
            except Exception:
                pass

        # Search filter
        search_term = st.text_input(
            "Filter log lines",
            key="log_search",
            label_visibility="visible",
            placeholder="Type to filter...",
        )
        if search_term:
            filtered = "\n".join(
                line
                for line in log_content.split("\n")
                if search_term.lower() in line.lower()
            )
            st.code(
                filtered if filtered else "No matching lines.", language="text"
            )
        else:
            st.code(log_content, language="text")

        st.download_button(
            "Download log",
            data=log_content.encode("utf-8"),
            file_name=selected_log,
            mime="text/plain",
            icon=":material/download:",
        )
