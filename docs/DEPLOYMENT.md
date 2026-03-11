# Deployment Guide

This guide explains how to deploy the job-helper-scraper service to Kubernetes.

## Prerequisites

- Docker installed
- Kubernetes cluster access
- kubectl configured
- Access to the container registry

## Building the Docker Image

1. **Build the image:**
   ```bash
   docker build -t job-helper-scraper:latest .
   ```

2. **Tag for your registry (if using one):**
   ```bash
   docker tag job-helper-scraper:latest your-registry/job-helper-scraper:latest
   ```

3. **Push to registry (if using one):**
   ```bash
   docker push your-registry/job-helper-scraper:latest
   ```

## Kubernetes Deployment

### 1. Update Configuration

Edit `kubernetes/deployment.yaml` and update:
- **Image name**: Change `job-helper-scraper:latest` to your registry path if needed
- **Database credentials**: Update `DB_NAME`, `DB_USER`, and `DB_PASSWORD` in ConfigMap and Secret
- **Namespace**: Update `resume-dev` if using a different namespace

### 2. Apply Configuration

```bash
kubectl apply -f kubernetes/deployment.yaml
```

### 3. Verify Deployment

```bash
# Check deployment status
kubectl get deployment job-helper-scraper -n resume-dev

# Check pods
kubectl get pods -n resume-dev -l app=job-helper-scraper

# View logs
kubectl logs -f deployment/job-helper-scraper -n resume-dev
```

## Environment Variables

The service uses the following environment variables (set via ConfigMap/Secret):

- `DB_HOST`: PostgreSQL host
- `DB_PORT`: PostgreSQL port
- `DB_NAME`: Database name
- `DB_USER`: Database user
- `DB_PASSWORD`: Database password (from Secret)

The `docker-entrypoint.sh` script automatically constructs `DATABASE_URL` from these components.

Alternatively, you can set `DATABASE_URL` directly if preferred.

## Service Behavior

- **No HTTP endpoints**: This is a background worker service, not a web API
- **Continuous operation**: Runs indefinitely, checking sources on their schedules
- **Logs to stdout**: All logs are sent to stdout for Kubernetes log collection
- **Single replica**: Runs as a single pod (no need for multiple replicas)

## Updating the Deployment

1. Build and push new image
2. Update image tag in `deployment.yaml` (if using versioned tags)
3. Apply changes:
   ```bash
   kubectl apply -f kubernetes/deployment.yaml
   kubectl rollout restart deployment/job-helper-scraper -n resume-dev
   ```

## Troubleshooting

### View Logs
```bash
kubectl logs -f deployment/job-helper-scraper -n resume-dev
```

### Check Pod Status
```bash
kubectl describe pod -l app=job-helper-scraper -n resume-dev
```

### Database Connection Issues
- Verify ConfigMap and Secret are created correctly
- Check database credentials
- Ensure network connectivity from pod to database

### Service Not Running
- Check pod logs for errors
- Verify database tables are created (run schema scripts)
- Ensure sources are enabled in `job_sources` table
