"""
Entry point for source_reconcile_tasks consumers ("Find broken boards").

Run with RABBITMQ_QUEUE=source_reconcile_tasks. One replica is enough: a run
takes ~30 minutes and runs are rare.
"""
import logging
import sys

from services.source_reconcile import process_reconcile_body
from workers.rabbitmq_worker import RabbitMQJobWorker

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

logger = logging.getLogger(__name__)


def main() -> None:
    logger.info("Starting source reconcile worker...")
    try:
        RabbitMQJobWorker(on_body=process_reconcile_body).run_forever()
    except ValueError as e:
        logger.error("%s", e)
        sys.exit(1)


if __name__ == "__main__":
    main()
