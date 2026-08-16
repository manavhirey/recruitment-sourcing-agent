import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime

from app.candidates.schemas import CandidateExperienceProfile, CandidateProfile
from app.clients.taxonomy import IndustryTaxonomy
from app.jobs.legal_policy import DEFAULT_SCORECARD_LEGAL_POLICY
from app.jobs.schemas import ConfirmedScorecard, CriterionKind, ScorecardCriterion
from app.matching.schemas import (
    CriterionEvaluation,
    EvidenceState,
    MatchResult,
    ScoreBreakdown,
)

_TITLE_ALIASES = {
    "sr": "senior",
    "sr product manager": "senior product manager",
    "sr manager": "senior manager",
    "vice president": "vp",
}
_SKILL_ALIASES = {
    "payment processing": "payments",
    "payments platform": "payments",
    "payments platform experience": "payments",
    "structured query language": "sql",
    "sql analytics experience": "sql",
}
_SENIORITY_ALIASES = {
    "entry level": "entry",
    "junior": "entry",
    "jr": "entry",
    "individual contributor": "individual_contributor",
    "ic": "individual_contributor",
    "manager": "manager",
    "senior": "senior",
    "sr": "senior",
    "lead": "lead",
    "head": "head",
    "director": "director",
    "vice president": "vp",
    "vp": "vp",
    "c level": "c_suite",
    "c suite": "c_suite",
}
_LOCATION_ALIASES = {
    "new york": "new york ny",
    "new york city": "new york ny",
    "new york ny": "new york ny",
    "nyc": "new york ny",
}
_ELIGIBILITY_ALIASES = {
    "not authorized to work in the united states": "us_not_work_authorized",
    "not authorized to work in us": "us_not_work_authorized",
    "not eligible to work in the united states": "us_not_work_authorized",
    "authorized to work in the united states": "us_work_authorized",
    "authorized to work in us": "us_work_authorized",
    "employment eligible in the united states": "us_work_authorized",
    "employment eligible in us": "us_work_authorized",
    "us work authorized": "us_work_authorized",
    "united states work authorized": "us_work_authorized",
    "authorized to work in india": "india_work_authorized",
    "legally employable in india": "india_work_authorized",
    "requires sponsorship in the united states": "us_sponsorship_required",
    "requires us sponsorship": "us_sponsorship_required",
    "does not require visa sponsorship": "no_sponsorship_required",
    "no visa sponsorship needed": "no_sponsorship_required",
}
_CURRENT_DATE_MARKERS = frozenset({"current", "now", "present"})
_SENIORITY_LEVELS = {
    "entry": 1,
    "individual_contributor": 2,
    "manager": 3,
    "senior": 4,
    "lead": 5,
    "head": 6,
    "director": 7,
    "vp": 8,
    "c_suite": 9,
}


@dataclass
class _AtomicEvaluation:
    key: str
    label: str
    state: EvidenceState
    summary: str
    evidence: tuple[str, ...]
    source_refs: tuple[str, ...]
    points: int = 0
    max_points: int = 0

    def to_schema(self) -> CriterionEvaluation:
        return CriterionEvaluation(
            key=self.key,
            label=self.label,
            state=self.state,
            summary=self.summary,
            points=self.points,
            max_points=self.max_points,
            evidence=self.evidence,
            source_refs=self.source_refs,
        )


class MatchingEngine:
    def __init__(self, taxonomy: IndustryTaxonomy | None = None) -> None:
        self._taxonomy = taxonomy or IndustryTaxonomy.load_version("v1")

    def evaluate(
        self, scorecard: ConfirmedScorecard, candidate: CandidateProfile
    ) -> MatchResult:
        scorecard_criteria = sorted(
            scorecard.criteria,
            key=lambda item: (item.key.casefold(), item.label.casefold()),
        )
        criterion_atoms = [
            self._criterion_evaluation(criterion, candidate)
            for criterion in scorecard_criteria
        ]
        criterion_by_key = {
            criterion.key: atom
            for criterion, atom in zip(scorecard_criteria, criterion_atoms, strict=True)
        }

        role_atoms = [self._role_evaluation(scorecard, candidate)]
        role_atoms.extend(
            criterion_by_key[criterion.key]
            for criterion in scorecard_criteria
            if criterion.kind is not CriterionKind.EXCLUSION
            and not _is_work_eligibility(criterion)
        )
        role_score = _allocate_supported_points(role_atoms, 35)

        scope_atoms = self._scope_evaluations(scorecard, candidate)
        scope_score = _allocate_supported_points(scope_atoms, 25)

        industry_atom = self._industry_evaluation(scorecard, candidate)
        industry_score = industry_atom.points

        location_atoms: list[_AtomicEvaluation] = []
        if scorecard.locations:
            location_atoms.append(self._location_evaluation(scorecard, candidate))
        location_atoms.extend(
            criterion_by_key[criterion.key]
            for criterion in scorecard_criteria
            if criterion.kind is not CriterionKind.EXCLUSION
            and _is_work_eligibility(criterion)
        )
        if not location_atoms:
            location_atoms.append(
                _unknown_atom(
                    "component.location_and_eligibility",
                    "Location and work eligibility",
                    "confirmed location or work-eligibility criterion",
                )
            )
        location_score = _allocate_supported_points(location_atoms, 10)

        recency_atoms = self._recency_and_trajectory(candidate, scorecard.confirmed_at)
        recency_score = _allocate_supported_points(recency_atoms, 10)

        all_atoms = [
            *role_atoms,
            *scope_atoms,
            industry_atom,
            *location_atoms,
            *recency_atoms,
        ]
        unique_atoms = _deduplicate_atoms(all_atoms)
        evaluations = tuple(
            atom.to_schema()
            for atom in sorted(
                unique_atoms,
                key=lambda item: (item.key.casefold(), item.label.casefold()),
            )
        )

        failed_must_haves = tuple(
            sorted(
                criterion.key
                for criterion in scorecard_criteria
                if criterion.kind is CriterionKind.MUST_HAVE
                and criterion_by_key[criterion.key].state is EvidenceState.FAILED
            )
        )
        mandatory_unknowns = tuple(
            sorted(
                criterion.key
                for criterion in scorecard_criteria
                if criterion.kind is CriterionKind.MUST_HAVE
                and criterion.evidence_required
                and criterion_by_key[criterion.key].state is EvidenceState.UNKNOWN
            )
        )
        breakdown = ScoreBreakdown(
            role_and_skills=role_score,
            scope_seniority_years=scope_score,
            industry=industry_score,
            location_and_eligibility=location_score,
            recency_and_trajectory=recency_score,
        )
        return MatchResult(
            classification=(
                "near_match" if failed_must_haves or mandatory_unknowns else "main"
            ),
            total=sum(breakdown.model_dump().values()),
            breakdown=breakdown,
            criteria=evaluations,
            failed_must_haves=failed_must_haves,
            unknown_keys=tuple(
                sorted(
                    {
                        evaluation.key
                        for evaluation in evaluations
                        if evaluation.state is EvidenceState.UNKNOWN
                    }
                )
            ),
        )

    def _criterion_evaluation(
        self, criterion: ScorecardCriterion, candidate: CandidateProfile
    ) -> _AtomicEvaluation:
        if _is_work_eligibility(criterion):
            return _eligibility_evaluation(criterion, candidate.work_eligibility)
        return _skill_evaluation(criterion, candidate.skills)

    def _role_evaluation(
        self, scorecard: ConfirmedScorecard, candidate: CandidateProfile
    ) -> _AtomicEvaluation:
        titles = _stable_values(
            (
                candidate.current_title,
                *(experience.title for experience in candidate.experiences),
            )
        )
        if not titles:
            return _unknown_atom(
                "component.role_title", "Target role", "candidate title"
            )
        matched = any(
            _title_matches(candidate_title, target_title)
            for candidate_title in titles
            for target_title in scorecard.target_titles
        )
        return _evidenced_atom(
            key="component.role_title",
            label="Target role",
            supported=matched,
            evidence=titles,
            source_refs=("candidate.current_title", "candidate.experiences[].title"),
            evidence_name="candidate title",
        )

    def _scope_evaluations(
        self, scorecard: ConfirmedScorecard, candidate: CandidateProfile
    ) -> list[_AtomicEvaluation]:
        atoms: list[_AtomicEvaluation] = []
        if scorecard.seniority:
            if candidate.seniority is None:
                atoms.append(
                    _unknown_atom(
                        "component.seniority", "Seniority", "candidate seniority"
                    )
                )
            else:
                candidate_level = _canonical_seniority(candidate.seniority)
                target_levels = {
                    _canonical_seniority(value) for value in scorecard.seniority
                }
                atoms.append(
                    _evidenced_atom(
                        key="component.seniority",
                        label="Seniority",
                        supported=candidate_level in target_levels,
                        evidence=(candidate.seniority,),
                        source_refs=("candidate.seniority",),
                        evidence_name="candidate seniority",
                    )
                )
        if scorecard.minimum_years is not None or scorecard.maximum_years is not None:
            if candidate.years_experience is None:
                atoms.append(
                    _unknown_atom(
                        "component.years_experience",
                        "Years of experience",
                        "candidate years of experience",
                    )
                )
            else:
                meets_minimum = (
                    scorecard.minimum_years is None
                    or candidate.years_experience >= scorecard.minimum_years
                )
                meets_maximum = (
                    scorecard.maximum_years is None
                    or candidate.years_experience <= scorecard.maximum_years
                )
                atoms.append(
                    _evidenced_atom(
                        key="component.years_experience",
                        label="Years of experience",
                        supported=meets_minimum and meets_maximum,
                        evidence=(str(candidate.years_experience),),
                        source_refs=("candidate.years_experience",),
                        evidence_name="candidate years of experience",
                    )
                )
        if not atoms:
            atoms.append(
                _unknown_atom(
                    "component.scope_seniority_years",
                    "Scope, seniority, and years",
                    "confirmed scope or experience requirement",
                )
            )
        return atoms

    def _industry_evaluation(
        self, scorecard: ConfirmedScorecard, candidate: CandidateProfile
    ) -> _AtomicEvaluation:
        raw_codes = _stable_values(candidate.industry_codes)
        if not raw_codes:
            atom = _unknown_atom(
                "component.industry",
                "Industry experience",
                "candidate industry code",
            )
            atom.max_points = 20
            return atom

        codes = tuple(value.strip().casefold() for value in raw_codes)
        known_codes = tuple(code for code in codes if self._taxonomy.contains(code))
        target = scorecard.industry_code.strip().casefold()
        if target in known_codes:
            return _AtomicEvaluation(
                key="component.industry",
                label="Industry experience",
                state=EvidenceState.SUPPORTED,
                summary=f"Industry experience: exact industry evidence ({target}).",
                evidence=(target,),
                source_refs=("candidate.industry_codes",),
                points=20,
                max_points=20,
            )

        approved = {
            code.strip().casefold()
            for code in scorecard.suggested_adjacent_industries
            if self._taxonomy.contains(code.strip().casefold())
            and self._taxonomy.is_adjacent(target, code.strip().casefold())
        }
        adjacent = tuple(sorted(set(known_codes) & approved))
        if adjacent:
            return _AtomicEvaluation(
                key="component.industry",
                label="Industry experience",
                state=EvidenceState.SUPPORTED,
                summary=(
                    "Industry experience: recruiter-approved adjacent industry "
                    f"evidence ({adjacent[0]})."
                ),
                evidence=adjacent,
                source_refs=("candidate.industry_codes",),
                points=12,
                max_points=20,
            )
        if not known_codes:
            atom = _unknown_atom(
                "component.industry",
                "Industry experience",
                "recognized candidate industry code",
            )
            atom.evidence = raw_codes
            atom.source_refs = ("candidate.industry_codes",)
            atom.max_points = 20
            return atom
        return _AtomicEvaluation(
            key="component.industry",
            label="Industry experience",
            state=EvidenceState.FAILED,
            summary=(
                "Industry experience: explicit candidate industry evidence is neither "
                f"exact nor recruiter-approved adjacent ({', '.join(known_codes)})."
            ),
            evidence=known_codes,
            source_refs=("candidate.industry_codes",),
            points=0,
            max_points=20,
        )

    def _location_evaluation(
        self, scorecard: ConfirmedScorecard, candidate: CandidateProfile
    ) -> _AtomicEvaluation:
        if candidate.location is None or not candidate.location.strip():
            return _unknown_atom("component.location", "Location", "candidate location")
        candidate_location = _canonical_location(candidate.location)
        locations = {_canonical_location(value) for value in scorecard.locations}
        return _evidenced_atom(
            key="component.location",
            label="Location",
            supported=candidate_location in locations,
            evidence=(candidate.location,),
            source_refs=("candidate.location",),
            evidence_name="candidate location",
        )

    def _recency_and_trajectory(
        self, candidate: CandidateProfile, confirmed_at: datetime
    ) -> list[_AtomicEvaluation]:
        return [
            _recency_evaluation(candidate.experiences, confirmed_at.date()),
            _trajectory_evaluation(candidate.experiences),
        ]


def _allocate_supported_points(atoms: list[_AtomicEvaluation], weight: int) -> int:
    ordered = sorted(
        atoms, key=lambda item: (item.key.casefold(), item.label.casefold())
    )
    base, remainder = divmod(weight, len(ordered))
    for index, atom in enumerate(ordered):
        atom.max_points = base + (1 if index < remainder else 0)
        atom.points = atom.max_points if atom.state is EvidenceState.SUPPORTED else 0
    return sum(atom.points for atom in ordered)


def _deduplicate_atoms(atoms: list[_AtomicEvaluation]) -> list[_AtomicEvaluation]:
    unique: dict[tuple[str, str], _AtomicEvaluation] = {}
    for atom in atoms:
        unique[(atom.key, atom.label)] = atom
    return list(unique.values())


def _skill_evaluation(
    criterion: ScorecardCriterion, skills: tuple[str, ...]
) -> _AtomicEvaluation:
    evidence = _stable_values(skills)
    if not evidence:
        return _unknown_atom(criterion.key, criterion.label, "candidate skill")
    targets = {
        _canonical_skill(criterion.key.replace("_", " ")),
        _canonical_skill(criterion.label),
    }
    matched = any(_canonical_skill(skill) in targets for skill in evidence)
    if criterion.kind is CriterionKind.EXCLUSION:
        matched = not matched
    return _evidenced_atom(
        key=criterion.key,
        label=criterion.label,
        supported=matched,
        evidence=evidence,
        source_refs=("candidate.skills",),
        evidence_name="candidate skill",
    )


def _eligibility_evaluation(
    criterion: ScorecardCriterion, work_eligibility: str | None
) -> _AtomicEvaluation:
    if work_eligibility is None or not work_eligibility.strip():
        return _unknown_atom(
            criterion.key, criterion.label, "candidate work-eligibility"
        )
    candidate_value = _canonical_eligibility(work_eligibility)
    targets = {
        _canonical_eligibility(criterion.label),
        _canonical_eligibility(criterion.source_text or ""),
    }
    targets.discard("")
    return _evidenced_atom(
        key=criterion.key,
        label=criterion.label,
        supported=candidate_value in targets,
        evidence=(work_eligibility,),
        source_refs=("candidate.work_eligibility",),
        evidence_name="candidate work-eligibility",
    )


def _recency_evaluation(
    experiences: tuple[CandidateExperienceProfile, ...], confirmed_on: date
) -> _AtomicEvaluation:
    evidence = _experience_evidence(experiences)
    if not evidence:
        return _unknown_atom(
            "component.experience_recency",
            "Experience recency",
            "dated candidate experience",
        )
    if any(
        _normalize(item.end_date or "") in _CURRENT_DATE_MARKERS for item in experiences
    ):
        return _evidenced_atom(
            key="component.experience_recency",
            label="Experience recency",
            supported=True,
            evidence=evidence,
            source_refs=("candidate.experiences[].end_date",),
            evidence_name="dated candidate experience",
        )
    end_dates = [
        parsed
        for item in experiences
        if (parsed := _parse_date(item.end_date)) is not None
    ]
    if not end_dates:
        return _unknown_atom(
            "component.experience_recency",
            "Experience recency",
            "dated candidate experience",
        )
    latest = max(end_dates)
    recent = (confirmed_on.year - latest.year) <= 3
    return _evidenced_atom(
        key="component.experience_recency",
        label="Experience recency",
        supported=recent,
        evidence=evidence,
        source_refs=("candidate.experiences[].end_date",),
        evidence_name="dated candidate experience",
    )


def _trajectory_evaluation(
    experiences: tuple[CandidateExperienceProfile, ...],
) -> _AtomicEvaluation:
    ranked: list[tuple[date, int, str]] = []
    for item in experiences:
        start = _parse_date(item.start_date)
        level = _title_seniority_level(item.title)
        if start is None or level is None or item.title is None:
            continue
        ranked.append((start, level, item.title))
    ranked.sort(
        key=lambda item: (
            item[0],
            _normalize_title(item[2]),
            item[2].casefold(),
            item[2],
        )
    )
    if len(ranked) < 2:
        return _unknown_atom(
            "component.career_trajectory",
            "Career trajectory",
            "dated candidate title progression",
        )
    evidence = tuple(f"{start.isoformat()}: {title}" for start, _, title in ranked)
    return _evidenced_atom(
        key="component.career_trajectory",
        label="Career trajectory",
        supported=ranked[-1][1] >= ranked[0][1],
        evidence=evidence,
        source_refs=(
            "candidate.experiences[].start_date",
            "candidate.experiences[].title",
        ),
        evidence_name="dated candidate title progression",
    )


def _unknown_atom(key: str, label: str, evidence_name: str) -> _AtomicEvaluation:
    return _AtomicEvaluation(
        key=key,
        label=label,
        state=EvidenceState.UNKNOWN,
        summary=f"{label}: explicit {evidence_name} evidence is unavailable.",
        evidence=(),
        source_refs=(),
    )


def _evidenced_atom(
    *,
    key: str,
    label: str,
    supported: bool,
    evidence: tuple[str, ...],
    source_refs: tuple[str, ...],
    evidence_name: str,
) -> _AtomicEvaluation:
    state = EvidenceState.SUPPORTED if supported else EvidenceState.FAILED
    outcome = "supports" if supported else "does not satisfy"
    return _AtomicEvaluation(
        key=key,
        label=label,
        state=state,
        summary=(
            f"{label}: explicit {evidence_name} evidence {outcome} this criterion "
            f"({', '.join(evidence)})."
        ),
        evidence=evidence,
        source_refs=source_refs,
    )


def _is_work_eligibility(criterion: ScorecardCriterion) -> bool:
    text = " ".join(
        (
            criterion.key.replace("_", " "),
            criterion.label,
            criterion.source_text or "",
        )
    )
    return DEFAULT_SCORECARD_LEGAL_POLICY.refers_to_work_authorization(text)


def _stable_values(values: Iterable[str | None]) -> tuple[str, ...]:
    normalized: dict[str, str] = {}
    for value in values:
        if value is None:
            continue
        display = " ".join(value.split())
        if display:
            key = display.casefold()
            current = normalized.get(key)
            if current is None or (display.casefold(), display) < (
                current.casefold(),
                current,
            ):
                normalized[key] = display
    return tuple(normalized[key] for key in sorted(normalized))


def _normalize(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).casefold()
    return " ".join(re.sub(r"[^a-z0-9]+", " ", value).split())


def _canonical_skill(value: str) -> str:
    normalized = _normalize(value)
    return _SKILL_ALIASES.get(normalized, normalized)


def _normalize_title(value: str) -> str:
    normalized = _normalize(value)
    if normalized in _TITLE_ALIASES:
        return _TITLE_ALIASES[normalized]
    return " ".join(_TITLE_ALIASES.get(token, token) for token in normalized.split())


def _title_matches(candidate_title: str, target_title: str) -> bool:
    candidate = _normalize_title(candidate_title)
    target = _normalize_title(target_title)
    return candidate == target or set(target.split()) <= set(candidate.split())


def _canonical_seniority(value: str) -> str:
    normalized = _normalize(value)
    return _SENIORITY_ALIASES.get(normalized, normalized.replace(" ", "_"))


def _canonical_location(value: str) -> str:
    normalized = _normalize(value)
    return _LOCATION_ALIASES.get(normalized, normalized)


def _canonical_eligibility(value: str) -> str:
    normalized = _normalize(value)
    padded = f" {normalized} "
    for alias in sorted(_ELIGIBILITY_ALIASES, key=lambda item: (-len(item), item)):
        if f" {alias} " in padded:
            return _ELIGIBILITY_ALIASES[alias]
    return normalized.replace(" ", "_")


def _parse_date(value: str | None) -> date | None:
    normalized = _normalize(value or "")
    if not normalized or normalized in _CURRENT_DATE_MARKERS:
        return None
    raw_value = (value or "").strip()
    try:
        return date.fromisoformat(raw_value)
    except ValueError:
        pass
    if match := re.fullmatch(r"(\d{4})-(\d{2})", raw_value):
        try:
            return date(int(match.group(1)), int(match.group(2)), 1)
        except ValueError:
            return None
    if re.fullmatch(r"\d{4}", raw_value):
        return date(int(raw_value), 1, 1)
    return None


def _title_seniority_level(title: str | None) -> int | None:
    normalized = _normalize_title(title or "")
    matches = [
        level
        for label, level in _SENIORITY_LEVELS.items()
        if label.replace("_", " ") in normalized
    ]
    return max(matches) if matches else None


def _experience_evidence(
    experiences: tuple[CandidateExperienceProfile, ...],
) -> tuple[str, ...]:
    values = (
        " | ".join(
            part
            for part in (
                item.title,
                item.start_date,
                item.end_date,
            )
            if part
        )
        for item in experiences
        if item.start_date or item.end_date
    )
    return _stable_values(values)
