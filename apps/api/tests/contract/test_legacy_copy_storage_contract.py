"""Optional PG copy storage survives actual native-worker hub retirement."""

from __future__ import annotations

import base64
from importlib.util import find_spec
import os
from pathlib import Path
import subprocess
import sys
import zlib

import pytest
from sqlalchemy.orm import Session

from tests.contract.test_lexical_backup_inventory_contract import (
    _BackupRuntime,
    backupRuntime as backupRuntime,
    postgresqlPrimaryRuntime as postgresqlPrimaryRuntime,
)


@pytest.mark.parametrize("backend", ["minio", "oss"])
def test_legacy_copy_reloads_frozen_cloud_input_after_native_worker_retires(
    backupRuntime: _BackupRuntime, backend: str
) -> None:
    if backend == "oss" and find_spec("oss2") is None:
        pytest.skip("OSS transport needs the optional worker storage dependency")
    program: str = """
from gevent import monkey
monkey.patch_all()
from psycogreen.gevent import patch_psycopg
patch_psycopg()
import base64
import os
os.environ.update(DATABASE_URL=os.environ['COPY_PROBE_DATABASE_URL'],
    S3_TYPE=os.environ['COPY_PROBE_BACKEND'], OSS_ENDPOINT='http://127.0.0.1:1', NO_PROXY='127.0.0.1,localhost', S3_ACCESS_KEY_ID='loopback-key', S3_SECRET_ACCESS_KEY='loopback-secret',
    S3_BUCKET_NAME='test-uploads', S3_RESULTS_BUCKET='test-uploads', S3_TEMP_PATH='/tmp', S3_REGION='us-east-1',
    TMP_PATH='/tmp/knowhere-loopback-s3-copy-test', AWS_EC2_METADATA_DISABLED='true')
import gevent
from gevent.pywsgi import WSGIServer
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool
import boto3
if os.environ['COPY_PROBE_BACKEND'] == 'oss':
    import oss2
from botocore.config import Config
from shared.core.config import app_config
from shared.core.config.storage import StorageConfig
from shared.models.database.lexicalbuild import LexicalBuild
from shared.services.background import BackgroundWorkPool
from shared.services.retrieval.lexical.legacy import LegacyLexicalCopy
payload = base64.b64decode(os.environ['COPY_PROBE_COMPRESSED_INPUT'])
requests = []
def respond(environment, startResponse):
    requests.append(environment['REQUEST_METHOD'])
    startResponse('200 OK', [('Content-Type','application/zlib'),('Content-Length',str(len(payload)))])
    return [payload] if environment['REQUEST_METHOD'] == 'GET' else []
server = WSGIServer(('127.0.0.1', 0), respond, log=None)
server.start()
endpoint = 'http://127.0.0.1:' + str(server.server_port)
app_config.S3_ENDPOINT_URL = endpoint
app_config.OSS_ENDPOINT = endpoint
clients = []
closed = []
createSession = boto3.session.Session
def createOwnedSession():
    session = createSession()
    createClient = session.client
    def trackClient(*arguments, **options):
        assert options.get('endpoint_url') == endpoint, 'Storage probe must stay on loopback'
        options['config'] = Config(s3={'addressing_style':'path'},retries={'total_max_attempts':1},connect_timeout=1,read_timeout=1)
        client = createClient(*arguments, **options)
        clients.append(client)
        close = client.close
        def closeOwned():
            close()
            closed.append(True)
        client.close = closeOwned
        return client
    session.client = trackClient
    return session
def refuseSharedClient(*arguments, **options):
    raise AssertionError('Copy requested boto3 default-session storage')
boto3.session.Session = createOwnedSession
boto3.client = refuseSharedClient
getOssBucket = StorageConfig.get_oss_bucket
def createOssBucket(self):
    bucket = getOssBucket(self)
    clients.append(bucket)
    close = bucket.session.session.close
    def closeOwned():
        close()
        closed.append(True)
    bucket.session.session.close = closeOwned
    return bucket
StorageConfig.get_oss_bucket = createOssBucket
engine = create_engine(make_url(os.environ['COPY_PROBE_DATABASE_URL']).set(drivername='postgresql+psycopg2'),poolclass=NullPool)
sessions = sessionmaker(engine)
copy = LegacyLexicalCopy(sessionFactory=sessions)
pool = BackgroundWorkPool(name='legacy-copy-storage-probe', maximumWorkers=1)
buildId = os.environ['COPY_PROBE_BUILD_ID']
results = []
def runCopy():
    try:
        results.append(copy.run(buildId))
    except Exception as error:
        results.append(type(error).__name__ + ': ' + str(error))
        raise
try:
    for generation in range(2):
        with sessions.begin() as db:
            build = db.get(LexicalBuild, buildId)
            assert build is not None
            build.legacyCopyState = 'pending'
        assert pool.trySubmit(runCopy)
        with gevent.Timeout(6):
            while len(results) <= generation:
                gevent.sleep(0.01)
        if generation == 0:
            assert pool._geventPool is not None
            with gevent.Timeout(3):
                while pool._geventPool.size:
                    gevent.sleep(0.01)
    assert results == [True, True], results
    assert requests == ['HEAD', 'GET', 'HEAD', 'GET'], requests
    assert len(clients) == 2 and closed == [True, True], 'Each copy must close its owned cloud transport'
finally:
    pool.close()
    engine.dispose()
    server.stop(timeout=0)
"""
    from shared.models.database.job_result import JobResult

    with Session(backupRuntime.engine) as db:
        revision = db.get(JobResult, backupRuntime.revisionId)
        assert revision is not None and revision.indexing_input_checksum is not None
        payload: bytes = zlib.compress(backupRuntime.payload.model_dump_json().encode())
    environment: dict[str, str] = dict(os.environ)
    environment.update(
        {
            "PYTHONPATH": str(
                Path(__file__).resolve().parents[4] / "packages/shared-python"
            ),
            "COPY_PROBE_DATABASE_URL": backupRuntime.engine.url.set(
                drivername="postgresql+asyncpg"
            ).render_as_string(hide_password=False),
            "COPY_PROBE_BUILD_ID": backupRuntime.buildId,
            "COPY_PROBE_BACKEND": backend,
            "COPY_PROBE_COMPRESSED_INPUT": base64.b64encode(payload).decode(),
        }
    )
    result = subprocess.run(
        [sys.executable, "-X", "faulthandler", "-c", program],
        env=environment,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
