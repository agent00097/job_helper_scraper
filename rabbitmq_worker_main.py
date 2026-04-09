"""
Entry point for the RabbitMQ job scrape worker (Kubernetes / production).
"""
import logging
import sys

from workers.rabbitmq_worker import RabbitMQJobWorker

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

logger = logging.getLogger(__name__)


def main() -> None:
    logger.info("Starting RabbitMQ job scrape worker...")
    try:
        RabbitMQJobWorker().run_forever()
    except ValueError as e:
        logger.error("%s", e)
        sys.exit(1)


if __name__ == "__main__":
    main()
