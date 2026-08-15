"""
Entry point for company_scrape_tasks consumers (scale this Deployment).
"""
import logging
import sys

from services.company_scrape_queue import process_company_scrape_body
from workers.rabbitmq_worker import RabbitMQJobWorker

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

logger = logging.getLogger(__name__)


def main() -> None:
    logger.info("Starting company scrape queue worker...")
    try:
        RabbitMQJobWorker(on_body=process_company_scrape_body).run_forever()
    except ValueError as e:
        logger.error("%s", e)
        sys.exit(1)


if __name__ == "__main__":
    main()
