"""Examples taken from draft-ietf-aipref-vocab-08, Sections 5.1 and 6."""

from usageledger import Preference, combine, parse_statement
from usageledger.vocab import Category, Statement, Vocabulary

A, D, U = Preference.ALLOW, Preference.DISALLOW, Preference.UNKNOWN


def prefs(value):
    return parse_statement(value).prefs


def test_section_6_example():
    assert prefs("train-ai=y, search=n") == {"train-ai": A, "ai-use": U, "search": D}


def test_duplicate_keys_with_non_token_values_are_unknown():
    # Section 6.5.1: last value wins, and a boolean / string is not a token.
    s = parse_statement('train-ai=y, train-ai, search=n, search="n"')
    assert s.parse_ok
    assert s.prefs == {"train-ai": U, "ai-use": U, "search": U}


def test_parameters_are_ignored_and_last_value_wins():
    # Section 6.5.2
    assert prefs("train-ai;allow=n, train-ai=y")["train-ai"] is A
    assert prefs('train-ai;has;parameters="?"')["train-ai"] is U


def test_uppercase_is_a_parse_failure():
    s = parse_statement("Train-AI=n")
    assert not s.parse_ok
    assert s.is_empty
    assert s.raw == "Train-AI=n"


def test_unknown_labels_and_values():
    s = parse_statement("bots=n, train-ai=maybe, ai-use=n")
    assert s.prefs == {"train-ai": U, "ai-use": D, "search": U}
    assert s.ignored_labels == ["bots"]


def test_none_and_empty():
    assert parse_statement(None).is_empty
    assert parse_statement("").is_empty and parse_statement("").parse_ok


def test_combine_most_restrictive():
    a = parse_statement("train-ai=y, search=y")
    b = parse_statement("train-ai=n")
    c = parse_statement("ai-use=y")
    out = combine([a, b, c])
    assert out == {"train-ai": D, "ai-use": A, "search": A}
    assert combine([]) == {"train-ai": U, "ai-use": U, "search": U}


def test_custom_vocabulary():
    v = Vocabulary("my-ext-1", (Category("train-ai", "AI Training", ""), Category("x-foo", "Foo", "")))
    s = parse_statement("x-foo=n, search=n", vocab=v)
    assert s.prefs == {"train-ai": U, "x-foo": D}
    assert s.ignored_labels == ["search"]


def test_str():
    assert str(parse_statement("train-ai=n, search=y")) == "train-ai=n, search=y"
    assert str(Statement({"train-ai": U})) == "(no known preferences)"
