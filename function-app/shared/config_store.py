"""Blob-backed configuration store for log type configs and settings.

Stores logtype configs, supported Azure log types, and disabled log types
in Azure Blob Storage under the 'config' container. Provides cached reads
and atomic writes.
"""

import json
import logging
import os
from typing import Dict, List, Optional

from azure.storage.blob import BlobServiceClient

logger = logging.getLogger(__name__)

CONTAINER_NAME = "config"
LOGTYPE_CONFIGS_PREFIX = "logtype-configs/"
SUPPORTED_TYPES_BLOB = "azure-log-types.json"
DISABLED_TYPES_BLOB = "disabled-logtypes.json"
CONFIGURED_RESOURCES_BLOB = "configured-resources.json"
CATEGORY_RESOURCE_TYPES_BLOB = "category-resource-types.json"
SCAN_STATE_BLOB = "scan-state.json"

# Sentinel for negative caching (config does not exist in blob)
_MISSING = object()

# In-memory caches (refreshed per function invocation cycle)
_cache = {
    "supported_types": None,
    "disabled_types": None,
    "logtype_configs": {},
    "configured_resources": None,
}


def _get_service_client() -> Optional[BlobServiceClient]:
    conn_str = os.environ.get("AzureWebJobsStorage", "")
    if not conn_str:
        logger.error("AzureWebJobsStorage environment variable is not set")
        return None
    return BlobServiceClient.from_connection_string(conn_str)


def _ensure_container(service_client: BlobServiceClient) -> None:
    container_client = service_client.get_container_client(CONTAINER_NAME)
    if not container_client.exists():
        container_client.create_container()
        logger.info("Created blob container '%s'", CONTAINER_NAME)


def _read_blob(blob_path: str) -> Optional[str]:
    service_client = _get_service_client()
    if not service_client:
        return None
    try:
        blob_client = service_client.get_blob_client(
            container=CONTAINER_NAME, blob=blob_path
        )
        return blob_client.download_blob().readall().decode("utf-8")
    except Exception as e:
        if "BlobNotFound" in str(e) or "not found" in str(e).lower():
            logger.debug("Blob not found: %s", blob_path)
        else:
            logger.error("Failed to read blob %s: %s", blob_path, e)
        return None


def _write_blob(blob_path: str, data: str) -> bool:
    service_client = _get_service_client()
    if not service_client:
        return False
    try:
        _ensure_container(service_client)
        blob_client = service_client.get_blob_client(
            container=CONTAINER_NAME, blob=blob_path
        )
        blob_client.upload_blob(data, overwrite=True)
        return True
    except Exception as e:
        logger.error("Failed to write blob %s: %s", blob_path, e)
        return False


def _delete_blob(blob_path: str) -> bool:
    service_client = _get_service_client()
    if not service_client:
        return False
    try:
        blob_client = service_client.get_blob_client(
            container=CONTAINER_NAME, blob=blob_path
        )
        blob_client.delete_blob()
        return True
    except Exception as e:
        if "BlobNotFound" not in str(e):
            logger.error("Failed to delete blob %s: %s", blob_path, e)
        return False


# ─── Supported Azure Log Types ───────────────────────────────────────────────


def get_supported_log_types() -> Dict:
    """Get supported Azure log types (cached in memory, persisted in blob)."""
    if _cache["supported_types"] is not None:
        return _cache["supported_types"]

    data = _read_blob(SUPPORTED_TYPES_BLOB)
    if data:
        _cache["supported_types"] = json.loads(data)
        return _cache["supported_types"]
    return {}


def save_supported_log_types(types_data: Dict) -> bool:
    """Save supported Azure log types to blob and update cache."""
    if _write_blob(SUPPORTED_TYPES_BLOB, json.dumps(types_data, indent=2)):
        _cache["supported_types"] = types_data
        return True
    return False


def is_supported_log_type(category: str) -> bool:
    """Check if a log category matches a supported Azure log type."""
    supported = get_supported_log_types()
    if not supported:
        return False
    normalized = category.replace("-", "").replace("_", "").replace(" ", "").lower()
    return normalized in supported


# ─── Log Type Configs (sourceConfig per category) ────────────────────────────


def _normalize_category(category: str) -> str:
    """Normalize category name to lowercase for consistent blob naming."""
    return category.replace("-", "").replace("_", "").replace(" ", "").lower()


def get_logtype_config(category: str) -> Optional[Dict]:
    """Get the sourceConfig for a specific log category."""
    config_key = f"S247_{_normalize_category(category)}"

    if config_key in _cache["logtype_configs"]:
        cached = _cache["logtype_configs"][config_key]
        # _MISSING sentinel means we already checked and it doesn't exist
        return None if cached is _MISSING else cached

    blob_path = f"{LOGTYPE_CONFIGS_PREFIX}{config_key}.json"
    data = _read_blob(blob_path)
    if data:
        config = json.loads(data)
        _cache["logtype_configs"][config_key] = config
        return config
    # Negative cache — avoid repeated blob reads for missing configs
    _cache["logtype_configs"][config_key] = _MISSING
    return None


def save_logtype_config(category: str, config: Dict) -> bool:
    """Save sourceConfig for a log category."""
    config_key = f"S247_{_normalize_category(category)}"
    blob_path = f"{LOGTYPE_CONFIGS_PREFIX}{config_key}.json"
    if _write_blob(blob_path, json.dumps(config, indent=2)):
        _cache["logtype_configs"][config_key] = config
        return True
    return False


def delete_logtype_config(category: str) -> bool:
    """Delete sourceConfig for a log category."""
    config_key = f"S247_{_normalize_category(category)}"
    blob_path = f"{LOGTYPE_CONFIGS_PREFIX}{config_key}.json"
    if _delete_blob(blob_path):
        _cache["logtype_configs"].pop(config_key, None)
        return True
    return False


def get_all_logtype_configs() -> Dict[str, Dict]:
    """List all stored logtype configs."""
    service_client = _get_service_client()
    if not service_client:
        return {}

    configs = {}
    try:
        container_client = service_client.get_container_client(CONTAINER_NAME)
        for blob in container_client.list_blobs(
            name_starts_with=LOGTYPE_CONFIGS_PREFIX
        ):
            if blob.name.endswith(".json"):
                key = blob.name.replace(LOGTYPE_CONFIGS_PREFIX, "").replace(
                    ".json", ""
                )
                data = _read_blob(blob.name)
                if data:
                    configs[key] = json.loads(data)
    except Exception as e:
        logger.error("Failed to list logtype configs: %s", e)
    return configs


# ─── Disabled Log Types ──────────────────────────────────────────────────────


def get_disabled_log_types() -> List[str]:
    """Get list of disabled log type categories."""
    if _cache["disabled_types"] is not None:
        return list(_cache["disabled_types"])

    data = _read_blob(DISABLED_TYPES_BLOB)
    if data:
        _cache["disabled_types"] = json.loads(data)
        return list(_cache["disabled_types"])
    return []


def save_disabled_log_types(disabled: List[str]) -> bool:
    """Save disabled log types list."""
    if _write_blob(DISABLED_TYPES_BLOB, json.dumps(disabled, indent=2)):
        _cache["disabled_types"] = disabled
        return True
    return False


def disable_log_type(category: str) -> bool:
    """Add a category to the disabled list."""
    disabled = get_disabled_log_types()
    normalized = category.lower()
    if normalized not in [d.lower() for d in disabled]:
        disabled.append(category)
        return save_disabled_log_types(disabled)
    return True


def enable_log_type(category: str) -> bool:
    """Remove a category from the disabled list."""
    disabled = get_disabled_log_types()
    updated = [d for d in disabled if d.lower() != category.lower()]
    if len(updated) != len(disabled):
        return save_disabled_log_types(updated)
    return True


def is_log_type_disabled(category: str) -> bool:
    """Check if a category is disabled."""
    disabled = get_disabled_log_types()
    return category.lower() in [d.lower() for d in disabled]


# ─── Configured Resources Tracking ──────────────────────────────────────────


def get_configured_resources() -> Dict:
    """Get the map of configured resources and their log type details.

    Structure: { resource_id: { "categories": [...], "storage_account": "...", "configured_at": "..." } }
    """
    if _cache["configured_resources"] is not None:
        return _cache["configured_resources"]

    data = _read_blob(CONFIGURED_RESOURCES_BLOB)
    if data:
        _cache["configured_resources"] = json.loads(data)
        return _cache["configured_resources"]
    return {}


def save_configured_resources(resources: Dict) -> bool:
    """Save configured resources map."""
    if _write_blob(CONFIGURED_RESOURCES_BLOB, json.dumps(resources, indent=2)):
        _cache["configured_resources"] = resources
        return True
    return False


# ─── Category → Resource Types mapping (from all discovered resources) ────────


def get_category_resource_types() -> Dict:
    """Get category → resource_types mapping built from all discovered resources."""
    data = _read_blob(CATEGORY_RESOURCE_TYPES_BLOB)
    if data:
        try:
            return json.loads(data)
        except json.JSONDecodeError:
            logger.error("Failed to parse category-resource-types blob")
    return {}


def save_category_resource_types(mapping: Dict) -> bool:
    """Save category → resource_types mapping to blob."""
    return _write_blob(CATEGORY_RESOURCE_TYPES_BLOB, json.dumps(mapping, indent=2))


def mark_resource_configured(
    resource_id: str, categories: List[str], storage_account: str
) -> bool:
    """Mark a resource as configured with its categories and target storage account."""
    from datetime import datetime, timezone

    configured = get_configured_resources()
    configured[resource_id] = {
        "categories": categories,
        "storage_account": storage_account,
        "configured_at": datetime.now(timezone.utc).isoformat(),
    }
    return save_configured_resources(configured)


def unmark_resource_configured(resource_id: str) -> bool:
    """Remove a resource from the configured tracking."""
    configured = get_configured_resources()
    if resource_id in configured:
        del configured[resource_id]
        return save_configured_resources(configured)
    return True


def clear_cache():
    """Clear all in-memory caches. Call at the start of each function invocation."""
    _cache["supported_types"] = None
    _cache["disabled_types"] = None
    _cache["logtype_configs"] = {}
    _cache["configured_resources"] = None


# ------------------------------------------------------------------
# Scan state (blob-backed, not app settings)
# ------------------------------------------------------------------

def save_scan_state(state: Dict) -> bool:
    """Save scan state (last scan time, stats) to blob storage."""
    return _write_blob(SCAN_STATE_BLOB, json.dumps(state, indent=2))


def get_scan_state() -> Dict:
    """Load scan state from blob storage."""
    raw = _read_blob(SCAN_STATE_BLOB)
    if raw:
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            logger.error("Failed to parse scan state blob")
    return {}
