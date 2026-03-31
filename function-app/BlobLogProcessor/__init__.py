"""Timer-triggered function that polls per-region Storage Accounts for diagnostic logs.

Polls blob containers every 2 minutes. Azure diagnostic settings write logs to
``insights-logs-{category}/...`` containers in per-region storage accounts
tagged ``managed-by: s247-diag-logs``. Loads sourceConfig from blob-based
config store (not env vars) and uses the proven log_sender.py upload flow.
"""

import os
import json
import logging
import time
import traceback
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import azure.functions as func
from azure.identity import DefaultAzureCredential
from azure.storage.blob import BlobServiceClient
from azure.mgmt.storage import StorageManagementClient

logger = logging.getLogger(__name__)

CHECKPOINT_CONTAINER = "s247-checkpoints"
CHECKPOINT_BLOB = "blob-processor-checkpoint.json"
# Blobs older than this are deleted even if they couldn't be forwarded to S247
STALE_BLOB_MAX_AGE_DAYS = 7
# Max records per upload batch — prevents memory issues and S247 payload limits
MAX_RECORDS_PER_BATCH = 5000
# Stop processing new blobs when this many seconds remain before function timeout
TIME_BUDGET_RESERVE_SEC = 60
# Warn when pending blob count exceeds this threshold
BACKLOG_WARN_THRESHOLD = 500
# Skip blobs larger than this to prevent OOM/timeout (50 MB)
MAX_BLOB_SIZE_BYTES = 50 * 1024 * 1024


def main(timer: func.TimerRequest) -> None:
    if timer.past_due:
        logger.warning("BlobLogProcessor: Timer is past due")

    processing_enabled = os.environ.get("PROCESSING_ENABLED", "true").lower() == "true"
    if not processing_enabled:
        logger.warning("BlobLogProcessor: Processing is DISABLED — skipping")
        return

    try:
        _process_all_regions()
    except Exception as e:
        logger.error("BlobLogProcessor: Unhandled error: %s", str(e))
        try:
            from shared.debug_logger import log_event
            log_event("error", "BlobLogProcessor", f"Unhandled error: {e}", {"traceback": traceback.format_exc()})
        except Exception:
            pass


def _process_all_regions():
    from shared.config_store import get_logtype_config, get_all_logtype_configs, clear_cache
    from shared.site24x7_client import Site24x7Client

    clear_cache()

    credential = DefaultAzureCredential()
    subscription_id = os.environ.get("SUBSCRIPTION_IDS", "").split(",")[0].strip()
    resource_group = os.environ.get(
        "RESOURCE_GROUP_NAME", os.environ.get("RESOURCE_GROUP", "s247-diag-logs-rg")
    )

    if not subscription_id:
        logger.error("BlobLogProcessor: No SUBSCRIPTION_IDS configured")
        return

    # Pre-load all logtype configs from blob storage
    all_configs = get_all_logtype_configs()
    general_config_b64 = os.environ.get("S247_GENERAL_LOGTYPE", "")
    general_enabled = os.environ.get("GENERAL_LOGTYPE_ENABLED", "false").lower() == "true"

    if not all_configs and not general_enabled:
        logger.info("BlobLogProcessor: No logtype configs found and general not enabled — nothing to process")
        return

    # Discover per-region storage accounts by tag
    storage_mgmt = StorageManagementClient(credential, subscription_id)
    regional_accounts = []
    for acct in storage_mgmt.storage_accounts.list_by_resource_group(resource_group):
        tags = acct.tags or {}
        if tags.get("managed-by") == "s247-diag-logs" and tags.get("purpose") == "diag-logs-regional":
            regional_accounts.append(acct)

    if not regional_accounts:
        logger.info("BlobLogProcessor: No regional storage accounts found — nothing to process")
        return

    logger.info("BlobLogProcessor: Found %d regional storage accounts, %d logtype configs",
                len(regional_accounts), len(all_configs))

    # Load checkpoint from the Function App's own storage
    main_conn_str = os.environ.get("AzureWebJobsStorage", "")
    checkpoints = _load_checkpoints(main_conn_str)

    client = Site24x7Client()
    total_stats = {"processed": 0, "uploaded": 0, "general": 0, "dropped": 0,
                   "blobs_deleted": 0, "stale_deleted": 0, "blobs_found": 0,
                   "batches_sent": 0, "time_budget_exhausted": False,
                   "error_blobs": []}
    stale_cutoff = datetime.now(timezone.utc) - timedelta(days=STALE_BLOB_MAX_AGE_DAYS)
    run_start = time.monotonic()

    for acct in regional_accounts:
        acct_name = acct.name
        region = (acct.tags or {}).get("region", acct.primary_location or "unknown")
        last_processed = checkpoints.get(acct_name, "")

        try:
            keys = storage_mgmt.storage_accounts.list_keys(resource_group, acct_name)
            acct_key = keys.keys[0].value
            conn_str = (
                f"DefaultEndpointsProtocol=https;AccountName={acct_name};"
                f"AccountKey={acct_key};EndpointSuffix=core.windows.net"
            )
            blob_service = BlobServiceClient.from_connection_string(conn_str)

            for container in blob_service.list_containers():
                cname = container["name"]
                if not cname.startswith("insights-logs-"):
                    continue

                # Extract category from container name: insights-logs-{category}
                raw_category = cname.replace("insights-logs-", "")
                # Normalize: remove hyphens to match S247 config key format
                category_normalized = raw_category.replace("-", "")
                config_key = f"S247_{category_normalized}"

                # Look up sourceConfig from blob store
                source_config_b64 = None
                if config_key in all_configs:
                    # Config is stored as JSON dict — re-encode to base64 for post_logs
                    import base64
                    source_config_b64 = base64.b64encode(
                        json.dumps(all_configs[config_key]).encode()
                    ).decode()
                elif general_enabled and general_config_b64:
                    source_config_b64 = general_config_b64
                else:
                    # No config — can't forward logs, clean up stale blobs with warning
                    logger.warning(
                        "BlobLogProcessor: No S247 config for category '%s' "
                        "(container: %s, account: %s) — logs cannot be forwarded. "
                        "Stale blobs older than %d days will be deleted.",
                        raw_category, cname, acct_name, STALE_BLOB_MAX_AGE_DAYS,
                    )
                    _cleanup_stale_blobs(blob_service, cname, stale_cutoff, total_stats)
                    continue

                container_client = blob_service.get_container_client(cname)
                processed_blobs = []

                for blob in container_client.list_blobs():
                    blob_time = blob.last_modified.isoformat() if blob.last_modified else ""
                    if last_processed and blob_time <= last_processed:
                        continue

                    # Time budget guard — stop processing before timeout
                    elapsed = time.monotonic() - run_start
                    if elapsed > (600 - TIME_BUDGET_RESERVE_SEC):
                        logger.warning(
                            "BlobLogProcessor: Time budget exhausted (%.0fs elapsed) — "
                            "remaining blobs will be processed next cycle",
                            elapsed,
                        )
                        total_stats["time_budget_exhausted"] = True
                        break

                    total_stats["blobs_found"] += 1

                    # Skip oversized blobs to prevent OOM/timeout
                    blob_size = blob.size or 0
                    if blob_size > MAX_BLOB_SIZE_BYTES:
                        logger.warning(
                            "BlobLogProcessor: Skipping oversized blob %s/%s "
                            "(%.1f MB > %.0f MB limit) in account %s",
                            cname, blob.name, blob_size / (1024*1024),
                            MAX_BLOB_SIZE_BYTES / (1024*1024), acct_name,
                        )
                        total_stats["dropped"] += 1
                        total_stats["error_blobs"].append({
                            "account": acct_name, "container": cname,
                            "blob": blob.name, "error": f"Oversized: {blob_size/(1024*1024):.1f} MB",
                        })
                        continue

                    # Clean up stale blobs that couldn't be processed
                    if blob.last_modified and blob.last_modified < stale_cutoff:
                        try:
                            container_client.delete_blob(blob.name)
                            total_stats["stale_deleted"] += 1
                        except Exception:
                            pass
                        continue

                    if not blob.name.endswith(".json"):
                        continue

                    try:
                        data = container_client.download_blob(blob.name).readall()
                        text = data.decode("utf-8")
                        # Azure diagnostic blobs may be:
                        # 1. Standard JSON: {"records": [...]}
                        # 2. NDJSON: one JSON object per line
                        try:
                            payload = json.loads(text)
                            records = payload.get("records", [])
                            if not records and isinstance(payload, dict):
                                # Single record not wrapped in "records"
                                records = [payload]
                        except json.JSONDecodeError:
                            # NDJSON format — parse each line separately
                            records = []
                            for line in text.splitlines():
                                line = line.strip()
                                if line:
                                    try:
                                        records.append(json.loads(line))
                                    except json.JSONDecodeError:
                                        pass

                        if not records:
                            processed_blobs.append(blob.name)
                            continue

                        total_stats["processed"] += len(records)

                        # Split large record sets into batches
                        all_success = True
                        for batch_start in range(0, len(records), MAX_RECORDS_PER_BATCH):
                            batch = records[batch_start:batch_start + MAX_RECORDS_PER_BATCH]
                            success = client.post_logs(source_config_b64, batch)
                            total_stats["batches_sent"] += 1
                            if success:
                                total_stats["uploaded"] += len(batch)
                                if config_key not in all_configs:
                                    total_stats["general"] += len(batch)
                            else:
                                total_stats["dropped"] += len(batch)
                                total_stats["error_blobs"].append({
                                    "account": acct_name, "container": cname,
                                    "blob": blob.name, "error": "post_logs failed",
                                })
                                all_success = False

                        if all_success:
                            processed_blobs.append(blob.name)
                            # Only advance checkpoint past successfully processed blobs
                            if blob_time and (not last_processed or blob_time > last_processed):
                                last_processed = blob_time

                    except Exception as e:
                        logger.error(
                            "BlobLogProcessor: Error reading blob %s/%s: %s",
                            cname, blob.name, str(e),
                        )
                        total_stats["dropped"] += 1
                        total_stats["error_blobs"].append({
                            "account": acct_name, "container": cname,
                            "blob": blob.name, "error": str(e)[:200],
                        })

                # Clean up successfully processed blobs
                for blob_name in processed_blobs:
                    try:
                        container_client.delete_blob(blob_name)
                        total_stats["blobs_deleted"] += 1
                    except Exception as e:
                        logger.warning(
                            "BlobLogProcessor: Failed to delete blob %s/%s: %s",
                            cname, blob_name, str(e),
                        )

                if total_stats["time_budget_exhausted"]:
                    break

            checkpoints[acct_name] = last_processed

        except Exception as e:
            logger.error(
                "BlobLogProcessor: Error processing account %s (%s): %s",
                acct_name, region, str(e),
            )

        if total_stats["time_budget_exhausted"]:
            break

    _save_checkpoints(main_conn_str, checkpoints)

    run_duration = time.monotonic() - run_start
    total_stats["duration_s"] = round(run_duration, 1)
    # Pending = found but not deleted (still in storage for next cycle)
    total_stats["pending_blobs"] = max(0,
        total_stats["blobs_found"] - total_stats["blobs_deleted"] - total_stats["stale_deleted"])

    logger.info(
        "BlobLogProcessor: Summary — processed=%d, uploaded=%d, general=%d, "
        "dropped=%d, blobs_deleted=%d, stale_deleted=%d, batches=%d, "
        "blobs_found=%d, pending=%d, duration=%.1fs%s",
        total_stats["processed"],
        total_stats["uploaded"],
        total_stats["general"],
        total_stats["dropped"],
        total_stats["blobs_deleted"],
        total_stats["stale_deleted"],
        total_stats["batches_sent"],
        total_stats["blobs_found"],
        total_stats["pending_blobs"],
        run_duration,
        " [TIME BUDGET EXHAUSTED]" if total_stats["time_budget_exhausted"] else "",
    )

    # Persist processing stats for Debug API
    try:
        from shared.debug_logger import save_processing_stats, log_event
        save_processing_stats(dict(total_stats))
        if total_stats["uploaded"] > 0 and total_stats["dropped"] == 0:
            log_event("info", "BlobLogProcessor",
                      f"Processed {total_stats['uploaded']} records in {total_stats['batches_sent']} batches "
                      f"({total_stats['duration_s']}s)",
                      {"stats": dict(total_stats)})
        if total_stats["dropped"] > 0:
            log_event("warning", "BlobLogProcessor",
                      f"Dropped {total_stats['dropped']} records",
                      {"stats": dict(total_stats)})
        if total_stats["pending_blobs"] > BACKLOG_WARN_THRESHOLD:
            log_event("warning", "BlobLogProcessor",
                      f"Backlog alert: {total_stats['pending_blobs']} pending blobs "
                      f"(threshold: {BACKLOG_WARN_THRESHOLD}). Processing may be "
                      "falling behind inflow rate.",
                      {"pending": total_stats["pending_blobs"],
                       "threshold": BACKLOG_WARN_THRESHOLD,
                       "duration_s": total_stats["duration_s"]})
        if total_stats["time_budget_exhausted"]:
            log_event("warning", "BlobLogProcessor",
                      f"Time budget exhausted after {total_stats['duration_s']}s — "
                      "remaining blobs deferred to next cycle",
                      {"stats": dict(total_stats)})
    except Exception:
        pass


def _load_checkpoints(conn_str: str) -> dict:
    """Load blob processing checkpoints from the main storage account."""
    if not conn_str:
        return {}
    try:
        blob_service = BlobServiceClient.from_connection_string(conn_str)
        container_client = blob_service.get_container_client(CHECKPOINT_CONTAINER)
        try:
            container_client.create_container()
        except Exception:
            pass
        blob_data = container_client.download_blob(CHECKPOINT_BLOB).readall()
        return json.loads(blob_data)
    except Exception:
        return {}


def _save_checkpoints(conn_str: str, checkpoints: dict) -> None:
    """Persist blob processing checkpoints to the main storage account."""
    if not conn_str:
        return
    try:
        blob_service = BlobServiceClient.from_connection_string(conn_str)
        container_client = blob_service.get_container_client(CHECKPOINT_CONTAINER)
        try:
            container_client.create_container()
        except Exception:
            pass
        container_client.upload_blob(
            CHECKPOINT_BLOB,
            json.dumps(checkpoints),
            overwrite=True,
        )
    except Exception as e:
        logger.error("BlobLogProcessor: Failed to save checkpoints: %s", str(e))


def _cleanup_stale_blobs(blob_service, container_name: str, cutoff, stats: dict):
    """Delete blobs older than cutoff in a container with no matching log type config."""
    try:
        container_client = blob_service.get_container_client(container_name)
        deleted = 0
        for blob in container_client.list_blobs():
            if blob.last_modified and blob.last_modified < cutoff:
                try:
                    container_client.delete_blob(blob.name)
                    deleted += 1
                except Exception:
                    pass
        if deleted:
            logger.warning(
                "BlobLogProcessor: Deleted %d stale blobs from %s (no S247 config, older than %d days) "
                "— these logs were NOT forwarded to Site24x7",
                deleted, container_name, STALE_BLOB_MAX_AGE_DAYS,
            )
            stats["stale_deleted"] += deleted
    except Exception as e:
        logger.warning("BlobLogProcessor: Failed to clean stale blobs from %s: %s", container_name, e)
