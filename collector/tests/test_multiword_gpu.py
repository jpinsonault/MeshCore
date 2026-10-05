"""Tests for the multiword GPU sweep (collector.brute_force_gpu.brute_force_words_batch_gpu).

GPU tests skip automatically when CuPy / a CUDA device is unavailable.
"""

import pytest

from collector import brute_force_gpu as g
from collector.crypto import Channel, encrypt_then_mac

gpu_only = pytest.mark.skipif(
    not g.is_available(), reason="no CUDA GPU / CuPy available"
)


def _packet(name, text="alice: hello mesh everyone"):
    ch = Channel.from_hashtag(name)
    pt = (1234567890).to_bytes(4, "little") + b"\x00" + (text + "\x00").encode()
    return ch, encrypt_then_mac(ch.secret, pt)


WORDS = ["north", "sound", "cats", "cute", "dog", "fire", "camp", "mesh",
         "in", "dresses", "the", "red", "blue", "a", "of", "and"]


@gpu_only
def test_two_word_hyphen():
    ch, mad = _packet("#north-sound")
    solved = g.brute_force_words_batch_gpu(
        {ch.hash: {"mac_and_data": mad, "extras": []}}, WORDS, 2)
    assert solved.get(ch.hash) == "#north-sound"


@gpu_only
def test_two_word_concat():
    ch, mad = _packet("#campfire")
    solved = g.brute_force_words_batch_gpu(
        {ch.hash: {"mac_and_data": mad, "extras": []}}, WORDS, 2)
    assert solved.get(ch.hash) == "#campfire"


@gpu_only
def test_four_word_concat():
    ch, mad = _packet("#catsincutedresses")  # cats+in+cute+dresses
    solved = g.brute_force_words_batch_gpu(
        {ch.hash: {"mac_and_data": mad, "extras": []}}, WORDS, 4)
    assert solved.get(ch.hash) == "#catsincutedresses"


@gpu_only
def test_three_word_mixed_separators():
    ch, mad = _packet("#red-dog-fire")
    solved = g.brute_force_words_batch_gpu(
        {ch.hash: {"mac_and_data": mad, "extras": []}}, WORDS, 3)
    assert solved.get(ch.hash) == "#red-dog-fire"


@gpu_only
def test_hyphen_only_mode_skips_concat():
    # '#campfire' (concat) must NOT be found when only hyphenated patterns run.
    ch, mad = _packet("#campfire")
    solved = g.brute_force_words_batch_gpu(
        {ch.hash: {"mac_and_data": mad, "extras": []}}, WORDS, 2,
        include_concat=False)
    assert ch.hash not in solved
    # but its hyphenated sibling '#camp-fire' is found in hyphen-only mode.
    ch2, mad2 = _packet("#camp-fire")
    solved2 = g.brute_force_words_batch_gpu(
        {ch2.hash: {"mac_and_data": mad2, "extras": []}}, WORDS, 2,
        include_concat=False)
    assert solved2.get(ch2.hash) == "#camp-fire"


@gpu_only
def test_multi_target_sweep():
    a, mad_a = _packet("#north-sound")
    b, mad_b = _packet("#campfire")
    targets = {
        a.hash: {"mac_and_data": mad_a, "extras": []},
        b.hash: {"mac_and_data": mad_b, "extras": []},
    }
    solved = g.brute_force_words_batch_gpu(targets, WORDS, 2)
    assert solved.get(a.hash) == "#north-sound"
    assert solved.get(b.hash) == "#campfire"


@gpu_only
def test_not_in_wordlist_returns_empty():
    # Pieces absent from WORDS -> no hit, no false positive.
    ch, mad = _packet("#zzqq-absent")
    solved = g.brute_force_words_batch_gpu(
        {ch.hash: {"mac_and_data": mad, "extras": []}}, WORDS, 2)
    assert solved == {}


@gpu_only
def test_over_length_name_pruned():
    # Two long words whose concat exceeds the 30-char generated cap: even though
    # both are in the list, the name can't be stored on-device so it's skipped.
    longwords = ["abcdefghijklmnop", "qrstuvwxyzabcdef"]  # 16 + 16
    ch, mad = _packet("#" + "abcdefghijklmnop" + "qrstuvwxyzabcdef")
    solved = g.brute_force_words_batch_gpu(
        {ch.hash: {"mac_and_data": mad, "extras": []}}, longwords, 2)
    assert solved == {}


@gpu_only
def test_should_stop_aborts():
    ch, mad = _packet("#north-sound")
    solved = g.brute_force_words_batch_gpu(
        {ch.hash: {"mac_and_data": mad, "extras": []}}, WORDS, 2,
        should_stop=lambda: True)
    assert solved == {}
