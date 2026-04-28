"""
Database schema definitions for Bitcoin mining monitor SQLite database.

This module defines all CREATE TABLE statements for the mining monitoring system.
Tables are organized by data source and include proper constraints, indexes hints,
and documentation.

Schema design choices:
- timestamps stored as Unix epoch (INTEGER) for efficient range queries
- UNIQUE constraints on (miner_id, timestamp) pairs for safe re-runs
- FOREIGN KEY constraints for referential integrity (enable with PRAGMA)
- Temperature/fan data split into separate tables for flexible querying
- Chain-level tables for future expansion (chain metrics, per-chip health)
"""

import sqlite3
from typing import List


# SQL statements for table creation
CREATE_SENSOR_READINGS = """
CREATE TABLE IF NOT EXISTS sensor_readings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp INTEGER UNIQUE NOT NULL,
    temperature_c REAL NOT NULL,
    humidity_pct REAL NOT NULL
);
"""

CREATE_MINER_METRICS = """
CREATE TABLE IF NOT EXISTS miner_metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp INTEGER UNIQUE NOT NULL,
    ghs_5s REAL NOT NULL,
    ghs_avg REAL NOT NULL,
    ghs_30m REAL NOT NULL,
    accepted INTEGER NOT NULL,
    rejected INTEGER NOT NULL,
    rejection_pct REAL NOT NULL,
    hardware_errors INTEGER NOT NULL,
    utility REAL NOT NULL,
    elapsed INTEGER NOT NULL,
    pool_rejected_pct REAL NOT NULL,
    frequency INTEGER,
    watt_actual REAL DEFAULT NULL,
    efficiency_jt REAL DEFAULT NULL
);
"""

CREATE_MINER_TEMPERATURES = """
CREATE TABLE IF NOT EXISTS miner_temperatures (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    miner_id INTEGER NOT NULL,
    timestamp INTEGER NOT NULL,
    temp1 INTEGER,
    temp2 INTEGER,
    temp3 INTEGER,
    temp_max INTEGER NOT NULL,
    UNIQUE(miner_id, timestamp)
);
"""

CREATE_MINER_FANS = """
CREATE TABLE IF NOT EXISTS miner_fans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    miner_id INTEGER NOT NULL,
    timestamp INTEGER NOT NULL,
    fan1 INTEGER NOT NULL,
    fan2 INTEGER NOT NULL,
    fan3 INTEGER NOT NULL,
    fan4 INTEGER NOT NULL,
    fan_avg REAL,
    UNIQUE(miner_id, timestamp)
);
"""

CREATE_CHAIN_METRICS = """
CREATE TABLE IF NOT EXISTS chain_metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    miner_id INTEGER NOT NULL,
    timestamp INTEGER NOT NULL,
    chain_number INTEGER NOT NULL,
    active_chips INTEGER,
    hardware_errors INTEGER,
    hash_rate_ghs REAL,
    frequency_mhz INTEGER,
    chip_status TEXT,
    UNIQUE(miner_id, timestamp, chain_number)
);
"""

CREATE_CHAIN_TEMPERATURES = """
CREATE TABLE IF NOT EXISTS chain_temperatures (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chain_id INTEGER NOT NULL,
    timestamp INTEGER NOT NULL,
    sensor_type TEXT NOT NULL,
    temperature_c REAL NOT NULL
);
"""

CREATE_POOL_STATS = """
CREATE TABLE IF NOT EXISTS pool_stats (
    timestamp            INTEGER PRIMARY KEY,
    hash_rate_5m_ghs     REAL NOT NULL,
    hash_rate_60m_ghs    REAL NOT NULL,
    hash_rate_24h_ghs    REAL NOT NULL,
    today_reward_btc     REAL NOT NULL,
    estimated_reward_btc REAL NOT NULL,
    current_balance_btc  REAL NOT NULL,
    ok_workers           INTEGER NOT NULL,
    low_workers          INTEGER NOT NULL,
    off_workers          INTEGER NOT NULL,
    dis_workers          INTEGER NOT NULL,
    shares_5m            INTEGER NOT NULL,
    shares_60m           INTEGER NOT NULL
);
"""

CREATE_POOL_DAILY_REWARDS = """
CREATE TABLE IF NOT EXISTS pool_daily_rewards (
    date              INTEGER PRIMARY KEY,
    total_reward_btc  REAL NOT NULL,
    mining_reward_btc REAL NOT NULL,
    fetched_at        INTEGER NOT NULL
);
"""

CREATE_DAILY_ENERGY = """
CREATE TABLE IF NOT EXISTS daily_energy (
    date          INTEGER PRIMARY KEY,  -- Unix epoch UTC midnight
    kwh           REAL,                 -- kWh consumed (NULL if coverage < 80%)
    cost_pyg      REAL,                 -- kwh * 435.51
    btc_usd_price REAL,                 -- BTC/USD price used
    usd_pyg_rate  REAL,                 -- USD/PYG rate used
    cost_usd      REAL,
    cost_btc      REAL,
    cost_sats     REAL,
    coverage_pct  REAL,                 -- fraction of 288 expected 5-min slots with data
    is_complete   INTEGER NOT NULL DEFAULT 0,
    fetched_at    INTEGER NOT NULL
);
"""


def get_all_create_statements() -> List[str]:
    """
    Return all CREATE TABLE statements in order of dependency.

    Returns:
        List of SQL CREATE TABLE statements
    """
    return [
        CREATE_SENSOR_READINGS,
        CREATE_MINER_METRICS,
        CREATE_MINER_TEMPERATURES,
        CREATE_MINER_FANS,
        CREATE_CHAIN_METRICS,
        CREATE_CHAIN_TEMPERATURES,
        CREATE_POOL_STATS,
        CREATE_POOL_DAILY_REWARDS,
        CREATE_DAILY_ENERGY,
    ]


def create_all_tables(db_path: str) -> None:
    """
    Create all tables in the database.

    Args:
        db_path: Path to SQLite database file

    Raises:
        sqlite3.Error: If table creation fails
    """
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    try:
        for statement in get_all_create_statements():
            cursor.execute(statement)
        conn.commit()
    except sqlite3.Error as e:
        conn.rollback()
        raise
    finally:
        cursor.close()
        conn.close()


def create_indexes(db_path: str) -> None:
    """
    Create indexes for optimized queries.

    Indexes are created separately from table creation for migration performance.
    Call this after initial data load to avoid index rebuild overhead.

    Args:
        db_path: Path to SQLite database file

    Raises:
        sqlite3.Error: If index creation fails
    """
    indexes = [
        # sensor_readings indexes
        "CREATE INDEX IF NOT EXISTS idx_sensor_readings_timestamp ON sensor_readings(timestamp);",
        "CREATE INDEX IF NOT EXISTS idx_sensor_readings_timestamp_desc ON sensor_readings(timestamp DESC);",

        # miner_metrics indexes
        "CREATE INDEX IF NOT EXISTS idx_miner_metrics_timestamp ON miner_metrics(timestamp);",
        "CREATE INDEX IF NOT EXISTS idx_miner_metrics_timestamp_desc ON miner_metrics(timestamp DESC);",

        # miner_temperatures indexes
        "CREATE INDEX IF NOT EXISTS idx_miner_temps_miner_timestamp ON miner_temperatures(miner_id, timestamp);",
        "CREATE INDEX IF NOT EXISTS idx_miner_temps_timestamp ON miner_temperatures(timestamp);",

        # miner_fans indexes
        "CREATE INDEX IF NOT EXISTS idx_miner_fans_miner_timestamp ON miner_fans(miner_id, timestamp);",
        "CREATE INDEX IF NOT EXISTS idx_miner_fans_timestamp ON miner_fans(timestamp);",

        # chain_metrics indexes
        "CREATE INDEX IF NOT EXISTS idx_chain_metrics_miner_timestamp ON chain_metrics(miner_id, timestamp);",
        "CREATE INDEX IF NOT EXISTS idx_chain_metrics_chain ON chain_metrics(chain_number);",

        # chain_temperatures indexes
        "CREATE INDEX IF NOT EXISTS idx_chain_temps_chain_timestamp ON chain_temperatures(chain_id, timestamp);",
        "CREATE INDEX IF NOT EXISTS idx_chain_temps_sensor ON chain_temperatures(sensor_type);",
    ]

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    try:
        for index_stmt in indexes:
            cursor.execute(index_stmt)
        conn.commit()
    except sqlite3.Error as e:
        conn.rollback()
        raise
    finally:
        cursor.close()
        conn.close()


def enable_foreign_keys(conn: sqlite3.Connection) -> None:
    """
    Enable foreign key constraint checking for a connection.

    SQLite has foreign keys disabled by default. Call this to enforce
    referential integrity (after initial data load is preferred to
    avoid constraint violations during migration).

    Args:
        conn: SQLite database connection
    """
    conn.execute("PRAGMA foreign_keys = ON;")


def set_performance_pragmas(conn: sqlite3.Connection) -> None:
    """
    Set PRAGMAs for optimal performance on Raspberry Pi.

    Configures:
    - journal_mode=WAL for better concurrency (3 writers, many readers)
    - synchronous=NORMAL for speed while maintaining durability
    - cache_size for limited RPi memory (set to -2000 = 2MB)

    Args:
        conn: SQLite database connection
    """
    pragmas = [
        "PRAGMA journal_mode=WAL;",
        "PRAGMA synchronous=NORMAL;",
        "PRAGMA cache_size=-2000;",
        "PRAGMA temp_store=MEMORY;",
    ]

    for pragma in pragmas:
        conn.execute(pragma)


def apply_pool_tables_migration(db_path: str) -> None:
    """
    Create pool_stats and pool_daily_rewards tables if they don't exist.
    Safe to run multiple times (CREATE TABLE IF NOT EXISTS).
    """
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    try:
        cursor.execute(CREATE_POOL_STATS)
        cursor.execute(CREATE_POOL_DAILY_REWARDS)
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_pool_stats_ts "
            "ON pool_stats(timestamp DESC);"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_pool_rewards_date "
            "ON pool_daily_rewards(date DESC);"
        )
        conn.commit()
    finally:
        cursor.close()
        conn.close()


def apply_daily_energy_migration(db_path: str) -> None:
    """
    Create daily_energy table and index if they don't exist.
    Safe to run multiple times (CREATE TABLE IF NOT EXISTS).
    """
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    try:
        cursor.execute(CREATE_DAILY_ENERGY)
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_daily_energy_date "
            "ON daily_energy(date DESC);"
        )
        conn.commit()
    finally:
        cursor.close()
        conn.close()


def apply_power_metrics_migration(db_path: str) -> None:
    """
    Add watt_actual and efficiency_jt columns to miner_metrics if missing.
    Safe to run multiple times (idempotent via duplicate-column-name guard).
    """
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    try:
        for stmt in [
            "ALTER TABLE miner_metrics ADD COLUMN watt_actual REAL DEFAULT NULL;",
            "ALTER TABLE miner_metrics ADD COLUMN efficiency_jt REAL DEFAULT NULL;",
        ]:
            try:
                cursor.execute(stmt)
            except sqlite3.OperationalError as e:
                if "duplicate column name" not in str(e):
                    raise
        conn.commit()
    finally:
        cursor.close()
        conn.close()
