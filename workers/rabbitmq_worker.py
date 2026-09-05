"""
RabbitMQ consumer for job_scrape_requests: thin callback, reconnect loop.
"""
from __future__ import annotations

import logging
import signal
import threading
import time
from typing import Any, Callable, Optional

import pika
from pika.adapters.blocking_connection import BlockingChannel
from pika.spec import Basic

from services.scrape_request_service import MessageDisposition, process_job_scrape_request_body
from utils.process_hygiene import (
    recycle_current_process,
    recycle_requested,
    stop_consumer_for_recycle,
)
from workers.rabbitmq_settings import RabbitMQWorkerSettings, load_rabbitmq_worker_settings

logger = logging.getLogger(__name__)

BodyHandler = Callable[[bytes], MessageDisposition]


class RabbitMQJobWorker:
    def __init__(
        self,
        settings: Optional[RabbitMQWorkerSettings] = None,
        on_body: Optional[BodyHandler] = None,
    ):
        self.settings = settings or load_rabbitmq_worker_settings()
        self._on_body = on_body or process_job_scrape_request_body
        self._channel: Optional[BlockingChannel] = None
        self._stopping = False

    def _connection_parameters(self) -> pika.ConnectionParameters:
        return pika.ConnectionParameters(
            host=self.settings.host,
            port=self.settings.port,
            virtual_host=self.settings.virtual_host,
            credentials=pika.PlainCredentials(self.settings.username, self.settings.password),
            heartbeat=self.settings.heartbeat,
            blocked_connection_timeout=self.settings.blocked_connection_timeout,
        )

    def _settle_delivery(
        self,
        channel: BlockingChannel,
        method: Basic.Deliver,
        disposition: MessageDisposition,
    ) -> None:
        tag = method.delivery_tag
        try:
            if not channel.is_open:
                logger.warning("channel closed before settle tag=%s", tag)
                return
            if disposition == MessageDisposition.ACK:
                channel.basic_ack(delivery_tag=tag)
            elif disposition == MessageDisposition.NACK_NO_REQUEUE:
                channel.basic_nack(delivery_tag=tag, requeue=False)
            else:
                channel.basic_nack(
                    delivery_tag=tag, requeue=self.settings.requeue_on_failure
                )
        except Exception:
            logger.exception("failed to settle delivery tag=%s", tag)
        finally:
            if stop_consumer_for_recycle(channel, self._stopping):
                self._stopping = True

    def _on_message(
        self,
        channel: BlockingChannel,
        method: Basic.Deliver,
        _properties: Any,
        body: bytes,
    ) -> None:
        """
        Run the handler on a worker thread and return immediately.

        Pika's BlockingConnection only sends heartbeats on the I/O thread.
        A 1200s company scrape that blocks this callback misses the 300s
        heartbeat; the broker resets the socket, ACK fails, and the same
        company is redelivered to every replica.
        """
        connection = channel.connection

        def work() -> None:
            try:
                disposition = self._on_body(body)
            except Exception:
                logger.exception("message handler failed")
                disposition = MessageDisposition.NACK_REQUEUE

            def settle() -> None:
                self._settle_delivery(channel, method, disposition)

            try:
                connection.add_callback_threadsafe(settle)
            except Exception as exc:
                logger.warning(
                    "could not schedule AMQP settle (connection likely closed): %s",
                    exc,
                )

        threading.Thread(target=work, name="amqp-handler", daemon=True).start()

    def _register_signals(self) -> None:
        def handle(_sig, _frame):
            logger.info("Shutdown signal received, stopping consumer...")
            self._stopping = True
            ch = self._channel
            if ch is not None and ch.is_open:
                try:
                    ch.stop_consuming()
                except Exception as e:
                    logger.debug("stop_consuming: %s", e)

        signal.signal(signal.SIGTERM, handle)
        signal.signal(signal.SIGINT, handle)

    def run_forever(self) -> None:
        self._register_signals()
        delay = self.settings.reconnect_delay_seconds
        while not self._stopping:
            try:
                self._run_session()
            except pika.exceptions.AMQPConnectionError as e:
                if self._stopping:
                    break
                logger.warning("RabbitMQ connection lost: %s; reconnecting in %ss", e, delay)
                time.sleep(delay)
            except pika.exceptions.ChannelWrongStateError:
                if self._stopping:
                    break
                logger.warning("RabbitMQ channel error; reconnecting in %ss", delay)
                time.sleep(delay)
            except Exception as e:
                if self._stopping:
                    break
                logger.exception("Unexpected consumer error: %s; reconnecting in %ss", e, delay)
                time.sleep(delay)
        if recycle_requested():
            recycle_current_process()
        logger.info("RabbitMQ worker exited.")

    def _run_session(self) -> None:
        logger.info(
            "Connecting to RabbitMQ %s:%s vhost=%r queue=%r",
            self.settings.host,
            self.settings.port,
            self.settings.virtual_host,
            self.settings.queue_name,
        )
        connection = pika.BlockingConnection(self._connection_parameters())
        try:
            channel = connection.channel()
            self._channel = channel
            channel.queue_declare(queue=self.settings.queue_name, durable=True, passive=False)
            channel.basic_qos(prefetch_count=self.settings.prefetch_count)
            channel.basic_consume(
                queue=self.settings.queue_name,
                on_message_callback=self._on_message,
                auto_ack=False,
            )
            logger.info("Consuming from queue %r", self.settings.queue_name)
            channel.start_consuming()
        finally:
            self._channel = None
            if connection.is_open:
                connection.close()
