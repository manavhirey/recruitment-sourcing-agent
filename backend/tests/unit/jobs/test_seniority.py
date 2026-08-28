import pytest

from app.jobs.seniority import (
    SeniorityLevel,
    UnknownSeniorityError,
    effective_experience_intervals,
    normalize_draft_seniority,
)


@pytest.mark.parametrize(
    ("years", "early", "mid", "senior"),
    [
        (0.0, True, False, False),
        (2.99, True, False, False),
        (3.0, True, True, False),
        (4.0, False, True, False),
        (9.0, False, True, False),
        (9.5, False, True, False),
        (9.99, False, True, False),
        (10.0, False, False, True),
        (10.01, False, False, True),
    ],
)
def test_preset_boundaries_cover_every_non_negative_year_value(
    years, early, mid, senior
):
    levels = [
        SeniorityLevel.EARLY_CAREER,
        SeniorityLevel.MID_LEVEL,
        SeniorityLevel.SENIOR,
    ]
    actual = [
        any(
            interval.contains(years)
            for interval in effective_experience_intervals([level], None, None)
        )
        for level in levels
    ]
    assert actual == [early, mid, senior]


def test_disjoint_presets_remain_disjoint():
    intervals = effective_experience_intervals(
        [SeniorityLevel.EARLY_CAREER, SeniorityLevel.SENIOR], None, None
    )
    assert any(interval.contains(3.0) for interval in intervals)
    assert not any(interval.contains(7.0) for interval in intervals)
    assert any(interval.contains(12.0) for interval in intervals)


def test_open_custom_range_overrides_all_presets():
    intervals = effective_experience_intervals([SeniorityLevel.EARLY_CAREER], 5, None)
    assert not any(interval.contains(3.0) for interval in intervals)
    assert any(interval.contains(5.0) for interval in intervals)


def test_draft_aliases_normalize_and_unknown_values_fail():
    assert normalize_draft_seniority(["mid-level", "SENIOR", "senior"]) == (
        SeniorityLevel.MID_LEVEL,
        SeniorityLevel.SENIOR,
    )
    with pytest.raises(UnknownSeniorityError, match="unknown seniority value"):
        normalize_draft_seniority(["manager"])
