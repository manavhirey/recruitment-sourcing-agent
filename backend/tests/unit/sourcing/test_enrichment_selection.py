from uuid import UUID, uuid4

from app.sourcing.enrichment import batch_candidates, stable_top_candidate_ids


def test_top_enrichment_selects_main_rank_only_with_stable_tie_break_and_cap() -> None:
    candidates = [
        (UUID(int=index + 1), 90 if index < 55 else 100, "main") for index in range(60)
    ]
    candidates.extend([(uuid4(), 100, "near_match"), (uuid4(), None, "main")])

    selected = stable_top_candidate_ids(candidates, limit=50)

    assert len(selected) == 50
    assert selected[:5] == [UUID(int=index + 56) for index in range(5)]
    assert selected[5:] == [UUID(int=index + 1) for index in range(45)]


def test_enrichment_batches_never_exceed_ten() -> None:
    candidates = [UUID(int=index + 1) for index in range(23)]

    batches = batch_candidates(candidates)

    assert [len(batch) for batch in batches] == [10, 10, 3]
    assert [item for batch in batches for item in batch] == candidates
