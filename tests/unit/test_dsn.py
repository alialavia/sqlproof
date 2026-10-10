"""Connection strings preserve credentials and transport settings."""

from __future__ import annotations

from sqlproof._dsn import conninfo_to_dict, make_conninfo, quote_identifier


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


def test_url_credentials_and_query_options_are_decoded() -> None:
    assert conninfo_to_dict("postgresql://u%40x:p%2F%27@localhost:5433/a%20b?sslmode=require") == {
        "user": "u@x",
        "password": "p/'",
        "host": "localhost",
        "port": "5433",
        "dbname": "a b",
        "sslmode": "require",
    }


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
