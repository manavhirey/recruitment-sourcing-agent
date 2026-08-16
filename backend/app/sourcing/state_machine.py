from enum import StrEnum


class RunState(StrEnum):
    QUEUED = "queued"
    SOURCING = "sourcing"
    MATCHING = "matching"
    ENRICHING = "enriching"
    PARTIALLY_READY = "partially_ready"
    READY = "ready"
    CANCELLED = "cancelled"
    FAILED = "failed"


class InvalidTransition(ValueError):
    def __init__(self, current: RunState, target: RunState) -> None:
        self.current = current
        self.target = target
        super().__init__(f"invalid sourcing transition: {current} -> {target}")


ALLOWED_TRANSITIONS: dict[RunState, frozenset[RunState]] = {
    RunState.QUEUED: frozenset(
        {RunState.SOURCING, RunState.CANCELLED, RunState.FAILED}
    ),
    RunState.SOURCING: frozenset(
        {
            RunState.MATCHING,
            RunState.PARTIALLY_READY,
            RunState.CANCELLED,
            RunState.FAILED,
        }
    ),
    RunState.MATCHING: frozenset(
        {
            RunState.ENRICHING,
            RunState.PARTIALLY_READY,
            RunState.CANCELLED,
            RunState.FAILED,
        }
    ),
    RunState.ENRICHING: frozenset(
        {
            RunState.READY,
            RunState.PARTIALLY_READY,
            RunState.CANCELLED,
            RunState.FAILED,
        }
    ),
    RunState.PARTIALLY_READY: frozenset(
        {RunState.ENRICHING, RunState.READY, RunState.CANCELLED}
    ),
    RunState.READY: frozenset(),
    RunState.CANCELLED: frozenset(),
    RunState.FAILED: frozenset(),
}


def transition_run(current: RunState, target: RunState) -> RunState:
    if target not in ALLOWED_TRANSITIONS[current]:
        raise InvalidTransition(current, target)
    return target
