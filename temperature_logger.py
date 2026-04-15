#!/usr/bin/env python3
"""
Temperature and humidity logger with SQLite write support.

Reads temperature/humidity from AHT10 sensor via I2C and writes to:
- SQLite database (primary, requires successful write)

CSV files from Phase 2/3 are preserved as historical archives (Feb 2 - Mar 23, 2026).

Runs continuously, collecting readings every 5 minutes.

Phase 4: CSV write operations have been removed. All new data writes to SQLite only.
"""

import time
import board
import busio
import adafruit_ahtx0
import logging
from datetime import datetime
import os
import sqlite3

# Phase 2: Import database utilities
from db_utils import ConnectionManager, csv_timestamp_to_epoch

# Use user's home directory for logs to avoid permission issues
log_dir = os.path.expanduser("~/mining_sensor_logs")
os.makedirs(log_dir, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(message)s',
    handlers=[
        logging.FileHandler(f'{log_dir}/temperature_humidity.log'),
        logging.StreamHandler()
    ]
)

logger = logging.getLogger(__name__)

# Phase 2: Database configuration
DB_PATH = f"{log_dir}/mining_data.db"
DB_MANAGER = None  # Will be initialized in main()

def read_sensor():
    try:
        i2c = busio.I2C(board.SCL, board.SDA)
        sensor = adafruit_ahtx0.AHTx0(i2c)
        return sensor.temperature, sensor.relative_humidity
    except Exception as e:
        logger.error(f"Error reading sensor: {e}")
        return None, None

def write_to_db(timestamp_str: str, temperature: float, humidity: float) -> bool:
    """
    Write sensor reading to SQLite database (required).

    Phase 4: SQLite is now the primary data store. CSV files are preserved as
    historical archives. Database write must succeed.

    Uses INSERT OR IGNORE for idempotency (can safely re-run same data).

    Args:
        timestamp_str: Timestamp in format "YYYY-MM-DD HH:MM:SS"
        temperature: Temperature in Celsius
        humidity: Humidity percentage

    Returns:
        True if write successful, False if failed (warning logged, continues)
    """
    if DB_MANAGER is None:
        logger.warning("Database manager not initialized, skipping DB write")
        return False

    try:
        # Convert CSV timestamp to Unix epoch
        epoch = csv_timestamp_to_epoch(timestamp_str)

        # Use context manager for safe connection handling
        with DB_MANAGER.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT OR IGNORE INTO sensor_readings
                (timestamp, temperature_c, humidity_pct)
                VALUES (?, ?, ?)
            """, (epoch, temperature, humidity))

        logger.debug(f"Wrote to DB: timestamp={epoch}, temp={temperature:.2f}°C, humidity={humidity:.2f}%")
        return True

    except Exception as e:
        # Log warning but continue - CSV write is the primary data source
        logger.warning(f"Failed to write to database: {e} (CSV write will continue)")
        return False


def main():
    global DB_MANAGER

    logger.info("Temperature/Humidity logger started")

    # Phase 2: Initialize database connection manager
    try:
        DB_MANAGER = ConnectionManager(DB_PATH, timeout=30.0, enable_wal=True)
        logger.info(f"Database manager initialized for {DB_PATH}")
    except Exception as e:
        logger.warning(f"Failed to initialize database manager: {e}")
        logger.warning("Will continue with CSV-only writes")
        DB_MANAGER = None

    while True:
        try:
            temp, humidity = read_sensor()

            if temp is not None and humidity is not None:
                log_message = f"Temperature: {temp:.2f}°C, Humidity: {humidity:.2f}%"
                logger.info(log_message)

                # Get timestamp once for consistent DB writes
                timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

                # Phase 4: Write to database only (CSV files preserved as archives)
                write_to_db(timestamp, temp, humidity)

            time.sleep(300)  # 5 minutes

        except KeyboardInterrupt:
            logger.info("Logger stopped by user")
            break
        except Exception as e:
            logger.error(f"Unexpected error: {e}")
            time.sleep(60)

if __name__ == "__main__":
    main()
