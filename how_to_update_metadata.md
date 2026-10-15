# How to Update Sensor Metadata

When changes are made to any sensor that affect:
- Data structure (new columns, different format)
- Data source (new FTP path, new API)
- Sensitivity (new N0 value, calibration)
- Location (sensor moved)
- Hardware (tubes added/removed, new probes)

The metadata needs to be updated:

## 1. Create New Phase in Metadata

- Open `processing/meta/sensor_metadata.xlsx`
- Go to the sensor's sheet
- Copy the previous row
- Update `Phase_End_Date` of the previous phase
- Set `Phase_Start_Date` for the new phase
  - Phases cover `[Phase_Start_Date, Phase_End_Date)`. A date without time includes the whole day; a blank end date means "until the next phase starts".
  - Records that fall between two phases are **dropped** during the merge, so only leave gaps where the sensor really was not measuring. Dropped ranges are listed in `processing/meta/logs/auto_append.log`.
- Update `Phase_Folder` naming (convention: `YYYYMMDD_YYYYMMDD` or `YYYYMMDD_now`); every phase needs its own folder
- If the data source changed, update `FTP_Folder` or `Field_Folder`
- Update the `Config` column to point to the new YAML (see next step)

## 2. Create YAML Config File

- Go to `processing/sensor_config/<sensor_id>/`
- Copy the previous YAML config (or start from `processing/sensor_config/_example/sensor_example.yaml` or the configs in `processing/sensor_config/01/`)
- Make relevant changes (column names, parse options, `sensor_info` site parameters, QA ranges)
- Keep `sensor_info.name` identical for all phases of a sensor
- Name it descriptively: `sensor_<id>_<description>.yaml`
- Update the `Config` column in `sensor_metadata.xlsx`

## 3. Update Data Sources (if needed)

- **New FTP server**: Add credentials to `processing/meta/ftp_sources.txt`
- **New HTTP API** (e.g. Finapp): Add entry to `processing/meta/api_sources.txt`

## 4. Test

Run the pipeline for the specific sensor:

```bash
python run_pipeline.py --sensor 01
# Or test multiple sensors
python run_pipeline.py --sensor 01 02
```

Or in the Data Hub (`streamlit run data_hub.py`) under **Pipeline**.

Check:
- Log files in `processing/meta/logs/` for errors; `auto_append.log` lists rows outside phase windows, column mismatches and config name mismatches
- Merged output in `data/.merged/merged_<sensor>.csv`
- That the new data appears correctly in the merged file
