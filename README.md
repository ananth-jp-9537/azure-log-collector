# Azure Log Collector

Automatically collects diagnostic logs from **all** Azure resources across your subscriptions and forwards them to Site24x7 AppLogs.

**One-time setup, zero ongoing maintenance.** A timer trigger discovers new resources every 6 hours and configures them automatically.

## How It Works

```
Azure Resources ──► Storage Accounts (per region) ──► Function App ──► Site24x7 AppLogs
                          ▲                                │
                     Diagnostic Settings              Timer Trigger (6h)
                     (auto-configured) ◄──── discovers new resources
```

- **Storage Account per region** — Diagnostic settings stream logs to a storage account in the same region
- **Function App** — BlobLogProcessor polls every 2 min, parses logs, forwards to Site24x7
- **Web Dashboard** — Monitor status, manage filters, trigger scans, debug issues

---

## Quick Start

### Prerequisites

- Azure CLI (`az`) installed and logged in
- `jq` and `zip` installed
- One or more Azure subscriptions with resources to monitor

### Step 1: Clone & Configure

```bash
git clone https://github.com/ananth-jp-9537/azure-log-collector.git
cd azure-log-collector/setup
cp config.env.example config.env
```

Edit `config.env`:
```bash
# REQUIRED — your Azure subscription ID(s), comma-separated
SUBSCRIPTION_IDS="xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"

# REQUIRED — your Site24x7 API token
SITE24X7_API_TOKEN="your-token-here"

# OPTIONAL — Azure region (default: eastus)
FUNCTION_APP_REGION="eastus"
```

### Step 2: Deploy

```bash
bash setup.sh
```

This takes 5–10 minutes and provisions:
- Resource group, storage account, Function App
- Managed Identity with Reader + Monitoring Contributor roles
- Initial resource scan and diagnostic settings configuration

At the end, you'll see the **Dashboard URL** — open it in your browser.

### Step 3: Open the Dashboard

The dashboard is served directly from the Function App:
```
https://<FUNCTION_APP_NAME>.azurewebsites.net/api/dashboard?code=<FUNCTION_KEY>
```

---

## Dashboard

| Tab | What it shows |
|-----|---------------|
| **Overview** | Subscriptions, regions, last scan details, errors |
| **Filters** | Ignore lists (subscriptions, RGs, locations, types, tags) + log type toggles |
| **Resources** | All configured resources with categories and storage accounts |
| **Debug** | System health, config validation, recent events, processing runs |

**Controls** (always visible):
- Toggle log processing, auto-scan, general log type, pipeline monitoring
- Trigger manual scan, check for updates, remove all diagnostic settings

---

## Teardown

```bash
cd azure-log-collector/setup
bash teardown.sh
```

Removes resource locks, cleans up diagnostic settings, then deletes the resource group.

## Cost Estimate

| Resource | Cost |
|----------|------|
| Function App (Consumption) | ~$0.20/million executions |
| Storage Account (per region) | ~$0.02/GB/month |
| **Total (typical)** | **~$5–10/month** |

No Event Hubs needed — storage-based polling eliminates the ~$11/region/month cost.

---

## Project Structure

```
├── setup/
│   ├── config.env.example    ← Configuration template
│   ├── setup.sh              ← One-click provisioning
│   ├── teardown.sh           ← One-click cleanup
│   └── azuredeploy.json      ← ARM template
├── function-app/
│   ├── VERSION               ← Semantic version (triggers CI/CD releases)
│   ├── shared/               ← Core Python modules
│   ├── BlobLogProcessor/     ← Timer: polls storage, forwards logs to S247
│   ├── DiagSettingsManager/  ← Timer: 6h resource scan + config
│   ├── Dashboard/            ← Web dashboard (single-page HTML)
│   ├── tests/                ← 217 unit tests (pytest)
│   └── ...                   ← 17 more endpoints (21 functions total)
├── testing/
│   ├── mock_s247_server.py   ← Mock S247 for local E2E testing
│   ├── test_e2e.py           ← End-to-end test suite
│   └── sample_blobs/         ← Sample Azure diagnostic log blobs
├── docs/
│   ├── architecture-document.md
│   └── developer-guide.md
└── .github/workflows/
    └── release-function-app.yml  ← CI/CD: auto-release on VERSION bump
```

## Auto-Updates

The Function App can self-update from GitHub Releases:

1. Set `UPDATE_CHECK_URL` app setting to `ananth-jp-9537/azure-log-collector`
2. Bump `function-app/VERSION` and push to `main`
3. GitHub Actions creates a release with the deployable zip
4. The Function App detects the new version and offers update via dashboard

## Development

```bash
cd function-app
pip install -r requirements-dev.txt
python3 -m pytest tests/ -v     # 217 tests, ~3s
```

See [docs/developer-guide.md](docs/developer-guide.md) for detailed development setup.

## License

Internal — Site24x7 / Zoho Corporation
