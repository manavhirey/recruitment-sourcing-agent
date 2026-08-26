import asyncio
import multiprocessing
import os
import sys
import threading
from collections.abc import Callable
from multiprocessing.connection import Connection
from multiprocessing.process import BaseProcess
from typing import Literal, Protocol

from app.jobs.document_extraction import (
    DefaultJobDescriptionExtractor,
    DocumentExtractionError,
    ExtractedJobDescription,
)

WORKER_CPU_SECONDS = 10
WORKER_MEMORY_BYTES = 512 * 1024 * 1024
WORKER_POLL_SECONDS = 0.01
WORKER_TERMINATE_GRACE_SECONDS = 0.25


def _available_cpu_count() -> int:
    affinity = getattr(os, "sched_getaffinity", None)
    if affinity is not None:
        try:
            return max(1, len(affinity(0)))
        except OSError:
            pass
    return max(1, os.cpu_count() or 1)


WORKER_CAPACITY = _available_cpu_count()
_WORKER_ADMISSION = threading.BoundedSemaphore(WORKER_CAPACITY)

WorkerTarget = Callable[[Connection, bytes, str, str | None], None]


class JobDescriptionExtractionRunner(Protocol):
    async def run(
        self,
        *,
        data: bytes,
        filename: str,
        media_type: str | None,
        timeout_seconds: float,
    ) -> ExtractedJobDescription: ...


def _apply_worker_limits() -> None:
    try:
        import resource
    except ImportError:  # pragma: no cover - unavailable on Windows
        return

    cpu_limit = getattr(resource, "RLIMIT_CPU", None)
    if cpu_limit is not None:
        try:
            resource.setrlimit(
                cpu_limit,
                (WORKER_CPU_SECONDS, WORKER_CPU_SECONDS),
            )
        except (OSError, ValueError):
            pass

    address_space_limit = getattr(resource, "RLIMIT_AS", None)
    if sys.platform.startswith("linux") and address_space_limit is not None:
        try:
            resource.setrlimit(
                address_space_limit,
                (WORKER_MEMORY_BYTES, WORKER_MEMORY_BYTES),
            )
        except (OSError, ValueError):
            pass


def extract_job_description_in_worker(
    connection: Connection,
    data: bytes,
    filename: str,
    media_type: str | None,
) -> None:
    try:
        _apply_worker_limits()
        result = DefaultJobDescriptionExtractor().extract(
            data=data,
            filename=filename,
            media_type=media_type,
        )
        connection.send(("success", result.text, result.filename, result.media_type))
    except DocumentExtractionError as error:
        connection.send(("document_error", error.code))
    except MemoryError:
        connection.send(("document_error", "job_description_file_too_complex"))
    except Exception:  # noqa: BLE001 - worker must redact every parser failure
        connection.send(("document_error", "job_description_extraction_unavailable"))
    finally:
        connection.close()


def _terminate_and_reap(process: BaseProcess) -> None:
    if process.is_alive():
        process.terminate()
        process.join(WORKER_TERMINATE_GRACE_SECONDS)
    if process.is_alive():
        process.kill()
    process.join()
    process.close()


def _decode_worker_result(payload: object) -> ExtractedJobDescription:
    if not isinstance(payload, tuple) or not payload:
        raise RuntimeError("document extraction worker returned invalid data")
    if (
        len(payload) == 4
        and payload[0] == "success"
        and all(isinstance(value, str) for value in payload[1:])
    ):
        return ExtractedJobDescription(
            text=payload[1],
            filename=payload[2],
            media_type=payload[3],
        )
    if (
        len(payload) == 2
        and payload[0] == "document_error"
        and isinstance(payload[1], str)
    ):
        raise DocumentExtractionError(payload[1])
    raise RuntimeError("document extraction worker returned invalid data")


class ProcessJobDescriptionExtractionRunner:
    def __init__(
        self,
        worker_target: WorkerTarget = extract_job_description_in_worker,
        *,
        context_name: Literal["spawn"] = "spawn",
    ) -> None:
        self._worker_target = worker_target
        self._context = multiprocessing.get_context(context_name)

    async def run(
        self,
        *,
        data: bytes,
        filename: str,
        media_type: str | None,
        timeout_seconds: float,
    ) -> ExtractedJobDescription:
        if not _WORKER_ADMISSION.acquire(blocking=False):
            raise DocumentExtractionError("job_description_extraction_unavailable")
        try:
            return await self._run_admitted(
                data=data,
                filename=filename,
                media_type=media_type,
                timeout_seconds=timeout_seconds,
            )
        finally:
            _WORKER_ADMISSION.release()

    async def _run_admitted(
        self,
        *,
        data: bytes,
        filename: str,
        media_type: str | None,
        timeout_seconds: float,
    ) -> ExtractedJobDescription:
        receiver, sender = self._context.Pipe(duplex=False)
        process = self._context.Process(
            target=self._worker_target,
            args=(sender, data, filename, media_type),
            daemon=True,
        )
        started = False
        try:
            loop = asyncio.get_running_loop()
            deadline = loop.time() + timeout_seconds
            process.start()
            started = True
            sender.close()
            while True:
                if receiver.poll():
                    try:
                        return _decode_worker_result(receiver.recv())
                    except EOFError as error:
                        raise RuntimeError(
                            "document extraction worker closed without a result"
                        ) from error
                if not process.is_alive():
                    if receiver.poll():
                        try:
                            return _decode_worker_result(receiver.recv())
                        except EOFError as error:
                            raise RuntimeError(
                                "document extraction worker closed without a result"
                            ) from error
                    raise RuntimeError(
                        "document extraction worker exited without a result"
                    )
                remaining = deadline - loop.time()
                if remaining <= 0:
                    raise TimeoutError("document extraction worker timed out")
                await asyncio.sleep(min(WORKER_POLL_SECONDS, remaining))
        finally:
            receiver.close()
            if started:
                _terminate_and_reap(process)
            else:
                sender.close()
