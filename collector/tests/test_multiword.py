"""Tests for the multiword candidate generator + exact estimate."""

import pytest

from collector import multiword
from collector.multiword import (
    count_candidates,
    estimate,
    iter_candidates,
    load_words,
    _separator_patterns,
    MAX_GENERATED_CHARS,
)


# --- separator patterns ---------------------------------------------------

def test_separator_patterns():
    assert _separator_patterns(1, True, True) == [()]
    assert sorted(_separator_patterns(2, True, True)) == [("",), ("-",)]
    # 3 words -> 4 gap patterns over the 2 gaps.
    assert sorted(_separator_patterns(3, True, True)) == sorted(
        [("", ""), ("", "-"), ("-", ""), ("-", "-")])
    assert _separator_patterns(2, True, False) == [("",)]       # concat only
    assert _separator_patterns(2, False, True) == [("-",)]      # hyphen only


def test_two_words_patterns_on_fixed_tuple():
    # A single-word list fixes the tuple to (x, x): concat + hyphen.
    assert sorted(iter_candidates(["x"], 2)) == ["x-x", "xx"]


def test_three_words_pattern_set():
    # Single-word list -> one tuple (a,a,a): 2**(3-1)=4 separator patterns.
    assert sorted(iter_candidates(["a"], 3)) == sorted(["aaa", "a-aa", "aa-a", "a-a-a"])


def test_full_cartesian_product():
    # Every ordered tuple (repeats allowed), concat only.
    assert sorted(iter_candidates(["a", "b"], 2, include_hyphen=False)) == \
        sorted(["aa", "ab", "ba", "bb"])


def test_hyphen_only_and_concat_only():
    assert sorted(iter_candidates(["a"], 2, include_concat=False)) == ["a-a"]
    assert sorted(iter_candidates(["a"], 2, include_hyphen=False)) == ["aa"]


def test_single_word():
    assert sorted(iter_candidates(["cat", "dog"], 1)) == ["cat", "dog"]


# --- length cap -----------------------------------------------------------

def test_length_cap_skips_overlong():
    words = ["aaaaaaaaaaaaaaaa"]  # 16 chars
    # (w,w): concat = 32 > 30 cap; hyphen = 33 > 30. Both dropped.
    assert list(iter_candidates(words, 2, max_generated_chars=MAX_GENERATED_CHARS)) == []
    # Cap that admits the concat but not the hyphenated form of the fixed tuple.
    words = ["abcdef"]  # 6 chars; (w,w) -> concat 12, hyphen 13
    got = list(iter_candidates(words, 2, max_generated_chars=12))
    assert got == ["abcdefabcdef"]          # 12 ok, hyphenated 13 dropped


# --- exact count matches brute enumeration --------------------------------

@pytest.mark.parametrize("n", [1, 2, 3])
@pytest.mark.parametrize("cap", [30, 12, 8, 5])
def test_count_matches_enumeration(n, cap):
    words = ["a", "bb", "ccc", "dddd", "ee"]  # varied lengths incl. duplicates-by-len
    exp = sum(1 for _ in iter_candidates(words, n, max_generated_chars=cap))
    got = count_candidates(words, n, max_generated_chars=cap)["under_cap"]
    assert got == exp


@pytest.mark.parametrize("n", [2, 3])
def test_count_matches_enumeration_hyphen_only(n):
    words = ["aa", "bbb", "c"]
    for ic, ih in [(True, False), (False, True)]:
        exp = sum(1 for _ in iter_candidates(
            words, n, include_concat=ic, include_hyphen=ih, max_generated_chars=10))
        got = count_candidates(
            words, n, include_concat=ic, include_hyphen=ih,
            max_generated_chars=10)["under_cap"]
        assert got == exp, (n, ic, ih)


def test_raw_and_patterns():
    c = count_candidates(["a", "b", "c", "d"], 2)  # W=4, patterns=2
    assert c["words"] == 4
    assert c["patterns"] == 2
    assert c["raw"] == 4 ** 2 * 2


def test_empty_wordlist():
    c = count_candidates([], 2)
    assert c["under_cap"] == 0 and c["raw"] == 0
    assert list(iter_candidates([], 2)) == []


# --- estimate / ETA -------------------------------------------------------

def test_estimate_eta():
    e = estimate(["a", "b", "c"], 2, hashrate=1000.0)
    assert e["eta_seconds"] == pytest.approx(e["under_cap"] / 1000.0)


# --- bundled wordlist -----------------------------------------------------

def test_bundled_wordlist_loads_and_is_clean():
    words = load_words()
    assert len(words) > 40_000
    # frequency order: very common words appear near the top
    assert "the" in words[:50]
    # tiers are prefixes
    top10k = load_words(limit=10_000)
    assert top10k == words[:10_000]
    # all lowercase a-z, length >= 2
    for w in words[:2000]:
        assert w.isalpha() and w.islower() and len(w) >= 2


def test_bundled_two_word_estimate_is_feasible():
    # 2 words on the full list: billions after separators, but a quick GPU sweep.
    words = load_words()
    e = estimate(words, 2)
    assert e["under_cap"] > 1e8          # real work
    assert e["eta_seconds"] < 60         # but a short sweep at ~2.2G/s


def test_bundled_four_word_is_flagged_huge():
    words = load_words(limit=50_000)
    e = estimate(words, 4)
    # 4 words on 50K is the infeasible corner the UI must warn about.
    assert e["under_cap"] > 1e16
