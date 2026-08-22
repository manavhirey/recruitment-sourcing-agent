import pytest

from app.sourcing.state_machine import (
    InvalidTransition,
    RunState,
    transition_run,
)


def test_ready_cannot_transition_back_to_sourcing() -> None:
    with pytest.raises(InvalidTransition):
        transition_run(RunState.READY, RunState.SOURCING)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (RunState.QUEUED, RunState.SOURCING),
        (RunState.SOURCING, RunState.MATCHING),
        (RunState.MATCHING, RunState.ENRICHING),
        (RunState.ENRICHING, RunState.READY),
        (RunState.PARTIALLY_READY, RunState.ENRICHING),
        (RunState.PARTIALLY_READY, RunState.READY),
    ],
)
def test_legal_progression_transitions_are_returned(
    current: RunState, target: RunState
) -> None:
    assert transition_run(current, target) is target


@pytest.mark.parametrize(
    "terminal", [RunState.READY, RunState.CANCELLED, RunState.FAILED]
)
def test_terminal_states_have_no_outbound_transition(terminal: RunState) -> None:
    for target in RunState:
        with pytest.raises(InvalidTransition):
            transition_run(terminal, target)
