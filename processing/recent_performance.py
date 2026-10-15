import os
import pandas as pd
import matplotlib.pyplot as plt
import matplotlib.gridspec as gridspec
from datetime import datetime, timedelta, timezone
import warnings
import yaml
from pathlib import Path

from sensor_metadata import METADATA_FILE, load_sensors_from_excel, sensor_name

warnings.filterwarnings("ignore")

# Configuration
DAYS_TO_SEARCH = 7
# Backup, selects number of rows
OBS_PERIOD = 504
PROCESSING_DIR = Path(__file__).resolve().parent
BASE_MERGED_DIR = PROCESSING_DIR.parent / "data" / ".merged"
SENSOR_CONFIG_DIR = BASE_MERGED_DIR / "sensor_configs"
BASE_OUTPUT_DIR = PROCESSING_DIR.parent


CB_BLUE = '#0072B2'      # Running/Active
CB_ORANGE = '#E69F00'    # Warning
CB_VERMILLION = '#D55E00' # Stale/No Data


def _utc_now():
    """Current time as naive UTC, matching the (UTC) timestamps of the merged data."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class SensorAnalyzer:
    def __init__(self, name, csv_path, yaml_config, location='Unknown'):
        self.name = name
        self.csv_path = csv_path
        self.yaml_config = yaml_config
        self.location = location
        self.df = pd.DataFrame()
        self.raw_df = pd.DataFrame()

    def load_data(self):
        if not os.path.exists(self.csv_path):
            print(f"[{self.name}] SKIPPED: Data file not found ({self.csv_path}).")
            return False

        try:
            temp_df = pd.read_csv(self.csv_path, sep=',', low_memory=False)
            if temp_df.empty:
                print(f"[{self.name}] SKIPPED: Data file is empty.")
                return False
            
            # Get the most recent rows
            if 'Date_Time' in temp_df.columns:
                temp_dt = pd.to_datetime(temp_df['Date_Time'], errors='coerce')
                if temp_dt.dt.tz is not None:
                    temp_dt = temp_dt.dt.tz_localize(None)
                
                cutoff_date = _utc_now() - timedelta(days=DAYS_TO_SEARCH)
                recent_df = temp_df[temp_dt >= cutoff_date]
                
                if not recent_df.empty:
                    self.raw_df = recent_df.copy()
                else:
                    self.raw_df = temp_df.tail(OBS_PERIOD).copy()
            else:
                self.raw_df = temp_df.tail(OBS_PERIOD).copy()
                
            return True
        except Exception as e:
            print(f"[{self.name}] Error reading file: {e}")
            return False

    def process_data(self):
        if self.raw_df.empty:
            return

        clean_df = pd.DataFrame()
        
        # Datetime
        if 'Date_Time' in self.raw_df.columns:
            clean_df['datetime'] = pd.to_datetime(self.raw_df['Date_Time'], errors='coerce')
        else:
            print(f"[{self.name}] Date_Time column not found.")
            return

        # Extract config keys
        key_info = self.yaml_config.get('time_series_data', {}).get('key_column_info', {})
        
        # Epithermal
        epithermal_cols = key_info.get('epithermal_neutron_columns', [])
        if epithermal_cols and isinstance(epithermal_cols, list):
            if len(epithermal_cols) > 0 and epithermal_cols[0] in self.raw_df.columns:
                clean_df['epithermal'] = pd.to_numeric(self.raw_df[epithermal_cols[0]], errors='coerce')
            if len(epithermal_cols) > 1 and epithermal_cols[1] in self.raw_df.columns:
                clean_df['epithermal2'] = pd.to_numeric(self.raw_df[epithermal_cols[1]], errors='coerce')

        # Pressure
        pressure_cols = key_info.get('pressure_columns', [])
        if pressure_cols and isinstance(pressure_cols, list) and len(pressure_cols) > 0 and pressure_cols[0] in self.raw_df.columns:
            clean_df['pressure'] = pd.to_numeric(self.raw_df[pressure_cols[0]], errors='coerce')

        # Temperature
        temp_cols = key_info.get('temperature_columns', [])
        if temp_cols and isinstance(temp_cols, list) and len(temp_cols) > 0 and temp_cols[0] in self.raw_df.columns:
            clean_df['temp'] = pd.to_numeric(self.raw_df[temp_cols[0]], errors='coerce')

        # Relative Humidity
        rh_cols = key_info.get('relative_humidity_columns', [])
        if rh_cols and isinstance(rh_cols, list) and len(rh_cols) > 0 and rh_cols[0] in self.raw_df.columns:
            clean_df['rh'] = pd.to_numeric(self.raw_df[rh_cols[0]], errors='coerce')

        # Battery
        if 'Battery_v' in self.raw_df.columns:
            clean_df['battery'] = pd.to_numeric(self.raw_df['Battery_v'], errors='coerce')

        # PR2.6 or PR2.4 columns
        pr2_names = ["PR2.6_10cm", "PR2.6_20cm", "PR2.6_30cm", "PR2.6_40cm", "PR2.6_60cm", "PR2.6_100cm"]
        if not any(col in self.raw_df.columns for col in pr2_names):
            pr2_names = ["PR2.4_10cm", "PR2.4_20cm", "PR2.4_30cm", "PR2.4_40cm"]
        for i, col in enumerate(pr2_names, start=1):
            if col in self.raw_df.columns:
                clean_df[f'pr{i}'] = pd.to_numeric(self.raw_df[col], errors='coerce')

        # SMT columns
        for i in range(1, 10):
            col = f"SMT{i}_permittivity"
            if col in self.raw_df.columns:
                clean_df[f'sm{i}'] = pd.to_numeric(self.raw_df[col], errors='coerce')

        self.df = clean_df.dropna(subset=['datetime']).sort_values(by='datetime')

    def check_staleness(self):
        if self.df.empty: 
            return {"status": "NO DATA", "is_stale": True}
            
        last_time = self.df['datetime'].iloc[-1]
        if last_time.tzinfo is not None:
            last_time = last_time.tz_localize(None)
            
        # Check if data is older than 48 hours (172800 seconds)
        is_stale = (_utc_now() - last_time).total_seconds() > 172800
        
        status_text = f"STALE (Last: {last_time})" if is_stale else f"RUNNING (Last: {last_time})"
        
        return {"status": status_text, "is_stale": is_stale}

    def check_data_quality(self):
        """
        Check data quality of the loaded dataframe.
        Flags:
        - battery: outside [12V, 15V]
        - counts (epithermal, epithermal2):
          outliers based on z-score (deviation from mean > 3 * std)
          or if count <= 0
        """
        if self.df.empty:
            return {
                "battery_faults": 0,
                "battery_total": 0,
                "counts_faults": 0,
                "counts_total": 0,
                "battery_flagged": False,
                "counts_flagged": False
            }

        # Battery check
        battery_faults = 0
        battery_total = 0
        battery_flagged = False
        if 'battery' in self.df.columns:
            bat_series = self.df['battery'].dropna()
            battery_total = len(bat_series)
            if battery_total > 0:
                self.df['battery_fault'] = (self.df['battery'] < 12.0) | (self.df['battery'] > 15.0)
                battery_faults = self.df['battery_fault'].sum()
                battery_flagged = battery_faults > 0
            else:
                self.df['battery_fault'] = False
        else:
            self.df['battery_fault'] = False

        # Counts check (deviation from mean & threshold on faulty count)
        counts_faults = 0
        counts_total = 0
        counts_flagged = False
        
        self.df['counts_fault'] = False
        
        # Read parameters from config if exists, else defaults
        qa_config = self.yaml_config.get('input_data_qa', {})
        counts_qa = qa_config.get('neutron_counts', {})
        z_threshold = counts_qa.get('z_threshold', 3.0)
        min_faulty_points = counts_qa.get('min_faulty_points', 5)
        max_faulty_percent = counts_qa.get('max_faulty_percent', 0.05) # 5%
        
        for col in ['epithermal', 'epithermal2']:
            if col in self.df.columns:
                series = self.df[col].dropna()
                counts_total += len(series)
                if len(series) > 0:
                    mean_val = series.mean()
                    std_val = series.std()
                    if std_val > 0.01:
                        is_outlier = ((self.df[col] - mean_val).abs() > z_threshold * std_val) | (self.df[col] <= 0)
                    else:
                        is_outlier = (self.df[col] <= 0)
                    
                    self.df['counts_fault'] = self.df['counts_fault'] | is_outlier
        
        counts_faults = self.df['counts_fault'].sum()
        
        # Flag if counts faults exceed threshold
        if counts_total > 0:
            percent_faulty = counts_faults / counts_total
            if counts_faults >= min_faulty_points or percent_faulty > max_faulty_percent:
                counts_flagged = True

        return {
            "battery_faults": int(battery_faults),
            "battery_total": int(battery_total),
            "counts_faults": int(counts_faults),
            "counts_total": int(counts_total),
            "battery_flagged": battery_flagged,
            "counts_flagged": counts_flagged
        }

    def generate_report(self):
        if self.df.empty:
            print(f"[{self.name}] SKIPPED REPORT: Dataframe empty.")
            return None

        if not os.path.exists(BASE_OUTPUT_DIR):
            os.makedirs(BASE_OUTPUT_DIR)

        dash_img_dir = os.path.join(BASE_OUTPUT_DIR, "dashboard", "images")
        if not os.path.exists(dash_img_dir):
            os.makedirs(dash_img_dir)

        safe_name = str(self.name).replace(' ', '_').replace('/', '-')
        
        # PNG filenames for HTML
        img_plot_name = f"{safe_name}_plot.png"
        img_table_name = f"{safe_name}_table.png"
        img_plot_path = os.path.join(dash_img_dir, img_plot_name)
        img_table_path = os.path.join(dash_img_dir, img_table_name)

        status_info = self.check_staleness()
        qa_info = self.check_data_quality()

        status_text = status_info['status']
        is_stale = status_info['is_stale']
        is_warning = False
        warning_reasons = []

        if not is_stale:
            if qa_info['battery_flagged']:
                is_warning = True
                warning_reasons.append("Battery")
            if qa_info['counts_flagged']:
                is_warning = True
                warning_reasons.append("Counts")
            
            if is_warning:
                last_time = self.df['datetime'].iloc[-1]
                if last_time.tzinfo is not None:
                    last_time = last_time.tz_localize(None)
                status_text = f"WARNING ({', '.join(warning_reasons)}) (Last: {last_time.strftime('%Y-%m-%d %H:%M')})"

        # Dashboard Plot
        fig = plt.figure(figsize=(11.69, 10)) 
        gs = gridspec.GridSpec(6, 1, height_ratios=[0.1, 1, 1, 1, 1, 1.5])

        # Header
        ax_title = fig.add_subplot(gs[0])
        ax_title.axis('off')
        
        if is_stale:
            title_color = CB_VERMILLION
        elif is_warning:
            title_color = CB_ORANGE
        else:
            title_color = CB_BLUE
            
        ax_title.text(0.5, 0.5, f"Sensor: {self.name} (Location: {self.location})\nStatus: {status_text}", 
                    ha='center', va='center', fontsize=14, weight='bold', color=title_color)

        x = self.df['datetime']

        # Plot 1: Battery
        ax1 = fig.add_subplot(gs[1])
        if 'battery' in self.df.columns and self.df['battery'].notna().any():
            y = self.df['battery']
            ax1.plot(x, y, label='Battery (V)', color='blue')
            ax1.set_ylabel('V')
            if len(y) > 0:
                ax1.annotate(f"{y.iloc[-1]:.2f}V", xy=(x.iloc[-1], y.iloc[-1]), 
                            xytext=(5, 5), textcoords='offset points', color='red')
            ax1.legend(loc='upper left')
        else:
            ax1.text(0.5, 0.5, "No Battery Data Found", ha='center', va='center', alpha=0.5, transform=ax1.transAxes)
        ax1.grid(True, alpha=0.3)

        # Plot 2: Counts
        ax2 = fig.add_subplot(gs[2], sharex=ax1)
        if 'epithermal' in self.df.columns and self.df['epithermal'].notna().any():
            ax2.plot(x, self.df['epithermal'], label='Counts', color='orange')
            
            qa_config = self.yaml_config.get('input_data_qa', {})
            counts_qa = qa_config.get('neutron_counts', {})
            z_threshold = counts_qa.get('z_threshold', 3.0)
            
            # Highlight epithermal outliers
            epith_series = self.df['epithermal'].dropna()
            if len(epith_series) > 0:
                mean_val = epith_series.mean()
                std_val = epith_series.std()
                if std_val > 0.01:
                    epi_faults = ((self.df['epithermal'] - mean_val).abs() > z_threshold * std_val) | (self.df['epithermal'] <= 0)
                else:
                    epi_faults = (self.df['epithermal'] <= 0)
                if epi_faults.any():
                    ax2.scatter(x[epi_faults], self.df['epithermal'][epi_faults], 
                                color=CB_VERMILLION, marker='o', facecolors='none', edgecolors=CB_VERMILLION, s=45, label='Outliers (>3σ or <=0)', zorder=5)
            
            if 'epithermal2' in self.df.columns and self.df['epithermal2'].notna().any():
                ax2.plot(x, self.df['epithermal2'], label='Counts 2', color='darkorange')
                
                epith2_series = self.df['epithermal2'].dropna()
                if len(epith2_series) > 0:
                    mean_val2 = epith2_series.mean()
                    std_val2 = epith2_series.std()
                    if std_val2 > 0.01:
                        epi2_faults = ((self.df['epithermal2'] - mean_val2).abs() > z_threshold * std_val2) | (self.df['epithermal2'] <= 0)
                    else:
                        epi2_faults = (self.df['epithermal2'] <= 0)
                    if epi2_faults.any():
                        ax2.scatter(x[epi2_faults], self.df['epithermal2'][epi2_faults], 
                                    color=CB_VERMILLION, marker='s', facecolors='none', edgecolors=CB_VERMILLION, s=45, label='Counts 2 Outliers', zorder=5)
            
            ax2.set_ylabel('Counts')
            ax2.legend(loc='upper left', fontsize='small')
        else:
            ax2.text(0.5, 0.5, "No Neutron Count Data Found", ha='center', va='center', alpha=0.5, transform=ax2.transAxes)
        ax2.grid(True, alpha=0.3)

        # Plot 3: Pressure
        ax3 = fig.add_subplot(gs[3], sharex=ax1)
        if 'pressure' in self.df.columns and self.df['pressure'].notna().any():
            ax3.plot(x, self.df['pressure'], label='Pressure', color='purple')
            ax3.set_ylabel('hPa')
            ax3.legend(loc='upper left')
        else:
            ax3.text(0.5, 0.5, "No Pressure Data Found", ha='center', va='center', alpha=0.5, transform=ax3.transAxes)
        ax3.grid(True, alpha=0.3)
        plt.setp(ax3.get_xticklabels(), visible=False)

        # Plot 4: Temp/RH
        ax4 = fig.add_subplot(gs[4], sharex=ax1)
        lines, labels = [], []
        if 'temp' in self.df.columns and self.df['temp'].notna().any():
            l1, = ax4.plot(x, self.df['temp'], label='Temp', color='red')
            lines.append(l1); labels.append('Temp')
            ax4.set_ylabel('°C')
        
        if 'rh' in self.df.columns and self.df['rh'].notna().any():
            ax4b = ax4.twinx()
            l2, = ax4b.plot(x, self.df['rh'], label='RH', color='cyan', linestyle='--')
            lines.append(l2); labels.append('RH')
            ax4b.set_ylabel('%')
        
        if lines: 
            ax4.legend(lines, labels, loc='upper left')
        else:
            ax4.text(0.5, 0.5, "No Temp/RH Data Found", ha='center', va='center', alpha=0.5, transform=ax4.transAxes)
        ax4.grid(True, alpha=0.3)

        # Plot 5: Permittivity
        ax5 = fig.add_subplot(gs[5], sharex=ax1)
        
        has_soil_data = False
        colors = ['#1f77b4', '#ff7f0e', '#2ca02c', '#d62728', '#9467bd', '#8c564b', '#e377c2', '#7f7f7f', '#bcbd22', '#17becf']
        color_idx = 0
        
        # Plot SMT columns
        for i in range(1, 10):
            col = f'sm{i}'
            if col in self.df.columns and self.df[col].notna().any():
                ax5.plot(x, self.df[col], label=f'SMT {i}', color=colors[color_idx % len(colors)], linewidth=1.5)
                has_soil_data = True
                color_idx += 1
                
        # Plot PR columns
        for i in range(1, 7):
            col = f'pr{i}'
            if col in self.df.columns and self.df[col].notna().any():
                ax5.plot(x, self.df[col], label=f'PR2-{i}', color=colors[color_idx % len(colors)], linewidth=1.5)
                has_soil_data = True
                color_idx += 1
        
        ax5.set_ylabel('Permittivity')
        if has_soil_data:
            ax5.legend(loc='upper center', bbox_to_anchor=(0.5, 1.15), ncol=4, fontsize='small', frameon=False)
        else:
            ax5.text(0.5, 0.5, "No Permittivity Data Found", ha='center', va='center', alpha=0.5, transform=ax5.transAxes)
        
        ax5.grid(True, alpha=0.3)

        fig.autofmt_xdate()
        plt.tight_layout()
        fig.savefig(img_plot_path, dpi=100)
        plt.close()

        # Table Plot
        fig_tbl = plt.figure(figsize=(11.69, 8.27))
        ax_tbl = fig_tbl.add_subplot(111); ax_tbl.axis('off')
        
        # Display the relevant columns extracted
        tbl_data = self.df.tail(20).copy()
        if not tbl_data.empty:
            tbl_data = tbl_data.drop(columns=['battery_fault', 'counts_fault'], errors='ignore')
            tbl_data['datetime'] = tbl_data['datetime'].dt.strftime('%Y-%m-%d %H:%M')
            num_cols = tbl_data.select_dtypes(include=['float']).columns
            tbl_data[num_cols] = tbl_data[num_cols].round(2)
            
            table = plt.table(cellText=tbl_data.values, colLabels=tbl_data.columns, 
                             loc='center', cellLoc='center')
            table.auto_set_font_size(False); table.set_fontsize(7); table.scale(1, 1.2)
            ax_tbl.set_title(f"Recent Data: {self.name} (Location: {self.location})", fontsize=12, weight='bold')
        
        fig_tbl.savefig(img_table_path, dpi=100, bbox_inches='tight')
        plt.close()

        print(f"[{self.name}] Report generated")
        return {
            "name": self.name,
            "location": self.location,
            "status": status_text,
            "is_stale": status_info['is_stale'],
            "is_warning": is_warning,
            "plot_img": f"images/{img_plot_name}",
            "table_img": f"images/{img_table_name}"
        }

def generate_dashboard_html(sensor_results):
    dash_dir = os.path.join(BASE_OUTPUT_DIR, "dashboard")
    if not os.path.exists(dash_dir):
        os.makedirs(dash_dir)
        
    html_path = os.path.join(dash_dir, "performance.html")
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M')
    
    # Generate HTML Cards
    grid_items = ""
    for s in sensor_results:
        is_stale = s['is_stale']
        is_warning = s.get('is_warning', False)
        
        if is_stale:
            status_class = "stale"
            icon_color = CB_VERMILLION
        elif is_warning:
            status_class = "warning"
            icon_color = CB_ORANGE
        else:
            status_class = "active"
            icon_color = CB_BLUE
        
        item = f"""
        <div class="sensor-card {status_class}" onclick="openModal('{s['name']}', '{s['location']}', '{s['status']}', '{s['plot_img']}', '{s['table_img']}')">
            <div class="icon" style="background-color: {icon_color};"></div>
            <h3>{s['name']}</h3>
            <p class="location-text"><strong>Location:</strong> {s['location']}</p>
            <p class="status-text">{s['status']}</p>
        </div>
        """
        grid_items += item

    # HTML Template
    html_content = f"""
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Sensor Dashboard</title>
        <style>
            body {{ font-family: sans-serif; background: #f4f6f9; padding: 20px; }}
            .legend-container {{ display: flex; flex-wrap: wrap; gap: 20px; margin: 15px 0 20px 0; background: white; padding: 12px 20px; border-radius: 8px; box-shadow: 0 2px 5px rgba(0,0,0,0.05); font-size: 0.9em; }}
            .legend-item {{ display: flex; align-items: center; gap: 8px; color: #444; }}
            .legend-badge {{ width: 14px; height: 14px; border-radius: 50%; display: inline-block; }}
            .grid-container {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(250px, 1fr)); gap: 20px; }}
            .sensor-card {{ background: white; padding: 20px; border-radius: 8px; cursor: pointer; text-align: center; border-top: 5px solid #ccc; box-shadow: 0 2px 5px rgba(0,0,0,0.1); }}
            .sensor-card:hover {{ transform: translateY(-3px); box-shadow: 0 5px 15px rgba(0,0,0,0.2); }}
            .sensor-card.active {{ border-top-color: {CB_BLUE}; }}
            .sensor-card.warning {{ border-top-color: {CB_ORANGE}; }}
            .sensor-card.stale {{ border-top-color: {CB_VERMILLION}; }}
            .icon {{ width: 20px; height: 20px; border-radius: 50%; margin: 0 auto 10px auto; }}
            .location-text {{ font-size: 0.9em; color: #555; margin: 4px 0 10px 0; }}
            .status-text {{ font-size: 0.85em; color: #666; margin: 0; }}
            
            /* Modal Styles */
            .modal {{ display: none; position: fixed; z-index: 1000; left: 0; top: 0; width: 100%; height: 100%; background-color: rgba(0,0,0,0.8); }}
            .modal-content {{ background-color: #fff; margin: 5% auto; padding: 20px; width: 90%; max-width: 1200px; border-radius: 8px; overflow-y: auto; max-height: 90vh; }}
            .modal img {{ width: 100%; margin-bottom: 20px; border: 1px solid #ddd; }}
            .close {{ float: right; font-size: 28px; cursor: pointer; }}
        </style>
    </head>
    <body>
        <h1>Sensor Dashboard <small style="font-size:0.5em; color:#666">Updated: {timestamp}</small></h1>
        <div class="legend-container">
            <div class="legend-item"><span class="legend-badge" style="background-color: {CB_BLUE};"></span> <strong>Active / Running</strong> (Data up-to-date & normal)</div>
            <div class="legend-item"><span class="legend-badge" style="background-color: {CB_ORANGE};"></span> <strong>Warning</strong> (Battery or Counts issue)</div>
            <div class="legend-item"><span class="legend-badge" style="background-color: {CB_VERMILLION};"></span> <strong>Stale / No Data</strong> (No update > 48h)</div>
        </div>
        <div class="grid-container">{grid_items}</div>

        <div id="myModal" class="modal">
            <div class="modal-content">
                <span class="close" onclick="closeModal()">&times;</span>
                <h2 id="modalTitle">Sensor</h2>
                <hr>
                <img id="modalPlot" src="">
                <img id="modalTable" src="">
            </div>
        </div>

        <script>
            function openModal(name, location, status, plot, table) {{
                document.getElementById('modalTitle').innerText = name + " - " + location + " (" + status + ")";
                document.getElementById('modalPlot').src = plot;
                document.getElementById('modalTable').src = table;
                document.getElementById('myModal').style.display = "block";
            }}
            function closeModal() {{
                document.getElementById('myModal').style.display = 'none';
            }}
            window.onclick = function(e) {{ if (e.target == document.getElementById('myModal')) closeModal(); }}
            document.onkeydown = function(evt) {{
                evt = evt || window.event;
                if (evt.keyCode == 27) closeModal();
            }};
        </script>
    </body>
    </html>
    """
    
    with open(html_path, "w", encoding='utf-8') as f:
        f.write(html_content)
    print(f"--- Dashboard generated: {html_path} ---")

def main():
    """Build the dashboard for all active sensors in sensor_metadata.xlsx."""
    print("--- Starting Sensor Performance Analysis ---")
    dashboard_data = []

    sensors = load_sensors_from_excel(METADATA_FILE)
    for sensor_key, sensor_data in sensors.items():
        if not sensor_data.get('active', False):
            print(f"[{sensor_key}] SKIPPED: Inactive sensor.")
            continue
        name = sensor_name(sensor_key, sensor_data)
        yaml_file = SENSOR_CONFIG_DIR / f"sensor_{name}_master.yaml"
        if not yaml_file.exists():
            print(f"[{name}] SKIPPED: no master config ({yaml_file.name}); run the merge first.")
            continue
        try:
            with open(yaml_file, 'r', encoding='utf-8') as f:
                config = yaml.safe_load(f) or {}
            sensor_location = str(config.get('sensor_info', {}).get('location', 'Unknown'))
            csv_path = os.path.join(BASE_MERGED_DIR, f"merged_{name}.csv")

            analyzer = SensorAnalyzer(name, csv_path, config, sensor_location)
            if analyzer.load_data():
                analyzer.process_data()
                result = analyzer.generate_report()
                if result:
                    dashboard_data.append(result)
        except Exception as e:
            print(f"Error processing {yaml_file}: {e}")

    generate_dashboard_html(dashboard_data)

if __name__ == "__main__":
    main()
