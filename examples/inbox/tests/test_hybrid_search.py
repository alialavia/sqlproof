"""Recipe 6: stable-vector-pagination.

The pagination property: paging through all results in chunks of N
yields the same set as fetching all results in one query, with no
duplicates and no skipped articles. The buggy ORDER BY has no
tiebreaker, so identical scores break pagination.
"""

from __future__ import annotations

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

PROOF = settings(
    max_examples=20,
    deadline=None,
    suppress_health_check=[
        HealthCheck.function_scoped_fixture,
        HealthCheck.data_too_large,
        HealthCheck.too_slow,
        HealthCheck.filter_too_much,
    ],
)


@PROOF
@given(
    data=st.data(),
    query_text=st.sampled_from(["a", "the", "support"]),
    page_size=st.integers(min_value=1, max_value=3),
)
def test_pagination_partitions_full_result_set(
    proof, data, query_text, page_size,
) -> None:
    # Force many tied scores by making all embeddings identical and
    # all titles miss the query text — every article gets score 0.7.
    fixed_vec = "[" + ",".join(["0.0"] * 384) + "]"

    dataset = data.draw(
        proof.dataset_strategy(
            sizes={
                "organizations":          1,
                "kb_articles":            8,
                "kb_article_embeddings":  8,
            },
            columns={
                "kb_article_embeddings.embedding": st.just(fixed_vec),
                "kb_articles.title": st.just("zzz_nomatch_zzz"),
                "kb_articles.published": st.just(True),
            },
        ),
    )
    with proof.client_for_dataset(dataset) as db:
        org_id = dataset["organizations"][0]["id"]

        all_at_once = db.query(
            "SELECT article_id FROM search_kb_hybrid(%s, %s::vector, %s, 100, 0)",
            org_id, fixed_vec, query_text,
        )
        full_set = {str(row["article_id"]) for row in all_at_once}

        paged_ids: list[str] = []
        offset = 0
        while True:
            page = db.query(
                "SELECT article_id FROM search_kb_hybrid(%s, %s::vector, %s, %s, %s)",
                org_id, fixed_vec, query_text, page_size, offset,
            )
            if not page:
                break
            paged_ids.extend(str(row["article_id"]) for row in page)
            offset += page_size

        assert len(paged_ids) == len(set(paged_ids)), (
            f"duplicate article ids across pages: "
            f"{[x for x in paged_ids if paged_ids.count(x) > 1]}"
        )
        assert set(paged_ids) == full_set, (
            f"paged set != full set: "
            f"missing={full_set - set(paged_ids)}, "
            f"extra={set(paged_ids) - full_set}"
        )
