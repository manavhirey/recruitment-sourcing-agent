from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum


class SeniorityLevel(StrEnum):
    EARLY_CAREER = "early_career"
    MID_LEVEL = "mid_level"
    SENIOR = "senior"


@dataclass(frozen=True)
class ExperienceInterval:
    minimum_years: int
    maximum_years: int | None

    def contains(self, years: float) -> bool:
        return years >= self.minimum_years and (
            self.maximum_years is None or years <= self.maximum_years
        )


@dataclass(frozen=True)
class SeniorityPreset:
    value: SeniorityLevel
    label: str
    minimum_years: int
    maximum_years: int | None


SENIORITY_PRESETS = (
    SeniorityPreset(SeniorityLevel.EARLY_CAREER, "Early-Career", 0, 3),
    SeniorityPreset(SeniorityLevel.MID_LEVEL, "Mid-Level", 3, 9),
    SeniorityPreset(SeniorityLevel.SENIOR, "Senior", 10, None),
)
_PRESET_BY_LEVEL = {preset.value: preset for preset in SENIORITY_PRESETS}
_ALIASES = {
    "early_career": SeniorityLevel.EARLY_CAREER,
    "early-career": SeniorityLevel.EARLY_CAREER,
    "mid_level": SeniorityLevel.MID_LEVEL,
    "mid-level": SeniorityLevel.MID_LEVEL,
    "senior": SeniorityLevel.SENIOR,
}


def normalize_draft_seniority(values: Iterable[str]) -> tuple[SeniorityLevel, ...]:
    requested: set[SeniorityLevel] = set()
    for raw in values:
        value = _ALIASES.get(raw.strip().casefold())
        if value is None:
            raise ValueError(f"unknown seniority value: {raw}")
        requested.add(value)
    return tuple(preset.value for preset in SENIORITY_PRESETS if preset.value in requested)


def validate_confirmed_seniority(values: Iterable[str]) -> tuple[SeniorityLevel, ...]:
    return normalize_draft_seniority(values)


def effective_experience_intervals(
    seniority: Sequence[SeniorityLevel | str],
    minimum_years: int | None,
    maximum_years: int | None,
) -> tuple[ExperienceInterval, ...]:
    if minimum_years is not None or maximum_years is not None:
        return (ExperienceInterval(minimum_years or 0, maximum_years),)
    levels = normalize_draft_seniority(str(value) for value in seniority)
    return tuple(
        ExperienceInterval(
            _PRESET_BY_LEVEL[level].minimum_years,
            _PRESET_BY_LEVEL[level].maximum_years,
        )
        for level in levels
    )
