"""Tests for shared/updater.py — version logic + mocked HTTP/Azure."""

import json
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from shared.updater import (
    get_local_version,
    parse_version,
    is_update_available,
    fetch_remote_version,
    deploy_update,
    check_and_apply_update,
    _resolve_update_url,
    _parse_github_release,
)


# ─── parse_version ──────────────────────────────────────────────────────────


class TestParseVersion:
    def test_normal_semver(self):
        assert parse_version("1.2.3") == (1, 2, 3, 1, 0)

    def test_major_only(self):
        assert parse_version("5") == (5, 1, 0)

    def test_two_part(self):
        assert parse_version("2.0") == (2, 0, 1, 0)

    def test_invalid(self):
        assert parse_version("not.a.ver") == (0, 0, 0, 0, 0)

    def test_none(self):
        assert parse_version(None) == (0, 0, 0, 0, 0)

    def test_empty(self):
        assert parse_version("") == (0, 0, 0, 0, 0)

    def test_prerelease_alpha(self):
        assert parse_version("0.1.0-alpha.1") == (0, 1, 0, 0, 1)

    def test_prerelease_beta(self):
        assert parse_version("2.0.0-beta") == (2, 0, 0, 0, 0)

    def test_prerelease_rc(self):
        assert parse_version("1.3.0-rc.2") == (1, 3, 0, 0, 2)

    def test_leading_v(self):
        assert parse_version("v1.2.3") == (1, 2, 3, 1, 0)

    def test_leading_v_with_prerelease(self):
        assert parse_version("v0.1.0-alpha.1") == (0, 1, 0, 0, 1)

    def test_alpha2_lt_alpha3(self):
        assert parse_version("0.1.0-alpha.2") < parse_version("0.1.0-alpha.3")

    def test_prerelease_lt_release(self):
        assert parse_version("0.1.0-alpha.9") < parse_version("0.1.0")

    def test_alpha3_gt_alpha2(self):
        assert parse_version("0.1.0-alpha.3") > parse_version("0.1.0-alpha.2")


# ─── is_update_available ────────────────────────────────────────────────────


class TestIsUpdateAvailable:
    def test_newer_available(self):
        assert is_update_available("1.0.0", "1.1.0") is True

    def test_same_version(self):
        assert is_update_available("1.0.0", "1.0.0") is False

    def test_older_remote(self):
        assert is_update_available("2.0.0", "1.0.0") is False

    def test_patch_bump(self):
        assert is_update_available("1.0.0", "1.0.1") is True

    def test_major_bump(self):
        assert is_update_available("1.9.9", "2.0.0") is True

    def test_prerelease_to_newer(self):
        assert is_update_available("0.1.0-alpha.1", "0.2.0") is True

    def test_same_prerelease(self):
        assert is_update_available("0.1.0-alpha.1", "0.1.0-alpha.2") is True  # alpha.2 > alpha.1


# ─── _resolve_update_url ────────────────────────────────────────────────────


class TestResolveUpdateUrl:
    def test_full_https_url(self):
        url = "https://example.com/version.json"
        assert _resolve_update_url(url) == url

    def test_github_shorthand(self):
        assert _resolve_update_url("owner/repo") == "https://api.github.com/repos/owner/repo/releases/latest"

    def test_github_shorthand_with_dots(self):
        assert _resolve_update_url("my-org/my-repo.v2") == "https://api.github.com/repos/my-org/my-repo.v2/releases/latest"

    def test_whitespace_trimmed(self):
        assert _resolve_update_url("  owner/repo  ") == "https://api.github.com/repos/owner/repo/releases/latest"


# ─── _parse_github_release ──────────────────────────────────────────────────


class TestParseGithubRelease:
    def test_with_zip_asset(self):
        data = {
            "tag_name": "v1.2.0",
            "body": "Bug fixes",
            "assets": [
                {
                    "name": "s247-function-app.zip",
                    "browser_download_url": "https://github.com/o/r/releases/download/v1.2.0/s247-function-app.zip",
                }
            ],
        }
        result = _parse_github_release(data)
        assert result["version"] == "1.2.0"
        assert result["package_url"].endswith(".zip")
        assert result["release_notes"] == "Bug fixes"

    def test_no_zip_asset(self):
        data = {"tag_name": "v1.0.0", "body": "", "assets": []}
        assert _parse_github_release(data) is None

    def test_no_tag(self):
        data = {"body": "", "assets": []}
        assert _parse_github_release(data) is None

    def test_fallback_to_any_zip(self):
        data = {
            "tag_name": "v2.0.0",
            "body": "",
            "assets": [
                {"name": "README.md", "browser_download_url": "https://example.com/README.md"},
                {"name": "deploy.zip", "browser_download_url": "https://example.com/deploy.zip"},
            ],
        }
        result = _parse_github_release(data)
        assert result["version"] == "2.0.0"
        assert result["package_url"] == "https://example.com/deploy.zip"


# ─── get_local_version ──────────────────────────────────────────────────────


class TestGetLocalVersion:
    @patch("shared.updater.VERSION_FILE")
    def test_reads_version_file(self, mock_path):
        mock_path.read_text.return_value = "1.2.3\n"
        assert get_local_version() == "1.2.3"

    @patch("shared.updater.VERSION_FILE")
    def test_fallback_on_error(self, mock_path):
        mock_path.read_text.side_effect = FileNotFoundError()
        assert get_local_version() == "0.0.0"


# ─── fetch_remote_version ───────────────────────────────────────────────────


class TestFetchRemoteVersion:
    @patch("shared.updater.requests.get")
    def test_direct_version_json(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "version": "2.0.0",
            "package_url": "https://example.com/v2.zip",
            "release_notes": "Bug fixes",
        }
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp

        result = fetch_remote_version("https://example.com/version.json")
        assert result["version"] == "2.0.0"
        assert result["package_url"] == "https://example.com/v2.zip"

    @patch("shared.updater.requests.get")
    def test_github_releases_api(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "tag_name": "v3.0.0",
            "body": "Major update",
            "assets": [
                {
                    "name": "s247-function-app.zip",
                    "browser_download_url": "https://github.com/o/r/releases/download/v3.0.0/s247-function-app.zip",
                },
            ],
        }
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp

        result = fetch_remote_version("https://api.github.com/repos/o/r/releases/latest")
        assert result["version"] == "3.0.0"
        assert "s247-function-app.zip" in result["package_url"]

    @patch("shared.updater.requests.get")
    def test_github_shorthand(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "tag_name": "v1.5.0",
            "body": "",
            "assets": [
                {
                    "name": "s247-function-app.zip",
                    "browser_download_url": "https://github.com/o/r/releases/download/v1.5.0/s247-function-app.zip",
                },
            ],
        }
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp

        result = fetch_remote_version("owner/repo")
        assert result["version"] == "1.5.0"

    @patch("shared.updater.requests.get")
    def test_missing_fields(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"version": "2.0.0"}  # missing package_url
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp

        assert fetch_remote_version("https://example.com/version.json") is None

    @patch("shared.updater.requests.get")
    def test_network_error(self, mock_get):
        mock_get.side_effect = Exception("Connection refused")
        assert fetch_remote_version("https://example.com/version.json") is None


# ─── deploy_update ───────────────────────────────────────────────────────────


class TestDeployUpdate:
    def test_no_func_app_name(self, monkeypatch):
        monkeypatch.setenv("SUBSCRIPTION_IDS", "sub1")
        result = deploy_update("https://example.com/pkg.zip")
        assert result["success"] is False
        assert "WEBSITE_SITE_NAME" in result["error"]

    def test_no_subscription(self, monkeypatch):
        monkeypatch.setenv("WEBSITE_SITE_NAME", "myapp")
        result = deploy_update("https://example.com/pkg.zip")
        assert result["success"] is False
        assert "subscription" in result["error"].lower()


# ─── check_and_apply_update ─────────────────────────────────────────────────


class TestCheckAndApplyUpdate:
    def test_no_update_url(self):
        result = check_and_apply_update()
        assert result["update_available"] is False
        assert "not configured" in result["message"]

    @patch("shared.updater.fetch_remote_version")
    @patch("shared.updater.get_local_version")
    def test_up_to_date(self, mock_local, mock_remote, monkeypatch):
        monkeypatch.setenv("UPDATE_CHECK_URL", "https://example.com/version.json")
        mock_local.return_value = "1.0.0"
        mock_remote.return_value = {
            "version": "1.0.0",
            "package_url": "https://example.com/v1.zip",
        }
        result = check_and_apply_update()
        assert result["update_available"] is False
        assert result["action"] == "up_to_date"

    @patch("shared.updater.fetch_remote_version")
    @patch("shared.updater.get_local_version")
    def test_update_available_no_auto(self, mock_local, mock_remote, monkeypatch):
        monkeypatch.setenv("UPDATE_CHECK_URL", "https://example.com/version.json")
        mock_local.return_value = "1.0.0"
        mock_remote.return_value = {
            "version": "2.0.0",
            "package_url": "https://example.com/v2.zip",
            "release_notes": "New stuff",
        }
        result = check_and_apply_update(auto_apply=False)
        assert result["update_available"] is True
        assert result["action"] == "update_available"
        assert result["package_url"] == "https://example.com/v2.zip"

    @patch("shared.updater.deploy_update")
    @patch("shared.updater.fetch_remote_version")
    @patch("shared.updater.get_local_version")
    def test_update_auto_apply(self, mock_local, mock_remote, mock_deploy, monkeypatch):
        monkeypatch.setenv("UPDATE_CHECK_URL", "https://example.com/version.json")
        mock_local.return_value = "1.0.0"
        mock_remote.return_value = {
            "version": "2.0.0",
            "package_url": "https://example.com/v2.zip",
        }
        mock_deploy.return_value = {"success": True, "status_code": 200}
        result = check_and_apply_update(auto_apply=True)
        assert result["action"] == "deployed"
        mock_deploy.assert_called_once_with("https://example.com/v2.zip")

    @patch("shared.updater.fetch_remote_version")
    @patch("shared.updater.get_local_version")
    def test_remote_fetch_fails(self, mock_local, mock_remote, monkeypatch):
        monkeypatch.setenv("UPDATE_CHECK_URL", "https://example.com/version.json")
        mock_local.return_value = "1.0.0"
        mock_remote.return_value = None
        result = check_and_apply_update()
        assert result["update_available"] is False
        assert "Could not fetch" in result["message"]

    @patch("shared.updater.fetch_remote_version")
    @patch("shared.updater.get_local_version")
    def test_github_shorthand_url(self, mock_local, mock_remote, monkeypatch):
        monkeypatch.setenv("UPDATE_CHECK_URL", "owner/repo")
        mock_local.return_value = "1.0.0"
        mock_remote.return_value = {
            "version": "1.1.0",
            "package_url": "https://github.com/owner/repo/releases/download/v1.1.0/s247-function-app.zip",
        }
        result = check_and_apply_update()
        assert result["update_available"] is True
        # Verify fetch was called with the shorthand (it internally resolves)
        mock_remote.assert_called_once_with("owner/repo")
