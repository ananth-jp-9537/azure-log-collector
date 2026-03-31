#!/usr/bin/env python3
"""End-to-end test: mock S247 server → Function App pipeline → verify uploads.

Tests the full flow:
  1. Start mock S247 server
  2. Call get_supported_log_types() — verify supported types returned
  3. Call create_log_type() per category — verify sourceConfig built correctly
  4. Parse sample Azure diagnostic blobs with the config
  5. Call post_logs() — verify gzipped data arrives at mock server
  6. Check mock server /mock/status for upload confirmation

Usage:
    python3 test_e2e.py [--port PORT]

Prerequisites:
    cd function-app && pip install -r requirements.txt  (for azure SDK stubs)
    The mock server is started automatically by this script.
"""

import os
import sys
import json
import time
import signal
import argparse
import subprocess
import urllib.request
from pathlib import Path

# Add function-app to path so we can import shared modules
FUNC_APP_DIR = Path(__file__).parent.parent / "function-app"
sys.path.insert(0, str(FUNC_APP_DIR))

MOCK_PORT = 8999
MOCK_PROCESS = None
SCRIPT_DIR = Path(__file__).parent

# ANSI colors
GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
CYAN = "\033[96m"
RESET = "\033[0m"
BOLD = "\033[1m"


def log(msg, color=RESET):
    print(f"{color}{msg}{RESET}")


def log_pass(test_name):
    log(f"  ✅ {test_name}", GREEN)


def log_fail(test_name, reason=""):
    log(f"  ❌ {test_name}: {reason}", RED)


def log_section(title):
    log(f"\n{BOLD}{'─' * 60}", CYAN)
    log(f"  {title}", CYAN)
    log(f"{'─' * 60}{RESET}", CYAN)


def start_mock_server(port):
    """Start the mock S247 server as a subprocess."""
    global MOCK_PROCESS
    server_script = SCRIPT_DIR / "mock_s247_server.py"
    MOCK_PROCESS = subprocess.Popen(
        [sys.executable, str(server_script), "--port", str(port)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    # Wait for server to be ready
    for _ in range(20):
        try:
            urllib.request.urlopen(f"http://localhost:{port}/mock/status", timeout=1)
            return True
        except Exception:
            time.sleep(0.25)
    return False


def stop_mock_server():
    """Stop the mock server."""
    global MOCK_PROCESS
    if MOCK_PROCESS:
        MOCK_PROCESS.terminate()
        MOCK_PROCESS.wait(timeout=5)
        MOCK_PROCESS = None


def http_get(url):
    """Simple HTTP GET returning parsed JSON."""
    resp = urllib.request.urlopen(url, timeout=10)
    return json.loads(resp.read().decode())


def setup_env(port):
    """Set environment variables for the function app modules."""
    os.environ["SITE24X7_BASE_URL"] = f"http://localhost:{port}"
    os.environ["SITE24X7_UPLOAD_DOMAIN"] = f"http://localhost:{port}"
    os.environ["SITE24X7_API_KEY"] = "mock-device-key"


def test_supported_log_types(port):
    """Test 1: Get supported log types via mock server."""
    log_section("Test 1: GET /applog/azure/logtype_supported")

    from shared.site24x7_client import Site24x7Client
    client = Site24x7Client()

    result = client.get_supported_log_types()
    passed = 0
    failed = 0

    if result is None:
        log_fail("get_supported_log_types returned None")
        return 0, 1

    if result.get("status") == "SUCCESS":
        log_pass("Status is SUCCESS")
        passed += 1
    else:
        log_fail("Status check", f"got {result.get('status')}")
        failed += 1

    types = result.get("supported_types", [])
    if len(types) >= 5:
        log_pass(f"Got {len(types)} supported types")
        passed += 1
    else:
        log_fail("Type count", f"expected >= 5, got {len(types)}")
        failed += 1

    type_names = [t["logtype"] for t in types]
    for expected in ["auditlogs", "functionapplogs", "appservicehttplogs"]:
        if expected in type_names:
            log_pass(f"Contains '{expected}'")
            passed += 1
        else:
            log_fail(f"Missing '{expected}'", f"available: {type_names}")
            failed += 1

    return passed, failed


def test_create_log_type(port):
    """Test 2: Create/check log types via /applog/logtype."""
    log_section("Test 2: GET /applog/logtype (auto-create)")

    from shared.site24x7_client import Site24x7Client
    client = Site24x7Client()

    categories = ["auditlogs", "FunctionAppLogs", "AppServiceHTTPLogs", "unknowntype"]
    passed = 0
    failed = 0

    for category in categories:
        config = client.create_log_type(category)

        if config is None:
            log_fail(f"create_log_type('{category}') returned None")
            failed += 1
            continue

        # Check required fields
        required = ["apiKey", "logType", "uploadDomain", "dateField", "dateFormat", "jsonPath"]
        missing = [f for f in required if f not in config]
        if missing:
            log_fail(f"'{category}' missing fields", str(missing))
            failed += 1
            continue

        if config["apiKey"] != "mock-device-key":
            log_fail(f"'{category}' apiKey", f"got {config['apiKey']}")
            failed += 1
            continue

        if f"localhost:{port}" not in config["uploadDomain"]:
            log_fail(f"'{category}' uploadDomain", f"got {config['uploadDomain']}")
            failed += 1
            continue

        if not isinstance(config["jsonPath"], list) or len(config["jsonPath"]) == 0:
            log_fail(f"'{category}' jsonPath", "empty or not a list")
            failed += 1
            continue

        log_pass(f"create_log_type('{category}') → logType={config['logType']}, {len(config['jsonPath'])} fields")
        passed += 1

    return passed, failed


def test_create_log_types_batch(port):
    """Test 3: Batch create via create_log_types()."""
    log_section("Test 3: Batch create_log_types()")

    from shared.site24x7_client import Site24x7Client
    client = Site24x7Client()

    categories = ["auditlogs", "functionapplogs"]
    results = client.create_log_types(categories)
    passed = 0
    failed = 0

    if results is None:
        log_fail("create_log_types returned None")
        return 0, 1

    if len(results) == 2:
        log_pass(f"Got {len(results)} results for {len(categories)} categories")
        passed += 1
    else:
        log_fail("Result count", f"expected 2, got {len(results)}")
        failed += 1

    for r in results:
        cat = r.get("category", "")
        config = r.get("sourceConfig", {})

        if cat.startswith("S247_"):
            log_pass(f"Category prefix correct: {cat}")
            passed += 1
        else:
            log_fail(f"Category prefix", f"expected S247_*, got {cat}")
            failed += 1

        if isinstance(config, dict) and "apiKey" in config:
            log_pass(f"{cat} has valid sourceConfig dict")
            passed += 1
        else:
            log_fail(f"{cat} sourceConfig", "not a valid dict")
            failed += 1

    return passed, failed


def test_parse_and_upload(port):
    """Test 4: Parse sample blobs and upload to mock server."""
    log_section("Test 4: Parse sample blobs → post_logs()")

    from shared.site24x7_client import Site24x7Client
    import base64
    client = Site24x7Client()

    passed = 0
    failed = 0

    # Test each sample blob
    sample_files = {
        "auditlogs": SCRIPT_DIR / "sample_blobs" / "insights-logs-auditlogs.json",
        "functionapplogs": SCRIPT_DIR / "sample_blobs" / "insights-logs-functionapplogs.json",
        "appservicehttplogs": SCRIPT_DIR / "sample_blobs" / "insights-logs-appservicehttplogs.json",
    }

    for category, blob_path in sample_files.items():
        if not blob_path.exists():
            log_fail(f"Sample blob missing: {blob_path}")
            failed += 1
            continue

        # Step 1: Get config from mock server
        config = client.create_log_type(category)
        if not config:
            log_fail(f"No config for '{category}'")
            failed += 1
            continue

        # Step 2: Load sample blob
        with open(blob_path) as f:
            blob_data = json.load(f)
        records = blob_data.get("records", [])
        log(f"  📄 Loaded {len(records)} records from {blob_path.name}")

        # Step 3: Base64-encode the config (as BlobLogProcessor does)
        config_b64 = base64.b64encode(json.dumps(config).encode()).decode()

        # Step 4: Upload
        success = client.post_logs(config_b64, records)

        if success:
            log_pass(f"post_logs('{category}') succeeded — {len(records)} records uploaded")
            passed += 1
        else:
            log_fail(f"post_logs('{category}') failed")
            failed += 1

    return passed, failed


def test_mock_server_received(port):
    """Test 5: Verify mock server received the uploads."""
    log_section("Test 5: Verify mock server received uploads")

    status = http_get(f"http://localhost:{port}/mock/status")
    passed = 0
    failed = 0

    upload_count = status.get("uploads_received", 0)
    if upload_count >= 3:
        log_pass(f"Mock server received {upload_count} uploads")
        passed += 1
    else:
        log_fail("Upload count", f"expected >= 3, got {upload_count}")
        failed += 1

    # Check details
    for detail in status.get("upload_details", []):
        lt = detail.get("log_type", "")
        rc = detail.get("record_count", 0)
        log(f"    📦 {lt}: {rc} records, {detail.get('size_bytes', 0)} bytes")

    created = status.get("created_logtypes", [])
    if len(created) >= 3:
        log_pass(f"Created {len(created)} log types: {created}")
        passed += 1
    else:
        log_fail("Created types", f"expected >= 3, got {created}")
        failed += 1

    # Verify individual uploads have actual record data
    uploads = http_get(f"http://localhost:{port}/mock/uploads")
    for upload in uploads.get("uploads", []):
        records = upload.get("records", [])
        lt = upload.get("log_type", "unknown")
        if records and len(records) > 0:
            first = records[0]
            has_timestamp = "_zl_timestamp" in first
            has_fields = len(first) >= 3
            if has_timestamp and has_fields:
                log_pass(f"Upload '{lt}': parsed correctly, has _zl_timestamp + {len(first)} fields")
                passed += 1
            else:
                log_fail(f"Upload '{lt}'", f"timestamp={has_timestamp}, fields={len(first)}")
                failed += 1

    return passed, failed


def main():
    parser = argparse.ArgumentParser(description="E2E test for S247 diagnostic logs pipeline")
    parser.add_argument("--port", type=int, default=MOCK_PORT, help="Mock server port")
    args = parser.parse_args()

    log(f"\n{BOLD}╔══════════════════════════════════════════════════════════╗", CYAN)
    log(f"║  Site24x7 Diagnostic Logs — End-to-End Test              ║", CYAN)
    log(f"╚══════════════════════════════════════════════════════════╝{RESET}", CYAN)

    # Start mock server
    log(f"\n🚀 Starting mock S247 server on port {args.port}...")
    if not start_mock_server(args.port):
        log("❌ Failed to start mock server!", RED)
        sys.exit(1)
    log("✅ Mock server ready\n", GREEN)

    # Set up env
    setup_env(args.port)

    total_passed = 0
    total_failed = 0

    try:
        # Run tests
        for test_fn in [
            test_supported_log_types,
            test_create_log_type,
            test_create_log_types_batch,
            test_parse_and_upload,
            test_mock_server_received,
        ]:
            p, f = test_fn(args.port)
            total_passed += p
            total_failed += f

    finally:
        stop_mock_server()

    # Summary
    log(f"\n{BOLD}{'═' * 60}", CYAN)
    total = total_passed + total_failed
    if total_failed == 0:
        log(f"  ✅ ALL {total} CHECKS PASSED", GREEN)
    else:
        log(f"  Results: {total_passed} passed, {total_failed} failed (of {total})",
            GREEN if total_failed == 0 else RED)
    log(f"{'═' * 60}{RESET}\n", CYAN)

    sys.exit(1 if total_failed > 0 else 0)


if __name__ == "__main__":
    main()
