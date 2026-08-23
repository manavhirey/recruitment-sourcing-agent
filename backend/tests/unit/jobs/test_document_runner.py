import asyncio
import multiprocessing
import os
import time
from multiprocessing.connection import Connection
from pathlib import Path

import pytest
from starlette.requests import ClientDisconnect, Request

from app.jobs.document_router import _run_extraction
from app.jobs.document_runner import ProcessJobDescriptionExtractionRunner
from tests.job_description_fixtures import readable_pdf


def blocking_worker(
    _connection: Connection,
    _data: bytes,
    _filename: str,
    _media_type: str | None,
) -> None:
    Path(os.environ["DOCUMENT_TEST_WORKER_MARKER"]).write_text(
        str(os.getpid()),
        encoding="utf-8",
    )
    while True:
        time.sleep(1)


async def wait_for_worker_pid(marker: Path) -> int:
    deadline = asyncio.get_running_loop().time() + 5
    while not marker.exists():
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("worker did not start")
        await asyncio.sleep(0.01)
    return int(marker.read_text(encoding="utf-8"))


def assert_process_reaped(worker_pid: int) -> None:
    assert worker_pid not in {child.pid for child in multiprocessing.active_children()}
    with pytest.raises(ProcessLookupError):
        os.kill(worker_pid, 0)


def test_production_runner_extracts_in_a_process() -> None:
    async def invoke():
        return await ProcessJobDescriptionExtractionRunner().run(
            data=readable_pdf(),
            filename="role.pdf",
            media_type="application/pdf",
            timeout_seconds=10,
        )

    result = asyncio.run(invoke())

    assert result.text == "Senior Product Designer"
    assert result.filename == "role.pdf"


def test_cancelling_runner_terminates_and_reaps_worker(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    marker = tmp_path / "cancelled-worker.pid"
    monkeypatch.setenv("DOCUMENT_TEST_WORKER_MARKER", str(marker))

    async def invoke() -> int:
        task = asyncio.create_task(
            ProcessJobDescriptionExtractionRunner(blocking_worker).run(
                data=b"blocking",
                filename="role.pdf",
                media_type="application/pdf",
                timeout_seconds=10,
            )
        )
        worker_pid = await wait_for_worker_pid(marker)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return worker_pid

    assert_process_reaped(asyncio.run(invoke()))


def test_request_disconnect_terminates_and_reaps_worker(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    marker = tmp_path / "disconnected-worker.pid"
    monkeypatch.setenv("DOCUMENT_TEST_WORKER_MARKER", str(marker))

    async def invoke() -> int:
        async def receive() -> dict[str, str]:
            await wait_for_worker_pid(marker)
            return {"type": "http.disconnect"}

        request = Request(
            {
                "type": "http",
                "method": "POST",
                "path": "/api/v1/job-descriptions/extract",
                "headers": [],
            },
            receive,
        )
        with pytest.raises(ClientDisconnect):
            await _run_extraction(
                request,
                ProcessJobDescriptionExtractionRunner(blocking_worker),
                data=b"blocking",
                filename="role.pdf",
                media_type="application/pdf",
            )
        return await wait_for_worker_pid(marker)

    assert_process_reaped(asyncio.run(invoke()))


def test_request_task_cancellation_terminates_and_reaps_worker(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    marker = tmp_path / "request-cancelled-worker.pid"
    monkeypatch.setenv("DOCUMENT_TEST_WORKER_MARKER", str(marker))

    async def invoke() -> None:
        async def receive() -> dict[str, str]:
            await asyncio.Event().wait()
            return {"type": "http.disconnect"}

        request = Request(
            {
                "type": "http",
                "method": "POST",
                "path": "/api/v1/job-descriptions/extract",
                "headers": [],
            },
            receive,
        )
        task = asyncio.create_task(
            _run_extraction(
                request,
                ProcessJobDescriptionExtractionRunner(blocking_worker),
                data=b"blocking",
                filename="role.pdf",
                media_type="application/pdf",
            )
        )
        worker_pid = await wait_for_worker_pid(marker)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert_process_reaped(worker_pid)

    asyncio.run(invoke())
