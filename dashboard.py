#!/usr/bin/env python3
import os
import sys
import csv
import json
import socket
import hashlib
import hmac
import secrets
import getpass
import sqlite3
import logging
from datetime import datetime, timedelta
from functools import wraps
from flask import Flask, render_template, request, session, redirect, url_for, jsonify

# ===== Import existing logger functions =====
sys.path.insert(0, '/home/bilthon/mining_monitor')
from miner_logger import get_miner_metrics, query_cgminer
from db_utils import ConnectionManager, epoch_to_csv_timestamp

# Try to import sensor reading function
try:
    import board
    import busio
    import adafruit_ahtx0

    def read_sensor():
        try:
            i2c = busio.I2C(board.SCL, board.SDA)
            sensor = adafruit_ahtx0.AHTx0(i2c)
            return sensor.temperature, sensor.relative_humidity
        except Exception as e:
            return None, None
except ImportError:
    def read_sensor():
        return None, None

# ===== Configuration =====
DATA_DIR = os.path.expanduser("~/mining_sensor_logs")
MINER_CSV = os.path.join(DATA_DIR, "miner_metrics.csv")
SENSOR_CSV = os.path.join(DATA_DIR, "sensor_data.csv")
DB_PATH = os.path.join(DATA_DIR, "mining_data.db")
DB_MANAGER = ConnectionManager(DB_PATH)

app = Flask(__name__)
app.config['SECRET_KEY'] = 'default-key'  # Will be set from config
app.config['SESSION_COOKIE_SECURE'] = True
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(hours=8)

# ===== Authentication =====
USERNAME = "admin"
PASSWORD_HASH = None
PASSWORD_SALT = None

def load_config():
    """Load credentials from dashboard_config.py if it exists"""
    global PASSWORD_HASH, PASSWORD_SALT, app
    config_path = '/home/bilthon/mining_monitor/dashboard_config.py'
    if os.path.exists(config_path):
        config = {}
        exec(open(config_path).read(), config)
        PASSWORD_HASH = config.get('PASSWORD_HASH')
        PASSWORD_SALT = config.get('PASSWORD_SALT')
        app.config['SECRET_KEY'] = config.get('SECRET_KEY', 'default-key')
        return True
    return False

def verify_password(password):
    """Verify password against stored hash"""
    if not PASSWORD_HASH or not PASSWORD_SALT:
        return False
    salt_bytes = bytes.fromhex(PASSWORD_SALT)
    hash_bytes = bytes.fromhex(PASSWORD_HASH)
    computed = hashlib.pbkdf2_hmac('sha256', password.encode(), salt_bytes, 260000)
    return hmac.compare_digest(computed, hash_bytes)

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('logged_in'):
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated

def read_sensor_from_csv():
    """Fallback: read the latest sensor reading from CSV file"""
    if not os.path.exists(SENSOR_CSV):
        return None, None

    try:
        with open(SENSOR_CSV, 'r') as f:
            lines = f.readlines()
            if len(lines) < 2:  # Need header + at least one data row
                return None, None

            # Read last line
            last_line = lines[-1].strip()
            if not last_line:
                return None, None

            # Parse CSV manually (avoid DictReader for speed)
            reader = csv.DictReader(lines)
            rows = list(reader)

            if not rows:
                return None, None

            last_row = rows[-1]
            temp = float(last_row.get('temperature', 0))
            humidity = float(last_row.get('humidity', 0))
            return temp, humidity
    except Exception as e:
        app.logger.error(f"Error reading sensor from CSV: {e}")
        return None, None

# ===== Rate Limiting =====
login_attempts = {}

def check_rate_limit(ip):
    """Check login rate limit: max 10 attempts per 5 minutes"""
    now = datetime.now()
    if ip not in login_attempts:
        login_attempts[ip] = []

    # Remove old attempts (older than 5 minutes)
    login_attempts[ip] = [t for t in login_attempts[ip] if (now - t).total_seconds() < 300]

    if len(login_attempts[ip]) >= 10:
        return False
    login_attempts[ip].append(now)
    return True

# ===== Routes =====
@app.route('/')
@login_required
def dashboard():
    return render_template('index.html', page='dashboard')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        ip = request.headers.get('CF-Connecting-IP', request.remote_addr)

        if not check_rate_limit(ip):
            return render_template('index.html', page='login', error='Too many login attempts. Try again later.'), 429

        username = request.form.get('username', '')
        password = request.form.get('password', '')

        if username == USERNAME and verify_password(password):
            session['logged_in'] = True
            session.permanent = True
            return redirect(url_for('dashboard'))
        else:
            return render_template('index.html', page='login', error='Invalid username or password')

    return render_template('index.html', page='login')

@app.route('/logout', methods=['POST'])
@login_required
def logout():
    session.clear()
    return redirect(url_for('login'))

@app.route('/api/live')
@login_required
def api_live():
    """Return live miner and sensor data (SQLite primary, CSV fallback)"""
    data = {'timestamp': datetime.now().isoformat()}

    # ===== GET MINER DATA (Try SQLite first) =====
    miner_data = None
    try:
        # Try to get latest miner data from SQLite
        with DB_MANAGER.get_connection() as conn:
            cursor = conn.cursor()

            # Query latest miner_metrics
            cursor.execute("""
                SELECT timestamp, ghs_5s, ghs_avg, ghs_30m, accepted, rejected,
                       rejection_pct, hardware_errors, utility, elapsed, pool_rejected_pct,
                       frequency, watt_actual, efficiency_jt
                FROM miner_metrics
                ORDER BY timestamp DESC
                LIMIT 1
            """)
            miner_row = cursor.fetchone()

            if miner_row:
                timestamp_epoch = miner_row[0]

                # Query latest temperatures and fans for same timestamp
                cursor.execute("""
                    SELECT temp1, temp2, temp3, temp_max
                    FROM miner_temperatures
                    WHERE timestamp = ? AND miner_id = 1
                    LIMIT 1
                """, (timestamp_epoch,))
                temp_row = cursor.fetchone()

                cursor.execute("""
                    SELECT fan1, fan2, fan3, fan4, fan_avg
                    FROM miner_fans
                    WHERE timestamp = ? AND miner_id = 1
                    LIMIT 1
                """, (timestamp_epoch,))
                fan_row = cursor.fetchone()

                if temp_row and fan_row:
                    fans = [int(fan_row[i] or 0) for i in range(4)]
                    active_fans = [f for f in fans if f > 0]
                    fan_avg = sum(active_fans) / len(active_fans) if active_fans else 0
                    miner_data = {
                        'ghs_5s': float(miner_row[1]),
                        'ghs_avg': float(miner_row[2]),
                        'ghs_30m': float(miner_row[3] or 0),
                        'temp1': int(temp_row[0] or 0),
                        'temp2': int(temp_row[1] or 0),
                        'temp3': int(temp_row[2] or 0),
                        'temp_max': int(temp_row[3]),
                        'fan_avg': fan_avg,
                        'fan1': fans[0],
                        'fan2': fans[1],
                        'fan3': fans[2],
                        'fan4': fans[3],
                        'accepted': int(miner_row[4]),
                        'rejected': int(miner_row[5]),
                        'rejection_pct': float(miner_row[6]),
                        'hardware_errors': int(miner_row[7]),
                        'utility': float(miner_row[8]),
                        'elapsed': int(miner_row[9]),
                        'pool_rejected_pct': float(miner_row[10] or 0),
                        'frequency': int(miner_row[11] or 0),
                        'watt_actual': float(miner_row[12]) if miner_row[12] is not None else None,
                        'efficiency_jt': float(miner_row[13]) if miner_row[13] is not None else None,
                    }
    except Exception as e:
        app.logger.warning(f"SQLite query failed for miner data: {e}")
        miner_data = None

    # Fallback: Get miner data from live API if SQLite failed
    if not miner_data:
        try:
            miner = get_miner_metrics()
            if miner:
                fans = [miner.get(f'fan{i}', 0) for i in range(1, 5)]
                fan_avg = sum(fans) / len([f for f in fans if f > 0]) if any(fans) else 0
                miner_data = {
                    'ghs_5s': float(miner.get('ghs_5s', 0)),
                    'ghs_avg': float(miner.get('ghs_avg', 0)),
                    'temp1': int(miner.get('temp1', 0)),
                    'temp2': int(miner.get('temp2', 0)),
                    'temp3': int(miner.get('temp3', 0)),
                    'temp_max': int(miner.get('temp_max', 0)),
                    'fan_avg': fan_avg,
                    'fan1': int(miner.get('fan1', 0)),
                    'fan2': int(miner.get('fan2', 0)),
                    'fan3': int(miner.get('fan3', 0)),
                    'fan4': int(miner.get('fan4', 0)),
                    'accepted': int(miner.get('accepted', 0)),
                    'rejected': int(miner.get('rejected', 0)),
                    'rejection_pct': float(miner.get('rejection_pct', 0)),
                    'hardware_errors': int(miner.get('hardware_errors', 0)),
                    'utility': float(miner.get('utility', 0)),
                    'elapsed': int(miner.get('elapsed', 0)),
                    'pool_rejected_pct': float(miner.get('pool_rejected_pct', 0)),
                    'frequency': int(miner.get('frequency', 0)),
                    'ghs_30m': float(miner.get('ghs_30m', 0)),
                    'watt_actual': miner.get('watt_actual'),
                    'efficiency_jt': miner.get('efficiency_jt'),
                }
        except Exception as e:
            app.logger.error(f"Error getting miner metrics from API: {e}")

    if miner_data:
        data['miner'] = miner_data

    # ===== GET SENSOR DATA =====
    sensor_data = None
    try:
        # Try to read from live sensor first
        temp, humidity = read_sensor()
        if temp is not None and humidity is not None:
            sensor_data = {
                'temperature': float(temp),
                'humidity': float(humidity),
            }
    except Exception as e:
        app.logger.debug(f"Error reading from live sensor: {e}")

    # Fallback: Try SQLite
    if not sensor_data:
        try:
            with DB_MANAGER.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT temperature_c, humidity_pct
                    FROM sensor_readings
                    ORDER BY timestamp DESC
                    LIMIT 1
                """)
                sensor_row = cursor.fetchone()
                if sensor_row:
                    sensor_data = {
                        'temperature': float(sensor_row[0]),
                        'humidity': float(sensor_row[1]),
                    }
        except Exception as e:
            app.logger.warning(f"SQLite query failed for sensor data: {e}")

    # Fallback: Try CSV
    if not sensor_data:
        try:
            temp, humidity = read_sensor_from_csv()
            if temp is not None and humidity is not None:
                sensor_data = {
                    'temperature': float(temp),
                    'humidity': float(humidity),
                }
        except Exception as e:
            app.logger.warning(f"CSV fallback failed for sensor data: {e}")

    if sensor_data:
        data['sensor'] = sensor_data

    return jsonify(data)

@app.route('/api/chain-temperatures')
@login_required
def api_chain_temperatures():
    """
    Return latest chain temperature readings for all 36 sensors (3 chains × 3 types × 4 positions).

    Returns JSON organized by chain and sensor type:
    {
        "timestamp": "2026-03-23T10:45:00Z",
        "chains": [
            {
                "chain_number": 1,
                "sensors": {
                    "chip": [61.0, 61.0, 69.0, 69.0],
                    "pcb": [58.0, 57.0, 65.0, 62.0],
                    "pic": [45.0, 46.0, 48.0, 47.0]
                }
            },
            ...
        ],
        "stats": {
            "max_temp": 70.0,
            "min_temp": 44.0,
            "avg_temp": 59.1
        }
    }

    Requires authentication (login_required).
    Fallback to empty response if data unavailable.
    """
    data = {
        'timestamp': datetime.now().isoformat(),
        'chains': [],
        'stats': {
            'max_temp': None,
            'min_temp': None,
            'avg_temp': None
        }
    }

    try:
        with DB_MANAGER.get_connection() as conn:
            cursor = conn.cursor()

            # Get the most recent timestamp for chain temperature data
            cursor.execute("""
                SELECT MAX(timestamp) FROM chain_temperatures
            """)
            latest_timestamp_row = cursor.fetchone()
            if not latest_timestamp_row or latest_timestamp_row[0] is None:
                # No data available
                return jsonify(data)

            latest_timestamp = latest_timestamp_row[0]

            # Get all chain_metrics IDs for this timestamp
            cursor.execute("""
                SELECT DISTINCT cm.id, cm.chain_number
                FROM chain_metrics cm
                WHERE cm.timestamp = ?
                ORDER BY cm.chain_number ASC
            """, (latest_timestamp,))

            chain_rows = cursor.fetchall()
            all_temps = []

            # Process each chain
            for chain_id, chain_number in chain_rows:
                chain_data = {
                    'chain_number': chain_number,
                    'sensors': {
                        'chip': [None, None, None, None],
                        'pcb': [None, None, None, None],
                        'pic': [None, None, None, None]
                    }
                }

                # Query all temperature readings for this chain
                cursor.execute("""
                    SELECT sensor_type, temperature_c
                    FROM chain_temperatures
                    WHERE chain_id = ? AND timestamp = ?
                    ORDER BY sensor_type
                """, (chain_id, latest_timestamp))

                temp_readings = cursor.fetchall()

                # Organize temperatures by sensor type and position
                for sensor_type, temp_value in temp_readings:
                    if temp_value is not None:
                        all_temps.append(float(temp_value))

                    # Parse sensor_type format: "chip_1", "pcb_2", "pic_3", etc.
                    parts = sensor_type.split('_')
                    if len(parts) == 2:
                        sensor_class, position_str = parts
                        try:
                            position = int(position_str) - 1  # Convert 1-indexed to 0-indexed
                            if 0 <= position < 4 and sensor_class in chain_data['sensors']:
                                chain_data['sensors'][sensor_class][position] = float(temp_value)
                        except (ValueError, IndexError):
                            pass

                data['chains'].append(chain_data)

            # Calculate statistics from all temperatures
            if all_temps:
                data['stats']['max_temp'] = round(max(all_temps), 1)
                data['stats']['min_temp'] = round(min(all_temps), 1)
                data['stats']['avg_temp'] = round(sum(all_temps) / len(all_temps), 1)

    except Exception as e:
        app.logger.warning(f"Failed to query chain temperatures: {e}")
        # Return empty data structure with error noted
        pass

    return jsonify(data)


@app.route('/api/error-history')
@login_required
def api_error_history():
    """Return hardware error rate history with per-interval deltas and restart markers."""
    range_param = request.args.get('range', '24h')

    TARGET_POINTS = 300
    now_epoch = int(datetime.now().timestamp())

    if range_param == 'all':
        try:
            with DB_MANAGER.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT MIN(timestamp) FROM miner_metrics")
                row = cursor.fetchone()
                cutoff_epoch = row[0] if (row and row[0]) else now_epoch - 86400
        except Exception:
            cutoff_epoch = now_epoch - 86400
    elif range_param == '7d':
        cutoff_epoch = now_epoch - 7 * 86400
    elif range_param == '30d':
        cutoff_epoch = now_epoch - 30 * 86400
    else:
        cutoff_epoch = now_epoch - 86400

    try:
        with DB_MANAGER.get_connection() as conn:
            cursor = conn.cursor()
            # Fetch one extra row before the cutoff so we can compute the first delta
            cursor.execute("""
                SELECT timestamp, hardware_errors, elapsed
                FROM miner_metrics
                WHERE timestamp >= (
                    SELECT COALESCE(MAX(timestamp), ?)
                    FROM miner_metrics WHERE timestamp < ?
                )
                ORDER BY timestamp ASC
            """, (cutoff_epoch, cutoff_epoch))
            all_rows = cursor.fetchall()
    except Exception as e:
        app.logger.error(f"Error querying error history: {e}")
        return jsonify({'error': str(e)}), 500

    if not all_rows:
        return jsonify({'labels': [], 'delta': [], 'cumulative': [], 'restarts': [],
                        'stats': {'total_errors': 0, 'avg_rate': 0, 'peak_rate': 0, 'restart_count': 0}})

    # Compute deltas and detect restarts (elapsed decreased = miner restarted)
    labels, deltas, cumulatives, restarts = [], [], [], []
    prev_errors = all_rows[0][1]
    prev_elapsed = all_rows[0][2]

    in_range_rows = [r for r in all_rows if r[0] >= cutoff_epoch]
    downsample = max(1, len(in_range_rows) // TARGET_POINTS)
    downsampled = [in_range_rows[i] for i in range(0, len(in_range_rows), downsample)]

    for row in downsampled:
        ts_epoch, hw_errors, elapsed = row
        label = epoch_to_csv_timestamp(ts_epoch)

        restarted = (elapsed is not None and prev_elapsed is not None and elapsed < prev_elapsed)
        delta = 0 if restarted or hw_errors < prev_errors else (hw_errors - prev_errors)

        if restarted:
            restarts.append(label)

        labels.append(label)
        deltas.append(delta)
        cumulatives.append(hw_errors)

        prev_errors = hw_errors
        prev_elapsed = elapsed

    total = cumulatives[-1] if cumulatives else 0
    non_zero = [d for d in deltas if d > 0]
    avg_rate = round(sum(non_zero) / len(deltas), 2) if deltas else 0
    peak_rate = max(deltas) if deltas else 0

    return jsonify({
        'labels': labels,
        'delta': deltas,
        'cumulative': cumulatives,
        'restarts': restarts,
        'stats': {
            'total_errors': total,
            'avg_rate': avg_rate,
            'peak_rate': peak_rate,
            'restart_count': len(restarts),
        }
    })


@app.route('/api/history')
@login_required
def api_history():
    """Return historical data (SQLite primary, CSV fallback).

    Downsampling uses SQL bucket-averaging: the time span is divided into
    TARGET_POINTS equal buckets and AVG() is applied within each bucket.
    This preserves the shape of the data regardless of range length.
    """
    range_param = request.args.get('range', '24h')
    TARGET_POINTS = 300

    now_epoch = int(datetime.now().timestamp())

    if range_param == 'all':
        try:
            with DB_MANAGER.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT MIN(timestamp) FROM miner_metrics")
                row = cursor.fetchone()
                cutoff_epoch = row[0] if (row and row[0]) else now_epoch - 86400
        except Exception:
            cutoff_epoch = now_epoch - 86400
    elif range_param == '7d':
        cutoff_epoch = now_epoch - 7 * 86400
    elif range_param == '30d':
        cutoff_epoch = now_epoch - 30 * 86400
    else:
        cutoff_epoch = now_epoch - 86400

    span = max(1, now_epoch - cutoff_epoch)
    # Each bucket is at least one raw reading interval (300 s)
    bucket_size = max(300, span // TARGET_POINTS)
    downsample = max(1, bucket_size // 300)  # stride for CSV fallback
    cutoff = datetime.fromtimestamp(cutoff_epoch)

    data = {'miner': {}, 'sensor': {}}

    # ===== MINER DATA (Try SQLite first) =====
    try:
        with DB_MANAGER.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT
                    (mm.timestamp / ?) * ? AS bucket_ts,
                    AVG(mm.ghs_avg), AVG(mm.ghs_5s), AVG(mm.ghs_30m),
                    AVG(COALESCE(mt.temp_max, 0)), AVG(COALESCE(mt.temp1, 0)),
                    AVG(COALESCE(mt.temp2, 0)), AVG(COALESCE(mt.temp3, 0)),
                    AVG(COALESCE(mf.fan1, 0)), AVG(COALESCE(mf.fan2, 0)),
                    AVG(COALESCE(mf.fan3, 0)), AVG(COALESCE(mf.fan4, 0)),
                    AVG(mm.watt_actual), AVG(mm.efficiency_jt)
                FROM miner_metrics mm
                LEFT JOIN miner_temperatures mt ON mm.timestamp = mt.timestamp AND mt.miner_id = 1
                LEFT JOIN miner_fans mf ON mm.timestamp = mf.timestamp AND mf.miner_id = 1
                WHERE mm.timestamp >= ?
                GROUP BY (mm.timestamp / ?)
                ORDER BY bucket_ts ASC
            """, (bucket_size, bucket_size, cutoff_epoch, bucket_size))

            rows = cursor.fetchall()

            if rows:
                labels, ghs_avg_list, ghs_5s_list, ghs_30m_list = [], [], [], []
                temp_max_list, temp_lists, fan_lists = [], [[], [], []], [[], [], [], []]
                watt_list, jt_list = [], []

                for row in rows:
                    labels.append(epoch_to_csv_timestamp(row[0]))
                    ghs_avg_list.append(float(row[1]))
                    ghs_5s_list.append(float(row[2]))
                    ghs_30m_list.append(float(row[3]))
                    temp_max_list.append(round(float(row[4])))
                    for i in range(3):
                        temp_lists[i].append(round(float(row[5 + i])))
                    for i in range(4):
                        fan_lists[i].append(round(float(row[8 + i])))
                    watt_list.append(round(float(row[12]), 1) if row[12] is not None else None)
                    jt_list.append(round(float(row[13]), 2) if row[13] is not None else None)

                if labels:
                    data['miner']['labels'] = labels
                    data['miner']['ghs_avg'] = ghs_avg_list
                    data['miner']['ghs_5s'] = ghs_5s_list
                    data['miner']['ghs_30m'] = ghs_30m_list
                    data['miner']['temp_max'] = temp_max_list
                    for i, tl in enumerate(temp_lists):
                        data['miner'][f'temp{i+1}'] = tl
                    for i, fl in enumerate(fan_lists):
                        data['miner'][f'fan{i+1}'] = fl
                    data['miner']['watt_actual'] = watt_list
                    data['miner']['efficiency_jt'] = jt_list

    except Exception as e:
        app.logger.warning(f"SQLite query failed for miner history: {e}")

    # CSV Fallback for miner data
    if not data['miner']:
        try:
            if os.path.exists(MINER_CSV):
                with open(MINER_CSV, 'r') as f:
                    reader = csv.DictReader(f)
                    rows = []
                    for i, row in enumerate(reader):
                        try:
                            ts = datetime.strptime(row['timestamp'], '%Y-%m-%d %H:%M:%S')
                            if ts >= cutoff and i % downsample == 0:
                                rows.append(row)
                        except (ValueError, KeyError):
                            continue

                    if rows:
                        data['miner']['labels'] = [row['timestamp'] for row in rows]
                        data['miner']['ghs_avg'] = [float(row.get('ghs_avg', 0)) for row in rows]
                        data['miner']['ghs_5s'] = [float(row.get('ghs_5s', 0)) for row in rows]
                        data['miner']['ghs_30m'] = [float(row.get('ghs_30m', 0)) for row in rows]
                        data['miner']['temp_max'] = [int(row.get('temp_max', 0)) for row in rows]
                        for i in range(1, 4):
                            data['miner'][f'temp{i}'] = [int(row.get(f'temp{i}', 0)) for row in rows]
                        for i in range(1, 5):
                            data['miner'][f'fan{i}'] = [int(row.get(f'fan{i}', 0)) for row in rows]
        except Exception as e:
            app.logger.error(f"Error reading miner CSV fallback: {e}")

    # ===== SENSOR DATA (Try SQLite first) =====
    try:
        with DB_MANAGER.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT
                    (timestamp / ?) * ? AS bucket_ts,
                    AVG(temperature_c), AVG(humidity_pct)
                FROM sensor_readings
                WHERE timestamp >= ?
                GROUP BY (timestamp / ?)
                ORDER BY bucket_ts ASC
            """, (bucket_size, bucket_size, cutoff_epoch, bucket_size))

            rows = cursor.fetchall()

            if rows:
                data['sensor']['labels'] = [epoch_to_csv_timestamp(row[0]) for row in rows]
                data['sensor']['temperature'] = [float(row[1]) for row in rows]
                data['sensor']['humidity'] = [float(row[2]) for row in rows]

    except Exception as e:
        app.logger.warning(f"SQLite query failed for sensor history: {e}")

    # CSV Fallback for sensor data
    if not data['sensor']:
        try:
            if os.path.exists(SENSOR_CSV):
                with open(SENSOR_CSV, 'r') as f:
                    reader = csv.DictReader(f)
                    rows = []
                    for i, row in enumerate(reader):
                        try:
                            ts = datetime.strptime(row['timestamp'], '%Y-%m-%d %H:%M:%S')
                            if ts >= cutoff and i % downsample == 0:
                                rows.append(row)
                        except (ValueError, KeyError):
                            continue

                    if rows:
                        data['sensor']['labels'] = [row['timestamp'] for row in rows]
                        data['sensor']['temperature'] = [float(row.get('temperature', 0)) for row in rows]
                        data['sensor']['humidity'] = [float(row.get('humidity', 0)) for row in rows]
        except Exception as e:
            app.logger.error(f"Error reading sensor CSV fallback: {e}")

    return jsonify(data)

@app.route('/api/watchdog')
@login_required
def api_watchdog():
    """Return watchdog state: last auto-reboot timestamp and total reboot count."""
    state_file = os.path.join(DATA_DIR, 'watchdog_state.json')
    if not os.path.exists(state_file):
        return jsonify({'last_reboot_ts': None, 'reboot_count': 0, 'last_reboot_str': 'Never'})
    try:
        with open(state_file) as f:
            state = json.load(f)
        last_ts = state.get('last_reboot_ts')
        if last_ts:
            last_reboot_str = datetime.fromtimestamp(last_ts).strftime('%Y-%m-%d %H:%M')
        else:
            last_reboot_str = 'Never'
        return jsonify({
            'last_reboot_ts': last_ts,
            'reboot_count': state.get('reboot_count', 0),
            'last_reboot_str': last_reboot_str,
        })
    except Exception as e:
        app.logger.error(f"Error reading watchdog state: {e}")
        return jsonify({'last_reboot_ts': None, 'reboot_count': 0, 'last_reboot_str': 'Never'})


# ===== Setup Mode =====
def setup_credentials():
    """Generate dashboard_config.py with hashed credentials"""
    print("\n=== Bitcoin Mining Dashboard Setup ===\n")

    username = "admin"
    password = getpass.getpass("Set dashboard password: ")

    if len(password) < 6:
        print("Error: Password must be at least 6 characters")
        return False

    # Generate salt and hash
    salt = secrets.token_bytes(16)
    password_hash = hashlib.pbkdf2_hmac('sha256', password.encode(), salt, 260000)
    secret_key = secrets.token_bytes(32)

    config_content = f'''# Generated dashboard configuration
# DO NOT EDIT MANUALLY
USERNAME = "{username}"
PASSWORD_HASH = "{password_hash.hex()}"
PASSWORD_SALT = "{salt.hex()}"
SECRET_KEY = "{secret_key.hex()}"
PORT = 5000
'''

    config_path = '/home/bilthon/mining_monitor/dashboard_config.py'
    with open(config_path, 'w') as f:
        f.write(config_content)

    os.chmod(config_path, 0o600)
    print(f"[OK] Configuration saved to {config_path}")
    print(f"[OK] Username: {username}")
    print("[OK] Password is hashed and cannot be recovered")
    print("\nYou can now run: python dashboard.py")
    return True

if __name__ == '__main__':
    if '--setup' in sys.argv:
        if setup_credentials():
            sys.exit(0)
        else:
            sys.exit(1)

    # Load config
    if not load_config():
        print("Error: dashboard_config.py not found!")
        print("Run: python dashboard.py --setup")
        sys.exit(1)

    print("Starting Bitcoin Mining Dashboard on http://127.0.0.1:5000")
    app.run(host='127.0.0.1', port=5000, debug=False)
