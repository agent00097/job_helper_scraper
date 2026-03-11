# Use Python 3.11 slim image as base
FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements file
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Create non-root user
RUN useradd -m -u 1000 appuser

# Copy application code (before switching user so we can set permissions)
COPY . .

# Copy and set up entrypoint script
COPY docker-entrypoint.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

# Change ownership of everything to appuser
RUN chown -R appuser:appuser /app /usr/local/bin/docker-entrypoint.sh

# Set environment variables
# PYTHONUNBUFFERED ensures logs appear immediately in Kubernetes/Lens
ENV PYTHONUNBUFFERED=1

# Switch to non-root user
USER appuser

# Use entrypoint script
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]

# Default command (using -u flag for unbuffered output to ensure logs appear in Lens)
CMD ["python", "-u", "main.py"]
