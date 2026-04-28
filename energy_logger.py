#!/usr/bin/env python3
"""
Daily electricity cost logger.

Once per UTC day, aggregates watt_actual readings from miner_metrics,
fetches the current BTC/USD price (Binance) and USD/PYG rate (open.er-api.com),
computes the electricity cost in sats at 435.51 PYG/kWh, and writes a row
to daily_energy.

Runs every 5 minutes but only fires the day-summary job when yesterday's row
is missing or incomplete.
"""

import os
import time
import logging
import requests
from datetime import datetime, timedelta, timezone

from db_utils import ConnectionManager, transaction
from db_schema import apply_daily_energy_migration

log_dir = os.path.expanduser("~/mining_sensor_logs")
os.makedirs(log_dir, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(message)s',
    handlers=[
        logging.FileHandler(f'{log_dir}/energy_metrics.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

DB_PATH = os.path.join(log_dir, "mining_data.db")
DB_MANAGER = None

ELECTRICITY_PYG_PER_KWH = 435.51
SATS_PER_BTC = 100_000_000
COVERAGE_THRESHOLD = 0.80
EXPECTED_READINGS_PER_DAY = 288  # 24h * 60min / 5min


def fetch_btc_usd_price() -> float | None:
    """Fetch current BTC/USD from Binance spot ticker."""
    try:
        r = requests.get(
            "https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT",
            timeout=10,
        )
        r.raise_for_status()
        return float(r.json()["price"])
    except Exception as e:
        logger.warning(f"fetch_btc_usd_price failed: {e}")
        return None


def fetch_usd_pyg_rate() -> float | None:
    """Fetch current USD/PYG from open.er-api.com (free tier, no key)."""
    try:
        r = requests.get("https://open.er-api.com/v6/latest/USD", timeout=10)
        r.raise_for_status()
        data = r.json()
        if data.get("result") != "success":
            logger.warning(f"ExchangeRate-API returned non-success: {data.get('result')}")
            return None
        return float(data["rates"]["PYG"])
    except Exception as e:
        logger.warning(f"fetch_usd_pyg_rate failed: {e}")
        return None


def compute_daily_kwh(day_start_epoch: int) -> tuple:
    """
    Aggregate watt_actual readings for the UTC day starting at day_start_epoch.
    Returns (kwh, coverage_pct). kwh is None if no readings found.
    Each reading represents a 5-minute window: energy = watts * (5/60) / 1000 kWh.
    """
    day_end_epoch = day_start_epoch + 86400
    try:
        with DB_MANAGER.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT COUNT(*), SUM(watt_actual * (5.0 / 60.0) / 1000.0)
                FROM miner_metrics
                WHERE timestamp >= ? AND timestamp < ?
                  AND watt_actual IS NOT NULL
            """, (day_start_epoch, day_end_epoch))
            row = cursor.fetchone()
        count, total_kwh = row
        coverage = (count or 0) / EXPECTED_READINGS_PER_DAY
        if not count:
            return None, 0.0
        return total_kwh, coverage
    except Exception as e:
        logger.warning(f"compute_daily_kwh failed: {e}")
        return None, 0.0


def run_daily_energy_job(day_epoch: int) -> bool:
    """
    Compute and store the daily_energy row for day_epoch (UTC midnight).
    Uses INSERT OR REPLACE so re-runs always refresh the figures.
    Returns True if row is complete (coverage OK and both prices fetched).
    """
    date_str = datetime.utcfromtimestamp(day_epoch).date()
    kwh, coverage = compute_daily_kwh(day_epoch)

    is_complete = 0
    cost_pyg = cost_usd = cost_btc = cost_sats = None
    btc_usd = usd_pyg = None

    if kwh is not None and coverage >= COVERAGE_THRESHOLD:
        btc_usd = fetch_btc_usd_price()
        usd_pyg = fetch_usd_pyg_rate()

        if btc_usd and usd_pyg:
            cost_pyg  = kwh * ELECTRICITY_PYG_PER_KWH
            cost_usd  = cost_pyg / usd_pyg
            cost_btc  = cost_usd / btc_usd
            cost_sats = cost_btc * SATS_PER_BTC
            is_complete = 1
            logger.info(
                f"Energy {date_str}: {kwh:.3f} kWh, "
                f"{cost_pyg:.2f} PYG, "
                f"${cost_usd:.4f} USD, "
                f"{cost_sats:.0f} sats "
                f"(BTC=${btc_usd:.0f}, 1USD={usd_pyg:.2f}PYG, "
                f"coverage={coverage:.0%})"
            )
        else:
            logger.warning(f"Energy {date_str}: price fetch failed, row marked incomplete")
    else:
        if kwh is None:
            logger.warning(f"Energy {date_str}: no watt_actual readings found")
        else:
            logger.warning(
                f"Energy {date_str}: coverage too low ({coverage:.0%} < {COVERAGE_THRESHOLD:.0%})"
            )

    fetched_at = int(datetime.utcnow().timestamp())
    try:
        with DB_MANAGER.get_connection(isolation_level='IMMEDIATE') as conn:
            with transaction(conn) as cursor:
                cursor.execute("""
                    INSERT OR REPLACE INTO daily_energy
                    (date, kwh, cost_pyg, btc_usd_price, usd_pyg_rate,
                     cost_usd, cost_btc, cost_sats, coverage_pct,
                     is_complete, fetched_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    day_epoch, kwh, cost_pyg, btc_usd, usd_pyg,
                    cost_usd, cost_btc, cost_sats, coverage,
                    is_complete, fetched_at,
                ))
    except Exception as e:
        logger.error(f"Failed to write daily_energy row for {date_str}: {e}")
        return False

    return is_complete == 1


def get_yesterday_midnight_utc() -> int:
    """Return Unix epoch of yesterday's UTC midnight."""
    yesterday = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    ) - timedelta(days=1)
    return int(yesterday.timestamp())


def should_run_energy_job() -> bool:
    """Return True if yesterday's row is absent or incomplete."""
    yesterday = get_yesterday_midnight_utc()
    try:
        with DB_MANAGER.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT is_complete FROM daily_energy WHERE date = ?", (yesterday,)
            )
            row = cursor.fetchone()
            return row is None or row[0] == 0
    except Exception:
        return True


def main():
    global DB_MANAGER
    logger.info("Energy logger started")

    apply_daily_energy_migration(DB_PATH)

    try:
        DB_MANAGER = ConnectionManager(DB_PATH, timeout=30.0, enable_wal=True)
        logger.info(f"Database initialised: {DB_PATH}")
    except Exception as e:
        logger.error(f"Failed to initialise DB: {e}")
        return

    while True:
        try:
            if should_run_energy_job():
                yesterday = get_yesterday_midnight_utc()
                run_daily_energy_job(yesterday)
            time.sleep(300)
        except KeyboardInterrupt:
            logger.info("Energy logger stopped")
            break
        except Exception as e:
            logger.error(f"Unexpected error: {e}")
            time.sleep(60)


if __name__ == "__main__":
    main()
