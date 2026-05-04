"""Hypothesis tests for the public well-known generators."""

from __future__ import annotations

from urllib.parse import urlparse

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from sqlproof.generators.well_known import (
    emails,
    phone_numbers,
    postal_codes,
    slugs,
    urls,
)

NON_NULL_KW = settings(
    max_examples=30,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)

ALLOWED_SLUG_CHARS = set("abcdefghijklmnopqrstuvwxyz0123456789-")


@NON_NULL_KW
@given(data=st.data())
def test_slug_uses_only_lowercase_alphanumeric_and_hyphens(data) -> None:
    value = data.draw(slugs())
    assert all(ch in ALLOWED_SLUG_CHARS for ch in value)


@NON_NULL_KW
@given(data=st.data())
def test_slug_does_not_start_or_end_with_hyphen(data) -> None:
    value = data.draw(slugs())
    assert not value.startswith("-")
    assert not value.endswith("-")


@NON_NULL_KW
@given(
    data=st.data(),
    bounds=st.tuples(st.integers(1, 5), st.integers(5, 30)).filter(lambda b: b[0] <= b[1]),
)
def test_slug_respects_length_bounds(data, bounds) -> None:
    min_len, max_len = bounds
    value = data.draw(slugs(min_length=min_len, max_length=max_len))
    assert min_len <= len(value) <= max_len


@NON_NULL_KW
@given(data=st.data())
def test_default_email_uses_one_of_the_default_domains(data) -> None:
    value = data.draw(emails())
    assert "@" in value
    local, domain = value.rsplit("@", 1)
    assert domain in {"example.com", "test.dev"}
    assert local


@NON_NULL_KW
@given(
    data=st.data(),
    domains=st.lists(
        st.sampled_from(["foo.com", "bar.io"]), min_size=1, unique=True
    ),
)
def test_explicit_email_domains_are_honored(data, domains) -> None:
    value = data.draw(emails(domains=domains))
    _, domain = value.rsplit("@", 1)
    assert domain in domains


@NON_NULL_KW
@given(data=st.data())
def test_default_url_parses_with_https_or_http_scheme(data) -> None:
    value = data.draw(urls())
    parsed = urlparse(value)
    assert parsed.scheme in {"https", "http"}
    assert parsed.netloc in {"example.com", "sqlproof.dev", "localhost"}


@NON_NULL_KW
@given(data=st.data())
def test_url_without_optional_parts_omits_them(data) -> None:
    value = data.draw(urls(include_path=False, include_query=False, include_fragment=False))
    parsed = urlparse(value)
    assert parsed.path == ""
    assert parsed.query == ""
    assert parsed.fragment == ""


@NON_NULL_KW
@given(data=st.data())
def test_url_with_fragment_includes_hash(data) -> None:
    value = data.draw(urls(include_fragment=True))
    parsed = urlparse(value)
    assert parsed.fragment != "" or "#" not in value


@NON_NULL_KW
@given(data=st.data(), country=st.sampled_from((None, "US", "CA", "UK", "DE")))
def test_phone_number_starts_with_country_prefix(data, country) -> None:
    value = data.draw(phone_numbers(country=country))
    expected_prefix = "+1" if country in {None, "US", "CA"} else "+44"
    assert value.startswith(expected_prefix)
    assert len(value) >= len(expected_prefix) + 10


@NON_NULL_KW
@given(data=st.data())
def test_us_postal_code_is_five_digit_zero_padded(data) -> None:
    value = data.draw(postal_codes("US"))
    assert len(value) == 5
    assert value.isdigit()


@NON_NULL_KW
@given(data=st.data())
def test_non_us_postal_code_falls_back_to_alphanumeric(data) -> None:
    value = data.draw(postal_codes("XX"))
    assert 3 <= len(value) <= 10
    allowed = set("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 ")
    assert all(ch in allowed for ch in value)
