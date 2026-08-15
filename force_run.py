"""
CLI script to force run a source immediately.
Usage: python force_run.py <source_name>
Example: python force_run.py greenhouse
"""
import logging
import sys
from scheduler import Scheduler
from utils.source_loader import get_source_config

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

logger = logging.getLogger(__name__)


def main():
    """Main function to force run a source."""
    if len(sys.argv) < 2:
        print("Usage: python force_run.py <source_name>")
        print("Example: python force_run.py greenhouse")
        sys.exit(1)
    
    source_name = sys.argv[1]
    logger.info(f"Force running source: {source_name}")
    
    # Get source configuration
    source_config = get_source_config(source_name)
    
    if not source_config:
        logger.error(f"Source not found: {source_name}")
        sys.exit(1)
    
    if not source_config.get("enabled"):
        logger.warning(f"Source {source_name} is disabled")
        sys.exit(1)
    
    try:
        logger.info(f"Running worker for {source_name}...")
        scheduler = Scheduler()
        scheduler.run_source_worker(source_config, trigger="manual")
        logger.info("=" * 60)
        logger.info(f"Force run dispatched for {source_name}")
        logger.info("=" * 60)
        
    except Exception as e:
        logger.error(f"Error force running source {source_name}: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
