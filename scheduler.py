"""
Scheduler for managing periodic job source workers.
"""
import logging
import time
import threading
from datetime import datetime, timedelta
from typing import Dict, List
from sources.source_factory import create_source
from utils.source_loader import get_source_config
from workers.source_worker import SourceWorker

logger = logging.getLogger(__name__)


class Scheduler:
    """Scheduler that manages periodic execution of source workers."""
    
    def __init__(self):
        """Initialize the scheduler."""
        self.running = False
        self.workers: Dict[str, threading.Thread] = {}
        self.source_configs: Dict[str, dict] = {}
    
    def load_sources(self) -> List[dict]:
        """
        Load all enabled sources from the database.
        
        Returns:
            List of source configurations
        """
        # For now, we'll load sources manually
        # In the future, we can query all enabled sources from database
        sources = []
        
        # Load Greenhouse
        greenhouse_config = get_source_config("greenhouse")
        if greenhouse_config and greenhouse_config.get("enabled"):
            sources.append(greenhouse_config)
        
        return sources
    
    def should_run_source(self, source_config: dict) -> bool:
        """
        Check if a source should run based on its schedule.
        
        Args:
            source_config: Source configuration
            
        Returns:
            True if source should run, False otherwise
        """
        last_run = source_config.get("last_run_at")
        schedule_hours = source_config.get("schedule_hours", 6)
        
        if not last_run:
            return True  # Never run before
        
        # Check if enough time has passed
        # Handle both datetime objects and strings
        if isinstance(last_run, str):
            try:
                last_run_time = datetime.fromisoformat(last_run.replace("Z", "+00:00"))
            except (ValueError, AttributeError):
                return True  # If we can't parse, run it
        elif isinstance(last_run, datetime):
            last_run_time = last_run
        else:
            return True  # Unknown type, run it
        
        next_run_time = last_run_time + timedelta(hours=schedule_hours)
        return datetime.now() >= next_run_time
    
    def run_source_worker(self, source_config: dict):
        """
        Run a single source worker.
        
        Args:
            source_config: Source configuration
        """
        source_name = source_config["name"]
        
        try:
            # Create source instance
            source = create_source(source_config)
            if not source:
                logger.error(f"Failed to create source: {source_name}")
                return
            
            # Create and run worker
            worker = SourceWorker(source)
            stats = worker.run()
            
            logger.info(f"Worker completed for {source_name}: {stats}")
            
        except Exception as e:
            logger.error(f"Error running worker for {source_name}: {e}", exc_info=True)
    
    def run_source_periodically(self, source_config: dict):
        """
        Run a source worker periodically in a separate thread.
        
        Args:
            source_config: Source configuration
        """
        source_name = source_config["name"]
        schedule_hours = source_config.get("schedule_hours", 6)
        
        logger.info(f"Starting periodic worker for {source_name} (every {schedule_hours} hours)")
        
        while self.running:
            try:
                # Check if it's time to run
                if self.should_run_source(source_config):
                    logger.info(f"Running scheduled worker for {source_name}")
                    self.run_source_worker(source_config)
                    
                    # Reload source config to get updated last_run_at
                    updated_config = get_source_config(source_name)
                    if updated_config:
                        source_config.update(updated_config)
                else:
                    logger.debug(f"Not yet time to run {source_name}, waiting...")
                
                # Sleep for a shorter interval and check again
                time.sleep(60)  # Check every minute
                
            except Exception as e:
                logger.error(f"Error in periodic worker for {source_name}: {e}", exc_info=True)
                time.sleep(300)  # Wait 5 minutes on error before retrying
    
    def start(self):
        """Start the scheduler."""
        logger.info("Starting scheduler...")
        self.running = True
        
        # Load all enabled sources
        sources = self.load_sources()
        
        if not sources:
            logger.warning("No enabled sources found")
            return
        
        logger.info(f"Loaded {len(sources)} enabled source(s)")
        
        # Start a thread for each source
        for source_config in sources:
            source_name = source_config["name"]
            
            # Run immediately on startup if needed
            if self.should_run_source(source_config):
                logger.info(f"Running {source_name} immediately on startup")
                # Run in a separate thread so we don't block
                thread = threading.Thread(
                    target=self.run_source_worker,
                    args=(source_config,),
                    daemon=True
                )
                thread.start()
            
            # Start periodic worker thread
            thread = threading.Thread(
                target=self.run_source_periodically,
                args=(source_config,),
                daemon=True,
                name=f"worker-{source_name}"
            )
            thread.start()
            self.workers[source_name] = thread
        
        logger.info(f"Scheduler started with {len(self.workers)} worker(s)")
    
    def stop(self):
        """Stop the scheduler."""
        logger.info("Stopping scheduler...")
        self.running = False
        
        # Wait for all workers to finish (with timeout)
        for source_name, thread in self.workers.items():
            logger.info(f"Waiting for worker {source_name} to finish...")
            thread.join(timeout=30)
        
        logger.info("Scheduler stopped")
