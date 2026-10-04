"""Tests for the GPU brute-force channel cracker (collector.brute_force_gpu).

GPU tests skip automatically when CuPy / a CUDA device is unavailable, so the
suite stays green on machines without an NVIDIA GPU.
"""

import pytest

from collector import brute_force_gpu as g
from collector.brute_force import brute_force_channel  # CPU reference
from collector.crypto import Channel, encrypt_then_mac

gpu_only = pytest.mark.skipif(
    not g.is_available(), reason="no CUDA GPU / CuPy available"
)


def _make_packet(name, text="alice: hello mesh"):
    """Build a realistic GRP_TXT mac_and_data blob for a hashtag channel."""
    ch = Channel.from_hashtag(name)
    plaintext = (1234567890).to_bytes(4, "little") + b"\x00" + (text + "\x00").encode()
    return ch, encrypt_then_mac(ch.secret, plaintext)


@gpu_only
def test_gpu_cracks_known_channel():
    ch, mac_and_data = _make_packet("test")
    result = g.brute_force_channel_gpu(
        ch.hash, mac_and_data, charset="abcdefghijklmnopqrstuvwxyz", max_length=4
    )
    assert result == "#test"


@gpu_only
def test_gpu_matches_cpu_result():
    ch, mac_and_data = _make_packet("qrs")
    gpu = g.brute_force_channel_gpu(ch.hash, mac_and_data, charset="qrs", max_length=3)
    cpu = brute_force_channel(ch.hash, mac_and_data, charset="qrs", max_length=3)
    assert gpu == cpu == "#qrs"


@gpu_only
def test_gpu_exhausts_without_false_positive():
    # Target channel is NOT reachable within the searched charset/length, so a
    # correct cracker returns None rather than a bogus hit.
    ch, mac_and_data = _make_packet("zzzz")  # len 4
    result = g.brute_force_channel_gpu(
        ch.hash, mac_and_data, charset="ab", max_length=3
    )
    assert result is None


@gpu_only
def test_gpu_no_false_positive_for_out_of_space_name():
    # Regression: "#wardriving" (10 chars) is outside a 6-char a-z0-9 search.
    # A 2-byte MAC collision once returned a bogus "#0w0cb7"; strict GRP_TXT
    # validation must now exhaust to None.
    ch, mac_and_data = _make_packet("wardriving", text="bob: node up on the ridge")
    result = g.brute_force_channel_gpu(
        ch.hash, mac_and_data,
        charset="abcdefghijklmnopqrstuvwxyz0123456789", max_length=6,
    )
    assert result is None


def test_rejects_malformed_ciphertext():
    # Not a multiple of the AES block size -> None (no GPU needed).
    if not g.is_available():
        pytest.skip("no CUDA GPU / CuPy available")
    assert g.brute_force_channel_gpu(0, b"\x00\x00" + b"\x01" * 15) is None
