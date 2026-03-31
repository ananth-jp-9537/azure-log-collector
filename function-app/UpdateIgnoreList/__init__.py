import json
import logging

import azure.functions as func


def main(req: func.HttpRequest) -> func.HttpResponse:
    from shared.ignore_list import load_ignore_list, save_ignore_list, is_ignored
    from shared.config_store import (
        get_configured_resources,
        save_configured_resources,
        unmark_resource_configured,
    )

    logging.info("UpdateIgnoreList: Updating ignore list")

    try:
        body = req.get_json()
    except ValueError:
        return func.HttpResponse(
            json.dumps({"error": "Invalid JSON body"}),
            mimetype="application/json",
            status_code=400,
        )

    try:
        save_ignore_list(body)
        updated = load_ignore_list()

        # Clean up diagnostic settings for newly ignored resources
        diag_removed = 0
        try:
            from shared.azure_manager import AzureManager
            azure_mgr = AzureManager()
            configured = get_configured_resources()
            resources_to_remove = []

            for resource_id in list(configured.keys()):
                resource_info = {
                    "id": resource_id,
                    "location": "",
                    "resource_group": "",
                }
                if is_ignored(resource_info, updated):
                    try:
                        azure_mgr.delete_diagnostic_setting(resource_id)
                        resources_to_remove.append(resource_id)
                        diag_removed += 1
                    except Exception as e:
                        logging.warning(
                            "Failed to remove diag setting for %s: %s",
                            resource_id, str(e),
                        )

            for rid in resources_to_remove:
                configured.pop(rid, None)
            if resources_to_remove:
                save_configured_resources(configured)
        except Exception as e:
            logging.warning("Error cleaning up diag settings for ignored resources: %s", str(e))

        return func.HttpResponse(
            json.dumps({
                "ignore_list": updated,
                "diag_settings_removed": diag_removed,
            }, indent=2),
            mimetype="application/json",
            status_code=200,
        )

    except Exception as e:
        logging.error("UpdateIgnoreList: Error: %s", str(e))
        return func.HttpResponse(
            json.dumps({"error": str(e)}),
            mimetype="application/json",
            status_code=500,
        )
