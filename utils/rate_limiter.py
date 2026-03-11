"""
Rate limiter utility for API requests.
"""
import time
from threading import Lock


class RateLimiter:
    """Simple rate limiter to enforce delays between requests."""
    
    def __init__(self, requests_per_minute: int):
        """
        Initialize rate limiter.
        
        Args:
            requests_per_minute: Maximum requests per minute
        """
        self.requests_per_minute = requests_per_minute
        self.delay_seconds = 60.0 / requests_per_minute if requests_per_minute > 0 else 0
        self.last_request_time = 0
        self.lock = Lock()
    
    def wait_if_needed(self):
        """Wait if necessary to respect rate limit."""
        with self.lock:
            current_time = time.time()
            time_since_last = current_time - self.last_request_time
            
            if time_since_last < self.delay_seconds:
                sleep_time = self.delay_seconds - time_since_last
                time.sleep(sleep_time)
            
            self.last_request_time = time.time()
