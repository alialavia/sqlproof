"""PostgreSQL connection strings and identifier quoting."""

from __future__ import annotations

import re
from urllib.parse import unquote

from sqlproof.exceptions import SqlProofUsageError

_PARAMETER = re.compile(
    r"\s*(?P<key>[a-zA-Z_][a-zA-Z_0-9]*)\s*=\s*"
    r"(?:'(?P<quoted>(?:\\.|[^'\\])*)'|(?!')(?P<plain>(?:\\.|[^\s\\])*))"
)
_URI = re.compile(r"postgres(?:ql)?://(?P<authority>[^/?]*)(?P<suffix>.*)", re.DOTALL)
_BAD_PERCENT = re.compile(r"%(?![0-9A-Fa-f]{2})|%00")


def conninfo_to_dict(dsn: str) -> dict[str, str]:
    """Split a libpq URI or keyword connection string into its options.

    Error messages name the offending position or key, never a value, so
    they are safe to log even when the string carries a password.
    """
    uri = _URI.fullmatch(dsn)
    if uri is not None:
        return _parse_uri(uri["authority"], uri["suffix"])
    result: dict[str, str] = {}
    position = 0
    while dsn[position:].strip():
        match = _PARAMETER.match(dsn, position)
        if match is None:
            msg = (
                "Invalid PostgreSQL keyword connection string: expected key=value "
                f"at character {position}."
            )
            raise SqlProofUsageError(msg)
        value = match.group("quoted")
        if value is None:
            value = match.group("plain")
        result[match.group("key")] = re.sub(r"\\(.)", r"\1", value)
        position = match.end()
    return result


def _parse_uri(authority: str, suffix: str) -> dict[str, str]:
    """Mirror libpq's URI parser (conninfo_uri_parse_options).

    Python's urlsplit is deliberately not used: it rejects a multi-host
    authority containing IPv6, lowercases host names, and treats '#' as a
    fragment, where libpq keeps it as part of the database name. Checked
    against libpq itself in tests/unit/test_dsn_libpq_oracle.py.
    """
    result: dict[str, str] = {}
    credentials, has_credentials, hosts_part = authority.rpartition("@")
    if has_credentials:
        user, has_password, password = credentials.partition(":")
        # libpq ignores an empty user or password rather than storing "".
        if user:
            result["user"] = _decode(user)
        if has_password and password:
            result["password"] = _decode(password)
    else:
        hosts_part = authority
    path, has_query, query = suffix.partition("?")
    if path[1:]:
        result["dbname"] = _decode(path[1:])
    if hosts_part:
        hosts: list[str] = []
        ports: list[str] = []
        for entry in hosts_part.split(","):
            host, port = _split_host_port(entry)
            hosts.append(_decode(host))
            ports.append(port)
        result["host"] = ",".join(hosts)
        # Empty port entries mean the default port for that host. libpq
        # emits the comma-joined list whenever there is more than one host,
        # even if every entry is empty, and omits it for a lone host with
        # no port.
        if any(ports) or len(ports) > 1:
            result["port"] = ",".join(ports)
    for option in query.split("&") if has_query else ():
        if not option:
            continue
        key, has_value, value = option.partition("=")
        key = _decode(key)
        if not has_value or not key:
            # libpq rejects both with "invalid URI query parameter". A repeated
            # key is accepted by libpq, last one wins, and it overrides the
            # same option given in the authority (?host=... beats the URI host).
            msg = f"Invalid PostgreSQL URI: invalid query parameter {key!r}."
            raise SqlProofUsageError(msg)
        # libpq decodes percent escapes, but preserves literal '+'.
        result[key] = _decode(value)
    return result


def _decode(part: str) -> str:
    if _BAD_PERCENT.search(part):
        raise SqlProofUsageError("Invalid PostgreSQL URI: invalid percent-encoded token.")
    return unquote(part)


def _split_host_port(entry: str) -> tuple[str, str]:
    if entry.startswith("["):
        # IPv6 literal, optionally followed by :port.
        closing = entry.find("]")
        if closing == -1:
            raise SqlProofUsageError("Invalid PostgreSQL URI: unterminated IPv6 address.")
        rest = entry[closing + 1 :]
        if rest and not rest.startswith(":"):
            raise SqlProofUsageError("Invalid PostgreSQL URI: unexpected text after IPv6 address.")
        return entry[1:closing], rest[1:]
    host, has_port, port = entry.rpartition(":")
    if not has_port:
        return entry, ""
    return host, port


def make_conninfo(dsn: str = "", **overrides: str) -> str:
    parts = conninfo_to_dict(dsn) | overrides
    return " ".join(f"{key}='{_escape(value)}'" for key, value in parts.items())


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


def quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'
