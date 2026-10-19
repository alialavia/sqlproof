from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from math import isnan
from typing import Any

from hypothesis import strategies as st
from hypothesis.errors import InvalidArgument
from hypothesis.strategies import SearchStrategy

from sqlproof._range import Range


@st.composite
def ranges(draw: st.DrawFn, elements: SearchStrategy[Any]) -> Range:
    """Draw empty, bounded and unbounded PostgreSQL ranges.

    ``elements`` supplies non-null, mutually comparable endpoints of the
    range's PostgreSQL subtype. NaN endpoints are refused: PostgreSQL
    orders NaN above every number, so they cannot bound a range the way
    the endpoint suggests.

    Continuous subtypes (``numrange``, ``tsrange``, ``tstzrange``) get all
    four bound forms ``[)``, ``[]``, ``(]`` and ``()``, single-point ranges
    ``[x,x]``, and inclusive or exclusive finite ends on half-unbounded
    ranges, so inclusive-versus-exclusive boundary bugs are reachable.
    Discrete subtypes (``int4range``, ``int8range``, ``daterange``, whose
    endpoints are ``int`` or ``date``) use ``[)``: PostgreSQL stores every
    discrete range in that form, so other forms would only change how the
    value reads back, not which values it contains.

    Equal endpoints with a non-``[]`` form produce an empty range.
    Timezone-aware datetimes are ordered by instant, including repeated
    hours. Use as a column override when a property needs these edge
    cases; schema-derived ranges remain finite and nonempty by default.
    """
    first = draw(elements)
    _refuse_nan(first)
    discrete = _is_discrete(first)
    shapes = ["empty", "bounded", "lower_unbounded", "upper_unbounded", "unbounded"]
    if not discrete:
        shapes.append("point")
    shape = draw(st.sampled_from(shapes))
    if shape == "empty":
        return Range(empty=True)
    if shape == "unbounded":
        return Range(bounds="()")
    if shape == "point":
        return Range(first, first, "[]")
    inclusive = False if discrete else draw(st.booleans())
    if shape == "lower_unbounded":
        return Range(None, first, "(]" if inclusive else "()")
    if shape == "upper_unbounded":
        return Range(first, None, "[)" if discrete or inclusive else "()")
    second = draw(elements)
    _refuse_nan(second)
    bounds = "[)" if discrete else draw(st.sampled_from(("[)", "[]", "(]", "()")))
    first_key, second_key = _range_order_key(first), _range_order_key(second)
    if first_key == second_key:
        return Range(first, second, "[]") if bounds == "[]" else Range(empty=True)
    if first_key > second_key:
        first, second = second, first
    return Range(first, second, bounds)


def _is_discrete(value: Any) -> bool:
    return (isinstance(value, int) and not isinstance(value, bool)) or (
        isinstance(value, date) and not isinstance(value, datetime)
    )


def _refuse_nan(value: Any) -> None:
    if (isinstance(value, float) and isnan(value)) or (
        isinstance(value, Decimal) and value.is_nan()
    ):
        msg = (
            "ranges() endpoints cannot be NaN; pass elements that exclude it, "
            "for example st.decimals(allow_nan=False) or st.floats(allow_nan=False)."
        )
        raise InvalidArgument(msg)


def _range_order_key(value: Any) -> Any:
    # Python ignores fold when both datetimes share a tzinfo. Fixed offsets
    # preserve the instants and compare them correctly without UTC overflow.
    if isinstance(value, datetime):
        offset = value.utcoffset()
        if offset is not None:
            return value.replace(tzinfo=timezone(offset))
    return value


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
