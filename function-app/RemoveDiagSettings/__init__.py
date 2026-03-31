import os
import json
import logging

import azure.functions as func


def main(req: func.HttpRequest) -> func.HttpResponse:
    from shared.azure_manager import AzureManager

    """Remove s247-diag-logs diagnostic settings from all resources.

    POST /api/remove-diagnostic-settings

    Iterates through all resources in configured subscriptions and
    deletes the s247-diag-logs diagnostic setting where it exists.
    """
    logging.info("RemoveDiagSettings: Starting bulk removal of diagnostic settings")

    try:
        subscription_ids = [
            s.strip()
            for s in os.environ.get("SUBSCRIPTION_IDS", "").split(",")
            if s.strip()
        ]

        if not subscription_ids:
            return func.HttpResponse(
                json.dumps({"error": "No SUBSCRIPTION_IDS configured"}),
                mimetype="application/json",
                status_code=400,
            )

        azure_mgr = AzureManager()
        result = azure_mgr.remove_all_diagnostic_settings(subscription_ids)

        logging.info(
            "RemoveDiagSettings: Complete — removed=%d, skipped=%d, errors=%d",
            result["removed"],
            result["skipped"],
            result["errors"],
        )

        # Disable auto-scan to prevent the next timer from recreating settings
        try:
            azure_mgr.update_app_setting("AUTO_SCAN_ENABLED", "false")
            os.environ["AUTO_SCAN_ENABLED"] = "false"
            result["auto_scan_disabled"] = True
            logging.info("RemoveDiagSettings: Auto-scan disabled to prevent recreation")
        except Exception as e:
            logging.warning("RemoveDiagSettings: Failed to disable auto-scan: %s", e)
            result["auto_scan_disabled"] = False

        return func.HttpResponse(
            json.dumps(result, indent=2),
            mimetype="application/json",
            status_code=200,
        )

    except Exception as e:
        logging.error("RemoveDiagSettings: Error: %s", str(e))
        return func.HttpResponse(
            json.dumps({"error": str(e)}),
            mimetype="application/json",
            status_code=500,
        )
