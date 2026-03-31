#!/usr/bin/env python3
"""Mock / Proxy Site24x7 server for local end-to-end testing.

Two modes:

  MOCK MODE (default):
    Returns hardcoded responses for supported log types, log type config,
    and upload endpoints — no real Site24x7 needed.

  PROXY MODE (--proxy TARGET_URL):
    Acts as a reverse proxy, forwarding every request to your local
    Site24x7 build (Docker or native) and returning the real response.
    This lets Azure Functions reach your local build via ngrok:
      Azure Function → ngrok → this proxy → local Site24x7 build

Usage:
    # Mock mode
    python3 mock_s247_server.py [--port PORT]

    # Proxy mode — forward to Docker build
    python3 mock_s247_server.py --proxy https://localhost:9443

    # Proxy mode — forward to native build
    python3 mock_s247_server.py --proxy https://your-internal-server.example.com:8443

Then set on Azure Function App:
    SITE24X7_BASE_URL=<ngrok-url>
    SITE24X7_UPLOAD_DOMAIN=<ngrok-url>
    SITE24X7_API_KEY=<your-real-device-key>
"""

import gzip
import json
import ssl
import argparse
import logging
import urllib.request
import urllib.error
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, urlencode

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# ─── Global Mode ─────────────────────────────────────────────────────────
PROXY_TARGET = None          # Set by --proxy flag; None = mock mode
SSL_CONTEXT = None           # Permissive SSL context for self-signed certs

# ─── Sample Azure Log Type Configs ───────────────────────────────────────
# These mirror what the real Site24x7 server returns from azureLogTypes.json.

SUPPORTED_TYPES = {
    "auditlogs": {
        "display_name": "AuditLogs",
        "log_categories": ["AuditLogs"],
        "fields": [
            {"name": "AADOperationType"},
            {"name": "AADTenantId"},
            {"name": "ActivityDisplayName"},
            {"name": "AdditionalDetails", "type": "json-object"},
            {"name": "Id"},
            {"name": "InitiatedBy", "type": "json-object"},
            {"name": "LoggedByService"},
            {"name": "Resource"},
            {"name": "ResourceGroup"},
            {"name": "ResourceId"},
            {"name": "Result"},
            {"name": "ResultDescription"},
            {"name": "ResultReason"},
            {"name": "SourceSystem"},
            {"name": "TargetResources", "type": "json-object"},
        ],
    },
    "functionapplogs": {
        "display_name": "FunctionAppLogs",
        "log_categories": ["FunctionAppLogs"],
        "fields": [
            {"name": "ActivityId"},
            {"name": "ExceptionDetails"},
            {"name": "ExceptionMessage"},
            {"name": "ExceptionType"},
            {"name": "FunctionInvocationId"},
            {"name": "FunctionName"},
            {"name": "HostInstanceId"},
            {"name": "HostVersion"},
            {"name": "Level"},
            {"name": "Message"},
            {"name": "ProcessId", "type": "number"},
        ],
    },
    "appservicehttplogs": {
        "display_name": "AppServiceHTTPLogs",
        "log_categories": ["AppServiceHTTPLogs"],
        "fields": [
            {"name": "CIp"},
            {"name": "CsBytes", "type": "number"},
            {"name": "CsHost"},
            {"name": "CsMethod"},
            {"name": "CsUriStem"},
            {"name": "Result"},
            {"name": "ScBytes", "type": "number"},
            {"name": "ScStatus", "type": "number"},
            {"name": "ScSubStatus"},
            {"name": "SPort"},
            {"name": "TimeTaken", "type": "number"},
            {"name": "UserAgent"},
        ],
    },
    "networksecuritygroupflowevent": {
        "display_name": "Azure NSG Flow Logs",
        "log_categories": ["NetworkSecurityGroupFlowEvent"],
        "fields": [
            {"name": "FlowLogResourceId"},
            {"name": "FlowLogVersion", "type": "number"},
            {"name": "MacAddress"},
            {"name": "Rule"},
            {"name": "FlowState"},
            {"name": "SrcIP"},
            {"name": "DstIP"},
            {"name": "SrcPort", "type": "number"},
            {"name": "DstPort", "type": "number"},
            {"name": "Protocol"},
            {"name": "TrafficDecision"},
            {"name": "TrafficFlow"},
        ],
    },
    "containerregistryloginevents": {
        "display_name": "ContainerRegistryLoginEvents",
        "log_categories": ["ContainerRegistryLoginEvents"],
        "fields": [
            {"name": "LoginServer"},
            {"name": "Identity"},
            {"name": "ResultDescription"},
            {"name": "ResultType"},
            {"name": "OperationName"},
            {"name": "CallerIpAddress"},
        ],
    },
}

# Auto-created log types per user (simulates server state)
_created_logtypes = {}

# Log uploads received
_uploads_received = []


def _build_json_path(fields):
    """Build jsonPath array from field definitions (same as server does)."""
    json_path = [
        {"name": "time", "key": "time"},
        {"name": "resourceId", "key": "resourceId"},
        {"name": "operationName", "key": "operationName"},
        {"name": "category", "key": "category"},
        {"name": "resultType", "key": "resultType"},
        {"name": "level", "key": "level"},
    ]
    for field in fields:
        name = field["name"]
        entry = {"name": name, "key": f"properties.{name}"}
        if "type" in field:
            entry["type"] = field["type"]
        json_path.append(entry)
    return json_path


def _build_logtype_response(logtype_key, config):
    """Build the response for /applog/logtype (mimics getConfigDetailsForApiUpload)."""
    return {
        "status": "SUCCESS",
        "apiUpload": True,
        "logType": logtype_key,
        "dateField": "time",
        "dateFormat": "yyyy-MM-dd'T'HH:mm:ss.SSSZ",
        "json_path": _build_json_path(config["fields"]),
    }


# ─── Proxy Request Log ───────────────────────────────────────────────────
_proxy_log = []   # Stores request/response summaries in proxy mode


def _forward_request(method, path, headers_dict, body=None):
    """Forward a request to the real Site24x7 build and return (status, headers, body).

    Args:
        method: HTTP method (GET, POST, etc.)
        path: Full path + query string (e.g., /applog/logtype?logType=foo)
        headers_dict: Dict of request headers to forward
        body: Raw bytes for POST body, or None for GET

    Returns:
        Tuple of (status_code, response_headers_dict, response_body_bytes)
    """
    target_url = f"{PROXY_TARGET}{path}"
    logger.info("  ↗ PROXY %s %s", method, target_url)

    # Build request — forward relevant headers
    req = urllib.request.Request(target_url, data=body, method=method)
    skip_headers = {"host", "connection", "transfer-encoding", "content-length"}
    for key, val in headers_dict.items():
        if key.lower() not in skip_headers:
            req.add_header(key, val)
    if body is not None:
        req.add_header("Content-Length", str(len(body)))

    try:
        resp = urllib.request.urlopen(req, context=SSL_CONTEXT, timeout=30)
        resp_body = resp.read()
        resp_headers = dict(resp.headers)
        status = resp.status
        logger.info("  ↙ PROXY %d (%d bytes)", status, len(resp_body))
        return status, resp_headers, resp_body
    except urllib.error.HTTPError as e:
        resp_body = e.read()
        resp_headers = dict(e.headers)
        logger.warning("  ↙ PROXY %d (%d bytes)", e.code, len(resp_body))
        return e.code, resp_headers, resp_body
    except Exception as e:
        logger.error("  ✖ PROXY error: %s", e)
        error_body = json.dumps({"proxy_error": str(e)}).encode()
        return 502, {"Content-Type": "application/json"}, error_body


class MockS247Handler(BaseHTTPRequestHandler):
    """HTTP handler — mock mode or reverse-proxy mode."""

    def log_message(self, format, *args):
        mode = "PROXY" if PROXY_TARGET else "MOCK"
        logger.info("[%s] %s %s", mode, self.command, self.path)

    def _send_json(self, data, status=200):
        body = json.dumps(data, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self):
        """Read raw request body bytes."""
        content_length = int(self.headers.get("Content-Length", 0))
        if content_length > 0:
            return self.rfile.read(content_length)
        return None

    def _get_headers_dict(self):
        """Convert self.headers to a plain dict."""
        return {k: v for k, v in self.headers.items()}

    def _proxy_and_respond(self, method):
        """Forward request to target and relay response back to caller."""
        body = self._read_body() if method in ("POST", "PUT", "PATCH") else None
        headers = self._get_headers_dict()

        status, resp_headers, resp_body = _forward_request(
            method, self.path, headers, body
        )

        # Log for debug endpoint
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "method": method,
            "path": self.path,
            "status": status,
            "request_size": len(body) if body else 0,
            "response_size": len(resp_body),
        }
        # Try to capture response body summary
        try:
            resp_text = resp_body.decode("utf-8", errors="replace")[:500]
            entry["response_preview"] = resp_text
        except Exception:
            entry["response_preview"] = f"<binary {len(resp_body)} bytes>"
        _proxy_log.append(entry)
        if len(_proxy_log) > 100:
            _proxy_log.pop(0)

        # Relay response
        self.send_response(status)
        skip_resp = {"transfer-encoding", "connection", "content-length", "content-encoding"}
        for key, val in resp_headers.items():
            if key.lower() not in skip_resp:
                self.send_header(key, val)
        self.send_header("Content-Length", str(len(resp_body)))
        self.end_headers()
        self.wfile.write(resp_body)

    # ─── Debug endpoints (always available, both modes) ───────────────────

    def _handle_debug_endpoint(self, path):
        """Handle /mock/* debug endpoints. Returns True if handled."""
        if path == "/mock/status":
            if PROXY_TARGET:
                self._send_json({
                    "mode": "proxy",
                    "target": PROXY_TARGET,
                    "requests_proxied": len(_proxy_log),
                    "recent_requests": [
                        {k: v for k, v in e.items() if k != "response_preview"}
                        for e in _proxy_log[-20:]
                    ],
                })
            else:
                self._send_json({
                    "mode": "mock",
                    "created_logtypes": list(_created_logtypes.keys()),
                    "uploads_received": len(_uploads_received),
                    "upload_details": [
                        {
                            "log_type": u["log_type"],
                            "record_count": u["record_count"],
                            "size_bytes": u["size_bytes"],
                            "timestamp": u["timestamp"],
                        }
                        for u in _uploads_received[-20:]
                    ],
                })
            return True

        if path == "/mock/uploads":
            if PROXY_TARGET:
                self._send_json({
                    "mode": "proxy",
                    "count": len(_proxy_log),
                    "recent": _proxy_log[-10:],
                })
            else:
                self._send_json({
                    "mode": "mock",
                    "count": len(_uploads_received),
                    "uploads": _uploads_received[-5:],
                })
            return True

        if path == "/mock/reset":
            _created_logtypes.clear()
            _uploads_received.clear()
            _proxy_log.clear()
            logger.info("  → State reset")
            self._send_json({"status": "reset"})
            return True

        return False

    # ─── GET ──────────────────────────────────────────────────────────────

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        # Debug endpoints — always handled locally
        if path.startswith("/mock/"):
            if self._handle_debug_endpoint(path):
                return

        # ── PROXY MODE: forward everything else ──
        if PROXY_TARGET:
            self._proxy_and_respond("GET")
            return

        # ── MOCK MODE: hardcoded responses ──
        params = parse_qs(parsed.query)

        if path == "/applog/azure/logtype_supported":
            device_key = params.get("deviceKey", [""])[0]
            if not device_key:
                self._send_json({"status": "ERROR", "message": "Invalid DeviceKey"}, 400)
                return

            types_array = []
            for key, config in SUPPORTED_TYPES.items():
                entry = {"logtype": key, "display_name": config["display_name"]}
                if "log_categories" in config:
                    entry["log_categories"] = config["log_categories"]
                types_array.append(entry)

            logger.info("  → Returning %d supported log types", len(types_array))
            self._send_json({"status": "SUCCESS", "supported_types": types_array})
            return

        if path == "/applog/logtype":
            device_key = params.get("deviceKey", [""])[0]
            log_type = params.get("logType", [""])[0]

            if not device_key:
                self._send_json({"status": "ERROR", "message": "Invalid DeviceKey"}, 400)
                return
            if not log_type:
                self._send_json({"status": "ERROR", "message": "logType parameter required"}, 400)
                return

            normalized = log_type.replace("-", "").replace("_", "").replace(" ", "").lower()

            if normalized in SUPPORTED_TYPES:
                config = SUPPORTED_TYPES[normalized]
                _created_logtypes[normalized] = True
                logger.info("  → Auto-created Azure log type '%s' (%s)", normalized, config["display_name"])
                self._send_json(_build_logtype_response(normalized, config))
                return

            for key, config in SUPPORTED_TYPES.items():
                for cat in config.get("log_categories", []):
                    cat_norm = cat.replace("-", "").replace("_", "").replace(" ", "").lower()
                    if cat_norm == normalized:
                        _created_logtypes[key] = True
                        logger.info("  → Auto-created Azure log type '%s' via category '%s'", key, cat)
                        self._send_json(_build_logtype_response(key, config))
                        return

            logger.info("  → Unknown type '%s', returning generic config", normalized)
            generic_fields = [
                {"name": "resultDescription"},
                {"name": "resultType"},
                {"name": "properties", "type": "json-object"},
            ]
            self._send_json({
                "status": "SUCCESS",
                "apiUpload": True,
                "logType": normalized,
                "dateField": "time",
                "dateFormat": "yyyy-MM-dd'T'HH:mm:ss.SSSZ",
                "json_path": _build_json_path(generic_fields),
            })
            return

        self.send_error(404, f"Unknown path: {path}")

    # ─── POST ─────────────────────────────────────────────────────────────

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path

        # ── PROXY MODE: forward everything ──
        if PROXY_TARGET:
            self._proxy_and_respond("POST")
            return

        # ── MOCK MODE ──
        if path == "/upload":
            content_length = int(self.headers.get("Content-Length", 0))
            content_encoding = self.headers.get("Content-Encoding", "")
            log_type = self.headers.get("X-LogType", "unknown")
            device_key = self.headers.get("X-DeviceKey", "unknown")
            log_size = self.headers.get("Log-Size", "0")

            raw_data = self.rfile.read(content_length)

            records = []
            try:
                if content_encoding == "gzip":
                    decompressed = gzip.decompress(raw_data)
                else:
                    decompressed = raw_data
                records = json.loads(decompressed.decode("utf-8"))
            except Exception as e:
                logger.error("  → Failed to decode upload: %s", e)

            upload_entry = {
                "log_type": log_type,
                "device_key": device_key,
                "record_count": len(records) if isinstance(records, list) else 1,
                "size_bytes": len(raw_data),
                "log_size_header": log_size,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "records": records if isinstance(records, list) else [records],
            }
            _uploads_received.append(upload_entry)

            logger.info(
                "  → Upload received: logType=%s, records=%d, size=%d bytes",
                log_type, upload_entry["record_count"], len(raw_data),
            )

            if records and isinstance(records, list) and len(records) > 0:
                first = records[0]
                keys = list(first.keys())[:5]
                logger.info("  → First record keys: %s", keys)

            upload_id = f"mock-{len(_uploads_received):04d}"
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("x-uploadid", upload_id)
            body = json.dumps({"status": "success", "uploadid": upload_id}).encode()
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        self.send_error(404, f"Unknown POST path: {path}")


def main():
    global PROXY_TARGET, SSL_CONTEXT

    parser = argparse.ArgumentParser(
        description="Mock / Proxy Site24x7 server for local testing",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Mock mode (hardcoded responses)
  python3 mock_s247_server.py

  # Proxy to Docker build
  python3 mock_s247_server.py --proxy https://localhost:9443

  # Proxy to native build
  python3 mock_s247_server.py --proxy https://your-internal-server.example.com:8443

  # With ngrok for Azure Functions
  ngrok http 8999
  # Then set on Azure: SITE24X7_BASE_URL=<ngrok-url>
        """,
    )
    parser.add_argument("--port", type=int, default=8999,
                        help="Port to listen on (default: 8999)")
    parser.add_argument("--proxy", type=str, default=None, metavar="TARGET_URL",
                        help="Proxy mode: forward all requests to TARGET_URL "
                             "(e.g., https://localhost:9443)")
    parser.add_argument("--bind", type=str, default="0.0.0.0",
                        help="Bind address (default: 0.0.0.0 for ngrok compatibility)")
    args = parser.parse_args()

    if args.proxy:
        PROXY_TARGET = args.proxy.rstrip("/")
        # Allow self-signed certs on local builds
        SSL_CONTEXT = ssl.create_default_context()
        SSL_CONTEXT.check_hostname = False
        SSL_CONTEXT.verify_mode = ssl.CERT_NONE

    server = HTTPServer((args.bind, args.port), MockS247Handler)

    if PROXY_TARGET:
        logger.info("=" * 60)
        logger.info("  PROXY MODE — forwarding to: %s", PROXY_TARGET)
        logger.info("=" * 60)
        logger.info("")
        logger.info("Listening on http://%s:%d", args.bind, args.port)
        logger.info("")
        logger.info("For Azure Functions, expose via ngrok:")
        logger.info("  ngrok http %d", args.port)
        logger.info("")
        logger.info("Then set on Azure Function App:")
        logger.info("  SITE24X7_BASE_URL=<ngrok-url>")
        logger.info("  SITE24X7_UPLOAD_DOMAIN=<ngrok-url>")
        logger.info("  SITE24X7_API_KEY=<your-real-device-key>")
    else:
        logger.info("=" * 60)
        logger.info("  MOCK MODE — returning hardcoded responses")
        logger.info("=" * 60)
        logger.info("")
        logger.info("Listening on http://%s:%d", args.bind, args.port)
        logger.info("")
        logger.info("Set these env vars for the Function App:")
        logger.info("  SITE24X7_BASE_URL=http://localhost:%d", args.port)
        logger.info("  SITE24X7_UPLOAD_DOMAIN=http://localhost:%d", args.port)
        logger.info("  SITE24X7_API_KEY=mock-device-key")

    logger.info("")
    logger.info("Debug endpoints (both modes):")
    logger.info("  GET /mock/status   — Show stats + recent requests")
    logger.info("  GET /mock/uploads  — Show recent uploads/proxied requests")
    logger.info("  GET /mock/reset    — Reset all state")
    logger.info("")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Shutting down")
        server.shutdown()


if __name__ == "__main__":
    main()
