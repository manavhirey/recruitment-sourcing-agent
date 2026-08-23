from collections.abc import Iterable

from app.clients.taxonomy import IndustryTaxonomy
from app.jobs.schemas import ConfirmedScorecard, CriterionKind
from app.jobs.seniority import SeniorityLevel, validate_confirmed_seniority
from app.providers.base import ProviderQuery

_APOLLO_BY_LEVEL = {
    SeniorityLevel.EARLY_CAREER: ("entry", "intern"),
    SeniorityLevel.MID_LEVEL: ("entry", "senior", "manager"),
    SeniorityLevel.SENIOR: ("senior", "manager", "director", "head", "vp", "c_suite"),
}


def _stable_unique(values: Iterable[str]) -> tuple[str, ...]:
    unique: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = " ".join(value.split())
        key = normalized.casefold()
        if normalized and key not in seen:
            seen.add(key)
            unique.append(normalized)
    return tuple(unique)


def _chunks(values: tuple[str, ...], size: int) -> Iterable[tuple[str, ...]]:
    for index in range(0, len(values), size):
        yield values[index : index + size]


def _provider_seniorities(scorecard: ConfirmedScorecard) -> tuple[str, ...]:
    if scorecard.minimum_years is not None or scorecard.maximum_years is not None:
        return ()
    levels = validate_confirmed_seniority(scorecard.seniority)
    return _stable_unique(
        value for level in levels for value in _APOLLO_BY_LEVEL[level]
    )


class QueryPlanner:
    def __init__(
        self,
        max_queries: int = 8,
        taxonomy: IndustryTaxonomy | None = None,
    ) -> None:
        if max_queries < 1:
            raise ValueError("max_queries must be positive")
        self._max_queries = min(max_queries, 8)
        self._taxonomy = taxonomy or IndustryTaxonomy.load_version("v1")

    def compile(self, scorecard: ConfirmedScorecard) -> tuple[ProviderQuery, ...]:
        titles = _stable_unique(scorecard.target_titles)
        seniorities = _provider_seniorities(scorecard)
        locations = _stable_unique(scorecard.locations)
        criterion_keywords = _stable_unique(
            criterion.label
            for criterion in scorecard.criteria
            if criterion.kind is not CriterionKind.EXCLUSION
        )
        industries = _stable_unique(
            [scorecard.industry_code, *scorecard.suggested_adjacent_industries]
        )

        planned: list[ProviderQuery] = []
        seen_hashes: set[str] = set()
        for industry_code in industries:
            industry = self._taxonomy.get(industry_code.casefold())
            keywords = _stable_unique((industry.label, *criterion_keywords))
            for title_group in _chunks(titles, 3):
                query = ProviderQuery(
                    titles=title_group,
                    seniorities=seniorities,
                    person_locations=locations,
                    industry_codes=(industry.code,),
                    keywords=keywords,
                )
                if query.query_hash in seen_hashes:
                    continue
                seen_hashes.add(query.query_hash)
                planned.append(query)
                if len(planned) == self._max_queries:
                    return tuple(planned)
        return tuple(planned)
