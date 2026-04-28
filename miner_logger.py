#!/usr/bin/env python3
"""
Miner metrics logger with SQLite write support.

Queries Antminer T21 via CGMiner API to collect hash rate, temperature,
fan, and share metrics. Writes to SQLite.

CSV files from Phase 2/3 are preserved as historical archives (Feb 2 - Mar 23, 2026).

Runs continuously, collecting metrics every 5 minutes (synchronized with
temperature logger).

Phase 4: CSV write operations have been removed. All new data writes to SQLite only.
"""

import socket
import json
import time
import logging
from datetime import datetime
import os
import sqlite3
import requests
from requests.auth import HTTPDigestAuth

# Phase 2: Import database utilities
from db_utils import ConnectionManager, csv_timestamp_to_epoch, transaction

# Use user's home directory for logs to avoid permission issues
log_dir = os.path.expanduser("~/mining_sensor_logs")
os.makedirs(log_dir, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(message)s',
    handlers=[
        logging.FileHandler(f'{log_dir}/miner_metrics.log'),
        logging.StreamHandler()
    ]
)

logger = logging.getLogger(__name__)

# Phase 2: Database configuration
DB_PATH = f"{log_dir}/mining_data.db"
DB_MANAGER = None  # Will be initialized in main()
MINER_ID = 1  # Single miner in this system

# Miner configuration
MINER_HOST = "192.168.18.7"
MINER_PORT = 4028
TIMEOUT = 5
MINER_WEB_USER = "root"
MINER_WEB_PASS = "root"
HTTP_TIMEOUT = 5  # separate from TIMEOUT (CGMiner socket timeout)

def query_cgminer(command):
    """Query CGMiner API and return JSON response"""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(TIMEOUT)
        sock.connect((MINER_HOST, MINER_PORT))

        # Send command as JSON
        request = json.dumps({"command": command})
        sock.sendall(request.encode())

        # Receive response
        response = b""
        sock.settimeout(1)  # Short timeout for receiving data
        while True:
            try:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                response += chunk
            except socket.timeout:
                break

        sock.close()

        if response:
            # Parse only the first complete JSON object
            response_str = response.decode().strip()
            # Find the end of the first JSON object
            depth = 0
            for i, char in enumerate(response_str):
                if char == '{':
                    depth += 1
                elif char == '}':
                    depth -= 1
                    if depth == 0:
                        return json.loads(response_str[:i+1])
        return None
    except Exception as e:
        logger.error(f"Error querying miner: {e}")
        return None

def query_stats_http():
    """
    Query the Antminer T21 native HTTP API for power metrics (watt, jt).
    Returns {"watt": float, "jt": float} or None on any failure.
    Non-fatal: CGMiner collection continues regardless.
    """
    url = f"http://{MINER_HOST}/cgi-bin/stats.cgi"
    try:
        r = requests.get(
            url,
            auth=HTTPDigestAuth(MINER_WEB_USER, MINER_WEB_PASS),
            timeout=HTTP_TIMEOUT,
        )
        r.raise_for_status()
        payload = r.json()
        stats_list = payload.get("STATS", [])
        if not stats_list:
            logger.warning("query_stats_http: empty STATS array in response")
            return None
        stats = stats_list[0]  # HTTP API: watt/jt are in STATS[0] (unlike CGMiner TCP which uses STATS[1])
        watt = stats.get("watt")
        jt = stats.get("jt")
        if watt is None or jt is None:
            logger.warning(f"query_stats_http: missing watt/jt in response: {list(stats.keys())}")
            return None
        return {"watt": float(watt), "jt": float(jt)}
    except requests.exceptions.Timeout:
        logger.warning(f"query_stats_http: timed out connecting to {url}")
        return None
    except requests.exceptions.ConnectionError:
        logger.warning(f"query_stats_http: could not connect to {url}")
        return None
    except Exception as e:
        logger.warning(f"query_stats_http: unexpected error: {e}")
        return None


def parse_temperature_string(temp_str):
    """
    Parse temperature string in format like '61-61-69-69' into list of floats.

    Args:
        temp_str: String in format 'XX-XX-XX-XX' or similar

    Returns:
        List of temperature values as floats, empty list if parsing fails
    """
    if not temp_str:
        return []

    try:
        # Split by hyphen and convert to floats
        temps = [float(t.strip()) for t in str(temp_str).split('-')]
        return temps
    except (ValueError, AttributeError):
        return []


def get_miner_metrics():
    """Get key metrics from miner"""
    try:
        # Get summary data
        summary_data = query_cgminer("summary")
        if not summary_data or "SUMMARY" not in summary_data:
            return None

        # Get detailed stats
        stats_data = query_cgminer("stats")
        if not stats_data or "STATS" not in stats_data:
            return None

        summary = summary_data["SUMMARY"][0]
        stats = stats_data["STATS"][1]  # Index 1 is the actual stats (index 0 is metadata)

        # Extract key metrics
        metrics = {
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "ghs_5s": summary.get("GHS 5s", 0),
            "ghs_avg": summary.get("GHS av", 0),
            "ghs_30m": summary.get("GHS 30m", 0),
            "accepted": summary.get("Accepted", 0),
            "rejected": summary.get("Rejected", 0),
            "rejection_pct": round((summary.get("Rejected", 0) / max(summary.get("Accepted", 1) + summary.get("Rejected", 1), 1)) * 100, 2),
            "hardware_errors": summary.get("Hardware Errors", 0),
            "utility": summary.get("Utility", 0),
            "temp1": stats.get("temp1", 0),
            "temp2": stats.get("temp2", 0),
            "temp3": stats.get("temp3", 0),
            "temp_max": max(stats.get("temp1", 0), stats.get("temp2", 0), stats.get("temp3", 0)),
            "fan1": stats.get("fan1", 0),
            "fan2": stats.get("fan2", 0),
            "fan3": stats.get("fan3", 0),
            "fan4": stats.get("fan4", 0),
            "frequency": stats.get("frequency", 0),
            "elapsed": summary.get("Elapsed", 0),
            "pool_rejected_pct": summary.get("Pool Rejected%", 0),
        }

        # Query native HTTP API for power metrics — non-fatal if unavailable
        power_data = query_stats_http()
        metrics["watt_actual"] = power_data["watt"] if power_data else None
        metrics["efficiency_jt"] = power_data["jt"] if power_data else None

        # Phase 5: Extract chain temperature data
        # Parse detailed per-chain temperatures (3 chains × 3 sensor types × 4 readings)
        chain_temps = {}
        for chain_num in range(1, 4):  # Chains 1, 2, 3
            chain_data = {
                "chip": parse_temperature_string(stats.get(f"temp_chip{chain_num}", "")),
                "pcb": parse_temperature_string(stats.get(f"temp_pcb{chain_num}", "")),
                "pic": parse_temperature_string(stats.get(f"temp_pic{chain_num}", "")),
            }
            chain_temps[f"chain_{chain_num}"] = chain_data

        metrics["chain_temperatures"] = chain_temps

        return metrics
    except Exception as e:
        logger.error(f"Error extracting metrics: {e}")
        return None

def write_to_db(metrics: dict) -> bool:
    """
    Write miner metrics to SQLite database in a transaction (required).

    Phase 5: SQLite is now the primary data store. CSV files are preserved as
    historical archives. Database write must succeed.

    Writes to multiple tables in a single transaction for consistency:
    - miner_metrics: Basic hash rate and share metrics
    - miner_temperatures: Temperature readings (references miner_metrics via miner_id)
    - miner_fans: Fan speed readings (references miner_metrics via miner_id)
    - chain_metrics: Per-chain metrics (placeholder for future expansion)
    - chain_temperatures: Detailed per-sensor temperature readings (36 sensors: 3 chains × 3 types × 4 readings)

    Uses INSERT OR IGNORE for idempotency.
    Automatic rollback if any statement fails.

    Args:
        metrics: Dictionary with keys: timestamp, ghs_5s, ghs_avg, ghs_30m,
                accepted, rejected, rejection_pct, hardware_errors, utility,
                temp1, temp2, temp3, temp_max, fan1, fan2, fan3, fan4,
                frequency, elapsed, pool_rejected_pct, chain_temperatures

    Returns:
        True if all writes successful, False if failed (warning logged)
    """
    if DB_MANAGER is None:
        logger.warning("Database manager not initialized, skipping DB write")
        return False

    try:
        # Convert CSV timestamp to Unix epoch
        epoch = csv_timestamp_to_epoch(metrics['timestamp'])

        with DB_MANAGER.get_connection(isolation_level='IMMEDIATE') as conn:
            with transaction(conn) as cursor:
                # Insert into miner_metrics (primary table)
                cursor.execute("""
                    INSERT OR IGNORE INTO miner_metrics
                    (timestamp, ghs_5s, ghs_avg, ghs_30m, accepted, rejected,
                     rejection_pct, hardware_errors, utility, elapsed,
                     pool_rejected_pct, frequency, watt_actual, efficiency_jt)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    epoch,
                    metrics['ghs_5s'],
                    metrics['ghs_avg'],
                    metrics['ghs_30m'],
                    metrics['accepted'],
                    metrics['rejected'],
                    metrics['rejection_pct'],
                    metrics['hardware_errors'],
                    metrics['utility'],
                    metrics['elapsed'],
                    metrics['pool_rejected_pct'],
                    metrics['frequency'],
                    metrics.get('watt_actual'),
                    metrics.get('efficiency_jt'),
                ))

                # Insert into miner_temperatures (child table)
                cursor.execute("""
                    INSERT OR IGNORE INTO miner_temperatures
                    (miner_id, timestamp, temp1, temp2, temp3, temp_max)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (
                    MINER_ID,
                    epoch,
                    metrics['temp1'],
                    metrics['temp2'],
                    metrics['temp3'],
                    metrics['temp_max'],
                ))

                # Insert into miner_fans (child table)
                cursor.execute("""
                    INSERT OR IGNORE INTO miner_fans
                    (miner_id, timestamp, fan1, fan2, fan3, fan4)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (
                    MINER_ID,
                    epoch,
                    metrics['fan1'],
                    metrics['fan2'],
                    metrics['fan3'],
                    metrics['fan4'],
                ))

                # Phase 5: Insert chain temperatures (3 chains × 3 sensor types × 4 readings = 36 sensors)
                chain_temps = metrics.get('chain_temperatures', {})
                for chain_key, sensor_data in chain_temps.items():
                    # Extract chain number from key (e.g., "chain_1" -> 1)
                    try:
                        chain_num = int(chain_key.split('_')[1])
                    except (IndexError, ValueError):
                        continue

                    # Create or get chain_metrics entry for this chain
                    cursor.execute("""
                        INSERT OR IGNORE INTO chain_metrics
                        (miner_id, timestamp, chain_number, active_chips, hardware_errors, hash_rate_ghs, frequency_mhz, chip_status)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        MINER_ID,
                        epoch,
                        chain_num,
                        None,  # active_chips - could be parsed from stats if available
                        None,  # hardware_errors - could be parsed from stats if available
                        None,  # hash_rate_ghs - could be parsed from stats if available
                        None,  # frequency_mhz - could be parsed from stats if available
                        None,  # chip_status - could be derived from temps
                    ))

                    # Get the chain_metrics ID for this chain/timestamp (for reference)
                    cursor.execute("""
                        SELECT id FROM chain_metrics
                        WHERE miner_id = ? AND timestamp = ? AND chain_number = ?
                    """, (MINER_ID, epoch, chain_num))
                    chain_id_row = cursor.fetchone()
                    chain_id = chain_id_row[0] if chain_id_row else None

                    if not chain_id:
                        logger.warning(f"Failed to get chain_id for chain {chain_num}")
                        continue

                    # Insert detailed temperature readings for this chain
                    for sensor_type, temp_list in sensor_data.items():
                        for position, temp_value in enumerate(temp_list, start=1):
                            if temp_value is not None:
                                # Sensor type naming: "chip_1", "chip_2", "chip_3", "chip_4", "pcb_1", etc.
                                sensor_name = f"{sensor_type}_{position}"
                                cursor.execute("""
                                    INSERT OR IGNORE INTO chain_temperatures
                                    (chain_id, timestamp, sensor_type, temperature_c)
                                    VALUES (?, ?, ?, ?)
                                """, (
                                    chain_id,
                                    epoch,
                                    sensor_name,
                                    float(temp_value),
                                ))

        logger.debug(f"Wrote to DB: timestamp={epoch}, avg_ghs={metrics['ghs_avg']:.2f}, chain_temps=36")
        return True

    except Exception as e:
        # Log warning but continue - CSV write is the primary data source
        logger.warning(f"Failed to write to database: {e} (CSV write will continue)")
        return False


def main():
    global DB_MANAGER

    logger.info("Miner metrics logger started")
    logger.info(f"Connecting to miner at {MINER_HOST}:{MINER_PORT}")

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
            metrics = get_miner_metrics()

            if metrics:
                # Log to console/file
                power_str = ""
                if metrics.get('watt_actual') is not None:
                    power_str = f", Power: {metrics['watt_actual']:.0f}W ({metrics['efficiency_jt']:.1f} J/TH)"

                log_message = (
                    f"GH/s: {metrics['ghs_avg']:.2f} (5s: {metrics['ghs_5s']:.2f}), "
                    f"Temps: {metrics['temp1']}°C/{metrics['temp2']}°C/{metrics['temp3']}°C "
                    f"(max: {metrics['temp_max']}°C), "
                    f"Fans: {metrics['fan1']}/{metrics['fan2']}/{metrics['fan3']}/{metrics['fan4']} RPM, "
                    f"Shares: {metrics['accepted']}A/{metrics['rejected']}R "
                    f"({metrics['rejection_pct']:.2f}%){power_str}"
                )
                logger.info(log_message)

                # Phase 4: Write to database only (CSV files preserved as archives)
                write_to_db(metrics)
            else:
                logger.warning("Failed to get metrics from miner")

            time.sleep(300)  # 5 minutes - same interval as temperature logger

        except KeyboardInterrupt:
            logger.info("Logger stopped by user")
            break
        except Exception as e:
            logger.error(f"Unexpected error: {e}")
            time.sleep(60)

if __name__ == "__main__":
    main()
