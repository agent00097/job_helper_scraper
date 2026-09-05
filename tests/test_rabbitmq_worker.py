"""Consumer callback must not block the pika I/O thread."""
from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock

from services.scrape_request_service import MessageDisposition
from utils.process_hygiene import request_recycle, reset_recycle_state_for_tests
from workers.rabbitmq_worker import RabbitMQJobWorker


def _worker(on_body) -> RabbitMQJobWorker:
    settings = MagicMock()
    settings.requeue_on_failure = True
    return RabbitMQJobWorker(settings=settings, on_body=on_body)


def _channel(scheduled: list):
    connection = MagicMock()
    connection.add_callback_threadsafe.side_effect = lambda cb: scheduled.append(cb)
    channel = MagicMock()
    channel.connection = connection
    channel.is_open = True
    return channel


def _wait_scheduled(scheduled: list, timeout: float = 2.0):
    deadline = time.time() + timeout
    while not scheduled and time.time() < deadline:
        time.sleep(0.01)
    assert scheduled, "handler did not schedule AMQP settle"


def test_on_message_returns_before_handler_finishes_and_acks_on_ioloop():
    ioloop_ident = threading.get_ident()
    started = threading.Event()
    release = threading.Event()
    handler_ident = {}

    def on_body(_body):
        handler_ident["id"] = threading.get_ident()
        started.set()
        assert release.wait(timeout=2)
        return MessageDisposition.ACK

    worker = _worker(on_body)
    scheduled: list = []
    channel = _channel(scheduled)
    method = MagicMock(delivery_tag=7)

    worker._on_message(channel, method, None, b"{}")

    assert started.wait(timeout=2)
    assert handler_ident["id"] != ioloop_ident
    channel.basic_ack.assert_not_called()

    release.set()
    _wait_scheduled(scheduled)
    scheduled[0]()
    channel.basic_ack.assert_called_once_with(delivery_tag=7)


def test_on_message_nacks_without_requeue_on_poison_disposition():
    worker = _worker(lambda _body: MessageDisposition.NACK_NO_REQUEUE)
    scheduled: list = []
    channel = _channel(scheduled)
    method = MagicMock(delivery_tag=3)

    worker._on_message(channel, method, None, b"{}")
    _wait_scheduled(scheduled)
    scheduled[0]()
    channel.basic_nack.assert_called_once_with(delivery_tag=3, requeue=False)


def test_handler_exception_nacks_for_requeue():
    def on_body(_body):
        raise RuntimeError("boom")

    worker = _worker(on_body)
    scheduled: list = []
    channel = _channel(scheduled)
    method = MagicMock(delivery_tag=9)

    worker._on_message(channel, method, None, b"{}")
    _wait_scheduled(scheduled)
    scheduled[0]()
    channel.basic_nack.assert_called_once_with(delivery_tag=9, requeue=True)


def test_settle_stops_consumer_when_recycle_requested():
    reset_recycle_state_for_tests()
    request_recycle("test")
    worker = _worker(lambda _body: MessageDisposition.ACK)
    scheduled: list = []
    channel = _channel(scheduled)
    method = MagicMock(delivery_tag=1)

    worker._on_message(channel, method, None, b"{}")
    _wait_scheduled(scheduled)
    scheduled[0]()
    channel.stop_consuming.assert_called_once()
    reset_recycle_state_for_tests()
