"""Webhook transport ownership survives native background worker retirement."""

import os
from pathlib import Path
import subprocess
import sys


def test_webhook_transport_survives_native_worker_retirement() -> None:
    program: str = """
from gevent import monkey
monkey.patch_all()
import os
os.environ.update(DATABASE_URL='postgresql+asyncpg://test:test@localhost/test',
    TMP_PATH='/tmp/knowhere-webhook-client-test', S3_BUCKET_NAME='test', S3_TEMP_PATH='/tmp',
    QSTASH_TOKEN='local-test-token', QSTASH_CALLBACK_BASE_URL='https://callback.example.invalid')
import gevent
from gevent.pywsgi import WSGIServer
from shared.core.config import app_config
from shared.services.background import BackgroundWorkPool
from shared.services.jobs.lifecycle.webhook_outbox import SyncJobWebhookOutbox
from shared.services.webhook import qstash_publisher
def respond(environment, startResponse):
    environment['wsgi.input'].read(int(environment.get('CONTENT_LENGTH') or 0))
    body = b'{"messageId":"owned-loopback-message"}'
    startResponse('200 OK', [('Content-Type','application/json'),('Content-Length',str(len(body)))])
    return [body]
server = WSGIServer(('127.0.0.1', 0), respond, log=None)
server.start()
app_config.QSTASH_BASE_URL = 'http://127.0.0.1:' + str(server.server_port)
pool = BackgroundWorkPool(name='webhook-transport-probe', maximumWorkers=1)
results = []
closed = []
class LocalPublisher(qstash_publisher.QStashWebhookPublisher):
    def publish_event(self, eventId):
        adapter = self._client_adapter
        try:
            adapter.get_client().http._retry = {'retries': 0, 'backoff': lambda attempt: 0}
            result = adapter.publish_webhook(target_url='https://destination.example.invalid',
                payload={'event':'job.parsed'}, signature='test-signature', event_id=eventId)
            results.append(result)
            return result
        except Exception as error:
            results.append(type(error).__name__)
            raise
    def close(self):
        transport = self._client_adapter.get_client().http._client
        super().close()
        closed.append(transport.is_closed)
qstash_publisher.QStashWebhookPublisher = LocalPublisher
def publish():
    SyncJobWebhookOutbox()._publishEvent('test-event')
try:
    assert pool.trySubmit(publish)
    with gevent.Timeout(2):
        while not results:
            gevent.sleep(0.01)
    gevent.sleep(1.2)
    assert pool.trySubmit(publish)
    with gevent.Timeout(2):
        while len(closed) < 2:
            gevent.sleep(0.01)
    assert results == ['owned-loopback-message', 'owned-loopback-message'], results
    assert closed == [True, True], 'Dispatch did not close each owned transport'
finally:
    pool.close()
    server.stop(timeout=0)
"""
    environment: dict[str, str] = dict(os.environ)
    environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[2])
    result = subprocess.run([sys.executable, "-X", "faulthandler", "-c", program],
        env=environment, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
