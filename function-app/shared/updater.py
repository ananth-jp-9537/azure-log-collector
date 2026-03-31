"""Self-update logic for the Function App.

Checks a remote endpoint for newer versions and deploys updates
automatically using the Azure Management API.

Supports two URL formats for UPDATE_CHECK_URL:

1. **Direct version.json** — a JSON file with:
   ``{"version": "1.1.0", "package_url": "https://...", "release_notes": "..."}``

2. **GitHub Releases API** — the ``/releases/latest`` endpoint, e.g.
   ``https://api.github.com/repos/owner/repo/releases/latest``
   The release tag must be ``vX.Y.Z`` and have a ``s247-function-app.zip``
   asset attached.  A ``version.json`` asset is also accepted.

3. **GitHub shorthand** — just ``owner/repo`` (e.g. ``ananth-jp-9537/azure-log-collector``).
   Automatically expanded to the GitHub Releases latest API URL.
"""

import json
import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests
from azure.identity import DefaultAzureCredential

logger = logging.getLogger(__name__)

VERSION_FILE = Path(__file__).resolve().parent.parent / "VERSION"


def get_local_version() -> str:
    """Read the current version from the VERSION file."""
    try:
        return VERSION_FILE.read_text().strip()
    except Exception:
        return "0.0.0"


def parse_version(version_str: str) -> Tuple[int, ...]:
    """Parse a semver string into a comparable tuple."""
    try:
        return tuple(int(x) for x in version_str.split("."))
    except (ValueError, AttributeError):
        return (0, 0, 0)


def _resolve_update_url(raw_url: str) -> str:
    """Resolve shorthand ``owner/repo`` to full GitHub API URL.

    If the URL is already a full HTTP URL it is returned as-is.
    """
    raw_url = raw_url.strip()
    if raw_url.startswith(("http://", "https://")):
        return raw_url
    # Treat as GitHub owner/repo shorthand
    if re.match(r"^[\w.-]+/[\w.-]+$", raw_url):
        return f"https://api.github.com/repos/{raw_url}/releases/latest"
    return raw_url


def _parse_github_release(data: Dict) -> Optional[Dict]:
    """Extract version info from a GitHub Releases API response."""
    tag = data.get("tag_name", "")
    version = tag.lstrip("v")
    if not version:
        return None

    assets: List[Dict] = data.get("assets", [])

    # Look for version.json asset first (has explicit package_url)
    for asset in assets:
        if asset.get("name") == "version.json":
            try:
                dl_url = asset.get("browser_download_url", "")
                resp = requests.get(dl_url, timeout=30)
                resp.raise_for_status()
                return resp.json()
            except Exception:
                logger.debug("Could not download version.json asset — falling back to zip detection")

    # Fall back to finding the zip asset directly
    zip_url = None
    for asset in assets:
        name = asset.get("name", "")
        if name.endswith(".zip") and "function-app" in name.lower():
            zip_url = asset.get("browser_download_url")
            break

    if not zip_url:
        # Accept any .zip asset
        for asset in assets:
            if asset.get("name", "").endswith(".zip"):
                zip_url = asset.get("browser_download_url")
                break

    if not zip_url:
        logger.error("GitHub release %s has no zip asset", tag)
        return None

    return {
        "version": version,
        "package_url": zip_url,
        "release_notes": data.get("body", ""),
    }


def fetch_remote_version(update_url: str) -> Optional[Dict]:
    """Fetch the remote version info from UPDATE_CHECK_URL.

    Supports both direct ``version.json`` and GitHub Releases API responses.
    Returns dict with ``version``, ``package_url``, ``release_notes`` or None.
    """
    resolved_url = _resolve_update_url(update_url)

    try:
        headers = {"Accept": "application/json"}
        # Add GitHub API header if it's a GitHub URL
        if "api.github.com" in resolved_url:
            headers["Accept"] = "application/vnd.github+json"
            gh_token = os.environ.get("GITHUB_TOKEN", "")
            if gh_token:
                headers["Authorization"] = f"Bearer {gh_token}"

        resp = requests.get(resolved_url, timeout=30, headers=headers)
        resp.raise_for_status()
        data = resp.json()

        # Detect GitHub Releases API response (has tag_name field)
        if "tag_name" in data:
            return _parse_github_release(data)

        # Direct version.json format
        if "version" not in data or "package_url" not in data:
            logger.error("Remote version.json missing 'version' or 'package_url'")
            return None
        return data

    except Exception as e:
        logger.error("Failed to fetch remote version from %s: %s", resolved_url, e)
        return None


def is_update_available(local_version: str, remote_version: str) -> bool:
    """Compare versions — returns True if remote is newer."""
    return parse_version(remote_version) > parse_version(local_version)


def deploy_update(package_url: str) -> Dict:
    """Download the package and deploy it to this Function App.

    Uses the Azure ARM API zipdeploy endpoint with Managed Identity.
    """
    resource_group = os.environ.get(
        "RESOURCE_GROUP_NAME", os.environ.get("RESOURCE_GROUP", "s247-diag-logs-rg")
    )
    func_app_name = os.environ.get("WEBSITE_SITE_NAME", "")
    sub_id = os.environ.get("SUBSCRIPTION_IDS", "").split(",")[0].strip()

    if not func_app_name:
        return {"success": False, "error": "WEBSITE_SITE_NAME not set"}
    if not sub_id:
        return {"success": False, "error": "No subscription ID available"}

    try:
        # Download the package
        logger.info(f"Downloading update from {package_url} ...")
        with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
            resp = requests.get(package_url, timeout=300, stream=True)
            resp.raise_for_status()
            for chunk in resp.iter_content(chunk_size=8192):
                tmp.write(chunk)
            tmp_path = tmp.name
        logger.info(f"Downloaded to {tmp_path}")

        # Deploy via ARM API
        credential = DefaultAzureCredential()
        token = credential.get_token("https://management.azure.com/.default")

        deploy_url = (
            f"https://management.azure.com/subscriptions/{sub_id}"
            f"/resourceGroups/{resource_group}"
            f"/providers/Microsoft.Web/sites/{func_app_name}"
            f"/extensions/zipdeploy?api-version=2023-01-01"
        )

        with open(tmp_path, "rb") as f:
            deploy_resp = requests.post(
                deploy_url,
                headers={
                    "Authorization": f"Bearer {token.token}",
                    "Content-Type": "application/octet-stream",
                },
                data=f,
                timeout=600,
            )

        os.unlink(tmp_path)

        if deploy_resp.status_code in (200, 202):
            logger.info("Update deployed successfully")
            return {"success": True, "status_code": deploy_resp.status_code}
        else:
            error_msg = deploy_resp.text[:500]
            logger.error(
                f"Deploy failed (HTTP {deploy_resp.status_code}): {error_msg}"
            )
            return {
                "success": False,
                "status_code": deploy_resp.status_code,
                "error": error_msg,
            }

    except Exception as e:
        logger.error(f"Update deployment failed: {e}")
        return {"success": False, "error": str(e)}


def check_and_apply_update(auto_apply: bool = False) -> Dict:
    """Full update check workflow.

    Args:
        auto_apply: If True, automatically deploy the update.
                    If False, only report availability.

    Returns:
        Status dict with update info and action taken.
    """
    update_url = os.environ.get("UPDATE_CHECK_URL", "")
    if not update_url:
        return {
            "update_available": False,
            "message": "UPDATE_CHECK_URL not configured — auto-updates disabled",
            "local_version": get_local_version(),
        }

    local_ver = get_local_version()
    remote_info = fetch_remote_version(update_url)

    if not remote_info:
        return {
            "update_available": False,
            "message": "Could not fetch remote version info",
            "local_version": local_ver,
        }

    remote_ver = remote_info["version"]
    has_update = is_update_available(local_ver, remote_ver)

    result = {
        "update_available": has_update,
        "local_version": local_ver,
        "remote_version": remote_ver,
        "release_notes": remote_info.get("release_notes", ""),
    }

    if has_update and auto_apply:
        logger.info(f"Applying update: {local_ver} → {remote_ver}")
        deploy_result = deploy_update(remote_info["package_url"])
        result["deploy_result"] = deploy_result
        result["action"] = "deployed" if deploy_result["success"] else "deploy_failed"
    elif has_update:
        result["action"] = "update_available"
        result["package_url"] = remote_info["package_url"]
    else:
        result["action"] = "up_to_date"

    return result
