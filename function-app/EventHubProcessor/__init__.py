import os
import json
import logging
from collections import defaultdict



def main(events: list):
    from shared.log_parser import parse_diagnostic_records
    from shared.site24x7_client import Site24x7Client

    # Check if processing is enabled (stop button sets this to false)
    processing_enabled = os.environ.get("PROCESSING_ENABLED", "true").lower() == "true"
    if not processing_enabled:
        logging.warning("EventHubProcessor: Processing is DISABLED — dropping %d events", len(events))
        return

    logging.info("EventHubProcessor: Received batch of %d events", len(events))

    client = Site24x7Client()
    general_enabled = os.environ.get("GENERAL_LOGTYPE_ENABLED", "false").lower() == "true"

    stats = {"processed": 0, "specific": 0, "general": 0, "dropped": 0}
    # Batch records by logTypeConfig for efficient posting
    batches = defaultdict(list)

    for event in events:
        try:
            if isinstance(event, str):
                event_data = json.loads(event)
            else:
                event_data = event

            records = parse_diagnostic_records(event_data)
            stats["processed"] += len(records)

            for record in records:
                category = record.get("category", "")
                config_key = f"S247_{category}"
                log_type_config = os.environ.get(config_key)

                if log_type_config:
                    batches[log_type_config].append(record)
                    stats["specific"] += 1
                elif general_enabled:
                    general_config = os.environ.get("S247_GENERAL_LOGTYPE", "")
                    if general_config:
                        batches[general_config].append(record)
                        stats["general"] += 1
                    else:
                        logging.warning(
                            "EventHubProcessor: General logtype enabled but S247_GENERAL_LOGTYPE not set, dropping record category=%s",
                            category,
                        )
                        stats["dropped"] += 1
                else:
                    logging.warning(
                        "EventHubProcessor: No log type config for category=%s, dropping record",
                        category,
                    )
                    stats["dropped"] += 1

        except Exception as e:
            logging.error("EventHubProcessor: Error processing event: %s", str(e))
            stats["dropped"] += 1

    # Post each batch to Site24x7
    for config, records in batches.items():
        try:
            client.post_logs(config, records)
            logging.info("EventHubProcessor: Posted %d records to config=%s", len(records), config)
        except Exception as e:
            logging.error("EventHubProcessor: Failed to post batch config=%s: %s", config, str(e))

    logging.info(
        "EventHubProcessor: Summary — processed=%d, specific=%d, general=%d, dropped=%d",
        stats["processed"],
        stats["specific"],
        stats["general"],
        stats["dropped"],
    )
