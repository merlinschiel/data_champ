"""
Plots and Reports Generation for CRNS Harmonization Pipeline
============================================================
Handles automatic visualization and report generation:
  1. Full-timeseries Neptoon PNG figures.
  2. Neptoon PDF Report.
  3. Calibration diagnostic plots.
"""

import os
import sys
import shutil
import logging
from pathlib import Path
from typing import Union, Optional, List, Dict

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

logger = logging.getLogger("pipeline_plots")


def generate_neptoon_figures(
    df: pd.DataFrame,
    sensor_name: str,
    output_dir: Union[str, Path],
    sensor_info_dict: Optional[Dict] = None,
    create_pdf_report: bool = True,
) -> List[Path]:
    """Generate and save full-timeseries Neptoon figures and PDF report using native Neptoon CRNSDataHub.

    Parameters
    ----------
    df : pd.DataFrame
        Processed concatenated timeseries DataFrame with datetime index.
    sensor_name : str
        Sensor identifier/name (e.g. '01').
    output_dir : str or Path
        Target directory to save figures (e.g. data/.processed/figures/01).
    sensor_info_dict : dict, optional
        Metadata dict for SensorInfo (lat, lon, elevation, N0, etc.).
    create_pdf_report : bool
        Whether to generate Report-<sensor_name>.pdf.

    Returns
    -------
    List[Path]
        Paths to generated PNG and PDF files.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    generated_files = []

    if df is None or df.empty:
        logger.warning(f"[{sensor_name}] Cannot generate figures: DataFrame is empty.")
        return generated_files

    # Ensure DatetimeIndex
    if not isinstance(df.index, pd.DatetimeIndex):
        for col in ['DateTime', 'DateTime_utc', 'date_time', 'time']:
            if col in df.columns:
                df = df.set_index(pd.to_datetime(df[col], utc=True))
                break

    from magazine import Magazine
    from neptoon.hub.crns_data_hub import CRNSDataHub
    from neptoon.config.configuration_input import SensorInfo

    Magazine.active = True
    Magazine.clean()

    # Extract sensor metadata — prefer sensor_info_dict (from YAML config), fallback to DataFrame columns
    lat = 0.0
    lon = 0.0
    elev = 0.0
    n0_val = 1000.0

    if sensor_info_dict:
        lat = float(sensor_info_dict.get('latitude', lat))
        lon = float(sensor_info_dict.get('longitude', lon))
        elev = float(sensor_info_dict.get('elevation', elev))
        n0_val = float(sensor_info_dict.get('N0', n0_val))

    # Override from DataFrame if available
    if 'latitude' in df.columns and not df['latitude'].dropna().empty:
        lat = float(df['latitude'].dropna().iloc[0])
    if 'longitude' in df.columns and not df['longitude'].dropna().empty:
        lon = float(df['longitude'].dropna().iloc[0])
    if 'elevation' in df.columns and not df['elevation'].dropna().empty:
        elev = float(df['elevation'].dropna().iloc[0])
    if 'N0' in df.columns and not df['N0'].dropna().empty:
        n0_val = float(df['N0'].dropna().iloc[0])

    s_info = SensorInfo(
        name=str(sensor_info_dict.get('name', sensor_name) if sensor_info_dict else sensor_name),
        identifier=str(sensor_info_dict.get('identifier', sensor_name) if sensor_info_dict else sensor_name),
        country=str(sensor_info_dict.get('country', '') if sensor_info_dict else ''),
        install_date=str(sensor_info_dict.get('install_date', '2019-01-01') if sensor_info_dict else '2019-01-01'),
        time_zone=int(sensor_info_dict.get('time_zone', 0) if sensor_info_dict else 0),
        latitude=lat,
        longitude=lon,
        elevation=elev,
        N0=n0_val,
    )

    hub = CRNSDataHub(
        crns_data_frame=df,
        sensor_info=s_info,
    )

    if 'soil_moisture_volumetric' not in df.columns or df['soil_moisture_volumetric'].isna().all():
        hub.produce_soil_moisture_estimates()

    hub.create_figures(create_all=True)
    hub.flags_data_frame = pd.DataFrame(index=df.index)

    # Save via Neptoon SaveAndArchiveOutputs
    nep_sub = output_dir / f"{sensor_name}_"
    if nep_sub.exists():
        shutil.rmtree(nep_sub, ignore_errors=True)
    hub.save_data(save_folder_location=output_dir, append_timestamp=False)

    # Move only figures (PNG) and report (PDF) to output_dir root
    for item in output_dir.rglob('*'):
        if item.is_file() and item.parent != output_dir:
            if item.suffix.lower() in ['.png', '.pdf']:
                dest = output_dir / item.name
                if not dest.exists() or dest.stat().st_size != item.stat().st_size:
                    shutil.copy2(item, dest)
                generated_files.append(dest)
        elif item.is_file() and item.suffix.lower() in ['.png', '.pdf']:
            generated_files.append(item)

    # Clean up subdirectories created by neptoon saver
    for sub in list(output_dir.glob(f"{sensor_name}*")):
        if sub.is_dir():
            shutil.rmtree(sub, ignore_errors=True)

    generated_files = list(set(generated_files))
    logger.info(f"[{sensor_name}] Generated {len(generated_files)} Neptoon figures & reports in {output_dir}")
    return generated_files


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
    site_props = report_dict.get('site_properties', {})
    bd = site_props.get('dry_soil_bulk_density_g_cm3', 0.0)
    soc = site_props.get('soil_organic_carbon_g_g', 0.0)
    lw = site_props.get('lattice_water_g_g', 0.0)

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
    for i, c_day in enumerate(calib_days):
        color = colors[i % len(colors)]
        c_str = str(c_day.date())
        win_start = c_day - pd.Timedelta(days=7)
        win_end = c_day + pd.Timedelta(days=7)
        sub = ts_df.loc[(ts_df.index >= win_start) & (ts_df.index <= win_end)]

        if not sub.empty and 'soil_moisture_volumetric' in sub.columns:
            ax2.plot(sub.index, sub['soil_moisture_volumetric'] * 100, color=color, lw=1.4,
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
