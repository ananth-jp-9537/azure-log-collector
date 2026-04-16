# Security Review Report: Azure Log Collector

**Date:** 2026-04-16
**Reviewer:** Claude (automated)
**Repo:** `ananth-jp-9537/azure-log-collector`
**Version:** `0.1.0-alpha.5`
**Scope:** Full codebase — 22 Azure Functions, 8 shared modules, ARM template, CI/CD, setup scripts, dashboard
**Context:** This tool will be deployed into **customer Azure environments** with access to their Azure subscriptions, diagnostic logs, and Site24x7 accounts.

---

## Executive Summary

The codebase has a solid security foundation — Function Key auth on all HTTP endpoints, Managed Identity for Azure SDK access, secrets in app settings (not code), and proper TLS enforcement. However, **7 high-severity and 8 medium-severity issues** were found that must be addressed before customer deployment. The most critical are: XSS vectors in the dashboard, unvalidated input on write endpoints, information disclosure via error tracebacks, and an ARM template misconfiguration that deploys the function app package from a public GitHub URL.

| Severity | Count | Summary |
|----------|-------|---------|
| **Critical** | 2 | Stored XSS via unescaped API data in dashboard; ARM template pulls code from public URL |
| **High** | 5 | Traceback exposure; no input validation on ignore list; shell command injection in HealthCheck; device key in blob metadata; CORS not restricted |
| **Medium** | 8 | Stale secrets in blob configs; no audit logging; race conditions; blob read-modify-write without leases; testing proxy code in production; placeholder secret detection too narrow; config download exposes internal state; no CSP header |
| **Low** | 4 | EventHubProcessor dead code; env key enumeration in health check; no rate limiting on write endpoints; setup script uses md5 for uniqueness |

---

## Critical Findings

### SEC-01: Stored XSS via Dashboard innerHTML (Critical)

**Location:** `function-app/Dashboard/__init__.py` — lines 488, 497, 519, 1176
**Description:** The dashboard renders API response data using `innerHTML` with template literals. While `esc()` and `escAttr()` functions exist (line 413-414) and are used in some places (log type names, resource types), several locations inject API data **without escaping**:

```javascript
// Line 488 — subscription_ids from API, unescaped
subEl.innerHTML = s.subscription_ids.map(id => `<div class="sub-item">${id}</div>`).join('');

// Line 497 — region names and storage account names, unescaped
`<div class="region-item"><span class="region-name">${r.region}</span>
 <span class="region-sa">${r.storage_account}</span></div>`

// Line 519 — error messages from API, unescaped
errEl.innerHTML = allErrors.map(e => `<div class="error-item">${e}</div>`).join('');

// Line 1176 — processing stats (could contain blob names), unescaped
Blobs: ${r.blobs_found||...} | Records: ${r.processed||...}
```

**Impact:** If an attacker can influence Azure resource names, subscription display names, storage account names, or error messages (e.g., via a crafted resource name in the customer's Azure tenant), they can execute JavaScript in the context of any user viewing the dashboard. Since the dashboard is protected by a function key, this requires the attacker to either (a) be a user with dashboard access, or (b) influence Azure resource metadata visible to the function app.

**Remediation:**
- Apply `esc()` to ALL values rendered via `innerHTML` — subscription IDs, region names, storage account names, error messages, blob stats.
- Consider switching from `innerHTML` to `textContent` where HTML structure isn't needed.
- Add a Content-Security-Policy header (see SEC-12).

---

### SEC-02: ARM Template Pulls Code from Public GitHub URL (Critical)

**Location:** `setup/azuredeploy.json` — line 47, 216
**Description:** The ARM template hardcodes a public GitHub Release URL as the function app package source:
```json
"functionZipUrl": "https://github.com/ananth-jp-9537/azure-log-collector/releases/latest/download/s247-function-app.zip"
```
And uses it as `WEBSITE_RUN_FROM_PACKAGE`:
```json
{ "name": "WEBSITE_RUN_FROM_PACKAGE", "value": "[parameters('functionZipUrl')]" }
```

**Impact:**
1. **Supply chain risk:** Anyone with push access to the GitHub repo can modify what code gets deployed into customer environments. A compromised GitHub account or malicious PR could inject code that exfiltrates customer Azure credentials or diagnostic logs.
2. **No integrity verification:** The zip is downloaded without checksum/signature verification. A MITM between Azure and GitHub (unlikely but possible) could tamper with the package.
3. **Availability risk:** If the GitHub repo goes private, gets deleted, or GitHub has an outage, new deployments fail.
4. **Conflicts with Oryx build:** `WEBSITE_RUN_FROM_PACKAGE` set to a URL bypasses Oryx remote build (`ENABLE_ORYX_BUILD=true` on the same template). Per CP-64, this was manually fixed on every deploy.

**Remediation:**
- Host the deployment package in a customer-controlled Azure Storage Account (with SAS token or managed identity access), NOT a public GitHub URL.
- Add SHA256 checksum verification to the deployment workflow.
- Remove `WEBSITE_RUN_FROM_PACKAGE` from the ARM template and rely on Kudu zipdeploy with Oryx build instead.
- For "Deploy to Azure" button scenarios, use a customer-facing artifact repository (Azure Blob, Azure Marketplace).

---

## High-Severity Findings

### SEC-03: Traceback Exposure in Error Responses (High)

**Location:** `GetStatus/__init__.py:160`, `GetDebugInfo/__init__.py:142-145`
**Description:** Multiple endpoints return full Python tracebacks in error responses:
```python
# GetStatus line 160
return func.HttpResponse(
    json.dumps({"error": str(e), "traceback": traceback.format_exc()}, indent=2),
    ...status_code=500,
)
```

**Impact:** Tracebacks expose internal file paths, module structure, Azure SDK versions, and potentially secrets embedded in exception messages (e.g., connection string fragments in `azure.storage.blob` errors). In a customer environment, this information aids attackers in targeting specific vulnerability paths.

**Remediation:**
- Never return `traceback.format_exc()` in HTTP responses in production.
- Log tracebacks server-side (Application Insights captures these).
- Return generic error messages: `{"error": "Internal server error", "request_id": "<correlation_id>"}`.

---

### SEC-04: No Input Validation on UpdateIgnoreList (High)

**Location:** `UpdateIgnoreList/__init__.py:27`
**Description:** The entire JSON request body is passed directly to `save_ignore_list(body)` with no validation:
```python
body = req.get_json()
save_ignore_list(body)  # No schema validation, no type checking
```

**Impact:**
- **Arbitrary blob write:** An attacker with the function key can write any JSON structure to the ignore list blob, potentially corrupting the data format and crashing `is_ignored()` on every subsequent scan.
- **Denial of service:** A very large JSON payload (e.g., 100MB) would be written to blob storage and loaded into memory on every BlobLogProcessor and DiagSettingsManager invocation.
- **Logic bypass:** Injecting unexpected keys could bypass ignore logic (e.g., setting `resource_ids` to a non-list type would crash the comparison).

**Same issue affects:** `UpdateGeneralLogType`, `UpdateDisabledLogTypes` (partially — has action validation but no category name validation).

**Remediation:**
- Validate the request body against a JSON schema (expected keys, types, max lengths).
- Enforce maximum array lengths (e.g., max 1000 resource IDs in ignore list).
- Validate subscription IDs match UUID format, resource IDs match Azure resource ID pattern.
- Reject unknown keys.

---

### SEC-05: Shell Command Execution in HealthCheck (High)

**Location:** `HealthCheck/__init__.py:40`
**Description:**
```python
"python_version": os.popen("python --version 2>&1").read().strip(),
```
`os.popen()` executes a shell command. While the command itself is hardcoded (not user-influenced), using `os.popen` in a customer-facing endpoint is a code smell that could be exploited if the pattern is extended. More importantly, it spawns a subprocess on every health check call.

**Remediation:**
- Replace with `sys.version` or `platform.python_version()` — no subprocess needed.

---

### SEC-06: Device Key Exposed in Blob Metadata (High)

**Location:** `site24x7_client.py:880`
**Description:** When using relay blob upload, the device key is stored as blob metadata:
```python
metadata = {
    "logtype": log_type,
    "devicekey": config["apiKey"],  # <-- Device key in blob metadata
    "logsize": str(log_size),
    ...
}
```

**Impact:** Anyone with read access to the relay storage account (e.g., a Contributor on the resource group, or via a storage account key leak) can read all blob metadata and extract the Site24x7 device key. This key grants full API access to the customer's Site24x7 AppLogs account.

**Remediation:**
- Do not store the device key in blob metadata. Pass it via a separate secure channel or use Azure Key Vault references.
- If the relay pattern is production-only testing infrastructure, ensure it is removed before customer deployment (see SEC-09).

---

### SEC-07: No CORS Restriction on API Endpoints (High)

**Location:** `function-app/host.json` (missing CORS configuration)
**Description:** No CORS policy is configured in `host.json`. Azure Functions defaults to allowing all origins, meaning any website can make authenticated requests to the dashboard API if the function key is known (e.g., bookmarked in a URL).

**Remediation:**
Add to `host.json`:
```json
"extensions": {
  "http": {
    "customHeaders": {
      "Access-Control-Allow-Origin": ""
    }
  }
}
```
Or configure via Azure Portal: Function App → CORS → Remove `*`, add only the dashboard's own origin.

---

## Medium-Severity Findings

### SEC-08: Stale Secrets in Blob-Stored Configs (Medium)

**Location:** `site24x7_client.py:546` — `post_logs()` reads `apiKey` from base64-decoded config
**Description:** Log type configs stored in blob contain `apiKey` and `uploadDomain` baked at scan time. If the device key is rotated or the upload domain changes, all stored configs contain stale credentials. The `post_logs()` function uses these stale values without override (the CP-61/62 fix is NOT in the repo).

**Impact:** After a key rotation, uploads fail with "License Limit Reached" or auth errors until a full re-scan regenerates all configs. During this window, diagnostic logs accumulate and are eventually deleted by the 7-day stale blob cleanup.

**Remediation:** Port the CP-61/62 fix — override `apiKey` and `uploadDomain` in `post_logs()` from the current environment values, not the baked config.

---

### SEC-09: Testing/Proxy Code Shipped in Production (Medium)

**Location:** `site24x7_client.py:178-188, 228-265`
**Description:** The Zoho Flow proxy feature (`SITE24X7_PROXY_URL`) and relay blob upload (`RELAY_UPLOAD_CONN_STR`) are present in production code with `# TESTING ONLY` comments. While they are gated behind environment variables, they represent unnecessary attack surface:

- `SITE24X7_PROXY_URL` — If set by mistake, all Site24x7 API calls are routed through a third-party Zoho Flow webhook, leaking the device key and all API payloads.
- `RELAY_UPLOAD_CONN_STR` — If set, all log uploads go to a relay storage account, and the device key is exposed in blob metadata (SEC-06).

**Remediation:**
- Remove proxy and relay code from the production codebase.
- If needed for testing, keep it in a separate branch or behind a build flag that is stripped during the release workflow.

---

### SEC-10: No Audit Logging for Destructive Operations (Medium)

**Location:** `RemoveDiagSettings/__init__.py`, `UpdateIgnoreList/__init__.py`, `UpdateSettings/__init__.py`, `StopProcessing/__init__.py`
**Description:** Destructive operations (removing all diagnostic settings, updating ignore lists, changing settings, stopping processing) are logged to the function app's standard logger but not to a dedicated audit trail. In a customer environment, it's critical to know WHO made changes and WHEN.

**Remediation:**
- Log all write operations to a dedicated audit blob or Application Insights custom event with: timestamp, caller IP, operation, old value, new value.
- Consider requiring a confirmation token for destructive operations like `RemoveDiagSettings`.

---

### SEC-11: Blob Read-Modify-Write Without Concurrency Control (Medium)

**Location:** `debug_logger.py:88-97`, `config_store.py` (multiple), `ignore_list.py:101-130`
**Description:** All blob-backed stores (events, configs, ignore list, scan state) use a read-download-modify-upload pattern without blob leases or ETags. Concurrent function invocations (BlobLogProcessor every 2 min + DiagSettingsManager every 6h + manual scans) can overwrite each other's changes.

**Impact:** Data loss — a scan result could be overwritten by a concurrent BlobLogProcessor stats save, or two ignore list updates could lose one set of changes.

**Remediation:**
- Use blob lease or ETag-based conditional writes for all read-modify-write operations.
- At minimum, use `if_match=etag` parameter on `upload_blob()`.

---

### SEC-12: No Content-Security-Policy on Dashboard (Medium)

**Location:** `Dashboard/__init__.py` — HTML response headers
**Description:** The dashboard returns HTML without a Content-Security-Policy (CSP) header. Combined with the XSS vectors in SEC-01, this means injected scripts can load external resources, exfiltrate data, or execute arbitrary code.

**Remediation:**
Add CSP header to the dashboard response:
```python
headers={
    "Content-Security-Policy": "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src 'self' data:; connect-src 'self'",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
}
```

---

### SEC-13: Debug Endpoint Exposes Internal Configuration (Medium)

**Location:** `GetDebugInfo/__init__.py:46-101`
**Description:** The debug endpoint returns extensive internal state: scan state, processing history, all logtype config keys, configured resource counts, environment variables (some masked), circuit breaker state. While secrets are masked, the endpoint confirms which environment variables are set, their names, and the function app's internal architecture.

**Impact:** In a customer environment, a user with the function key can map the entire internal state of the system. The `?download=1` parameter creates a downloadable bundle of this information.

**Remediation:**
- Consider gating the debug endpoint behind a separate key or admin-only auth level.
- Reduce the amount of internal state exposed (e.g., don't enumerate all logtype config keys).
- Add rate limiting to prevent automated state harvesting.

---

### SEC-14: Placeholder Secret Detection Is Too Narrow (Medium)

**Location:** `debug_logger.py:183`
**Description:**
```python
elif var == "SITE24X7_API_KEY" and val in ("vuvuvi", "test", "changeme"):
```
Only 3 placeholder values are detected. Common placeholders like `"your-key-here"`, `"TODO"`, `"xxx"`, `"placeholder"`, `"dummy"`, or the ARM template default (if any) are not caught.

**Remediation:**
- Validate the device key format: Site24x7 device keys follow patterns like `ab_<hex>`, `aa_<hex>`, `in_<hex>`, etc.
- Flag any key shorter than 20 characters or not matching the expected prefix pattern.

---

### SEC-15: HealthCheck Enumerates Environment Variable Names (Medium)

**Location:** `HealthCheck/__init__.py:41`
**Description:**
```python
"env_keys": sorted([k for k in os.environ if k.startswith(("AZURE", "FUNCTIONS", "WEBSITE", "SUBSCRIPTION", "RESOURCE", "PROCESSING", "SITE24X7", "DIAG_STORAGE", "UPDATE"))]),
```
The health endpoint returns all environment variable NAMES matching common prefixes. While values are not exposed, the names reveal which features are configured, which Azure settings exist, and the overall deployment topology.

**Remediation:**
- Remove `env_keys` from the default health response.
- Move environment enumeration to the debug endpoint (already behind function key, but see SEC-13).

---

## Low-Severity Findings

### SEC-16: EventHubProcessor Dead Code (Low)

**Location:** `function-app/EventHubProcessor/`
**Description:** Legacy Event Hub trigger still in the codebase. If `AzureWebJobs.EventHubProcessor.Disabled=true` is accidentally removed and `EVENTHUB_CONN` is set, the function would activate and process events from an Event Hub — potentially one the customer uses for other purposes.

**Remediation:** Delete the `EventHubProcessor/` directory entirely.

---

### SEC-17: No Rate Limiting on Write Endpoints (Low)

**Location:** All HTTP PUT/POST endpoints
**Description:** No rate limiting beyond Azure Functions' built-in throttling. An attacker with the function key could flood write endpoints (update ignore list, toggle log types, trigger scans) to cause resource exhaustion on the consumption plan.

**Remediation:** Add a simple rate limiter (e.g., blob-backed counter) for write operations, or document consumption plan limits for customers.

---

### SEC-18: Setup Script Uses md5 for Uniqueness (Low)

**Location:** `setup/setup.sh:70`
```bash
unique_suffix=$(echo "${SUBSCRIPTION_IDS}" | md5sum ...)
```
**Description:** md5 is used to generate a unique suffix from subscription IDs. This is not a security use of md5 (no password hashing or integrity checking), but the use of md5 in customer-deployed scripts may trigger security scanners.

**Remediation:** Use `sha256sum` instead, or Azure's `uniqueString()` function (already used in the ARM template).

---

### SEC-19: Kudu Credentials Retrieved in Setup Script (Low)

**Location:** `setup/setup.sh:450-457`
**Description:** The setup script retrieves Kudu publishing credentials (username + password) and uses them in a curl command. These credentials are logged to `/tmp/s247-setup-*.log`.

**Remediation:**
- Don't log publishing credentials.
- Use `az functionapp deployment source config-zip` instead of direct Kudu curl (avoids credential exposure).
- If Kudu is needed, use `--output none` and don't store credentials in variables that get logged.

---

## Recommendations Summary (Priority Order)

### Must Fix Before Customer Deployment

| # | Finding | Fix |
|---|---------|-----|
| 1 | SEC-01 | Escape ALL `innerHTML` data with `esc()`/`escAttr()` |
| 2 | SEC-02 | Remove `WEBSITE_RUN_FROM_PACKAGE` from ARM template; use customer-controlled storage |
| 3 | SEC-03 | Remove tracebacks from HTTP error responses |
| 4 | SEC-04 | Add input validation on all write endpoints |
| 5 | SEC-08 | Port CP-60/61/62 fixes (API domain routing + stale config override) |
| 6 | SEC-09 | Remove testing proxy/relay code from production |
| 7 | SEC-12 | Add CSP, X-Content-Type-Options, X-Frame-Options headers to dashboard |

### Should Fix

| # | Finding | Fix |
|---|---------|-----|
| 8 | SEC-05 | Replace `os.popen` with `sys.version` |
| 9 | SEC-06 | Remove device key from blob metadata |
| 10 | SEC-07 | Configure CORS to restrict origins |
| 11 | SEC-10 | Add audit logging for destructive operations |
| 12 | SEC-13 | Restrict debug endpoint access |
| 13 | SEC-15 | Remove env key enumeration from health check |
| 14 | SEC-16 | Delete EventHubProcessor dead code |

### Nice to Have

| # | Finding | Fix |
|---|---------|-----|
| 15 | SEC-11 | Add blob lease/ETag concurrency control |
| 16 | SEC-14 | Improve placeholder secret detection |
| 17 | SEC-17 | Add rate limiting on write endpoints |
| 18 | SEC-18 | Replace md5 with sha256 in setup script |
| 19 | SEC-19 | Don't log Kudu credentials |

---

## Positive Security Observations

1. **Function Key auth on all HTTP endpoints** — No anonymous access.
2. **Managed Identity for Azure SDK** — No service principal secrets stored.
3. **TLS 1.2 minimum** enforced on storage accounts (ARM template).
4. **`allowBlobPublicAccess: false`** on both storage accounts.
5. **Resource lock** on the resource group prevents accidental deletion.
6. **Proper RBAC** — Reader + Monitoring Contributor (not Owner/Contributor on subscription).
7. **Secrets masked in debug output** — Device key and connection strings shown as `***set***`.
8. **Circuit breaker** prevents cascading failures when Site24x7 is unreachable.
9. **`esc()` and `escAttr()` functions exist** in the dashboard (just not applied consistently).
10. **Connection strings in app settings** — Not hardcoded in source code.

---

*Report generated by Claude Code security review. Manual penetration testing recommended before production customer deployment.*
