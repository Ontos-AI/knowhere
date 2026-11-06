"""Exercise standalone sync/async Python SDK clients against real job HTTP routes."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import inspect
import json
import sys
from typing import Awaitable, TypeVar, Union, cast

import httpx
from knowhere import (
    APIConnectionError,
    APIStatusError,
    AsyncKnowhere,
    JobFailedError,
    Knowhere,
    PollingTimeoutError,
)
from knowhere.types import JobResult

_Value = TypeVar("_Value")


def _object(value: object) -> dict[str, object]:
    assert isinstance(value, dict)
    return cast(dict[str, object], value)


def _list(value: object) -> list[object]:
    assert isinstance(value, list)
    return cast(list[object], value)


async def _resolve(value: Union[_Value, Awaitable[_Value]]) -> _Value:
    if inspect.isawaitable(value):
        return await value
    return cast(_Value, value)


async def main() -> None:
    kind, baseURL, serialized = sys.argv[1:]
    jobs: dict[str, str] = json.loads(serialized)
    client: Union[Knowhere, AsyncKnowhere] = (
        AsyncKnowhere if kind == "python-async" else Knowhere
    )(api_key="synthetic-http-key", base_url=baseURL, max_retries=3, timeout=2.0)
    async with httpx.AsyncClient(base_url=baseURL, trust_env=False) as controls:

        async def request(path: str, method: str = "GET") -> dict[str, object]:
            response = await controls.request(method, path)
            response.raise_for_status()
            return _object(response.json())

        async def load(job: JobResult | None) -> None:
            assert job is not None
            result = await _resolve(client.jobs.load(job, verify_checksum=False))
            assert result.chunks[0].content == "Synthetic retained evidence"

        try:
            running = await _resolve(
                client.jobs.wait(jobs["running"], wait_for="parsed")
            )
            assert (
                running.status == "running"
                and not running.is_done
                and running.parsing_status == "completed"
            )
            await load(running)
            try:
                await _resolve(
                    client.jobs.wait(
                        jobs["running"], poll_timeout=0.001, poll_interval=0.001
                    )
                )
                raise AssertionError("Default wait did not time out")
            except PollingTimeoutError as error:
                assert error.job_result is not None
                assert (
                    error.job_id == jobs["running"]
                    and error.job_result.parsing_status == "completed"
                )
            if isinstance(client, Knowhere):
                with ThreadPoolExecutor(max_workers=1) as pool:
                    waiting = pool.submit(
                        client.jobs.wait,
                        jobs["running"],
                        poll_interval=0.005,
                        poll_timeout=2.0,
                    )
                    assert not (
                        await _resolve(
                            client.jobs.wait(jobs["running"], wait_for="parsed")
                        )
                    ).is_done
                    await request(f"/test/fail/{jobs['running']}", "POST")
                    try:
                        waiting.result(timeout=3)
                        raise AssertionError("Default sync wait returned early")
                    except JobFailedError as error:
                        assert (
                            error.failure_stage == "indexing"
                            and error.job_result_id == running.job_result_id
                        )
                        await load(error.job_result)
            else:
                waiting = asyncio.create_task(
                    client.jobs.wait(
                        jobs["running"], poll_interval=0.005, poll_timeout=2.0
                    )
                )
                assert not (
                    await client.jobs.wait(jobs["running"], wait_for="parsed")
                ).is_done
                await request(f"/test/fail/{jobs['running']}", "POST")
                try:
                    await waiting
                    raise AssertionError("Default async wait returned early")
                except JobFailedError as error:
                    assert (
                        error.failure_stage == "indexing"
                        and error.job_result_id == running.job_result_id
                    )
                    await load(error.job_result)
            assert (
                await _resolve(client.jobs.wait(jobs["running"], wait_for="parsed"))
            ).is_failed
            for mode, status in [
                ("unavailable", 503),
                ("invalid", 500),
                ("timeout", 504),
                ("conflict", 409),
            ]:
                before = await _resolve(client.jobs.get(jobs[mode]))
                try:
                    await _resolve(client.jobs.retry_indexing(jobs[mode]))
                    raise AssertionError("Expected canonical API failure")
                except APIStatusError as error:
                    assert error.job_result is not None
                    assert error.status_code == status
                    assert (
                        error.job_id == jobs[mode]
                        and error.job_result_id == before.job_result_id
                    )
                    assert (
                        error.job_result.parsing_status == "completed"
                        and error.job_result.job_id == jobs[mode]
                    )
                    if status != 409:
                        assert error.failure_stage == "indexing"
                    await load(error.job_result)
                after = await _resolve(client.jobs.get(jobs[mode]))
                assert after.credits_spent == before.credits_spent
                assert not (
                    await _resolve(client.jobs.wait(jobs[mode], wait_for="parsed"))
                ).is_done
            try:
                await _resolve(client.jobs.wait(jobs["unavailable"]))
                raise AssertionError("Parsed-only subscription changed default waiting")
            except JobFailedError as error:
                assert error.failure_stage == "indexing"
            earlyDelivery = await request(
                f"/test/deliver-parsed/{jobs['completed']}", "POST"
            )
            assert (
                earlyDelivery["event"] == "job.parsed"
                and earlyDelivery["status"] == "running"
                and earlyDelivery["uniqueEvents"] == 1
            )
            ready = await _resolve(client.jobs.retry_indexing(jobs["completed"]))
            assert ready.is_done and ready.indexing_status == "completed"
            await load(ready)
            assert (
                await _resolve(client.jobs.retry_indexing(jobs["completed"]))
            ).job_result_id == ready.job_result_id
            delivery = await request(f"/test/deliver/{jobs['completed']}", "POST")
            assert (
                delivery["deliveredNames"]
                == ["job.completed", "job.parsed", "job.parsed"]
                and delivery["uniqueEvents"] == 2
            )
            byName = {
                _object(event)["event"]: _object(event)
                for event in _list(delivery["events"])
            }
            assert (
                byName["job.parsed"]["status"] == "running"
                and byName["job.completed"]["status"] == "completed"
            )
            assert (await _resolve(client.jobs.get(jobs["completed"]))).is_done
            try:
                await _resolve(
                    client.jobs.retry_indexing(jobs["response-lost"], timeout=0.02)
                )
                raise AssertionError("Expected lost HTTP response observation")
            except APIConnectionError as error:
                assert error.job_id == jobs["response-lost"]
            assert (
                await _resolve(
                    client.jobs.wait(
                        jobs["response-lost"], poll_interval=0.005, poll_timeout=2.0
                    )
                )
            ).is_done
            metrics = await request("/test/metrics")
            for mode in [
                "unavailable",
                "invalid",
                "timeout",
                "conflict",
                "response-lost",
            ]:
                assert _object(metrics["mutations"])[jobs[mode]] == 1
            assert (
                _object(metrics["nativeWrites"])["completed"] == 1
                and _object(metrics["nativeWrites"])["response-lost"] == 1
            )
            assert metrics["artifactAuthorization"] == []
            assert [
                _object(event)["event"] for event in _list(metrics["delivered"])
            ] == [
                "job.parsed",
                "job.completed",
                "job.parsed",
                "job.parsed",
            ]
            print(
                json.dumps(
                    {
                        "status": "passed",
                        "sdk": kind,
                        "statuses": [200, 409, 500, 503, 504],
                        "callbackDeliveries": 4,
                        "uniqueEvents": 2,
                        "retryMutationRequests": metrics["mutations"],
                        "nativeWrites": metrics["nativeWrites"],
                    }
                )
            )
        finally:
            await _resolve(client.close())


if __name__ == "__main__":
    asyncio.run(main())
