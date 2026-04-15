#!/usr/bin/env python3
"""
Probe Antminer T21 web UI for available endpoints and power metrics.
Tests common Bitmain endpoints to discover what data is exposed.
"""

import requests
import json
from typing import Optional, Dict, Any
from urllib.parse import urljoin

# Antminer T21 web UI (adjust IP if needed)
BASE_URL = "http://192.168.18.7"
TIMEOUT = 5

# Common Antminer API endpoints to test
ENDPOINTS = [
    "/api/system/info",           # BitAxe-style (unlikely but worth checking)
    "/api/config",                # Configuration
    "/api/pools",                 # Pool info
    "/api/stats",                 # Mining stats
    "/api/summary",               # Mining summary
    "/api/devs",                  # Device info
    "/api/miner_get_status",      # Status endpoint
    "/api/miner_status",          # Alt status
    "/api/get_system_info",       # System info
    "/api/power",                 # Power-specific endpoint
    "/cgi-bin/api_command.cgi",   # CGI interface
    "/status",                    # Status page
    "/index.html",                # Main page
]

# Parameters to try with some endpoints
PARAMS_TO_TRY = [
    {},
    {"command": "stats"},
    {"command": "summary"},
    {"command": "devs"},
]


def test_endpoint(endpoint: str, params: Optional[Dict] = None) -> Optional[Dict[str, Any]]:
    """Test a single endpoint and return parsed JSON if successful."""
    url = urljoin(BASE_URL, endpoint)
    try:
        resp = requests.get(url, params=params, timeout=TIMEOUT)
        if resp.status_code == 200:
            try:
                return resp.json()
            except json.JSONDecodeError:
                # Not JSON, but endpoint exists
                return {"_raw_response": resp.text[:200]}
        return None
    except Exception as e:
        return None


def print_response(endpoint: str, params: Optional[Dict], data: Dict[str, Any]) -> None:
    """Pretty print an endpoint response."""
    param_str = f" (params: {params})" if params else ""
    print(f"\n[OK] {endpoint}{param_str}")
    print("-" * 60)

    if "_raw_response" in data:
        print(f"[Raw HTML/Text Response]\n{data['_raw_response']}")
    else:
        print(json.dumps(data, indent=2))


def search_for_power_data(data: Dict[str, Any], path: str = "") -> list:
    """Recursively search for power-related keys in nested dict."""
    power_keys = []
    power_keywords = ["power", "watts", "current", "voltage", "efficiency", "j/w", "j/th", "consumption"]

    if isinstance(data, dict):
        for key, value in data.items():
            key_lower = key.lower()
            if any(keyword in key_lower for keyword in power_keywords):
                full_path = f"{path}.{key}" if path else key
                power_keys.append((full_path, value))

            if isinstance(value, (dict, list)):
                power_keys.extend(search_for_power_data(value, f"{path}.{key}" if path else key))

    elif isinstance(data, list):
        for i, item in enumerate(data):
            power_keys.extend(search_for_power_data(item, f"{path}[{i}]"))

    return power_keys


def main():
    """Probe all endpoints and report findings."""
    print("=" * 60)
    print("Antminer T21 Web UI Endpoint Probe")
    print("=" * 60)
    print(f"Target: {BASE_URL}")
    print(f"Testing {len(ENDPOINTS)} endpoints...\n")

    successful_endpoints = []
    power_data_found = []

    for endpoint in ENDPOINTS:
        print(f"Testing: {endpoint}...", end=" ", flush=True)

        # Try without params first
        data = test_endpoint(endpoint)
        if data:
            print("[OK]")
            successful_endpoints.append((endpoint, data, {}))

            # Search for power-related data
            power_keys = search_for_power_data(data)
            if power_keys:
                power_data_found.extend([(endpoint, key, value) for key, value in power_keys])
        else:
            # Try with various params
            found = False
            for params in PARAMS_TO_TRY[1:]:  # Skip empty dict (already tried)
                data = test_endpoint(endpoint, params)
                if data:
                    print(f"[OK] (with {params})")
                    successful_endpoints.append((endpoint, data, params))
                    power_keys = search_for_power_data(data)
                    if power_keys:
                        power_data_found.extend([(endpoint, key, value) for key, value in power_keys])
                    found = True
                    break

            if not found:
                print("[FAIL]")

    # Print successful endpoints
    print("\n" + "=" * 60)
    print(f"SUCCESSFUL ENDPOINTS ({len(successful_endpoints)})")
    print("=" * 60)

    for endpoint, data, params in successful_endpoints[:5]:  # Limit to first 5
        print_response(endpoint, params if params else None, data)

    if len(successful_endpoints) > 5:
        print(f"\n... and {len(successful_endpoints) - 5} more endpoints")

    # Report power-related data
    print("\n" + "=" * 60)
    print(f"POWER-RELATED DATA FOUND ({len(power_data_found)})")
    print("=" * 60)

    if power_data_found:
        for endpoint, key_path, value in power_data_found:
            print(f"  {endpoint}: {key_path} = {value}")
    else:
        print("  [None found - power data may not be exposed via web UI]")

    # Summary
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    print(f"Endpoints responding: {len(successful_endpoints)}/{len(ENDPOINTS)}")
    print(f"Power metrics found: {len(power_data_found)}")

    if not power_data_found:
        print("\n[WARNING] No power metrics found in web UI.")
        print("Next steps:")
        print("  1. Check if Antminer has SSH access for direct monitoring")
        print("  2. Install external power meter (INA260/smart PDU)")
        print("  3. Query PSU directly if it has network interface")
    else:
        print("\n[SUCCESS] Power data available! Details above.")


if __name__ == "__main__":
    main()
