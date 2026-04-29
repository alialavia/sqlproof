from __future__ import annotations

from hypothesis import strategies as st
from hypothesis.strategies import SearchStrategy


def slugs(min_length: int = 1, max_length: int = 64) -> SearchStrategy[str]:
    alphabet = "abcdefghijklmnopqrstuvwxyz0123456789-"
    return st.text(alphabet=alphabet, min_size=min_length, max_size=max_length).filter(
        lambda value: not value.startswith("-") and not value.endswith("-")
    )


def emails(domains: list[str] | None = None) -> SearchStrategy[str]:
    domain_strategy = (
        st.sampled_from(domains) if domains else st.sampled_from(["example.com", "test.dev"])
    )
    local = st.text(
        alphabet="abcdefghijklmnopqrstuvwxyz0123456789._-",
        min_size=1,
        max_size=32,
    ).filter(lambda value: value.strip("."))
    return st.builds(_join_email, local, domain_strategy)


def urls(
    schemes: tuple[str, ...] = ("https", "http"),
    *,
    include_path: bool = True,
    include_query: bool = True,
    include_fragment: bool = False,
) -> SearchStrategy[str]:
    scheme = st.sampled_from(schemes)
    host = st.sampled_from(["example.com", "sqlproof.dev", "localhost"])
    path = slugs(max_length=20).map(lambda value: f"/{value}") if include_path else st.just("")
    query = st.text(max_size=10).map(lambda value: f"?q={value}") if include_query else st.just("")
    fragment = (
        slugs(max_length=10).map(lambda value: f"#{value}") if include_fragment else st.just("")
    )
    return st.builds(_join_url, scheme, host, path, query, fragment)


def phone_numbers(country: str | None = None) -> SearchStrategy[str]:
    prefix = "+1" if country in {None, "US", "CA"} else "+44"
    return st.integers(2_000_000_000, 9_999_999_999).map(lambda number: f"{prefix}{number}")


def postal_codes(country: str) -> SearchStrategy[str]:
    if country.upper() in {"US", "USA"}:
        return st.integers(0, 99_999).map(lambda number: f"{number:05d}")
    return st.text(alphabet="ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 ", min_size=3, max_size=10)


def _join_email(lhs: str, rhs: str) -> str:
    return f"{lhs}@{rhs}"


def _join_url(scheme: str, host: str, path: str, query: str, fragment: str) -> str:
    return f"{scheme}://{host}{path}{query}{fragment}"
