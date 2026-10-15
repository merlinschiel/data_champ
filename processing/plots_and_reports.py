"""
Plots and Reports Generation for CRNS Harmonization Pipeline
============================================================
Handles automatic visualization and report generation:
  1. Full-timeseries Neptoon PNG figures (neutrons, corrections, soil moisture + uncertainty, meteo, NMDB).
  2. Neptoon PDF Report using Magazine/Publish.
  3. Calibration diagnostic plots (CRNS vs in-situ weighted fit, sample depth/radial profiles).

Usage (regenerate figures from data/.processed/processed_<sensor>.csv):
  python processing/plots_and_reports.py --sensor 01
"""

import argparse
import shutil
import logging
from datetime import date, datetime
from pathlib import Path
from typing import Union, Optional, List, Dict

import pandas as pd
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

logger = logging.getLogger("pipeline_plots")

# neptoon >= 1.2 column names (older outputs used the long names)
SM_VOL_COLUMNS = ("smv", "soil_moisture_volumetric")
SM_GRAV_COLUMNS = ("smg", "soil_moisture_gravimetric")


def _first_column(df: pd.DataFrame, candidates) -> Optional[str]:
    return next((c for c in candidates if c in df.columns), None)


def _sensor_info(sensor_info_dict: Optional[Dict], df: pd.DataFrame, sensor_name: str):
    """Build a neptoon SensorInfo from YAML sensor_info, preferring values neptoon wrote into df."""
    from neptoon.config.models.common import SensorInfo

    data = {k: v for k, v in (sensor_info_dict or {}).items()
            if k in SensorInfo.model_fields and v is not None}
    if isinstance(data.get("install_date"), date) and not isinstance(data["install_date"], datetime):
        data["install_date"] = datetime.combine(data["install_date"], datetime.min.time())
    data["name"] = str(data.get("name", sensor_name))
    for col in ("latitude", "longitude", "elevation", "N0"):
        if col in df.columns and df[col].notna().any():
            data[col] = float(df[col].dropna().iloc[-1])
    return SensorInfo(**data)


def generate_neptoon_figures(
    df: pd.DataFrame,
    sensor_name: str,
    output_dir: Union[str, Path],
    sensor_info_dict: Optional[Dict] = None,
    create_pdf_report: bool = True,
) -> List[Path]:
    """Generate full-timeseries neptoon figures (PNG) and the PDF report for a processed series.

    Parameters
    ----------
    df : pd.DataFrame
        Processed time series (neptoon output) with a DatetimeIndex.
    sensor_name : str
        Sensor name (e.g. '01').
    output_dir : str or Path
        Target directory (e.g. data/.processed/figures/01).
    sensor_info_dict : dict, optional
        sensor_info block of the sensor YAML (location, N0, ...).
    create_pdf_report : bool
        Whether to also produce neptoon's PDF report.

    Returns
    -------
    List[Path]
        Paths to generated PNG and PDF files.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    if df is None or df.empty:
        logger.warning(f"[{sensor_name}] Cannot generate figures: DataFrame is empty.")
        return []

    if not isinstance(df.index, pd.DatetimeIndex):
        for col in ['Date_Time', 'DateTime', 'DateTime_utc', 'date_time', 'time']:
            if col in df.columns:
                df = df.set_index(pd.to_datetime(df[col], utc=True)).drop(columns=[col])
                break

    from magazine import Magazine
    from neptoon.config.models.output import DataStorageConfig
    from neptoon.hub.crns_data_hub import CRNSDataHub
    from neptoon.runtime import ProcessingContext

    Magazine.clean()

    hub = CRNSDataHub(
        crns_data_frame=df,
        sensor_info=_sensor_info(sensor_info_dict, df, sensor_name),
        data_storage=DataStorageConfig(create_report=bool(create_pdf_report)),
        processing_context=ProcessingContext(report_enabled=bool(create_pdf_report)),
    )
    hub.create_figures(create_all=True)

    # neptoon writes data, flags, figures and the report into one folder; keep only figures/report
    staging_name = f"_{sensor_name}_neptoon_output"

    def _staging_dirs():
        return [p for p in output_dir.glob(f"{staging_name}*") if p.is_dir()]

    for old in _staging_dirs():
        shutil.rmtree(old, ignore_errors=True)
    hub.save_data(folder_name=staging_name, save_folder_location=output_dir, append_timestamp=False)

    generated = []
    for staging in _staging_dirs():
        for item in staging.rglob('*'):
            if item.is_file() and item.suffix.lower() in ('.png', '.pdf'):
                dest = output_dir / item.name
                shutil.copy2(item, dest)
                generated.append(dest)
        shutil.rmtree(staging, ignore_errors=True)

    logger.info(f"[{sensor_name}] Generated {len(generated)} neptoon figures & reports in {output_dir}")
    return generated


def generate_calibration_plots(
    sensor_id: str,
    ts_df: pd.DataFrame,
    results_df: pd.DataFrame,
    weights_df: Optional[pd.DataFrame],
    report_dict: Dict,
    output_dir: Union[str, Path],
) -> List[Path]:
    """Generate high-quality calibration overview and diagnostic plots.

    Parameters
    ----------
    sensor_id : str
        Sensor identifier (e.g. '01').
    ts_df : pd.DataFrame
        Corrected timeseries around calibration.
    results_df : pd.DataFrame
        Calibration campaign summary dataframe.
    weights_df : pd.DataFrame, optional
        Sample weighting dataframe with distances, depths, and weights.
    report_dict : dict
        Full calibration report dict ($N_0$, site properties, etc.).
    output_dir : str or Path
        Target directory (e.g. data/.processed/calibration_results).

    Returns
    -------
    List[Path]
        Paths to generated calibration plot files.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    generated = []

    if ts_df is None or ts_df.empty or results_df is None or results_df.empty:
        logger.warning(f"[{sensor_id}] Insufficient data for calibration plots.")
        return generated

    # Ensure DatetimeIndex for timeseries
    if not isinstance(ts_df.index, pd.DatetimeIndex):
        for col in ['DateTime', 'DateTime_utc', 'date_time', 'time']:
            if col in ts_df.columns:
                ts_df = ts_df.set_index(pd.to_datetime(ts_df[col], utc=True))
                break

    calib_days = pd.to_datetime(results_df['calibration_day']).tolist()
    opt_n0 = report_dict.get('optimal_N0', 'N/A')
    method = report_dict.get('method', 'desilets_etal_2010')
    site_props = report_dict.get('sample_averages') or {}
    bd = site_props.get('avg_dry_soil_bulk_density', 0.0)
    soc = site_props.get('avg_soil_organic_carbon', 0.0)
    lw = site_props.get('avg_lattice_water', 0.0)

    # -----------------------------------------------------------------------
    # 1. Calibration Fit Overview Plot
    # -----------------------------------------------------------------------
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(12, 7.5), dpi=150)
    plt.subplots_adjust(hspace=0.35)

    colors = ['#D55E00', '#0072B2', '#009E73', '#E69F00']

    # Subplot 1: Corrected Neutrons around calibration
    for i, c_day in enumerate(calib_days):
        color = colors[i % len(colors)]
        c_str = str(c_day.date())
        win_start = c_day - pd.Timedelta(days=7)
        win_end = c_day + pd.Timedelta(days=7)
        sub = ts_df.loc[(ts_df.index >= win_start) & (ts_df.index <= win_end)]

        if not sub.empty and 'corrected_epithermal_neutrons' in sub.columns:
            ax1.plot(sub.index, sub['corrected_epithermal_neutrons'], color=color, lw=1.3,
                     label=f'Neutrons ±7d around {c_str}')
        ax1.axvline(c_day, color=color, linestyle='--', alpha=0.8, lw=1.5)

    ax1.set_title(
        f"Sensor {sensor_id} Calibration Window — Optimal $N_0 = {opt_n0}$ cph ({method})",
        fontsize=12, fontweight='bold'
    )
    ax1.set_ylabel("Corrected Neutrons (cph)", fontsize=10)
    ax1.grid(True, alpha=0.3)
    ax1.legend(loc='upper right', frameon=True, fontsize=9)

    # Subplot 2: CRNS Soil Moisture vs In-situ Sample Average
    sm_col = _first_column(ts_df, SM_VOL_COLUMNS)
    for i, c_day in enumerate(calib_days):
        color = colors[i % len(colors)]
        c_str = str(c_day.date())
        win_start = c_day - pd.Timedelta(days=7)
        win_end = c_day + pd.Timedelta(days=7)
        sub = ts_df.loc[(ts_df.index >= win_start) & (ts_df.index <= win_end)]

        if not sub.empty and sm_col:
            ax2.plot(sub.index, sub[sm_col] * 100, color=color, lw=1.4,
                     label=f'CRNS SM ±7d ({c_str})')

        # In-situ weighted mean from results_df
        row = results_df[pd.to_datetime(results_df['calibration_day']) == c_day]
        if not row.empty and 'field_average_soil_moisture_volumetric' in row.columns:
            val_vol = float(row['field_average_soil_moisture_volumetric'].iloc[0]) * 100
            ax2.scatter([c_day], [val_vol], color=color, s=110, edgecolors='black', zorder=6,
                        label=f'In-situ Sample Weighted Avg ({val_vol:.1f}%)')
        ax2.axvline(c_day, color=color, linestyle='--', alpha=0.8, lw=1.5)

    subtitle_text = f"Site Properties: Bulk Density={bd:.3f} g/cm³, SOC={soc:.4f} g/g, Lattice Water={lw:.4f} g/g"
    ax2.set_title(f"CRNS Soil Moisture vs. Calibration Campaign Ground Truth\n{subtitle_text}",
                  fontsize=10, fontweight='medium')
    ax2.set_ylabel("Soil Moisture (vol %)", fontsize=10)
    ax2.set_xlabel("Date (UTC)", fontsize=10)
    ax2.grid(True, alpha=0.3)
    ax2.legend(loc='upper right', frameon=True, fontsize=9)

    fit_file = output_dir / f"calibration_overview_{sensor_id}.png"
    fig.savefig(fit_file, bbox_inches='tight')
    plt.close(fig)
    generated.append(fit_file)
    logger.info(f"  -> Saved calibration overview plot: {fit_file.name}")

    # -----------------------------------------------------------------------
    # 2. Sample Depth & Radial Distribution Plot (if weights_df available)
    # -----------------------------------------------------------------------
    if weights_df is not None and not weights_df.empty:
        try:
            fig, (ax_rad, ax_prof) = plt.subplots(1, 2, figsize=(13, 5), dpi=150)
            
            # Left: Distance vs Depth with sample weights
            dist = weights_df['distance'] if 'distance' in weights_df.columns else weights_df.get('Distance_to_CRNS_m', [0])
            depth = weights_df['depth'] if 'depth' in weights_df.columns else weights_df.get('Profile_Depth_cm', [0])
            sm_val = (weights_df['soil_moisture_gravimetric'] * 100) if 'soil_moisture_gravimetric' in weights_df.columns else 0

            # Normalize horizontal * vertical weights for marker sizing
            if 'horizontal_weight' in weights_df.columns and 'vertical_weights' in weights_df.columns:
                comb_weights = weights_df['horizontal_weight'] * weights_df['vertical_weights']
                norm_sizes = 30 + 400 * (comb_weights / comb_weights.max() if comb_weights.max() > 0 else 1)
            else:
                norm_sizes = 50

            scatter = ax_rad.scatter(dist, depth, c=sm_val, s=norm_sizes, cmap='Blues', edgecolors='#333333', alpha=0.85)
            cbar = plt.colorbar(scatter, ax=ax_rad)
            cbar.set_label("Sample Gravimetric SM (%)", fontsize=9)
            ax_rad.set_title("Calibration Samples: Radial Distance vs. Depth", fontsize=11, fontweight='bold')
            ax_rad.set_xlabel("Distance to CRNS (m)", fontsize=10)
            ax_rad.set_ylabel("Depth (cm)", fontsize=10)
            ax_rad.invert_yaxis()
            ax_rad.grid(True, alpha=0.3)

            # Right: Soil Moisture Depth Profiles by Profile ID
            pid_col = 'pid' if 'pid' in weights_df.columns else 'Profile_ID'
            if pid_col in weights_df.columns:
                for pid, group in weights_df.groupby(pid_col):
                    sorted_g = group.sort_values(by='depth' if 'depth' in group.columns else 'Profile_Depth_cm')
                    d_pts = sorted_g['depth'] if 'depth' in sorted_g.columns else sorted_g['Profile_Depth_cm']
                    sm_pts = (sorted_g['soil_moisture_gravimetric'] * 100) if 'soil_moisture_gravimetric' in sorted_g.columns else sorted_g.get('SoilMoisture_g_g', 0) * 100
                    ax_prof.plot(sm_pts, d_pts, marker='o', label=f'Profile {pid}', alpha=0.7, lw=1.2)

                ax_prof.set_title("Soil Moisture Depth Profiles", fontsize=11, fontweight='bold')
                ax_prof.set_xlabel("Gravimetric SM (%)", fontsize=10)
                ax_prof.set_ylabel("Depth (cm)", fontsize=10)
                ax_prof.invert_yaxis()
                ax_prof.grid(True, alpha=0.3)
                if len(weights_df[pid_col].unique()) <= 10:
                    ax_prof.legend(loc='lower right', fontsize=8)

            profile_file = output_dir / f"calibration_sample_profiles_{sensor_id}.png"
            fig.savefig(profile_file, bbox_inches='tight')
            plt.close(fig)
            generated.append(profile_file)
            logger.info(f"  -> Saved calibration profiles plot: {profile_file.name}")

        except Exception as e:
            logger.warning(f"[{sensor_id}] Could not generate sample profiles plot: {e}")

    return generated


def main():
    """Regenerate neptoon figures/PDF reports from the processed CSVs."""
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from log_utils import get_logger
    from sensor_metadata import (
        METADATA_FILE, TARGET_DATA_DIR, load_phase_config, load_sensors_from_excel,
        parse_sensor_filter, phase_windows, sensor_matches, sensor_name,
    )

    global logger
    logger = get_logger("plots_and_reports", "plots_and_reports.log")

    parser = argparse.ArgumentParser(description="Generate neptoon figures and PDF reports from processed data.")
    parser.add_argument("--sensor", "-s", type=str, nargs="+", default=None,
                        help="Sensor(s) to plot (default: all sensors with processed data)")
    args, _ = parser.parse_known_args()

    processed_dir = TARGET_DATA_DIR / ".processed"
    sensor_filter = parse_sensor_filter(args.sensor)
    failed = []
    for key, data in load_sensors_from_excel(METADATA_FILE).items():
        if not sensor_matches(sensor_filter, key, data):
            continue
        name = sensor_name(key, data)
        csv_path = processed_dir / f"processed_{name}.csv"
        if not csv_path.exists():
            if sensor_filter:
                logger.warning(f"[{name}] No processed data at {csv_path}; run neptoon_pipeline.py first.")
            continue
        df = pd.read_csv(csv_path)
        df = df.set_index(pd.to_datetime(df.pop("Date_Time"), utc=True)).drop(columns=["phase"], errors="ignore")
        windows = phase_windows(data.get("phase", []))
        latest_cfg = next((load_phase_config(w["config_file"]) for w in reversed(windows)
                           if load_phase_config(w["config_file"])), {}) or {}
        try:
            generate_neptoon_figures(df, name, processed_dir / "figures" / name,
                                     sensor_info_dict=latest_cfg.get("sensor_info", {}))
        except Exception as e:
            logger.error(f"[{name}] Figure generation failed: {e}")
            failed.append(name)
    if failed:
        raise SystemExit(f"Figure generation failed for: {failed}")


if __name__ == "__main__":
    main()
