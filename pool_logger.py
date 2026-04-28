#!/usr/bin/env python3
"""
Braiins Pool API logger.
Polls /accounts/profile/json/btc every 5 min and /accounts/rewards/json/btc
once daily. Writes to pool_stats and pool_daily_rewards SQLite tables.
"""

import os
import time
import logging
import requests
from datetime import datetime

from db_utils import ConnectionManager, transaction
from db_schema import apply_pool_tables_migration

log_dir = os.path.expanduser("~/mining_sensor_logs")
os.makedirs(log_dir, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(message)s',
    handlers=[
        logging.FileHandler(f'{log_dir}/pool_metrics.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

DB_PATH = os.path.join(log_dir, "mining_data.db")
DB_MANAGER = None

BASE_URL = "https://pool.braiins.com"
POLL_INTERVAL = 300  # 5 minutes

UNIT_TO_GHS = {
    "gh/s": 1.0,
    "th/s": 1_000.0,
    "ph/s": 1_000_000.0,
    "mh/s": 0.001,
}


def load_api_token() -> str:
    """Read Pool-Auth-Token from ~/.braiins.txt (line: 'api key: <token>')."""
    path = os.path.expanduser("~/.braiins.txt")
    try:
        with open(path) as f:
            for line in f:
                stripped = line.strip()
                if stripped.lower().startswith("api key:"):
                    token = stripped.split(":", 1)[1].strip()
                    if token:
                        return token
        raise ValueError(f"No 'api key:' line found in {path}")
    except FileNotFoundError:
        raise FileNotFoundError(f"Braiins token file not found: {path}")


def to_ghs(value: float, unit: str) -> float:
    """Convert hash rate value to GH/s."""
    return float(value) * UNIT_TO_GHS.get(unit.lower().strip(), 1.0)


def fetch_profile(token: str) -> dict | None:
    """
    GET /accounts/profile/json/btc
    Returns normalised dict or None on failure.
    """
    url = f"{BASE_URL}/accounts/profile/json/btc"
    try:
        r = requests.get(url, headers={"Pool-Auth-Token": token}, timeout=15)
        r.raise_for_status()
        btc = r.json().get("btc", {})
        unit = btc.get("hash_rate_unit", "Gh/s")
        return {
            "hash_rate_5m_ghs":       to_ghs(btc["hash_rate_5m"], unit),
            "hash_rate_60m_ghs":      to_ghs(btc["hash_rate_60m"], unit),
            "hash_rate_24h_ghs":      to_ghs(btc["hash_rate_24h"], unit),
            "today_reward_btc":       float(btc["today_reward"]),
            "estimated_reward_btc":   float(btc["estimated_reward"]),
            "current_balance_btc":    float(btc["current_balance"]),
            "ok_workers":             int(btc["ok_workers"]),
            "low_workers":            int(btc["low_workers"]),
            "off_workers":            int(btc["off_workers"]),
            "dis_workers":            int(btc["dis_workers"]),
            "shares_5m":              int(btc["shares_5m"]),
            "shares_60m":             int(btc["shares_60m"]),
        }
    except requests.exceptions.RequestException as e:
        logger.warning(f"fetch_profile failed: {e}")
        return None
    except (KeyError, ValueError, TypeError) as e:
        logger.warning(f"fetch_profile parse error: {e}")
        return None


def fetch_daily_rewards(token: str) -> list | None:
    """
    GET /accounts/rewards/json/btc
    Returns list of {date, total_reward_btc, mining_reward_btc} or None.
    """
    url = f"{BASE_URL}/accounts/rewards/json/btc"
    try:
        r = requests.get(url, headers={"Pool-Auth-Token": token}, timeout=30)
        r.raise_for_status()
        rewards = []
        for entry in r.json().get("btc", {}).get("daily_rewards", []):
            try:
                rewards.append({
                    "date":              int(entry["date"]),
                    "total_reward_btc":  float(entry["total_reward"]),
                    "mining_reward_btc": float(entry["mining_reward"]),
                })
            except (KeyError, ValueError, TypeError) as e:
                logger.warning(f"Skipping malformed reward entry: {e}")
        return rewards
    except requests.exceptions.RequestException as e:
        logger.warning(f"fetch_daily_rewards failed: {e}")
        return None
    except (ValueError, TypeError) as e:
        logger.warning(f"fetch_daily_rewards parse error: {e}")
        return None


def should_fetch_rewards() -> bool:
    """Return True if today's UTC midnight epoch is not yet in pool_daily_rewards."""
    if DB_MANAGER is None:
        return False
    today_midnight = int(
        datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    )
    try:
        with DB_MANAGER.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT MAX(date) FROM pool_daily_rewards")
            row = cursor.fetchone()
            latest = row[0] if row and row[0] else 0
            return latest < today_midnight
    except Exception:
        return True


def write_pool_stats(stats: dict) -> bool:
    if DB_MANAGER is None:
        return False
    try:
        epoch = int(datetime.now().timestamp())
        with DB_MANAGER.get_connection(isolation_level='IMMEDIATE') as conn:
            with transaction(conn) as cursor:
                cursor.execute("""
                    INSERT OR IGNORE INTO pool_stats
                    (timestamp, hash_rate_5m_ghs, hash_rate_60m_ghs, hash_rate_24h_ghs,
                     today_reward_btc, estimated_reward_btc, current_balance_btc,
                     ok_workers, low_workers, off_workers, dis_workers,
                     shares_5m, shares_60m)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    epoch,
                    stats["hash_rate_5m_ghs"],
                    stats["hash_rate_60m_ghs"],
                    stats["hash_rate_24h_ghs"],
                    stats["today_reward_btc"],
                    stats["estimated_reward_btc"],
                    stats["current_balance_btc"],
                    stats["ok_workers"],
                    stats["low_workers"],
                    stats["off_workers"],
                    stats["dis_workers"],
                    stats["shares_5m"],
                    stats["shares_60m"],
                ))
        logger.info(
            f"pool_stats: 5m={stats['hash_rate_5m_ghs']:.0f} GH/s, "
            f"today={stats['today_reward_btc']:.8f} BTC, "
            f"workers {stats['ok_workers']}ok/{stats['low_workers']}lo/"
            f"{stats['off_workers']}off/{stats['dis_workers']}dis"
        )
        return True
    except Exception as e:
        logger.warning(f"write_pool_stats failed: {e}")
        return False


def write_daily_rewards(rewards: list) -> int:
    if DB_MANAGER is None:
        return 0
    fetched_at = int(datetime.now().timestamp())
    written = 0
    try:
        with DB_MANAGER.get_connection(isolation_level='IMMEDIATE') as conn:
            with transaction(conn) as cursor:
                for row in rewards:
                    cursor.execute("""
                        INSERT OR REPLACE INTO pool_daily_rewards
                        (date, total_reward_btc, mining_reward_btc, fetched_at)
                        VALUES (?, ?, ?, ?)
                    """, (row["date"], row["total_reward_btc"], row["mining_reward_btc"], fetched_at))
                    written += 1
        logger.info(f"pool_daily_rewards: {written} rows written")
    except Exception as e:
        logger.warning(f"write_daily_rewards failed: {e}")
    return written


def main():
    global DB_MANAGER

    logger.info("Pool logger started")

    apply_pool_tables_migration(DB_PATH)

    try:
        DB_MANAGER = ConnectionManager(DB_PATH, timeout=30.0, enable_wal=True)
    except Exception as e:
        logger.error(f"Failed to initialise DB: {e}")

    try:
        token = load_api_token()
        logger.info("Braiins token loaded")
    except (FileNotFoundError, ValueError) as e:
        logger.error(f"Cannot load token: {e} — aborting")
        return

    while True:
        try:
            stats = fetch_profile(token)
            if stats:
                write_pool_stats(stats)
            else:
                logger.warning("Profile fetch returned no data")

            if should_fetch_rewards():
                rewards = fetch_daily_rewards(token)
                if rewards:
                    write_daily_rewards(rewards)
                else:
                    logger.warning("Daily rewards fetch returned no data")

            time.sleep(POLL_INTERVAL)

        except KeyboardInterrupt:
            logger.info("Pool logger stopped")
            break
        except Exception as e:
            logger.error(f"Unexpected error: {e}")
            time.sleep(60)


if __name__ == "__main__":
    main()
