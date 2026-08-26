import asyncio
import multiprocessing
import os
import threading
import time
from multiprocessing.connection import Connection
from pathlib import Path

import pytest
from starlette.requests import ClientDisconnect, Request

from app.jobs import document_router, document_runner
from app.jobs.document_extraction import (
    DocumentExtractionError,
    ExtractedJobDescription,
)
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


def selectable_worker(
    connection: Connection,
    data: bytes,
    filename: str,
    media_type: str | None,
) -> None:
    if data == b"blocking":
        blocking_worker(connection, data, filename, media_type)
        return
    if data == b"error":
        connection.send(("document_error", "job_description_file_unreadable"))
        connection.close()
        return
    connection.send(("success", "Recovered", filename, media_type or "application/pdf"))
    connection.close()


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


def test_runner_drains_a_result_that_becomes_visible_as_the_worker_exits() -> None:
    class RaceReceiver:
        def __init__(self) -> None:
            self.poll_count = 0

        def poll(self) -> bool:
            self.poll_count += 1
            return self.poll_count >= 2

        def recv(self) -> tuple[str, str, str, str]:
            return ("success", "Race recovered", "role.pdf", "application/pdf")

        def close(self) -> None:
            return None

    class RaceSender:
        def close(self) -> None:
            return None

    class ExitedProcess:
        def start(self) -> None:
            return None

        def is_alive(self) -> bool:
            return False

        def join(self, timeout: float | None = None) -> None:
            return None

        def close(self) -> None:
            return None

    class RaceContext:
        def __init__(self) -> None:
            self.receiver = RaceReceiver()
            self.process = ExitedProcess()

        def Pipe(self, duplex: bool) -> tuple[RaceReceiver, RaceSender]:
            assert duplex is False
            return self.receiver, RaceSender()

        def Process(self, **_kwargs: object) -> ExitedProcess:
            return self.process

    runner = ProcessJobDescriptionExtractionRunner(selectable_worker)
    race_context = RaceContext()
    runner._context = race_context  # type: ignore[assignment]

    result = asyncio.run(
        runner.run(
            data=b"race",
            filename="role.pdf",
            media_type="application/pdf",
            timeout_seconds=1,
        )
    )

    assert result.text == "Race recovered"
    assert race_context.receiver.poll_count == 2


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


def test_runner_rejects_saturation_and_recovers_after_cancellation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    marker = tmp_path / "admitted-worker.pid"
    monkeypatch.setenv("DOCUMENT_TEST_WORKER_MARKER", str(marker))
    monkeypatch.setattr(
        document_runner,
        "_WORKER_ADMISSION",
        threading.BoundedSemaphore(1),
        raising=False,
    )
    runner = ProcessJobDescriptionExtractionRunner(selectable_worker)

    async def invoke() -> int:
        admitted = asyncio.create_task(
            runner.run(
                data=b"blocking",
                filename="admitted.pdf",
                media_type="application/pdf",
                timeout_seconds=10,
            )
        )
        worker_pid = await wait_for_worker_pid(marker)

        with pytest.raises(DocumentExtractionError) as saturated:
            await runner.run(
                data=b"recovery",
                filename="saturated.pdf",
                media_type="application/pdf",
                timeout_seconds=10,
            )
        assert saturated.value.code == "job_description_extraction_unavailable"

        admitted.cancel()
        with pytest.raises(asyncio.CancelledError):
            await admitted

        with pytest.raises(DocumentExtractionError) as failed:
            await runner.run(
                data=b"error",
                filename="failed.pdf",
                media_type="application/pdf",
                timeout_seconds=10,
            )
        assert failed.value.code == "job_description_file_unreadable"

        recovered = await runner.run(
            data=b"recovery",
            filename="recovered.pdf",
            media_type="application/pdf",
            timeout_seconds=10,
        )
        assert recovered.text == "Recovered"
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


def test_disconnect_monitor_failure_preserves_valid_worker_completion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def failed_monitor(_request: Request) -> None:
        raise RuntimeError("monitor crashed")

    class SuccessfulRunner:
        async def run(
            self,
            *,
            data: bytes,
            filename: str,
            media_type: str | None,
            timeout_seconds: float,
        ) -> ExtractedJobDescription:
            await asyncio.sleep(0.01)
            return ExtractedJobDescription(
                text="Worker completed",
                filename=filename,
                media_type=media_type or "application/pdf",
            )

    monkeypatch.setattr(document_router, "_wait_for_disconnect", failed_monitor)
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/v1/job-descriptions/extract",
            "headers": [],
        }
    )

    result = asyncio.run(
        _run_extraction(
            request,
            SuccessfulRunner(),
            data=b"worker input",
            filename="role.pdf",
            media_type="application/pdf",
        )
    )

    assert result.text == "Worker completed"
