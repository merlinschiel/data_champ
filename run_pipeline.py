"""
CRNS Processing Pipeline Runner
================================
Cross-platform entry point that runs the full processing pipeline:
  1. Data Sync   (copydata.py)     - Syncs FTP data into local data/ tree
  2. Data Merge  (auto_append.py)  - Merges raw files into per-sensor CSVs
  3. Calibration (calibrate_sensor.py)        - [optional]
  4. Neptoon Processing (neptoon_pipeline.py) - [optional]
  5. Dashboard   (recent_performance.py)      - Monitoring dashboard

Usage:
  python run_pipeline.py                       # sync + merge + dashboard
  python run_pipeline.py --field               # sync includes field data + merge + dashboard
  python run_pipeline.py --process             # sync + merge + neptoon + dashboard
  python run_pipeline.py --process --sensor x+y  # neptoon only for x and y
  python run_pipeline.py --calibrate --update-yaml  # calibrate and write N0 to the phase YAMLs

Each step's status in last_run.json is SUCCESS, COMPLETED_WITH_ERRORS (the step ran
but logged errors) or FAILED (the step raised).
"""

import sys
import time
import json
import logging
import logging.handlers
import argparse
import inspect
import traceback
from pathlib import Path
from datetime import datetime


SCRIPT_DIR = Path(__file__).resolve().parent
PROCESSING_DIR = SCRIPT_DIR / "processing"
META_DIR = PROCESSING_DIR / "meta"
LOG_DIR = META_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
sys.path.insert(0, str(PROCESSING_DIR))

LOG_FILE = LOG_DIR / "pipeline.log"
LOG_HISTORY = 7  # previous runs kept as pipeline.log.1 ... pipeline.log.7
LOG_FORMAT = '%(asctime)s - %(levelname)s - %(message)s'


class ErrorCounter(logging.Handler):
    """Collects ERROR/CRITICAL records so a step that logs errors is not reported as clean."""

    def __init__(self):
        super().__init__(level=logging.ERROR)
        self.messages = []

    def emit(self, record):
        self.messages.append(record.getMessage())

    def reset(self):
        self.messages = []


def setup_logging() -> ErrorCounter:
    file_handler = logging.handlers.RotatingFileHandler(
        LOG_FILE, maxBytes=0, backupCount=LOG_HISTORY, encoding='utf-8', delay=True)
    if LOG_FILE.exists() and LOG_FILE.stat().st_size > 0:
        file_handler.doRollover()
    file_handler.setFormatter(logging.Formatter(LOG_FORMAT))
    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter(LOG_FORMAT))
    counter = ErrorCounter()

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for handler in (file_handler, console, counter):
        root.addHandler(handler)
    return counter


error_counter = setup_logging()
logger = logging.getLogger("pipeline")


def run_pipeline():

    parser = argparse.ArgumentParser(
        description="CRNS Data Pipeline — sync, merge, process, dashboard."
    )
    parser.add_argument(
        "--process", action="store_true",
        help="Run neptoon processing step (Quality Assurance + corrections + soil moisture)"
    )
    parser.add_argument(
        "--calibrate", action="store_true",
        help="Run sensor calibration step (calculates optimal N0 per phase)"
    )
    parser.add_argument(
        "--update-yaml", action="store_true",
        help="With --calibrate: write the calibrated N0 into the YAML of each calibrated phase"
    )
    parser.add_argument(
        "--sensor", "-s", type=str, nargs="+", default=None,
        help="Process only specific sensor name(s) (applies to all steps)"
    )
    parser.add_argument(
        "--process-config", type=str, default=None,
        help="Path to process config YAML for Neptoon (e.g. processing/process_config/default_config.yaml)"
    )
    parser.add_argument(
        "--by-phase", action="store_true",
        help="Run Neptoon processing phase-by-phase"
    )
    parser.add_argument(
        "--field", action="store_true",
        help="Enable field/SD card data sync (source: parent folder of the project)"
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Force re-sync and re-merge bypassing caches"
    )
    args, _ = parser.parse_known_args()

    steps = [
        ("Data Sync",  "copydata"),
        ("Data Merge",  "auto_append"),
    ]
    if args.calibrate:
        steps.append(("Neptoon Calibrate", "calibrate_sensor"))
    if args.process:
        steps.append(("Neptoon Process", "neptoon_pipeline"))
    steps.append(("Dashboard",  "recent_performance"))

    logger.info("=" * 60)
    logger.info("CRNS Data Pipeline started")
    if args.calibrate:
        logger.info("  -> Sensor calibration ENABLED" + (" (YAML update ON)" if args.update_yaml else ""))
    if args.process:
        logger.info("  -> Neptoon processing ENABLED")
        logger.info(f"  -> Neptoon mode: {'Phase-by-Phase' if args.by_phase else 'Unified Merged'}")
    if args.sensor:
        logger.info(f"  -> Sensor filter: {args.sensor}")
    if args.field:
        logger.info("  -> Field data sync ENABLED")
    logger.info("=" * 60)

    results = []
    all_ok = True

    for step_name, module_name in steps:
        logger.info(f"--- Step: {step_name} ({module_name}.py) ---")
        start = time.time()
        error_counter.reset()

        try:
            module = __import__(module_name)

            # Pass arguments dynamically to modules that support run_pipeline
            if hasattr(module, 'run_pipeline'):
                sig = inspect.signature(module.run_pipeline)
                kwargs = {}
                if 'sensor' in sig.parameters and args.sensor:
                    kwargs['sensor'] = args.sensor
                if 'field' in sig.parameters and args.field:
                    kwargs['field'] = args.field
                if 'force' in sig.parameters and args.force:
                    kwargs['force'] = args.force
                if 'process_config' in sig.parameters and args.process_config:
                    kwargs['process_config'] = args.process_config
                if 'by_phase' in sig.parameters and args.by_phase:
                    kwargs['by_phase'] = args.by_phase
                if 'update_yaml' in sig.parameters:
                    kwargs['update_yaml'] = args.update_yaml
                module.run_pipeline(**kwargs)
            else:
                module.main()

            duration = time.time() - start
            entry = {"step": step_name, "status": "SUCCESS", "duration_s": round(duration, 2)}
            if error_counter.messages:
                entry["status"] = "COMPLETED_WITH_ERRORS"
                entry["error_count"] = len(error_counter.messages)
                entry["error"] = " | ".join(m.strip().splitlines()[0] for m in error_counter.messages[:5])
                all_ok = False
                logger.warning(f"  -> {step_name} completed in {duration:.1f}s with "
                               f"{len(error_counter.messages)} logged error(s)")
            else:
                logger.info(f"  -> {step_name} completed in {duration:.1f}s")
            results.append(entry)

        except Exception as e:
            duration = time.time() - start
            results.append({
                "step": step_name,
                "status": "FAILED",
                "error": str(e),
                "duration_s": round(duration, 2)
            })
            logger.error(f"  -> {step_name} FAILED after {duration:.1f}s: {e}\n{traceback.format_exc()}")
            all_ok = False

    # Write run summary
    summary = {
        "timestamp": datetime.now().isoformat(),
        "overall_status": "SUCCESS" if all_ok else "PARTIAL_FAILURE",
        "neptoon_enabled": args.process,
        "sensor_filter": args.sensor,
        "results": results
    }

    status_file = SCRIPT_DIR / "last_run.json"
    with open(status_file, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)

    logger.info("=" * 60)
    logger.info(f"Pipeline finished: {summary['overall_status']}")
    logger.info(f"Status written to {status_file}")
    logger.info("=" * 60)

    return all_ok


if __name__ == "__main__":
    success = run_pipeline()
    sys.exit(0 if success else 1)
