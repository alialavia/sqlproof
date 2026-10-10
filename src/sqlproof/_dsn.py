"""PostgreSQL connection strings and identifier quoting."""

from __future__ import annotations

import re
from urllib.parse import unquote, urlsplit

from sqlproof.exceptions import SqlProofUsageError

_PARAMETER = re.compile(
    r"\s*(?P<key>[a-zA-Z_][a-zA-Z_0-9]*)\s*=\s*"
    r"(?:'(?P<quoted>(?:\\.|[^'\\])*)'|(?!')(?P<plain>(?:\\.|[^\s\\])*))"
)
_URI = re.compile(r"postgres(?:ql)?://(?P<authority>[^/?#]*)(?P<suffix>.*)", re.DOTALL)


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
    # Python rejects a multi-host authority containing IPv6, although libpq
    # accepts it, and urlsplit lowercases host names. Parse the authority by
    # hand and only use urlsplit for the path and query.
    url = urlsplit("postgresql://localhost" + suffix)
    result: dict[str, str] = {}
    credentials, has_credentials, hosts_part = authority.rpartition("@")
    if has_credentials:
        user, has_password, password = credentials.partition(":")
        result["user"] = unquote(user)
        if has_password:
            result["password"] = unquote(password)
    else:
        hosts_part = authority
    if url.path[1:]:
        result["dbname"] = unquote(url.path[1:])
    if hosts_part:
        hosts: list[str] = []
        ports: list[str] = []
        for entry in hosts_part.split(","):
            host, port = _split_host_port(entry)
            hosts.append(unquote(host))
            ports.append(port)
        result["host"] = ",".join(hosts)
        # Empty port entries mean the default port for that host.
        if any(ports):
            result["port"] = ",".join(ports)
    for option in url.query.split("&"):
        if not option:
            continue
        key, has_value, value = option.partition("=")
        if not has_value:
            msg = f"Invalid PostgreSQL URI: query parameter {unquote(key)!r} has no '=' sign."
            raise SqlProofUsageError(msg)
        # libpq decodes percent escapes, but preserves literal '+'.
        result[unquote(key)] = unquote(value)
    return result


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
