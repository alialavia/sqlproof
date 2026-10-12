"""Our connection-string parser must agree with libpq, the parser that
actually consumes the strings at connect time.

PyGreSQL links libpq into the process but does not wrap its parser, so
these tests call ``PQconninfoParse`` through ctypes. No database is needed.
"""

from __future__ import annotations

import ctypes
import os
from ctypes.util import find_library
from pathlib import Path
from urllib.parse import quote

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st

from sqlproof._dsn import conninfo_to_dict, make_conninfo
from sqlproof.exceptions import SqlProofUsageError


class _Option(ctypes.Structure):
    _fields_ = [
        ("keyword", ctypes.c_char_p),
        ("envvar", ctypes.c_char_p),
        ("compiled", ctypes.c_char_p),
        ("val", ctypes.c_char_p),
        ("label", ctypes.c_char_p),
        ("dispchar", ctypes.c_char_p),
        ("dispsize", ctypes.c_int),
    ]


def _load_libpq() -> ctypes.CDLL | None:
    import pg  # noqa: F401 - loads libpq into the process

    candidates: list[str | None] = [
        # dlopen(NULL) sees libpq once PyGreSQL has loaded it (not on Windows,
        # where ctypes rejects None).
        None,
        find_library("pq"),
        find_library("libpq"),
        "libpq.so.5",
        "libpq.5.dylib",
        str(Path(os.environ["PGBIN"]) / "libpq.dll") if os.environ.get("PGBIN") else None,
    ]
    for name in candidates:
        try:
            library = ctypes.CDLL(name)
        except (OSError, TypeError):
            continue
        if hasattr(library, "PQconninfoParse"):
            library.PQconninfoParse.restype = ctypes.POINTER(_Option)
            library.PQconninfoParse.argtypes = [ctypes.c_char_p, ctypes.POINTER(ctypes.c_char_p)]
            library.PQconninfoFree.argtypes = [ctypes.POINTER(_Option)]
            library.PQfreemem.argtypes = [ctypes.c_void_p]
            return library
    return None


LIBPQ = _load_libpq()
pytestmark = pytest.mark.skipif(LIBPQ is None, reason="libpq symbols are not reachable via ctypes")

ERROR = "error"


def libpq_parse(dsn: str) -> dict[str, str] | str:
    """The oracle: libpq's own parse, as {keyword: value}, or ERROR."""
    assert LIBPQ is not None
    message = ctypes.c_char_p()
    options = LIBPQ.PQconninfoParse(dsn.encode(), ctypes.byref(message))
    if not options:
        LIBPQ.PQfreemem(message)
        return ERROR
    parsed: dict[str, str] = {}
    index = 0
    while options[index].keyword:
        if options[index].val is not None:
            parsed[options[index].keyword.decode()] = options[index].val.decode()
        index += 1
    LIBPQ.PQconninfoFree(options)
    return parsed


def ours(dsn: str) -> dict[str, str] | str:
    try:
        return conninfo_to_dict(dsn)
    except SqlProofUsageError:
        return ERROR


# libpq rejects option names it does not know; we accept them and let libpq
# report the problem at connect time (see test at the bottom), so generated
# strings use names every supported libpq knows.
KNOWN_KEYS = st.sampled_from(
    [
        "host",
        "port",
        "dbname",
        "user",
        "password",
        "application_name",
        "options",
        "client_encoding",
        "connect_timeout",
        "sslmode",
        "sslrootcert",
        "target_session_attrs",
    ]
)
# NUL cannot travel through a C string, and libpq rejects %00 outright.
TEXT = st.text(
    alphabet=st.characters(blacklist_categories=("Cs",), blacklist_characters="\x00"),
    max_size=12,
)
# URI components: either fully percent-encoded arbitrary text, or raw text
# from the characters libpq accepts unencoded (including a literal '+').
COMPONENT = st.one_of(
    TEXT.map(lambda text: quote(text, safe="")),
    st.from_regex(r"\A[A-Za-z0-9._~+-]{0,8}\Z"),
)
HOST = st.one_of(
    st.from_regex(r"\A[A-Za-z0-9.-]{1,12}\Z"),
    st.just("[::1]"),
    st.just("[2001:db8::7]"),
    TEXT.map(lambda text: quote(text, safe="")).filter(bool),  # e.g. socket directories
)
PORT = st.one_of(st.just(""), st.integers(0, 99999).map(str))


@st.composite
def uris(draw: st.DrawFn) -> str:
    scheme = draw(st.sampled_from(["postgresql://", "postgres://"]))
    userinfo = ""
    if draw(st.booleans()):
        userinfo = draw(COMPONENT)
        if draw(st.booleans()):
            userinfo += ":" + draw(COMPONENT)
        userinfo += "@"
    hosts = draw(st.lists(st.tuples(HOST, PORT), max_size=3))
    authority = ",".join(host + (":" + port if port else "") for host, port in hosts)
    path = ""
    if draw(st.booleans()):
        path = "/" + draw(COMPONENT)
    query = ""
    if draw(st.booleans()):
        # A list, not a dict, so duplicate keys occur and exercise libpq's rejection.
        pairs = draw(st.lists(st.tuples(KNOWN_KEYS, COMPONENT), max_size=3))
        query = "?" + "&".join(f"{key}={value}" for key, value in pairs)
    fragment = draw(st.sampled_from(["", "#frag"]))
    return scheme + userinfo + authority + path + query + fragment


@settings(max_examples=400, deadline=None)
@given(dsn=uris())
@example(dsn="postgresql://:pw@localhost/db")
@example(dsn="postgresql://u:@localhost/db")
@example(dsn="postgresql://u@localhost/db?host=a&host=b")  # last wins, beats the URI host
@example(dsn="postgresql://u@localhost/db?=x")
@example(dsn="postgresql://u@localhost/db?user=%zz")
@example(dsn="postgresql://u@localhost/db#frag")
@example(dsn="postgresql://u:p@host1,[::1]:5433/base?application_name=review+test")
@example(dsn="postgresql://%2Fvar%2Frun/x")
def test_uri_parsing_agrees_with_libpq(dsn: str) -> None:
    """Property: for any URI built from libpq's grammar, we reject it exactly
    when libpq rejects it, and otherwise produce the same option values.
    Oracle: libpq's PQconninfoParse."""
    assert ours(dsn) == libpq_parse(dsn)


@settings(max_examples=300, deadline=None)
@given(options=st.dictionaries(KNOWN_KEYS, TEXT, max_size=5))
@example(options={"password": "'"})
@example(options={"password": "\\"})
@example(options={"password": "a b"})
@example(options={"password": ""})
def test_rendered_keyword_strings_are_read_back_by_libpq_unchanged(options: dict[str, str]) -> None:
    """Property: libpq parses our rendered keyword string into exactly the
    options it was built from. This is the direction the mutation runner
    depends on when it hands a clone DSN to libpq.
    Oracle: libpq's PQconninfoParse."""
    assert libpq_parse(make_conninfo(**options)) == options


@settings(max_examples=200, deadline=None)
@given(options=st.dictionaries(KNOWN_KEYS, TEXT, max_size=5))
def test_keyword_parsing_agrees_with_libpq_on_rendered_strings(options: dict[str, str]) -> None:
    """Property: our keyword parser and libpq's read the same rendered string
    identically. Oracle: libpq's PQconninfoParse."""
    dsn = make_conninfo(**options)
    assert ours(dsn) == libpq_parse(dsn)


@pytest.mark.parametrize("dsn", ["host=a nosuchkey=b", "postgresql://a/db?nosuchkey=1"])
def test_unknown_option_names_are_the_one_deliberate_divergence(dsn: str) -> None:
    """libpq rejects option names it does not know. We pass them through:
    libpq's set grows between versions (load_balance_hosts, sslnegotiation),
    and user DSNs go to libpq verbatim at connect time, where it reports the
    problem itself. Pinned so the divergence is visible, not accidental."""
    assert libpq_parse(dsn) == ERROR
    assert isinstance(ours(dsn), dict)
