"""
Load RabbitMQ worker settings from YAML plus environment (secrets).
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass(frozen=True)
class RabbitMQWorkerSettings:
    host: str
    port: int
    virtual_host: str
    queue_name: str
    prefetch_count: int
    reconnect_delay_seconds: float
    requeue_on_failure: bool
    username: str
    password: str


def _config_path() -> Path:
    env_path = os.environ.get("RABBITMQ_WORKER_CONFIG")
    if env_path:
        return Path(env_path)
    return Path(__file__).resolve().parent.parent / "config" / "rabbitmq_worker.yaml"


def load_rabbitmq_worker_settings() -> RabbitMQWorkerSettings:
    path = _config_path()
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    password = os.environ.get("RABBITMQ_PASSWORD")
    if not password:
        raise ValueError(
            "RABBITMQ_PASSWORD is required for the RabbitMQ worker (set in Kubernetes Secret)."
        )

    username = os.environ.get("RABBITMQ_USER") or raw.get("username") or "guest"

    def req(key: str):
        if key not in raw or raw[key] is None:
            raise ValueError(f"Missing required key in {path}: {key}")
        return raw[key]

    host = (os.environ.get("RABBITMQ_HOST") or (raw.get("host") if raw.get("host") is not None else "")).strip()
    if not host:
        raise ValueError(
            "RabbitMQ host is not set. Set environment variable RABBITMQ_HOST "
            f"(recommended in Kubernetes) or non-empty `host` in {path}. "
            "Use your broker Service DNS name, e.g. rabbitmq.my-namespace.svc.cluster.local."
        )
    port_s = os.environ.get("RABBITMQ_PORT")
    port = int(port_s) if port_s else int(req("port"))
    vhost = os.environ.get("RABBITMQ_VHOST") or str(req("virtual_host"))
    queue_s = os.environ.get("RABBITMQ_QUEUE")
    queue_name = queue_s if queue_s else str(req("queue_name"))

    return RabbitMQWorkerSettings(
        host=host,
        port=port,
        virtual_host=vhost,
        queue_name=queue_name,
        prefetch_count=int(req("prefetch_count")),
        reconnect_delay_seconds=float(req("reconnect_delay_seconds")),
        requeue_on_failure=bool(req("requeue_on_failure")),
        username=username,
        password=password,
    )
