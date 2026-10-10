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
    uri = _URI.fullmatch(dsn)
    if uri is not None:
        # Python rejects a multi-host authority containing IPv6, although
        # libpq accepts it. Split the authority before using URL helpers.
        url = urlsplit("postgresql://localhost" + uri["suffix"])
        result: dict[str, str] = {}
        authority = uri["authority"]
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
        # Parse each host separately; urlsplit.port cannot read libpq's
        # multi-host URI syntax. Empty port entries mean the default port.
        if hosts_part:
            hosts = [urlsplit("//" + host) for host in hosts_part.split(",")]
            result["host"] = ",".join(unquote(host.hostname or "") for host in hosts)
            ports = [str(host.port) if host.port is not None else "" for host in hosts]
            if any(ports):
                result["port"] = ",".join(ports)
        for option in url.query.split("&"):
            if option:
                key, _, value = option.partition("=")
                # libpq decodes percent escapes, but preserves literal '+'.
                result[unquote(key)] = unquote(value)
        return result
    result = {}
    position = 0
    while dsn[position:].strip():
        match = _PARAMETER.match(dsn, position)
        if match is None:
            raise SqlProofUsageError("Invalid PostgreSQL keyword connection string.")
        value = match.group("quoted")
        if value is None:
            value = match.group("plain")
        result[match.group("key")] = re.sub(r"\\(.)", r"\1", value)
        position = match.end()
    return result


def make_conninfo(dsn: str = "", **overrides: str) -> str:
    parts = conninfo_to_dict(dsn) | overrides
    return " ".join(f"{key}='{_escape(value)}'" for key, value in parts.items())


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


def quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'
