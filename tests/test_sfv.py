import pytest

from usageledger import sfv


def parse(s):
    return sfv.parse_dictionary(s)


def test_tokens_and_booleans():
    d = parse("train-ai=n, search=y, flag")
    assert isinstance(d["train-ai"].value, sfv.Token) and d["train-ai"].value == "n"
    assert d["search"].value == "y"
    assert d["flag"].value is True


def test_bytes_input_and_whitespace():
    d = parse(b"  a=1,\tb=2  ")
    assert d["a"].value == 1 and d["b"].value == 2


def test_last_duplicate_wins():
    d = parse("a=1, a=2")
    assert d["a"].value == 2


def test_parameters():
    d = parse('a=x;p=1;q="s";r, b=?0')
    assert d["a"].params == {"p": 1, "q": "s", "r": True}
    assert d["b"].value is False


def test_inner_list():
    d = parse('a=(1 "two" three);p')
    assert isinstance(d["a"], sfv.InnerList)
    assert [i.value for i in d["a"].items] == [1, "two", "three"]
    assert d["a"].params == {"p": True}


def test_numbers():
    d = parse("a=-12, b=3.25, c=0.5")
    assert d["a"].value == -12 and d["b"].value == 3.25 and d["c"].value == 0.5


def test_bytes_date_displaystring():
    d = parse('a=:aGVsbG8=:, b=@1700000000, c=%"caf%c3%a9"')
    assert d["a"].value == b"hello"
    assert d["b"].value == sfv.Date(1700000000)
    assert d["c"].value == "café"


def test_string_escapes():
    d = parse(r'a="say \"hi\" \\ ok"')
    assert d["a"].value == 'say "hi" \\ ok'


@pytest.mark.parametrize(
    "bad",
    [
        "Train-AI=n",  # uppercase key
        "a=1,",  # trailing comma
        "a=1 b=2",  # missing comma
        "a=1,,b=2",
        'a="unterminated',
        r'a="bad \x escape"',
        "a=1.",  # decimal ending in "."
        "a=1.2345",  # too many fractional digits
        "a=1234567890123456",  # integer too long
        "a=?2",
        "a=@1.5",
        "a=:not base64!:",
        'a=%"UPPER%C3"',
        "a=(1 2",
        "a=;",
        "a;",  # trailing semicolon with no parameter key
        "train-ai=n é",
        "=n",
    ],
)
def test_rejects_invalid(bad):
    with pytest.raises(sfv.ParseError):
        parse(bad)


def test_empty_is_empty_dict():
    assert parse("") == {}
    assert parse("   ") == {}
