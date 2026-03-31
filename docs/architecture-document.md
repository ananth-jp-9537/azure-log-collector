# Site24x7 Azure Diagnostic Logs Collection — Architecture Document

**Version:** 1.0.0  
**Last Updated:** March 2026  
**Status:** Implementation Complete — Pending Deployment

---

## Table of Contents

1. [Executive Summary](#1-executive-summary)
2. [System Architecture Overview](#2-system-architecture-overview)
3. [Infrastructure Components](#3-infrastructure-components)
4. [Multi-Region Architecture](#4-multi-region-architecture)
5. [Core Workflows](#5-core-workflows)
6. [Shared Modules](#6-shared-modules)
7. [Function Inventory](#7-function-inventory)
8. [API Reference](#8-api-reference)
9. [Data Storage Architecture](#9-data-storage-architecture)
10. [Site24x7 Integration](#10-site24x7-integration)
11. [Security Architecture](#11-security-architecture)
12. [Resilience & Error Handling](#12-resilience--error-handling)
13. [Configuration Reference](#13-configuration-reference)
14. [Deployment Guide](#14-deployment-guide)

---

## 1. Executive Summary

This system automates the collection and forwarding of Azure diagnostic logs to Site24x7 AppLogs. It:

- **Discovers** all Azure resources across subscriptions that support diagnostic logging
- **Provisions** per-region Storage Accounts for log collection
- **Configures** Azure Diagnostic Settings to stream logs to regional storage
- **Auto-creates** Site24x7 log types for each Azure log category
- **Polls** Storage Account blobs every 2 minutes, parses logs, and uploads to Site24x7
- **Provides** a web dashboard and REST API for management and monitoring

**Cost:** ~$0.02/GB/month (Storage Account polling vs $11/region/month for Event Hub)

---

## 2. System Architecture Overview

```
╔══════════════════════════════════════════════════════════════════════════════════╗
║                    AZURE DIAGNOSTIC LOGS → SITE24X7 PIPELINE                    ║
╚══════════════════════════════════════════════════════════════════════════════════╝

┌──────────────────────────────────────────────────────────────────────────────────┐
│  AZURE SUBSCRIPTIONS (1..N)                                                      │
│                                                                                  │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐         │
│  │  VM (eastus)  │  │  SQL (westus) │  │  AKS (eastus)│  │  KV (westeu) │  ...   │
│  └──────┬────────┘  └──────┬───────┘  └──────┬───────┘  └──────┬───────┘         │
│         │ Diagnostic        │                  │                 │                 │
│         │ Settings           │                  │                 │                 │
│         ▼ (s247-diag-logs)   ▼                  ▼                 ▼                 │
│  ┌────────────────────┐  ┌────────────────────┐  ┌────────────────────┐           │
│  │ Storage: eastus     │  │ Storage: westus     │  │ Storage: westeu    │          │
│  │ s247diageastus...   │  │ s247diagwestus...   │  │ s247diagwesteu...  │          │
│  │ tag: managed-by:    │  │ tag: managed-by:    │  │ tag: managed-by:   │          │
│  │   s247-diag-logs    │  │   s247-diag-logs    │  │   s247-diag-logs   │          │
│  │                      │  │                      │  │                    │          │
│  │ ├─insights-logs-    │  │ ├─insights-logs-    │  │ ├─insights-logs-   │          │
│  │ │  AuditEvent/      │  │ │  SQLSecurityAudit/ │  │ │  AuditEvent/     │          │
│  │ ├─insights-logs-    │  │ ├─insights-logs-    │  │ └─insights-logs-   │          │
│  │ │  SignInLogs/      │  │ │  DatabaseWaitStat/ │  │    AppServiceHTTP/ │          │
│  │ └─...               │  │ └─...               │  │                    │          │
│  └────────┬─────────────┘  └────────┬────────────┘  └────────┬──────────┘          │
│           │                         │                         │                    │
└───────────┼─────────────────────────┼─────────────────────────┼────────────────────┘
            │           Poll every 2 min                        │
            ▼                         ▼                         ▼
╔══════════════════════════════════════════════════════════════════════════════════╗
║  FUNCTION APP: s247-diag-func-qt4xn2  (RG: s247-diag-logs-rg)                  ║
║  Auth: Managed Identity (Azure APIs) + Function Keys (HTTP endpoints)           ║
║                                                                                  ║
║  ┌─── TIMER FUNCTIONS ───────────────────────────────────────────────────────┐  ║
║  │                                                                            │  ║
║  │  ┌────────────────────────────┐    ┌─────────────────────────────────┐    │  ║
║  │  │  DiagSettingsManager       │    │  BlobLogProcessor               │    │  ║
║  │  │  (every 6h / on-demand)    │    │  (every 2 min)                  │    │  ║
║  │  │                            │    │                                 │    │  ║
║  │  │  1. List all resources     │    │  1. Find regional storage accs  │    │  ║
║  │  │  2. Reconcile regions      │    │     (by tag: s247-diag-logs)    │    │  ║
║  │  │     (create/delete storage │    │  2. Scan insights-logs-* ctrs   │    │  ║
║  │  │      accounts per region)  │    │  3. Load S247_{cat} config      │    │  ║
║  │  │  3. Fetch supported types  │    │     from blob store             │    │  ║
║  │  │     from Site24x7 API      │    │  4. Parse JSON blobs            │    │  ║
║  │  │  4. Batch-create log types │    │  5. Apply masking/hashing/      │    │  ║
║  │  │  5. Store sourceConfig in  │    │     filtering/derived fields    │    │  ║
║  │  │     blob config store      │    │  6. Gzip + POST to Site24x7    │    │  ║
║  │  │  6. Create diag settings   │    │  7. Delete processed blobs     │    │  ║
║  │  │     → regional storage     │    │  8. Update checkpoint           │    │  ║
║  │  └────────────┬───────────────┘    └──────────────┬──────────────────┘    │  ║
║  │               │                                   │                       │  ║
║  │  ┌────────────────────────────┐                   │                       │  ║
║  │  │  AutoUpdater               │                   │                       │  ║
║  │  │  (daily 3 AM UTC)          │                   │                       │  ║
║  │  │  Checks remote version.json│                   │                       │  ║
║  │  │  Auto-deploys if newer     │                   │                       │  ║
║  │  └────────────────────────────┘                   │                       │  ║
║  └───────────────────────────────────────────────────┼───────────────────────┘  ║
║                                                      │                          ║
║  ┌─── BLOB CONFIG STORE (AzureWebJobsStorage) ──────┼───────────────────────┐  ║
║  │  config/                                          │                       │  ║
║  │  ├── logtype-configs/S247_AuditEvent.json   ◄─────┤                       │  ║
║  │  ├── logtype-configs/S247_SignInLogs.json          │                       │  ║
║  │  ├── logtype-configs/S247_...json                  │                       │  ║
║  │  ├── azure-log-types.json  (supported types cache)│                       │  ║
║  │  ├── disabled-logtypes.json                        │                       │  ║
║  │  ├── configured-resources.json                     │                       │  ║
║  │  └── ignore-list.json                              │                       │  ║
║  │  s247-checkpoints/                                 │                       │  ║
║  │  └── blob-processor-checkpoint.json  ◄─────────────┘                       │  ║
║  └────────────────────────────────────────────────────────────────────────────┘  ║
║                                                                                  ║
║  ┌─── HTTP ENDPOINTS (protected by Function Keys) ───────────────────────────┐  ║
║  │  GET  /api/dashboard             — Web UI                                  │  ║
║  │  GET  /api/status                — System overview + resource counts        │  ║
║  │  GET  /api/health                — Liveness probe + dependency checks      │  ║
║  │  POST /api/scan                  — On-demand DiagSettingsManager run        │  ║
║  │  GET  /api/ignore-list           — Excluded RGs/locations/resources         │  ║
║  │  PUT  /api/ignore-list           — Update exclusions + cleanup diag        │  ║
║  │  GET  /api/disabled-logtypes     — Disabled log categories                 │  ║
║  │  POST /api/disabled-logtypes     — Disable/enable + cleanup                │  ║
║  │  PUT  /api/processing            — Toggle BlobLogProcessor on/off          │  ║
║  │  POST /api/remove-diag-settings  — Bulk remove all diagnostic settings     │  ║
║  │  GET  /api/general-logtype       — General catch-all log type config       │  ║
║  │  PUT  /api/general-logtype       — Update general log type                 │  ║
║  │  GET  /api/check-update          — Check for available updates             │  ║
║  │  POST /api/check-update          — Check and auto-apply update             │  ║
║  └────────────────────────────────────────────────────────────────────────────┘  ║
╚══════════════════════════════════════════════════════════════════════════════════╝
            │                                   │
            │  GET /applog/azure/               │  POST https://{uploadDomain}
            │    logtype_supported              │    /upload
            │  POST /applog/azure/              │  (gzip, X-DeviceKey,
            │    logtype_create                 │   X-LogType, X-StreamMode)
            ▼                                   ▼
╔══════════════════════════════════════════════════════════════════════════════════╗
║  SITE24X7 SERVER (applogs)                                                       ║
║                                                                                  ║
║  ┌─── AppLogServlet (deviceKey auth) ─────────────────────────────────────────┐  ║
║  │  GET  /applog/azure/logtype_supported → azureLogTypes.json (50+ types)    │  ║
║  │  POST /applog/azure/logtype_create    → LogTypeAPI → sourceConfig         │  ║
║  └────────────────────────────────────────────────────────────────────────────┘  ║
║                                                                                  ║
║  ┌─── Upload Endpoint ────────────────────────────────────────────────────────┐  ║
║  │  POST /upload — Receives gzipped log events, indexes into AppLogs         │  ║
║  └────────────────────────────────────────────────────────────────────────────┘  ║
║                                                                                  ║
║  AppLogs Console: Search, alerts, dashboards, saved searches                    ║
╚══════════════════════════════════════════════════════════════════════════════════╝
```

---

## 3. Infrastructure Components

### 3.1 Azure Resources

| Resource | Name | Purpose |
|----------|------|---------|
| **Resource Group** | `s247-diag-logs-rg` | Contains all pipeline resources |
| **Function App** | `s247-diag-func-qt4xn2` | Hosts all Azure Functions (Python 3.11) |
| **Storage Account** (primary) | `s247diagb6d656` | Function App storage + config blobs + checkpoints |
| **Storage Accounts** (regional) | `s247diag{region}{suffix}` | Per-region log collection (auto-provisioned) |
| **Managed Identity** | `906a475d-b3b7-459f-b018-2b859286dc21` | System-assigned identity for Azure API access |

### 3.2 RBAC Role Assignments (Managed Identity)

| Role | Scope | Purpose |
|------|-------|---------|
| **Reader** | Subscription | Discover all resources |
| **Monitoring Contributor** | Subscription | Create/delete diagnostic settings |
| **Contributor** | Resource Group (`s247-diag-logs-rg`) | Manage storage accounts, app settings |

### 3.3 Dependencies

```
azure-functions          # Azure Functions runtime
azure-identity           # DefaultAzureCredential (Managed Identity)
azure-mgmt-resource      # Resource discovery
azure-mgmt-monitor       # Diagnostic settings + categories
azure-mgmt-storage       # Storage account lifecycle
azure-mgmt-web           # Function App settings management
azure-storage-blob       # Blob read/write/delete
requests                 # HTTP calls to Site24x7
```

---

## 4. Multi-Region Architecture

### 4.1 Why Per-Region Storage?

Azure Diagnostic Settings require the destination Storage Account to be **in the same region** as the resource. The system automatically provisions one Storage Account per active region.

### 4.2 Regional Storage Lifecycle

```
┌───────────────────────────────────────────────────────────────────┐
│                    REGION RECONCILIATION FLOW                      │
│                                                                   │
│  ┌─────────────────┐    ┌──────────────────┐                     │
│  │ Active Resources │    │ Provisioned       │                     │
│  │ (from scan)      │    │ Storage Accounts  │                     │
│  │                  │    │ (by tag lookup)   │                     │
│  │ eastus     ──────┼────┼── s247diageastus  │  ✓ Already exists  │
│  │ westus     ──────┼────┼── (missing)       │  → Provision new   │
│  │ westeurope ──────┼────┼── s247diagwesteu  │  ✓ Already exists  │
│  │ (none)     ──────┼────┼── s247diagcentus  │  → Deprovision     │
│  └─────────────────┘    └──────────────────┘                     │
│                                                                   │
│  Reconciliation:                                                  │
│  • to_add    = active_regions - provisioned_regions               │
│  • to_remove = provisioned_regions - active_regions               │
└───────────────────────────────────────────────────────────────────┘
```

### 4.3 Storage Account Provisioning Details

| Property | Value |
|----------|-------|
| **Naming** | `s247diag{region}{suffix}` (max 24 chars, lowercase alphanumeric) |
| **SKU** | `Standard_LRS` |
| **Kind** | `StorageV2` |
| **TLS** | Minimum TLS 1.2 |
| **Public Access** | Disabled |
| **Base Container** | `insights-logs` (validation container) |
| **Resource Lock** | `CanNotDelete` (name: `s247-lock-{account_name}`) |
| **Tags** | `managed-by: s247-diag-logs`, `purpose: diag-logs-regional`, `region: {region}` |

### 4.4 Multi-Region Data Flow

```
 Subscription 1                          Subscription 2
 ┌─────────────────────────────┐         ┌─────────────────────────────┐
 │  VM-1 (eastus)              │         │  SQL-1 (eastus)             │
 │  VM-2 (westus)              │         │  AKS-1 (westeurope)        │
 │  KeyVault-1 (eastus)        │         │  AppService-1 (westus)     │
 └──────┬──────────┬───────────┘         └──────┬──────────┬──────────┘
        │          │                             │          │
        ▼          ▼                             ▼          ▼
 ┌────────────┐ ┌────────────┐          ┌────────────┐ ┌────────────┐
 │  Storage    │ │  Storage    │          │  Storage    │ │  Storage    │
 │  eastus     │ │  westus     │          │  eastus     │ │  westeurope │
 │             │ │             │          │  (shared)   │ │             │
 │ insights-   │ │ insights-   │          │             │ │ insights-   │
 │ logs-Audit/ │ │ logs-Audit/ │          │             │ │ logs-kube/  │
 │ logs-SignIn/│ │ logs-HTTP/  │          │             │ │             │
 └──────┬──────┘ └──────┬──────┘          └──────┬──────┘ └──────┬──────┘
        │               │                       │               │
        └───────────────┼───────────────────────┼───────────────┘
                        │                       │
                        ▼                       ▼
              ┌─────────────────────────────────────────┐
              │       BlobLogProcessor (every 2 min)     │
              │  Discovers all regional accounts by tag  │
              │  Polls insights-logs-* containers        │
              │  Uploads parsed logs to Site24x7         │
              │  Deletes processed blobs                 │
              └─────────────────────────────────────────┘
```

---

## 5. Core Workflows

### 5.1 Workflow 1: Resource Discovery & Configuration (DiagSettingsManager)

**Trigger:** Timer (configurable via `%TIMER_SCHEDULE%`) or on-demand via `POST /api/scan`

```
┌─────────────────────────────────────────────────────────────────────────┐
│  STEP 1: LOAD CONFIGURATION                                            │
│  ├─ Read SUBSCRIPTION_IDS, RESOURCE_GROUP_NAME, DIAG_STORAGE_SUFFIX    │
│  ├─ Clear in-memory config cache                                        │
│  └─ Load ignore list + configured resources from blob                   │
├─────────────────────────────────────────────────────────────────────────┤
│  STEP 2: FETCH SUPPORTED LOG TYPES                                      │
│  ├─ Check blob cache for azure-log-types.json                           │
│  ├─ If missing: GET /applog/azure/logtype_supported (Site24x7 API)     │
│  └─ Build normalized lookup map (remove hyphens, lowercase)             │
├─────────────────────────────────────────────────────────────────────────┤
│  STEP 3: DISCOVER RESOURCES                                             │
│  ├─ For each subscription:                                               │
│  │   └─ ResourceManagementClient.resources.list()                       │
│  ├─ Filter: supports_diagnostic_logs() == True                          │
│  └─ Filter: is_ignored() == False                                       │
├─────────────────────────────────────────────────────────────────────────┤
│  STEP 4: RECONCILE REGIONS                                              │
│  ├─ Extract active regions from resources                                │
│  ├─ List provisioned storage accounts (by tag)                          │
│  ├─ to_add = active - provisioned → provision_storage_account()         │
│  └─ to_remove = provisioned - active → deprovision_storage_account()    │
├─────────────────────────────────────────────────────────────────────────┤
│  STEP 5: CATEGORY COLLECTION                                            │
│  ├─ For each resource:                                                   │
│  │   ├─ Skip if already configured (in tracking blob OR has diag setting)│
│  │   ├─ Get diagnostic categories for resource                          │
│  │   ├─ Filter out disabled categories                                   │
│  │   ├─ Map resource → categories + storage account                     │
│  │   └─ Collect unconfigured categories for batch creation              │
│  └─ Build resource_category_map and categories_to_create list           │
├─────────────────────────────────────────────────────────────────────────┤
│  STEP 6: BATCH LOG TYPE CREATION                                        │
│  ├─ POST /applog/azure/logtype_create with categories list              │
│  ├─ Site24x7 returns: [{category, sourceConfig (base64)}]              │
│  ├─ Decode and save each sourceConfig to blob:                          │
│  │   config/logtype-configs/S247_{category}.json                        │
│  └─ Track logtypes_created count                                        │
├─────────────────────────────────────────────────────────────────────────┤
│  STEP 7: CONFIGURE DIAGNOSTIC SETTINGS                                  │
│  ├─ For each resource in resource_category_map:                         │
│  │   ├─ Verify at least one category has config (specific or general)   │
│  │   ├─ Build storage_account_id from region + suffix                   │
│  │   ├─ MonitorManagementClient.diagnostic_settings.create_or_update()  │
│  │   │   setting_name: "s247-diag-logs"                                 │
│  │   │   category_group: "allLogs" (captures everything)                │
│  │   │   storage_account_id: regional storage account                   │
│  │   └─ Mark resource as configured in blob tracking                    │
├─────────────────────────────────────────────────────────────────────────┤
│  STEP 8: FINALIZE                                                       │
│  ├─ Update LAST_SCAN_TIME app setting                                   │
│  └─ Return summary with all stats                                       │
└─────────────────────────────────────────────────────────────────────────┘
```

**Output Statistics:**
```json
{
  "scan_time": "2026-03-11T05:00:00Z",
  "total_resources": 150,
  "active_resources": 120,
  "ignored_resources": 30,
  "already_configured": 100,
  "newly_configured": 20,
  "logtypes_created": 5,
  "specific_logtypes": 18,
  "general_logtypes": 2,
  "skipped": 0,
  "errors": 0,
  "regions": {
    "active": ["eastus", "westus", "westeurope"],
    "added": [{"region": "westus", "storage_account": "s247diagwestusb6d656"}],
    "removed": ["centralus"]
  }
}
```

---

### 5.2 Workflow 2: Log Processing & Upload (BlobLogProcessor)

**Trigger:** Timer — every 2 minutes (`0 */2 * * * *`)

```
┌─────────────────────────────────────────────────────────────────────────┐
│  STEP 1: PRE-FLIGHT CHECKS                                             │
│  ├─ If PROCESSING_ENABLED == false → skip entirely                     │
│  ├─ Load all logtype configs from blob: config/logtype-configs/*.json  │
│  ├─ Load general config from S247_GENERAL_LOGTYPE env var              │
│  └─ If no configs found and general not enabled → exit early           │
├─────────────────────────────────────────────────────────────────────────┤
│  STEP 2: DISCOVER REGIONAL STORAGE ACCOUNTS                            │
│  ├─ List storage accounts in RG                                         │
│  ├─ Filter by tags: managed-by=s247-diag-logs, purpose=diag-logs-reg  │
│  └─ Load checkpoint: s247-checkpoints/blob-processor-checkpoint.json   │
├─────────────────────────────────────────────────────────────────────────┤
│  STEP 3: PROCESS EACH REGIONAL ACCOUNT                                  │
│  ├─ Get storage account keys                                            │
│  ├─ Get last_processed timestamp from checkpoint                        │
│  │                                                                      │
│  ├─ FOR EACH CONTAINER (matching insights-logs-*):                      │
│  │   ├─ Extract category from name: insights-logs-{cat} → {cat}        │
│  │   ├─ Normalize: remove hyphens → S247_{normalized}                  │
│  │   ├─ Look up sourceConfig:                                           │
│  │   │   ├─ Specific: config/logtype-configs/S247_{cat}.json           │
│  │   │   ├─ Fallback: general config (if enabled)                      │
│  │   │   └─ Skip container if neither found                            │
│  │   │                                                                  │
│  │   ├─ FOR EACH BLOB (.json files only):                              │
│  │   │   ├─ Skip if blob.last_modified ≤ last_processed (checkpoint)   │
│  │   │   ├─ Download blob content                                       │
│  │   │   ├─ Parse JSON → extract "records" array                       │
│  │   │   ├─ POST to Site24x7 via client.post_logs()                    │
│  │   │   │   ├─ Decode sourceConfig (base64 → JSON)                    │
│  │   │   │   ├─ Parse fields via jsonPath rules                        │
│  │   │   │   ├─ Apply filters (include/exclude by field patterns)      │
│  │   │   │   ├─ Apply masking (regex → replacement string)             │
│  │   │   │   ├─ Apply hashing (regex → SHA256 digest)                  │
│  │   │   │   ├─ Apply derived fields (regex → named groups)            │
│  │   │   │   ├─ Add _zl_timestamp (ms epoch) + s247agentuid (RG)      │
│  │   │   │   ├─ Gzip compress JSON array                               │
│  │   │   │   └─ POST to https://{uploadDomain}/upload                  │
│  │   │   ├─ If success: mark blob for deletion                         │
│  │   │   └─ Track latest blob timestamp for checkpoint                 │
│  │   │                                                                  │
│  │   └─ DELETE successfully processed blobs                            │
│  │                                                                      │
│  └─ Update checkpoint with latest timestamp per account                │
├─────────────────────────────────────────────────────────────────────────┤
│  STEP 4: SAVE CHECKPOINTS                                               │
│  └─ Write to s247-checkpoints/blob-processor-checkpoint.json           │
└─────────────────────────────────────────────────────────────────────────┘
```

**Output Statistics:**
```json
{
  "processed": 1250,
  "uploaded": 1200,
  "general": 50,
  "dropped": 0,
  "blobs_deleted": 45
}
```

---

### 5.3 Workflow 3: Disable Log Type

**Trigger:** `POST /api/disabled-logtypes` with `{"action": "disable", "category": "AuditEvent"}`

```
┌─────────────────────────────────────────────────────────────────────────┐
│  1. Add "AuditEvent" to disabled-logtypes.json                         │
│  2. Delete config/logtype-configs/S247_AuditEvent.json                 │
│  3. For each resource in configured-resources.json:                    │
│     ├─ If resource has "AuditEvent" in its categories:                 │
│     │   ├─ Remove "AuditEvent" from categories list                    │
│     │   ├─ If no categories remain:                                    │
│     │   │   ├─ DELETE diagnostic setting "s247-diag-logs" from resource│
│     │   │   └─ Remove resource from configured tracking                │
│     │   └─ Else: update resource with remaining categories             │
│  4. Return: disabled list + count of removed settings                  │
└─────────────────────────────────────────────────────────────────────────┘
```

### 5.4 Workflow 4: Enable Log Type

**Trigger:** `POST /api/disabled-logtypes` with `{"action": "enable", "category": "AuditEvent"}`

```
┌─────────────────────────────────────────────────────────────────────────┐
│  1. Remove "AuditEvent" from disabled-logtypes.json                    │
│  2. Next DiagSettingsManager scan will:                                 │
│     ├─ Re-create log type via Site24x7 API                             │
│     ├─ Store new sourceConfig in blob                                  │
│     └─ Re-configure diagnostic settings for affected resources         │
└─────────────────────────────────────────────────────────────────────────┘
```

### 5.5 Workflow 5: Exclude Resources

**Trigger:** `PUT /api/ignore-list`

```
┌─────────────────────────────────────────────────────────────────────────┐
│  1. Save new ignore list to config/ignore-list.json                    │
│  2. Load currently configured resources                                │
│  3. For each configured resource:                                      │
│     ├─ Check if now matches an ignore rule (RG, location, or ID)      │
│     ├─ If newly ignored:                                               │
│     │   ├─ DELETE diagnostic setting "s247-diag-logs"                  │
│     │   └─ Remove from configured-resources.json                       │
│  4. Return: updated ignore list + count of removed settings            │
└─────────────────────────────────────────────────────────────────────────┘
```

### 5.6 Workflow 6: Auto-Update

**Trigger:** Timer — daily at 3:00 AM UTC (`0 0 3 * * *`)

```
┌─────────────────────────────────────────────────────────────────────────┐
│  1. Read UPDATE_CHECK_URL env var (skip if not set)                    │
│  2. GET remote version.json from URL                                   │
│  3. Compare semver: local VERSION file vs remote version               │
│  4. If remote > local:                                                  │
│     ├─ Download package from package_url (timeout: 300s)               │
│     ├─ POST to ARM zipdeploy API                                       │
│     └─ Function App restarts with new version                          │
│  5. If local >= remote: log "up_to_date"                               │
└─────────────────────────────────────────────────────────────────────────┘
```

---

## 6. Shared Modules

### 6.1 region_manager.py — Regional Storage Lifecycle

Manages per-region Storage Account provisioning, deprovisioning, and reconciliation.

| Method | Purpose |
|--------|---------|
| `get_storage_name_for_region(region, suffix)` | Generate name: `s247diag{region}{suffix}` (max 24 chars) |
| `get_active_regions(resources)` | Extract unique regions from resource list |
| `get_provisioned_regions(resource_group)` | List accounts by tag: `managed-by: s247-diag-logs` |
| `provision_storage_account(rg, region, suffix)` | Create account + container + CanNotDelete lock |
| `deprovision_storage_account(rg, region, name)` | Remove lock + delete account |
| `reconcile_regions(rg, active, provisioned, suffix)` | Add missing / remove unused regions |
| `apply_lock(rg, name, type)` | Apply `CanNotDelete` management lock |
| `remove_lock(rg, lock_name)` | Remove lock before deletion |

### 6.2 azure_manager.py — Azure SDK Operations

Wraps all Azure Management SDK calls behind a clean interface.

| Method | Azure SDK Client | Purpose |
|--------|-----------------|---------|
| `get_all_resources(sub_ids)` | `ResourceManagementClient` | List all diagnostic-capable resources |
| `supports_diagnostic_logs(id, type)` | `MonitorManagementClient` | Check if resource type has log categories (cached) |
| `get_diagnostic_categories(id)` | `MonitorManagementClient` | List log category names for a resource |
| `get_diagnostic_setting(id)` | `MonitorManagementClient` | Check if `s247-diag-logs` setting exists |
| `create_diagnostic_setting(id, sa_id)` | `MonitorManagementClient` | Create with `allLogs` category group |
| `delete_diagnostic_setting(id)` | `MonitorManagementClient` | Remove `s247-diag-logs` setting |
| `remove_all_diagnostic_settings(sub_ids)` | `MonitorManagementClient` | Bulk removal across all resources |
| `update_app_setting(key, value)` | `WebSiteManagementClient` | Update Function App configuration |

### 6.3 config_store.py — Blob-Based Configuration

All configuration stored in blobs for dynamic updates without redeployment.

| Function | Blob Path | Purpose |
|----------|-----------|---------|
| `get_supported_log_types()` | `config/azure-log-types.json` | Cached supported types from S247 |
| `get_logtype_config(cat)` | `config/logtype-configs/S247_{cat}.json` | Per-category sourceConfig |
| `get_all_logtype_configs()` | `config/logtype-configs/*.json` | All configs for BlobLogProcessor |
| `get_disabled_log_types()` | `config/disabled-logtypes.json` | Disabled category list |
| `get_configured_resources()` | `config/configured-resources.json` | Resource tracking map |
| `clear_cache()` | N/A | Reset in-memory cache per invocation |

**Caching Strategy:** In-memory cache per function invocation. `clear_cache()` called at start of every scan. Reduces blob reads during a single invocation cycle.

### 6.4 site24x7_client.py — Site24x7 API Client

| Method | Endpoint | Purpose |
|--------|----------|---------|
| `get_supported_log_types()` | `GET /applog/azure/logtype_supported` | Fetch 50+ supported Azure log types |
| `create_log_types(categories)` | `POST /applog/azure/logtype_create` | Batch-create log types, returns sourceConfig |
| `check_log_type(category)` | `GET /applog/logtype` | Verify log type exists |
| `post_logs(config_b64, events)` | `POST https://{domain}/upload` | Full log upload pipeline |

**Log Upload Pipeline (post_logs):**
```
Input: base64 sourceConfig + raw Azure log events
  │
  ├─ 1. Decode sourceConfig → jsonPath rules, masking, hashing, filters, derived
  ├─ 2. Parse events: extract fields via jsonPath mapping
  ├─ 3. Apply filters: include/exclude based on field pattern matching
  ├─ 4. Apply masking: regex match → replace with mask string
  ├─ 5. Apply hashing: regex match → replace with SHA256 hex digest
  ├─ 6. Apply derived fields: regex named groups → new fields
  ├─ 7. Add metadata: _zl_timestamp (ms epoch), s247agentuid (resource group)
  ├─ 8. Gzip compress JSON array
  └─ 9. POST to https://{uploadDomain}/upload
         Headers: X-DeviceKey, X-LogType, X-StreamMode:1,
                  Log-Size, Content-Encoding:gzip,
                  User-Agent: AZURE-DiagLogs-Function
```

### 6.5 ignore_list.py — Resource Filtering

Multi-level filtering with case-insensitive matching:

```
Resource → is_ignored()?
  ├─ Check: resource_id ∈ ignore_list.resource_ids?  → IGNORED
  ├─ Check: resource_group ∈ ignore_list.resource_groups?  → IGNORED
  ├─ Check: location ∈ ignore_list.locations?  → IGNORED
  └─ None matched → NOT IGNORED
```

### 6.6 log_parser.py — Diagnostic Record Parsing

Parses Azure diagnostic log envelope format:
```json
{
  "records": [
    {
      "time": "2026-01-01T00:00:00Z",
      "resourceId": "/subscriptions/.../providers/.../resource",
      "category": "AuditEvent",
      "operationName": "VaultGet",
      "resultType": "Success",
      "level": "Information",
      "properties": { ... }
    }
  ]
}
```

### 6.7 updater.py — Self-Update Mechanism

Checks remote `version.json` and auto-deploys via ARM zipdeploy API.

---

## 7. Function Inventory

### 7.1 Timer-Triggered Functions

| Function | Schedule | Purpose |
|----------|----------|---------|
| **DiagSettingsManager** | `%TIMER_SCHEDULE%` (configurable) | Resource discovery, region reconciliation, log type creation, diagnostic settings configuration |
| **BlobLogProcessor** | `0 */2 * * * *` (every 2 min) | Poll regional storage accounts, parse log blobs, upload to Site24x7, cleanup |
| **AutoUpdater** | `0 0 3 * * *` (3 AM UTC daily) | Check for remote updates, auto-deploy if newer version found |

### 7.2 HTTP-Triggered Functions

| Function | Method | Route | Purpose |
|----------|--------|-------|---------|
| **Dashboard** | GET | `/api/dashboard` | Web UI with status, controls, monitoring |
| **GetStatus** | GET | `/api/status` | System overview: resources, regions, configs, errors |
| **HealthCheck** | GET | `/api/health` | Liveness probe + dependency checks |
| **TriggerScan** | POST | `/api/scan` | On-demand DiagSettingsManager execution |
| **GetIgnoreList** | GET | `/api/ignore-list` | List exclusion rules + available resources |
| **UpdateIgnoreList** | PUT | `/api/ignore-list` | Update exclusions + cleanup diagnostic settings |
| **GetDisabledLogTypes** | GET | `/api/disabled-logtypes` | List disabled categories + supported types |
| **UpdateDisabledLogTypes** | POST | `/api/disabled-logtypes` | Disable/enable log types with cleanup |
| **GetGeneralLogType** | GET | `/api/general-logtype` | General catch-all log type status |
| **UpdateGeneralLogType** | PUT | `/api/general-logtype` | Toggle general log type on/off |
| **StopProcessing** | PUT | `/api/processing` | Toggle BlobLogProcessor on/off |
| **RemoveDiagSettings** | POST | `/api/remove-diag-settings` | Bulk remove all diagnostic settings |
| **CheckUpdate** | GET/POST | `/api/check-update` | Check for updates / auto-apply |

### 7.3 Event-Triggered Functions (Alternative)

| Function | Trigger | Purpose |
|----------|---------|---------|
| **EventHubProcessor** | EventHub (`diag-logs`) | Alternative: process logs from Event Hub (requires `EVENTHUB_CONN`) |

---

## 8. API Reference

### 8.1 GET /api/status

Returns complete system status.

**Response:**
```json
{
  "last_scan_time": "2026-03-11T05:00:00Z",
  "subscription_ids": ["xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"],
  "general_logtype_enabled": false,
  "processing_enabled": true,
  "update_check_url": false,
  "provisioned_regions": [
    {"region": "eastus", "storage_account": "s247diageastusb6d656"},
    {"region": "westus", "storage_account": "s247diagwestusb6d656"}
  ],
  "resources": {"total": 150, "configured": 120, "ignored": 30},
  "logtypes": {
    "configured_count": 8,
    "configured_keys": ["S247_AuditEvent", "S247_SignInLogs", "..."],
    "disabled_count": 2,
    "disabled": ["AppServiceConsoleLogs", "AppServiceHTTPLogs"]
  },
  "configured_resources_count": 120,
  "errors": []
}
```

### 8.2 POST /api/scan

Triggers an immediate resource discovery and configuration scan.

**Response:** Same as DiagSettingsManager output statistics (see §5.1).

### 8.3 GET /api/ignore-list

**Response:**
```json
{
  "ignore_list": {
    "resource_groups": ["test-rg", "dev-rg"],
    "locations": ["westeurope"],
    "resource_ids": ["/subscriptions/.../providers/.../myVM"]
  },
  "available": {
    "resource_groups": ["prod-rg", "staging-rg", "..."],
    "locations": ["eastus", "westus", "westeurope", "..."],
    "resource_ids": [
      {"id": "...", "name": "myVM", "type": "Microsoft.Compute/virtualMachines", "location": "eastus", "resource_group": "prod-rg"}
    ]
  }
}
```

### 8.4 PUT /api/ignore-list

**Request:**
```json
{
  "resource_groups": ["test-rg", "dev-rg"],
  "locations": ["westeurope"],
  "resource_ids": ["/subscriptions/.../providers/.../myVM"]
}
```

**Response:**
```json
{
  "ignore_list": { ... },
  "diag_settings_removed": 5
}
```

### 8.5 GET /api/disabled-logtypes

**Response:**
```json
{
  "disabled_logtypes": ["AppServiceConsoleLogs"],
  "supported_types": [
    {"logtype": "AuditEvent", "display_name": "Audit Event", "disabled": false},
    {"logtype": "AppServiceConsoleLogs", "display_name": "App Service Console Logs", "disabled": true}
  ]
}
```

### 8.6 POST /api/disabled-logtypes

**Request:**
```json
{"action": "disable", "category": "AuditEvent"}
```

**Response:**
```json
{
  "action": "disable",
  "category": "AuditEvent",
  "diag_settings_removed": 3,
  "disabled_logtypes": ["AuditEvent", "AppServiceConsoleLogs"]
}
```

### 8.7 PUT /api/processing

**Request:**
```json
{"enabled": false}
```

**Response:**
```json
{"enabled": false, "message": "Log processing has been stopped"}
```

### 8.8 POST /api/remove-diagnostic-settings

**Response:**
```json
{
  "removed": 45,
  "skipped": 5,
  "errors": 0,
  "details": [
    {"id": "/subscriptions/.../myVM", "status": "removed"},
    {"id": "/subscriptions/.../mySQL", "status": "removed"}
  ]
}
```

### 8.9 GET /api/health

**Response:**
```json
{
  "status": "alive",
  "python_version": "3.11.x",
  "env_keys": ["SUBSCRIPTION_IDS", "SITE24X7_API_KEY", "..."],
  "dependencies": {
    "azure.identity": "ok",
    "azure.mgmt.resource": "ok",
    "azure.mgmt.monitor": "ok",
    "azure.mgmt.storage": "ok",
    "azure.mgmt.web": "ok",
    "azure.storage.blob": "ok",
    "requests": "ok",
    "shared.azure_manager": "ok",
    "shared.region_manager": "ok",
    "shared.ignore_list": "ok",
    "shared.log_parser": "ok",
    "shared.site24x7_client": "ok",
    "shared.updater": "ok"
  }
}
```

---

## 9. Data Storage Architecture

### 9.1 Primary Storage (AzureWebJobsStorage)

Used by the Function App runtime and for all configuration blobs.

```
AzureWebJobsStorage
├── config/                                    ← Configuration container
│   ├── azure-log-types.json                   ← Supported types cache from Site24x7
│   ├── disabled-logtypes.json                 ← ["AuditEvent", "SignInLogs"]
│   ├── configured-resources.json              ← {resource_id: {categories, storage, time}}
│   ├── ignore-list.json                       ← {resource_groups, locations, resource_ids}
│   └── logtype-configs/                       ← Per-category sourceConfig
│       ├── S247_AuditEvent.json
│       ├── S247_SignInLogs.json
│       ├── S247_SQLSecurityAuditEvents.json
│       └── ...
├── s247-checkpoints/                          ← Processing checkpoints
│   └── blob-processor-checkpoint.json         ← {account_name: last_processed_timestamp}
└── azure-webjobs-*/                           ← Function runtime (managed by Azure)
```

### 9.2 Regional Storage Accounts

Auto-provisioned per region. Azure Diagnostic Settings write logs here.

```
s247diag{region}{suffix}  (e.g., s247diageastusb6d656)
├── insights-logs/                             ← Base container (validation)
├── insights-logs-auditevent/                  ← Created by Azure Diagnostic Settings
│   └── resourceId=.../y=2026/m=03/d=11/h=05/m=00/PT1H.json
├── insights-logs-signinlogs/
│   └── resourceId=.../y=2026/m=03/d=11/h=05/m=00/PT1H.json
├── insights-logs-sqlsecurityauditevents/
│   └── ...
└── ...
```

### 9.3 Configured Resources Tracking

```json
{
  "/subscriptions/sub-123/resourceGroups/prod-rg/providers/Microsoft.KeyVault/vaults/myVault": {
    "categories": ["AuditEvent"],
    "storage_account": "s247diageastusb6d656",
    "configured_at": "2026-03-11T05:00:00Z"
  },
  "/subscriptions/sub-123/resourceGroups/prod-rg/providers/Microsoft.Sql/servers/mySQL": {
    "categories": ["SQLSecurityAuditEvents", "DatabaseWaitStatistics"],
    "storage_account": "s247diagwestusb6d656",
    "configured_at": "2026-03-11T05:00:00Z"
  }
}
```

---

## 10. Site24x7 Integration

### 10.1 Server-Side API (AppLogServlet — Java)

Two new endpoints added to `AppLogServlet.java`, authenticated via `deviceKey` query parameter.

#### GET /applog/azure/logtype_supported

Returns all supported Azure log types from `azureLogTypes.json` (50+ types).

```
GET /applog/azure/logtype_supported?deviceKey={api_key}

Response:
{
  "supported_types": [
    {
      "logtype": "S247_AuditEvent",
      "display_name": "Azure Audit Event",
      "log_categories": ["AuditEvent"]
    },
    ...
  ]
}
```

#### POST /applog/azure/logtype_create

Batch-creates log types for Azure resource categories.

```
POST /applog/azure/logtype_create?deviceKey={api_key}
Body: categories=["AuditEvent","SignInLogs","SQLSecurityAuditEvents"]

Response:
[
  {
    "category": "S247_AuditEvent",
    "sourceConfig": "eyJhcGlLZXkiOiAiLi4uIiwg..."  ← base64-encoded
  },
  ...
]
```

### 10.2 Authentication Flow

```
Function App                          Site24x7 Server
    │                                      │
    ├─ GET /applog/azure/logtype_supported │
    │  ?deviceKey={SITE24X7_API_KEY}       │
    │ ────────────────────────────────────►│
    │                                      ├─ Util.getUserIdFromAPIKey(deviceKey)
    │                                      │   ├─ Redis cache: AKEY-{key}
    │                                      │   ├─ Fallback: WM_API table lookup
    │                                      │   └─ Returns userId or null
    │                                      │
    │◄────────────────────────────────────│  200 + JSON (if valid)
    │                                      │  400 (if invalid key)
```

### 10.3 Log Upload Protocol

```
POST https://{uploadDomain}/upload

Headers:
  X-DeviceKey: {apiKey from sourceConfig}
  X-LogType: {logType from sourceConfig}
  X-StreamMode: 1
  Log-Size: {uncompressed byte size}
  Content-Type: application/json
  Content-Encoding: gzip
  User-Agent: AZURE-DiagLogs-Function

Body: gzip-compressed JSON array of parsed log records

Response Headers:
  x-uploadid: {upload tracking ID}
```

### 10.4 sourceConfig Structure

Returned by Site24x7 when creating a log type. Stored base64-encoded.

```json
{
  "apiKey": "device_key_for_uploads",
  "logType": "S247_AuditEvent",
  "uploadDomain": "logc.site24x7.com",
  "dateFormat": "%Y-%m-%dT%H:%M:%S.%f",
  "dateField": "time",
  "jsonPath": [
    {"name": "time", "key": "time"},
    {"name": "resourceId", "key": "resourceId"},
    {"name": "category", "key": "category"},
    {"name": "operationName", "key": "operationName"},
    {"name": "level", "key": "properties.level", "type": "string"},
    {"name": "properties", "key": "properties", "type": "json-object"}
  ],
  "filterConfig": {
    "level": {"match": true, "values": ["Error", "Warning", "Critical"]}
  },
  "maskingConfig": {
    "clientIpAddress": {"regex": "(\\d+\\.\\d+)\\.\\d+\\.\\d+", "string": "$1.xxx.xxx"}
  },
  "hashingConfig": {
    "callerIdentity": {"regex": "(.+)"}
  },
  "derivedConfig": {
    "operationName": [{"regex": "(?P<service>\\w+)/(?P<action>\\w+)"}]
  }
}
```

---

## 11. Security Architecture

### 11.1 Authentication Layers

| Component | Method | Details |
|-----------|--------|---------|
| **HTTP Endpoints** | Function Keys | `authLevel: "function"` — requires `?code=<key>` or `x-functions-key` header |
| **Azure APIs** | Managed Identity | `DefaultAzureCredential()` with system-assigned identity |
| **Site24x7 API** | Device Key | `SITE24X7_API_KEY` passed as `deviceKey` query parameter |
| **Site24x7 Upload** | API Key | `X-DeviceKey` header from sourceConfig |

### 11.2 Function Key Management

```
Azure Portal → Function App → App Keys
  ├── Host Keys (apply to all functions)
  │   └── default: <auto-generated>
  └── Function Keys (per-function)
      └── default: <auto-generated>

Usage:
  curl "https://s247-diag-func-qt4xn2.azurewebsites.net/api/status?code=<KEY>"
  curl -H "x-functions-key: <KEY>" "https://s247-diag-func-qt4xn2.azurewebsites.net/api/status"
```

### 11.3 Key Rotation

If a function key is exposed:
1. Navigate to Azure Portal → Function App → App Keys
2. Renew the compromised key (generates new value)
3. Update any clients using the old key
4. Old key is immediately invalidated

### 11.4 Storage Security

| Setting | Value |
|---------|-------|
| Minimum TLS | 1.2 |
| Public blob access | Disabled |
| Management locks | `CanNotDelete` on regional accounts |
| Access method | Connection string (AzureWebJobsStorage) / Managed Identity |

---

## 12. Resilience & Error Handling

### 12.1 Rate Limiter

```
Type: Token Bucket
Rate: 100 tokens/second
Behavior: Blocks until token available (prevents API throttling)
```

### 12.2 Circuit Breaker

```
States: CLOSED (normal) → OPEN (failing) → HALF_OPEN (testing)

CLOSED → OPEN:    After 5 consecutive failures
OPEN → HALF_OPEN: After 300 seconds (5 minutes)
HALF_OPEN → CLOSED: On first success
HALF_OPEN → OPEN:   On first failure

When OPEN: post_logs() returns False immediately (logs are dropped to prevent backlog)
```

### 12.3 Checkpoint Recovery

- BlobLogProcessor tracks last processed blob timestamp per storage account
- On function restart/retry, processing resumes from checkpoint
- No duplicate uploads, no data loss (at-least-once delivery)

### 12.4 Error Handling Patterns

| Scenario | Handling |
|----------|---------|
| Resource doesn't support diagnostic logs | Skip, continue scanning |
| Storage account provisioning fails | Log error, continue with other regions |
| Site24x7 API unavailable | Circuit breaker opens, logs dropped temporarily |
| Blob download fails | Skip blob, continue processing others |
| Diagnostic setting creation fails | Log error, track in error count |
| ManagementLockClient unavailable | Graceful fallback, proceed without locks |
| Config blob not found | Return empty/default structure |
| Partial scan failure | Continue processing remaining resources |

---

## 13. Configuration Reference

### 13.1 Required Environment Variables

| Variable | Description | Example |
|----------|-------------|---------|
| `SUBSCRIPTION_IDS` | Comma-separated Azure subscription IDs | `xxxxxxxx-...,a1b2c3d4-...` |
| `SITE24X7_API_KEY` | Site24x7 device key for API authentication | `us_abc123def456` |
| `AzureWebJobsStorage` | Function App storage connection string | (auto-set by Azure) |
| `TIMER_SCHEDULE` | CRON expression for DiagSettingsManager | `0 0 */6 * * *` (every 6h) |

### 13.2 Optional Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `SITE24X7_BASE_URL` | `https://www.site24x7.com` | Site24x7 DC URL |
| `RESOURCE_GROUP_NAME` | `s247-diag-logs-rg` | Resource group name |
| `DIAG_STORAGE_SUFFIX` | `""` | Suffix for storage account naming |
| `PROCESSING_ENABLED` | `true` | Toggle BlobLogProcessor |
| `GENERAL_LOGTYPE_ENABLED` | `false` | Enable general catch-all log type |
| `S247_GENERAL_LOGTYPE` | (none) | Base64-encoded general sourceConfig |
| `UPDATE_CHECK_URL` | (none) | URL to version.json for auto-updates |
| `LAST_SCAN_TIME` | `never` | Last scan timestamp (auto-updated) |
| `FUNCTION_APP_NAME` | `s247-diag-logs-func` | Function App name |

### 13.3 Runtime Configuration (host.json)

```json
{
  "version": "2.0",
  "logging": {
    "applicationInsights": {
      "samplingSettings": {
        "isEnabled": true,
        "excludedTypes": "Request"
      }
    },
    "logLevel": {
      "default": "Information",
      "Host.Results": "Error",
      "Function": "Information"
    }
  },
  "extensionBundle": {
    "id": "Microsoft.Azure.Functions.ExtensionBundle",
    "version": "[4.*, 5.0.0)"
  }
}
```

---

## 14. Deployment Guide

### 14.1 Prerequisites

- Azure subscription with Owner or User Access Administrator role
- Azure CLI (`az`) or Azure Cloud Shell access
- Site24x7 account with API key (device key)

### 14.2 Deploy Function App Code

```bash
# From Azure Cloud Shell:
az functionapp deployment source config-zip \
  --resource-group s247-diag-logs-rg \
  --name s247-diag-func-qt4xn2 \
  --src s247-function-app.zip \
  --build-remote true
```

### 14.3 Configure App Settings

```bash
az functionapp config appsettings set \
  --name s247-diag-func-qt4xn2 \
  --resource-group s247-diag-logs-rg \
  --settings \
    SITE24X7_API_KEY="your_device_key" \
    SITE24X7_BASE_URL="https://www.site24x7.com" \
    SUBSCRIPTION_IDS="sub-id-1,sub-id-2" \
    TIMER_SCHEDULE="0 0 */6 * * *"
```

### 14.4 Assign RBAC Roles

```bash
SUB_ID="your-subscription-id"
PRINCIPAL="managed-identity-principal-id"

az role assignment create --assignee "$PRINCIPAL" \
  --role "Reader" --scope "/subscriptions/$SUB_ID"

az role assignment create --assignee "$PRINCIPAL" \
  --role "Monitoring Contributor" --scope "/subscriptions/$SUB_ID"

az role assignment create --assignee "$PRINCIPAL" \
  --role "Contributor" \
  --scope "/subscriptions/$SUB_ID/resourceGroups/s247-diag-logs-rg"
```

### 14.5 Verify Deployment

```bash
# Get function key
FUNC_KEY=$(az functionapp keys list \
  --name s247-diag-func-qt4xn2 \
  --resource-group s247-diag-logs-rg \
  --query "functionKeys.default" -o tsv)

# Health check
curl "https://s247-diag-func-qt4xn2.azurewebsites.net/api/health?code=$FUNC_KEY"

# System status
curl "https://s247-diag-func-qt4xn2.azurewebsites.net/api/status?code=$FUNC_KEY"

# Trigger first scan
curl -X POST "https://s247-diag-func-qt4xn2.azurewebsites.net/api/scan?code=$FUNC_KEY"

# Open dashboard
open "https://s247-diag-func-qt4xn2.azurewebsites.net/api/dashboard?code=$FUNC_KEY"
```

### 14.6 Deploy Java API Changes

Deploy the updated `AppLogServlet.java` and `web.xml` to the Site24x7 applogs server using your standard Java deployment process.

---

## Appendix A: File Inventory

| File | Type | Description |
|------|------|-------------|
| `shared/azure_manager.py` | Module | Azure SDK wrapper (resources, diagnostics, settings) |
| `shared/region_manager.py` | Module | Regional storage account lifecycle |
| `shared/config_store.py` | Module | Blob-backed configuration store |
| `shared/site24x7_client.py` | Module | Site24x7 API client + log upload pipeline |
| `shared/ignore_list.py` | Module | Resource filtering (RG, location, ID) |
| `shared/log_parser.py` | Module | Azure diagnostic log envelope parser |
| `shared/updater.py` | Module | Auto-update mechanism |
| `DiagSettingsManager/__init__.py` | Timer Function | Resource discovery + configuration |
| `BlobLogProcessor/__init__.py` | Timer Function | Blob polling + log upload + cleanup |
| `AutoUpdater/__init__.py` | Timer Function | Self-update check |
| `Dashboard/__init__.py` | HTTP Function | Web UI |
| `GetStatus/__init__.py` | HTTP Function | System status |
| `HealthCheck/__init__.py` | HTTP Function | Health probe |
| `TriggerScan/__init__.py` | HTTP Function | Manual scan trigger |
| `GetIgnoreList/__init__.py` | HTTP Function | Get exclusion rules |
| `UpdateIgnoreList/__init__.py` | HTTP Function | Update exclusions + cleanup |
| `GetDisabledLogTypes/__init__.py` | HTTP Function | Get disabled log types |
| `UpdateDisabledLogTypes/__init__.py` | HTTP Function | Disable/enable log types |
| `GetGeneralLogType/__init__.py` | HTTP Function | Get general log type status |
| `UpdateGeneralLogType/__init__.py` | HTTP Function | Toggle general log type |
| `StopProcessing/__init__.py` | HTTP Function | Toggle processing |
| `RemoveDiagSettings/__init__.py` | HTTP Function | Bulk remove diagnostic settings |
| `CheckUpdate/__init__.py` | HTTP Function | Check/apply updates |
| `EventHubProcessor/__init__.py` | EventHub Function | Alternative: Event Hub log processing |
| `requirements.txt` | Config | Python dependencies |
| `host.json` | Config | Function App runtime settings |
| `VERSION` | Config | Current version (1.0.0) |

---

*Document generated from codebase analysis — March 2026*
