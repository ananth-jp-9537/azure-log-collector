"""AutoUpdater — Timer trigger that checks for and applies updates daily at 3 AM UTC."""

import logging

import azure.functions as func

logger = logging.getLogger(__name__)


def main(timer: func.TimerRequest) -> None:
    from shared.updater import check_and_apply_update

    if timer.past_due:
        logger.info("AutoUpdater timer is past due — running now")

    logger.info("AutoUpdater: checking for updates ...")
    result = check_and_apply_update(auto_apply=True)
    logger.info(f"AutoUpdater result: {result}")

    if result.get("action") == "deployed":
        logger.info(
            f"Successfully updated from {result['local_version']} "
            f"to {result['remote_version']}"
        )
    elif result.get("action") == "deploy_failed":
        logger.error(
            f"Update deployment failed: {result.get('deploy_result', {}).get('error', 'unknown')}"
        )
    elif result.get("action") == "up_to_date":
        logger.info(f"Already on latest version ({result['local_version']})")
    else:
        logger.info(f"Update check: {result.get('message', result.get('action', 'unknown'))}")
