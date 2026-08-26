"""RabbitMQ worker settings env overrides."""
from __future__ import annotations

from workers.rabbitmq_settings import load_rabbitmq_worker_settings

_YAML = """
host: localhost
port: 5672
virtual_host: jobs
queue_name: q
prefetch_count: 2
reconnect_delay_seconds: 5
requeue_on_failure: true
username: u
"""


def test_prefetch_env_override(tmp_path, monkeypatch):
    cfg = tmp_path / "rabbitmq_worker.yaml"
    cfg.write_text(_YAML)
    monkeypatch.setenv("RABBITMQ_WORKER_CONFIG", str(cfg))
    monkeypatch.setenv("RABBITMQ_PASSWORD", "x")
    monkeypatch.setenv("RABBITMQ_PREFETCH", "1")
    settings = load_rabbitmq_worker_settings()
    assert settings.prefetch_count == 1
