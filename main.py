"""
Main entry point for the job scraper service.
"""
import logging
import signal
import sys
from scheduler import Scheduler

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

logger = logging.getLogger(__name__)


def main():
    """Main function to start the scheduler."""
    logger.info("Starting job scraper service...")
    
    # Create and start scheduler
    scheduler = Scheduler()
    
    # Handle graceful shutdown
    def signal_handler(sig, frame):
        logger.info("Received shutdown signal, stopping scheduler...")
        scheduler.stop()
        sys.exit(0)
    
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    try:
        scheduler.start()
        
        # Keep main thread alive
        logger.info("Service running. Press Ctrl+C to stop.")
        while True:
            import time
            time.sleep(1)
            
    except KeyboardInterrupt:
        logger.info("Received keyboard interrupt")
        scheduler.stop()
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        scheduler.stop()
        sys.exit(1)


if __name__ == "__main__":
    main()
