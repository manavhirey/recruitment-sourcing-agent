from app.matching.schemas import EvidenceState, MatchExplanation, MatchResult


def format_explanation(result: MatchResult) -> MatchExplanation:
    return MatchExplanation(
        supported=tuple(
            item.summary
            for item in result.criteria
            if item.state is EvidenceState.SUPPORTED
        ),
        failed=tuple(
            item.summary
            for item in result.criteria
            if item.state is EvidenceState.FAILED
        ),
        unknown=tuple(
            item.summary
            for item in result.criteria
            if item.state is EvidenceState.UNKNOWN
        ),
    )
