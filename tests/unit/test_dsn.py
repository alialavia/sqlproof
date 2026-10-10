"""Connection strings preserve credentials and transport settings."""

from __future__ import annotations

import pytest
from hypothesis import example, given
from hypothesis import strategies as st

from sqlproof._dsn import conninfo_to_dict, make_conninfo, quote_identifier
from sqlproof.exceptions import SqlProofUsageError

# libpq keyword names: letters, digits and underscores, not starting with a digit.
KEYWORDS = st.from_regex(r"\A[a-zA-Z_][a-zA-Z_0-9]{0,15}\Z")
# Any text libpq could carry, except lone surrogates, which cannot be encoded.
VALUES = st.text(alphabet=st.characters(blacklist_categories=("Cs",)), max_size=60)


@given(options=st.dictionaries(KEYWORDS, VALUES, max_size=5))
@example(options={"password": ""})
@example(options={"password": " "})
@example(options={"password": "'"})
@example(options={"password": "\\"})
@example(options={"password": "\\'"})
@example(options={"password": "a=b c='d'"})
@example(options={"host": "h", "port": "5432", "dbname": "x y"})
def test_keyword_dsn_round_trips_every_option_unchanged(options: dict[str, str]) -> None:
    """Property: parsing a rendered keyword DSN returns the options it was built from.

    Oracle: the original dict. Catches escaping bugs for quotes, backslashes,
    whitespace and empty values, which libpq would otherwise misread as
    different credentials or as a syntax error.
    """
    assert conninfo_to_dict(make_conninfo(**options)) == options


def test_keyword_dsn_parsing_accepts_quotes_backslashes_and_empty_values() -> None:
    assert conninfo_to_dict(r"password='with space\\slash\'quote' user='' host=local\ host") == {
        "password": "with space\\slash'quote",
        "user": "",
        "host": "local host",
    }


def test_keyword_dsn_rendering_escapes_quotes_and_backslashes() -> None:
    assert make_conninfo(password="with space\\slash'quote") == (
        r"password='with space\\slash\'quote'"
    )


@pytest.mark.parametrize("dsn", ["password='unterminated", "host=a =b", "'a'=b"])
def test_malformed_keyword_dsns_are_rejected_with_a_usage_error(dsn: str) -> None:
    with pytest.raises(SqlProofUsageError, match="Invalid PostgreSQL keyword connection string"):
        conninfo_to_dict(dsn)


def test_url_credentials_and_query_options_are_decoded() -> None:
    assert conninfo_to_dict("postgresql://u%40x:p%2F%27@localhost:5433/a%20b?sslmode=require") == {
        "user": "u@x",
        "password": "p/'",
        "host": "localhost",
        "port": "5433",
        "dbname": "a b",
        "sslmode": "require",
    }


def test_urls_without_credentials_or_host_still_yield_the_database() -> None:
    assert conninfo_to_dict("postgresql://localhost/db") == {"host": "localhost", "dbname": "db"}
    assert conninfo_to_dict("postgresql:///db") == {"dbname": "db"}
    assert conninfo_to_dict("postgres://") == {}


def test_changing_database_preserves_credentials_and_ssl_options() -> None:
    dsn = make_conninfo("postgresql://u:p@localhost/base?sslmode=require", dbname="new ' db")
    assert conninfo_to_dict(dsn) == {
        "user": "u",
        "password": "p",
        "host": "localhost",
        "dbname": "new ' db",
        "sslmode": "require",
    }


def test_multi_host_urls_preserve_default_ports_ipv6_and_literal_plus_signs() -> None:
    assert conninfo_to_dict(
        "postgresql://u:p@host1,[::1]:5433/base?application_name=review+test"
    ) == {
        "user": "u",
        "password": "p",
        "host": "host1,::1",
        "port": ",5433",
        "dbname": "base",
        "application_name": "review+test",
    }


def test_identifiers_with_quotes_cannot_break_out_of_the_identifier() -> None:
    assert quote_identifier('db"; DROP DATABASE other; --') == '"db""; DROP DATABASE other; --"'


def test_url_host_names_keep_their_case_unlike_urlsplit() -> None:
    """libpq passes host names through unchanged; a lowercasing parser would
    break case-sensitive socket directories and some certificate checks."""
    assert conninfo_to_dict("postgresql://u@Db.Example.COM:5433,%2FVar%2FRun/x") == {
        "user": "u",
        "host": "Db.Example.COM,/Var/Run",
        "port": "5433,",
        "dbname": "x",
    }


def test_url_query_parameters_without_an_equals_sign_are_rejected_like_libpq() -> None:
    with pytest.raises(SqlProofUsageError, match="query parameter 'sslmode' has no '='"):
        conninfo_to_dict("postgresql://localhost/db?sslmode")


@pytest.mark.parametrize(
    "dsn",
    ["postgresql://[::1/db", "postgresql://[::1]x/db"],
)
def test_malformed_ipv6_hosts_are_rejected(dsn: str) -> None:
    with pytest.raises(SqlProofUsageError, match="IPv6"):
        conninfo_to_dict(dsn)


def test_parse_errors_name_the_position_but_never_echo_the_value() -> None:
    secret = "hunter2-secret"
    with pytest.raises(SqlProofUsageError) as exc:
        conninfo_to_dict(f"password={secret} 'broken")
    assert secret not in str(exc.value)
    assert "at character" in str(exc.value)
