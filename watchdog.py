#!/usr/bin/env python3
"""
Miner Auto-Reboot Watchdog

Monitors hash rate (ghs_5s) in the SQLite database. If the hash rate stays at
zero for ZERO_READINGS_REQUIRED consecutive readings (~5 min apart), sends the
CGMiner 'restart' command to recover the miner automatically.

Requires Privileged API Access to be enabled on the T21 web interface:
  http://192.168.18.7 → Settings → enable privileged/API access

A cooldown (COOLDOWN_MINUTES) prevents reboot loops if the miner keeps failing.
State is persisted to watchdog_state.json so it survives service restarts.
"""

import json
import logging
import os
import sys
import time
from datetime import datetime

import requests
from requests.auth import HTTPDigestAuth

sys.path.insert(0, '/home/bilthon/mining_monitor')
from db_utils import ConnectionManager

# ===== Configuration =====
ZERO_READINGS_REQUIRED = 3     # 3 × 5-min intervals = ~15 min of zero hash rate
COOLDOWN_MINUTES = 60          # Minimum gap between auto-reboots
CHECK_INTERVAL_SECONDS = 60    # How often the watchdog polls the DB
MINER_HOST = "192.168.18.7"
MINER_WEB_USER = "root"
MINER_WEB_PASS = "root"
HTTP_TIMEOUT = 10

# ===== Paths =====
LOG_DIR = os.path.expanduser("~/mining_sensor_logs")
os.makedirs(LOG_DIR, exist_ok=True)
DB_PATH = os.path.join(LOG_DIR, "mining_data.db")
STATE_FILE = os.path.join(LOG_DIR, "watchdog_state.json")

# ===== Logging =====
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [watchdog] %(levelname)s: %(message)s',
    handlers=[
        logging.FileHandler(os.path.join(LOG_DIR, 'watchdog.log')),
        logging.StreamHandler(),
    ]
)
logger = logging.getLogger(__name__)


def load_state():
    """Load persistent watchdog state from JSON file."""
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {"last_reboot_ts": None, "reboot_count": 0}


def save_state(state):
    """Persist watchdog state to JSON file."""
    try:
        with open(STATE_FILE, 'w') as f:
            json.dump(state, f)
    except Exception as e:
        logger.error(f"Failed to save watchdog state: {e}")


def hash_rate_has_been_zero(db_manager):
    """
    Return True if the last ZERO_READINGS_REQUIRED DB rows all have ghs_5s = 0
    AND the newest row is fresh (written within the last 7 minutes).
    Returns False if there is not enough data or the data is stale.
    """
    try:
        with db_manager.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT timestamp, ghs_5s
                FROM miner_metrics
                ORDER BY timestamp DESC
                LIMIT ?
            """, (ZERO_READINGS_REQUIRED,))
            rows = cursor.fetchall()

            if len(rows) < ZERO_READINGS_REQUIRED:
                logger.debug(f"Only {len(rows)} rows available, need {ZERO_READINGS_REQUIRED} — skipping check")
                return False

            # Guard against stale data (miner_logger stopped writing)
            now_epoch = int(time.time())
            newest_ts = rows[0][0]
            age_minutes = (now_epoch - newest_ts) / 60
            if age_minutes > 7:
                logger.debug(f"Newest reading is {age_minutes:.1f} min old — skipping check (stale data)")
                return False

            all_zero = all(row[1] == 0 for row in rows)
            if all_zero:
                logger.warning(
                    f"Hash rate has been 0 for the last {len(rows)} readings "
                    f"(~{len(rows) * 5} minutes)"
                )
            return all_zero

    except Exception as e:
        logger.error(f"DB query failed: {e}")
        return False


def trigger_restart():
    """
    POST to /cgi-bin/reboot.cgi on the miner web interface using HTTP Digest Auth.
    Returns True if the miner acknowledged the reboot (HTTP 200).
    """
    url = f"http://{MINER_HOST}/cgi-bin/reboot.cgi"
    try:
        r = requests.post(
            url,
            auth=HTTPDigestAuth(MINER_WEB_USER, MINER_WEB_PASS),
            timeout=HTTP_TIMEOUT,
        )
        if r.status_code == 200:
            return True
        logger.error(f"Reboot endpoint returned HTTP {r.status_code}: {r.text[:200]}")
        return False
    except requests.exceptions.ConnectionError:
        logger.error(f"Could not connect to miner at {MINER_HOST} — is it reachable?")
        return False
    except Exception as e:
        logger.error(f"Error calling reboot endpoint: {e}")
        return False


def main():
    logger.info(
        f"Watchdog started - trigger: {ZERO_READINGS_REQUIRED} consecutive zero readings, "
        f"cooldown: {COOLDOWN_MINUTES} min"
    )
    db_manager = ConnectionManager(DB_PATH)

    while True:
        try:
            if hash_rate_has_been_zero(db_manager):
                state = load_state()
                now = time.time()
                last_reboot = state.get("last_reboot_ts")

                if last_reboot and (now - last_reboot) < COOLDOWN_MINUTES * 60:
                    remaining = int((COOLDOWN_MINUTES * 60 - (now - last_reboot)) / 60)
                    logger.info(f"Hash rate zero but cooldown active — {remaining} min remaining, skipping reboot")
                else:
                    logger.warning("Triggering miner restart via CGMiner API")
                    success = trigger_restart()
                    if success:
                        state["last_reboot_ts"] = int(now)
                        state["reboot_count"] = state.get("reboot_count", 0) + 1
                        save_state(state)
                        logger.info(
                            f"Restart command sent successfully "
                            f"(total auto-reboots: {state['reboot_count']})"
                        )
                    else:
                        logger.error("Restart command failed — check API access settings")
            else:
                logger.debug("Hash rate OK")

        except Exception as e:
            logger.error(f"Unexpected error in watchdog loop: {e}")

        time.sleep(CHECK_INTERVAL_SECONDS)


if __name__ == '__main__':
    main()
