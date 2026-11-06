"""Background admission closes promptly; interpreter teardown drains native work."""

import os
from pathlib import Path
import subprocess
import sys


def test_gevent_background_shutdown_does_not_wait_for_transport() -> None:
    program: str = """
from gevent import monkey
monkey.patch_all()
from gevent.monkey import get_original
from shared.services.background import BackgroundWorkPool
import gevent
sleep = get_original('time', 'sleep')
clock = get_original('time', 'monotonic')
pool = BackgroundWorkPool(name='shutdown-probe', maximumWorkers=1)
assert pool.trySubmit(lambda: sleep(0.5))
assert not pool.trySubmit(lambda: None)
gevent.sleep(0.02)
start = clock()
pool.close()
assert clock() - start < 0.1, 'Shutdown waited for background transport'
assert not pool.trySubmit(lambda: None)
"""
    environment: dict[str, str] = dict(os.environ)
    environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[2])
    result = subprocess.run([sys.executable, "-c", program], env=environment, capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stderr


def test_graceful_interpreter_exit_drains_native_work_before_runtime_teardown(tmp_path: Path) -> None:
    program: str = """
import atexit
from pathlib import Path
import sys

@atexit.register
def verifyNativeWorkerRetired():
    if Path(sys.argv[3]).is_file():
        Path(sys.argv[2]).touch()

from gevent import monkey
monkey.patch_all()
import gevent
from gevent.threadpool import ThreadPool
from shared.services.background import BackgroundWorkPool

# Gevent removes its worker from the pool before the native thread exits.
runWorker = ThreadPool._WorkerGreenlet._begin
nativeSleep = monkey.get_original('time', 'sleep')
def delayNativeRetirement(worker):
    runWorker(worker)
    nativeSleep(0.15)
    Path(sys.argv[3]).touch()

ThreadPool._WorkerGreenlet._begin = delayNativeRetirement
pool = BackgroundWorkPool(name='interpreter-exit-probe', maximumWorkers=1)
def finishNativeOperation():
    gevent.sleep(0.05)
    Path(sys.argv[1]).write_text('completed')
assert pool.trySubmit(finishNativeOperation)
gevent.sleep(0.01)
pool.close()
"""
    marker: Path = tmp_path / "completed.txt"
    cleanupMarker: Path = tmp_path / "atexit-observed-retirement.txt"
    retirementMarker: Path = tmp_path / "retired.txt"
    environment: dict[str, str] = dict(os.environ)
    environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[2])
    result = subprocess.run(
        [sys.executable, "-X", "faulthandler", "-c", program,
         str(marker), str(cleanupMarker), str(retirementMarker)],
        env=environment, capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stderr
    assert marker.is_file(), "Interpreter teardown overtook the active native operation"
    assert cleanupMarker.is_file(), "Interpreter teardown overtook native thread retirement"


def test_gevent_background_thread_releases_its_native_hub() -> None:
    program: str = """
from gevent import monkey
monkey.patch_all()
from gevent.monkey import get_original
from shared.services.background import BackgroundWorkPool
import gevent
clock = get_original('time', 'monotonic')
mainHub = gevent.get_hub()
hubs = []
def runCooperativeOperation():
    hubs.append(gevent.get_hub())
    gevent.sleep(0.01)
pool = BackgroundWorkPool(name='native-hub-probe', maximumWorkers=1)
assert pool.trySubmit(runCooperativeOperation)
deadline = clock() + 1
while not hubs and clock() < deadline:
    gevent.sleep(0.01)
pool.close()
while clock() < deadline:
    if hubs and hubs[0].loop is None:
        break
    gevent.sleep(0.01)
assert hubs and hubs[0].loop is None, 'Finished native work retained its gevent hub'
assert hubs[0] is not mainHub and mainHub.loop is not None
"""
    environment: dict[str, str] = dict(os.environ)
    environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[2])
    result = subprocess.run([sys.executable, "-X", "faulthandler", "-c", program],
        env=environment, capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stderr
