"""A strict parser for Structured Field Dictionaries (RFC 9651).

AI usage preferences are serialized as a Structured Field Dictionary
(draft-ietf-aipref-vocab, Section 6). The vocabulary draft is explicit that a
parse failure makes *every* preference unknown, so the parser has to reject
exactly what RFC 9651 rejects - a lenient parser would invent preferences that
the declaring party never validly expressed.

Only parsing is implemented; usageledger never needs to serialize.
"""

from __future__ import annotations

import base64
import string
from dataclasses import dataclass, field
from typing import Union

__all__ = [
    "ParseError",
    "Token",
    "DisplayString",
    "Date",
    "Item",
    "InnerList",
    "parse_dictionary",
]


class ParseError(ValueError):
    """Raised when input is not a valid Structured Field Dictionary."""


class Token(str):
    """An sf-token. Subclasses ``str`` so it compares equal to plain strings,
    but ``isinstance(value, Token)`` distinguishes it from an sf-string."""

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return f"Token({str.__repr__(self)})"


class DisplayString(str):
    """An sf-displaystring (RFC 9651 Section 3.3.8)."""


@dataclass(frozen=True)
class Date:
    """An sf-date (RFC 9651 Section 3.3.7): seconds since the Unix epoch."""

    seconds: int


BareItem = Union[int, float, str, Token, DisplayString, bytes, bool, Date]


@dataclass
class Item:
    value: BareItem
    params: dict[str, BareItem] = field(default_factory=dict)


@dataclass
class InnerList:
    items: list[Item]
    params: dict[str, BareItem] = field(default_factory=dict)


Member = Union[Item, InnerList]

_LCALPHA = set(string.ascii_lowercase)
_DIGIT = set(string.digits)
_ALPHA = set(string.ascii_letters)
_KEY_FIRST = _LCALPHA | {"*"}
_KEY_REST = _LCALPHA | _DIGIT | set("_-.*")
_TCHAR = _ALPHA | _DIGIT | set("!#$%&'*+-.^_`|~")
_TOKEN_REST = _TCHAR | set(":/")
_BASE64 = set(string.ascii_letters + string.digits + "+/=")
_LCHEX = set("0123456789abcdef")


class _Cursor:
    __slots__ = ("s", "i")

    def __init__(self, s: str):
        self.s = s
        self.i = 0

    def empty(self) -> bool:
        return self.i >= len(self.s)

    def peek(self) -> str:
        return self.s[self.i] if self.i < len(self.s) else ""

    def take(self) -> str:
        if self.i >= len(self.s):
            raise ParseError("unexpected end of input")
        c = self.s[self.i]
        self.i += 1
        return c

    def skip_sp(self) -> None:
        while self.peek() == " ":
            self.i += 1

    def skip_ows(self) -> None:
        while self.peek() in (" ", "\t") and self.peek():
            self.i += 1


def parse_dictionary(data: bytes | str) -> dict[str, Member]:
    """Parse ``data`` as an RFC 9651 Dictionary.

    Duplicate keys follow RFC 9651: the last value wins. Raises
    :class:`ParseError` on any syntax error, including non-ASCII input and
    uppercase keys.
    """
    if isinstance(data, bytes):
        try:
            text = data.decode("ascii")
        except UnicodeDecodeError as exc:
            raise ParseError("input is not ASCII") from exc
    else:
        if not text_is_ascii(data):
            raise ParseError("input is not ASCII")
        text = data

    cur = _Cursor(text)
    cur.skip_sp()
    result = _parse_dict_members(cur)
    cur.skip_sp()
    if not cur.empty():
        raise ParseError(f"trailing characters at offset {cur.i}")
    return result


def text_is_ascii(s: str) -> bool:
    return all(ord(c) < 128 for c in s)


def _parse_dict_members(cur: _Cursor) -> dict[str, Member]:
    out: dict[str, Member] = {}
    while not cur.empty():
        key = _parse_key(cur)
        if cur.peek() == "=":
            cur.take()
            member = _parse_item_or_list(cur)
        else:
            member = Item(True, _parse_parameters(cur))
        out[key] = member
        cur.skip_ows()
        if cur.empty():
            return out
        if cur.take() != ",":
            raise ParseError(f"expected ',' at offset {cur.i - 1}")
        cur.skip_ows()
        if cur.empty():
            raise ParseError("trailing comma")
    return out


def _parse_key(cur: _Cursor) -> str:
    if cur.peek() not in _KEY_FIRST or not cur.peek():
        raise ParseError(f"invalid key start at offset {cur.i}")
    start = cur.i
    cur.i += 1
    while cur.peek() and cur.peek() in _KEY_REST:
        cur.i += 1
    return cur.s[start : cur.i]


def _parse_item_or_list(cur: _Cursor) -> Member:
    if cur.peek() == "(":
        return _parse_inner_list(cur)
    return _parse_item(cur)


def _parse_inner_list(cur: _Cursor) -> InnerList:
    cur.take()  # "("
    items: list[Item] = []
    while not cur.empty():
        cur.skip_sp()
        if cur.peek() == ")":
            cur.take()
            return InnerList(items, _parse_parameters(cur))
        items.append(_parse_item(cur))
        if cur.peek() not in (" ", ")") or not cur.peek():
            raise ParseError(f"invalid inner list at offset {cur.i}")
    raise ParseError("unterminated inner list")


def _parse_item(cur: _Cursor) -> Item:
    value = _parse_bare_item(cur)
    return Item(value, _parse_parameters(cur))


def _parse_parameters(cur: _Cursor) -> dict[str, BareItem]:
    params: dict[str, BareItem] = {}
    while not cur.empty():
        if cur.peek() != ";":
            break
        cur.take()
        cur.skip_sp()
        key = _parse_key(cur)
        value: BareItem = True
        if cur.peek() == "=":
            cur.take()
            value = _parse_bare_item(cur)
        params[key] = value
    return params


def _parse_bare_item(cur: _Cursor) -> BareItem:
    c = cur.peek()
    if not c:
        raise ParseError("expected a value")
    if c == "-" or c in _DIGIT:
        return _parse_number(cur)
    if c == '"':
        return _parse_string(cur)
    if c in _ALPHA or c == "*":
        return _parse_token(cur)
    if c == ":":
        return _parse_bytes(cur)
    if c == "?":
        return _parse_boolean(cur)
    if c == "@":
        cur.take()
        n = _parse_number(cur)
        if not isinstance(n, int):
            raise ParseError("date must be an integer")
        return Date(n)
    if c == "%":
        return _parse_display_string(cur)
    raise ParseError(f"unrecognised value at offset {cur.i}")


def _parse_number(cur: _Cursor) -> int | float:
    sign = 1
    if cur.peek() == "-":
        cur.take()
        sign = -1
    if not cur.peek() or cur.peek() not in _DIGIT:
        raise ParseError("number must start with a digit")
    num = ""
    is_decimal = False
    while cur.peek():
        c = cur.peek()
        if c in _DIGIT:
            num += c
            cur.take()
        elif not is_decimal and c == ".":
            if len(num) > 12:
                raise ParseError("decimal integer part too long")
            num += c
            is_decimal = True
            cur.take()
        else:
            break
        if not is_decimal and len(num) > 15:
            raise ParseError("integer too long")
        if is_decimal and len(num) > 16:
            raise ParseError("decimal too long")
    if not is_decimal:
        return sign * int(num)
    if num.endswith("."):
        raise ParseError("decimal ends with '.'")
    if len(num.split(".", 1)[1]) > 3:
        raise ParseError("too many fractional digits")
    return sign * float(num)


def _parse_string(cur: _Cursor) -> str:
    cur.take()  # opening quote
    out = []
    while not cur.empty():
        c = cur.take()
        if c == "\\":
            if cur.empty():
                raise ParseError("dangling escape")
            nxt = cur.take()
            if nxt not in ('"', "\\"):
                raise ParseError("invalid escape")
            out.append(nxt)
        elif c == '"':
            return "".join(out)
        elif ord(c) < 0x20 or ord(c) > 0x7E:
            raise ParseError("invalid character in string")
        else:
            out.append(c)
    raise ParseError("unterminated string")


def _parse_token(cur: _Cursor) -> Token:
    start = cur.i
    cur.take()
    while cur.peek() and cur.peek() in _TOKEN_REST:
        cur.i += 1
    return Token(cur.s[start : cur.i])


def _parse_bytes(cur: _Cursor) -> bytes:
    cur.take()  # ":"
    start = cur.i
    while cur.peek() and cur.peek() != ":":
        if cur.peek() not in _BASE64:
            raise ParseError("invalid base64 character")
        cur.i += 1
    if cur.empty():
        raise ParseError("unterminated byte sequence")
    b64 = cur.s[start : cur.i]
    cur.take()  # closing ":"
    try:
        return base64.b64decode(b64 + "=" * (-len(b64) % 4), validate=True)
    except (ValueError, base64.binascii.Error) as exc:
        raise ParseError("invalid base64") from exc


def _parse_boolean(cur: _Cursor) -> bool:
    cur.take()  # "?"
    c = cur.take() if not cur.empty() else ""
    if c == "1":
        return True
    if c == "0":
        return False
    raise ParseError("invalid boolean")


def _parse_display_string(cur: _Cursor) -> DisplayString:
    cur.take()  # "%"
    if cur.peek() != '"':
        raise ParseError("display string must start with %\"")
    cur.take()
    buf = bytearray()
    while not cur.empty():
        c = cur.take()
        o = ord(c)
        if o <= 0x1F or o >= 0x7F:
            raise ParseError("invalid character in display string")
        if c == "%":
            hx = cur.s[cur.i : cur.i + 2]
            if len(hx) != 2 or any(h not in _LCHEX for h in hx):
                raise ParseError("invalid percent-encoding")
            cur.i += 2
            buf.append(int(hx, 16))
        elif c == '"':
            try:
                return DisplayString(buf.decode("utf-8"))
            except UnicodeDecodeError as exc:
                raise ParseError("display string is not valid UTF-8") from exc
        else:
            buf.append(o)
    raise ParseError("unterminated display string")
