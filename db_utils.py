"""
Database utilities for SQLite connection and transaction management.

This module provides shared utilities for:
- Connection management with connection pooling patterns
- Transaction handling for multi-table writes
- Error handling and logging
- Timestamp conversion between CSV format and Unix epoch

Used by both the logging services and the migration script.
"""

import sqlite3
import logging
from contextlib import contextmanager
from datetime import datetime
from typing import Optional, Generator
import sys

# Set up module logger
logger = logging.getLogger(__name__)


class DatabaseError(Exception):
    """Base exception for database operations."""
    pass


class ConnectionManager:
    """
    Manages SQLite database connections with consistent configuration.

    Handles:
    - Opening connections with proper timeout and check_same_thread settings
    - Applying performance PRAGMAs
    - Safe connection closure with rollback on error
    - Context manager support for automatic cleanup

    Suitable for Raspberry Pi with 3 concurrent writers and variable network load.
    """

    def __init__(self, db_path: str, timeout: float = 30.0, enable_wal: bool = True):
        """
        Initialize connection manager.

        Args:
            db_path: Path to SQLite database file
            timeout: Lock timeout in seconds (default 30s for RPi I/O)
            enable_wal: Enable WAL mode for better concurrency (default True)
        """
        self.db_path = db_path
        self.timeout = timeout
        self.enable_wal = enable_wal

    @contextmanager
    def get_connection(self, isolation_level: Optional[str] = None) -> Generator[sqlite3.Connection, None, None]:
        """
        Context manager for database connections with automatic cleanup.

        Yields a connection with proper transaction handling. Automatically
        commits on success, rolls back on exception.

        Args:
            isolation_level: Transaction isolation level (None=autocommit, 'DEFERRED', 'IMMEDIATE', 'EXCLUSIVE')

        Yields:
            sqlite3.Connection configured and ready for use

        Example:
            with manager.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("INSERT INTO sensor_readings ...")
                # Automatically commits on successful exit
        """
        conn = sqlite3.connect(self.db_path, timeout=self.timeout, check_same_thread=False)
        conn.isolation_level = isolation_level

        try:
            # Apply configuration PRAGMAs
            conn.execute("PRAGMA journal_mode=WAL;" if self.enable_wal else "PRAGMA journal_mode=DELETE;")
            conn.execute("PRAGMA synchronous=NORMAL;")
            conn.execute("PRAGMA cache_size=-2000;")
            conn.execute("PRAGMA temp_store=MEMORY;")
            conn.execute("PRAGMA foreign_keys=OFF;")  # Off during migration, can enable later

            yield conn
            conn.commit()

        except (sqlite3.Error, Exception) as e:
            conn.rollback()
            logger.error(f"Database error: {e}")
            raise DatabaseError(f"Database operation failed: {e}") from e

        finally:
            conn.close()

    def get_connection_sync(self) -> sqlite3.Connection:
        """
        Get a connection without context manager (for manual transaction control).

        Less preferred than get_connection() context manager, but useful when
        you need to manage multiple statements with explicit commit/rollback.

        Returns:
            sqlite3.Connection configured and ready for use

        Note:
            Caller is responsible for calling .close() when done.
        """
        conn = sqlite3.connect(self.db_path, timeout=self.timeout, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL;" if self.enable_wal else "PRAGMA journal_mode=DELETE;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA cache_size=-2000;")
        conn.execute("PRAGMA temp_store=MEMORY;")
        conn.execute("PRAGMA foreign_keys=OFF;")
        return conn


def csv_timestamp_to_epoch(timestamp_str: str) -> int:
    """
    Convert CSV timestamp string to Unix epoch integer.

    Handles CSV format: "YYYY-MM-DD HH:MM:SS"

    Args:
        timestamp_str: Timestamp in CSV format (e.g., "2026-03-22 20:15:40")

    Returns:
        Unix epoch seconds (integer)

    Raises:
        ValueError: If timestamp format is invalid

    Example:
        >>> csv_timestamp_to_epoch("2026-03-22 20:15:40")
        1742766940
    """
    try:
        dt = datetime.strptime(timestamp_str.strip(), "%Y-%m-%d %H:%M:%S")
        return int(dt.timestamp())
    except ValueError as e:
        raise ValueError(f"Invalid timestamp format '{timestamp_str}': {e}") from e


def epoch_to_csv_timestamp(epoch: int) -> str:
    """
    Convert Unix epoch integer back to CSV timestamp string.

    Inverse of csv_timestamp_to_epoch().

    Args:
        epoch: Unix epoch seconds

    Returns:
        Timestamp in CSV format (e.g., "2026-03-22 20:15:40")

    Example:
        >>> epoch_to_csv_timestamp(1742766940)
        "2026-03-22 20:15:40"
    """
    dt = datetime.fromtimestamp(epoch)
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def validate_timestamp(timestamp_str: str) -> bool:
    """
    Validate that a string is a valid CSV timestamp.

    Args:
        timestamp_str: String to validate

    Returns:
        True if valid, False otherwise
    """
    try:
        csv_timestamp_to_epoch(timestamp_str)
        return True
    except ValueError:
        return False


@contextmanager
def transaction(conn: sqlite3.Connection) -> Generator[sqlite3.Cursor, None, None]:
    """
    Context manager for explicit transaction control.

    Use when you need to execute multiple statements as a single atomic unit.
    Automatically rolls back on exception, commits on success.

    Args:
        conn: sqlite3.Connection object

    Yields:
        sqlite3.Cursor for executing statements

    Example:
        with transaction(conn) as cursor:
            cursor.execute("INSERT INTO miner_metrics ...")
            cursor.execute("INSERT INTO miner_temperatures ...")
            # Both execute or both roll back atomically
    """
    cursor = conn.cursor()

    try:
        cursor.execute("BEGIN IMMEDIATE;")
        yield cursor
        cursor.execute("COMMIT;")

    except (sqlite3.Error, Exception) as e:
        try:
            cursor.execute("ROLLBACK;")
        except sqlite3.Error:
            pass
        logger.error(f"Transaction error: {e}")
        raise DatabaseError(f"Transaction failed: {e}") from e

    finally:
        cursor.close()


def execute_query(conn: sqlite3.Connection, query: str, params: tuple = ()) -> list:
    """
    Execute a SELECT query and return results.

    Args:
        conn: sqlite3.Connection object
        query: SQL SELECT query
        params: Query parameters (for parameterized queries)

    Returns:
        List of tuples representing rows

    Example:
        rows = execute_query(conn, "SELECT * FROM sensor_readings WHERE timestamp > ?", (1234567890,))
    """
    cursor = conn.cursor()
    try:
        cursor.execute(query, params)
        return cursor.fetchall()
    finally:
        cursor.close()


def get_table_row_count(conn: sqlite3.Connection, table_name: str) -> int:
    """
    Get the row count for a table.

    Args:
        conn: sqlite3.Connection object
        table_name: Name of table to count

    Returns:
        Number of rows in table
    """
    cursor = conn.cursor()
    try:
        cursor.execute(f"SELECT COUNT(*) FROM {table_name};")
        return cursor.fetchone()[0]
    finally:
        cursor.close()


def check_database_integrity(db_path: str) -> tuple[bool, str]:
    """
    Run PRAGMA integrity_check on the database.

    Args:
        db_path: Path to SQLite database file

    Returns:
        Tuple of (is_ok, message) where is_ok is True if integrity is good
    """
    try:
        conn = sqlite3.connect(db_path, timeout=10.0)
        cursor = conn.cursor()
        cursor.execute("PRAGMA integrity_check;")
        result = cursor.fetchone()[0]
        cursor.close()
        conn.close()

        if result == "ok":
            return True, "Database integrity OK"
        else:
            return False, f"Integrity check failed: {result}"

    except sqlite3.Error as e:
        return False, f"Integrity check error: {e}"


def configure_logging(log_level: int = logging.INFO, log_file: Optional[str] = None) -> None:
    """
    Configure logging for database operations.

    Args:
        log_level: Logging level (default INFO)
        log_file: Optional log file path (if None, logs to stderr)
    """
    formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )

    # Console handler
    console_handler = logging.StreamHandler(sys.stderr)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)
    logger.setLevel(log_level)

    # File handler (optional)
    if log_file:
        try:
            file_handler = logging.FileHandler(log_file, mode='a')
            file_handler.setFormatter(formatter)
            logger.addHandler(file_handler)
        except IOError as e:
            logger.warning(f"Could not create log file {log_file}: {e}")
