import pandas as pd
import folium
from pathlib import Path

PROCESSING_DIR = Path(__file__).resolve().parent
METADATA_FILE = PROCESSING_DIR / "meta" / "location_metadata.xlsx"
OUTPUT_FILE = PROCESSING_DIR / "meta" / "sensor_map.html"


def generate_map():
    if not METADATA_FILE.exists():
        print(f"Error: Metadata file not found at {METADATA_FILE}")
        return

    df = pd.read_excel(METADATA_FILE, sheet_name="Locations")
    df.columns = df.columns.str.strip()

    m = folium.Map(
        location=[51.1657, 10.4515],
        zoom_start=6,
        control_scale=True,
        tiles="https://tile.openstreetmap.org/{z}/{x}/{y}.png",
        attr='&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
    )

    valid_coords = []

    # Group by location for multi-phase popup tables
    for loc_name, group in df.groupby("Location"):
        first = group.iloc[0]
        lat = first.get("Latitude")
        lon = first.get("Longitude")

        if pd.isna(lat) or pd.isna(lon):
            continue

        valid_coords.append([float(lat), float(lon)])

        desc = str(first.get("Description", loc_name))
        popup_html = f"<h4>{desc}</h4><b>Coordinates:</b> {lat:.5f}, {lon:.5f}<br><br>"

        popup_html += "<table border='1' cellpadding='3' cellspacing='0' style='border-collapse:collapse;'>"
        popup_html += "<tr><th>Sensor</th><th>Start Date</th><th>End Date</th></tr>"
        for _, row in group.iterrows():
            sensor = str(row.get("Sensor", "Unknown"))
            start = str(row.get("Start_Date", "Unknown"))
            end = str(row.get("End_Date", "now"))
            if not end or end == "nan":
                end = "now"
            popup_html += f"<tr><td>{sensor}</td><td>{start}</td><td>{end}</td></tr>"
        popup_html += "</table>"

        folium.Marker(
            location=[float(lat), float(lon)],
            popup=folium.Popup(popup_html, max_width=400),
            tooltip=str(loc_name),
            icon=folium.Icon(color='blue', icon='info-sign'),
        ).add_to(m)

    if valid_coords:
        m.fit_bounds(valid_coords)

    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    m.save(str(OUTPUT_FILE))
    print(f"Map successfully generated and saved to {OUTPUT_FILE}")


if __name__ == "__main__":
    generate_map()
