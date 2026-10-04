"""Tests for hashtag channel key derivation and config loading."""

import hashlib
import struct
import pytest

from collector.crypto import (
    Channel,
    CIPHER_KEY_SIZE,
    PUB_KEY_SIZE,
    encrypt_then_mac,
    try_decode_group_message,
    PAYLOAD_TYPE_GRP_TXT,
    ROUTE_TYPE_FLOOD,
)
from collector.config import load_channels


# --- Channel.from_hashtag ---

class TestChannelFromHashtag:
    def test_basic_derivation(self):
        ch = Channel.from_hashtag("#test")
        assert ch.name == "#test"
        assert len(ch.secret) == PUB_KEY_SIZE
        assert ch.secret[CIPHER_KEY_SIZE:] == b"\x00" * CIPHER_KEY_SIZE

    def test_name_normalization(self):
        """Name without '#' gets it prepended."""
        ch = Channel.from_hashtag("catlovers")
        assert ch.name == "#catlovers"

    def test_already_has_hash(self):
        """Name with '#' stays as-is."""
        ch = Channel.from_hashtag("#meshcore")
        assert ch.name == "#meshcore"

    def test_psk_is_sha256_of_name(self):
        """PSK = SHA-256('#name')[:16]."""
        ch = Channel.from_hashtag("#test")
        expected_psk = hashlib.sha256(b"#test").digest()[:CIPHER_KEY_SIZE]
        assert ch.secret[:CIPHER_KEY_SIZE] == expected_psk

    def test_hash_is_sha256_of_psk(self):
        """Channel hash = SHA-256(psk)[:1]."""
        ch = Channel.from_hashtag("#test")
        psk = hashlib.sha256(b"#test").digest()[:CIPHER_KEY_SIZE]
        expected_hash = hashlib.sha256(psk).digest()[0]
        assert ch.hash == expected_hash

    def test_known_vector(self):
        """Verify against pre-computed value from plan."""
        ch = Channel.from_hashtag("#test")
        psk_hex = ch.secret[:CIPHER_KEY_SIZE].hex()
        assert psk_hex == "9cd8fcf22a47333b591d96a2b848b73f"

    def test_different_names_different_keys(self):
        ch1 = Channel.from_hashtag("#alpha")
        ch2 = Channel.from_hashtag("#beta")
        assert ch1.secret != ch2.secret
        assert ch1.hash != ch2.hash or ch1.secret[:CIPHER_KEY_SIZE] != ch2.secret[:CIPHER_KEY_SIZE]

    def test_round_trip_encryption(self):
        """Message encrypted with hashtag key can be decrypted."""
        ch = Channel.from_hashtag("#test")
        plaintext = struct.pack("<I", 1700000000) + b"\x00" + b"Alice: hello!\x00"
        mac_and_data = encrypt_then_mac(ch.secret, plaintext)
        # Build a full packet
        header = (PAYLOAD_TYPE_GRP_TXT << 2) | ROUTE_TYPE_FLOOD
        raw = bytes([header, 0x00]) + bytes([ch.hash]) + mac_and_data
        msg = try_decode_group_message(raw, [ch], 1.0)
        assert msg is not None
        assert msg.sender == "Alice"
        assert msg.text == "hello!"
        assert msg.channel_name == "#test"


# --- Config: hashtag channel loading ---

class TestConfigHashtagChannels:
    def test_hashtag_entry_loads(self):
        config = {"channels": [{"name": "#catlovers"}]}
        channels = load_channels(config)
        assert len(channels) == 1
        assert channels[0].name == "#catlovers"

    def test_hashtag_without_hash_prefix(self):
        """Name not starting with '#' and no psk is skipped."""
        config = {"channels": [{"name": "nohash"}]}
        channels = load_channels(config)
        assert len(channels) == 0

    def test_hashtag_with_explicit_psk_uses_psk(self):
        """If a #name entry has an explicit psk, use from_psk instead."""
        import base64
        psk = base64.b64encode(b"\x01" * 16).decode()
        config = {"channels": [{"name": "#override", "psk": psk}]}
        channels = load_channels(config)
        assert len(channels) == 1
        # Should use from_psk, not from_hashtag
        ch = channels[0]
        assert ch.name == "#override"
        assert ch.secret[:16] == b"\x01" * 16

    def test_mixed_hashtag_and_psk(self):
        """Both hashtag and PSK entries work together."""
        import base64
        psk = base64.b64encode(b"\x02" * 16).decode()
        config = {"channels": [
            {"name": "#public"},
            {"name": "Private", "psk": psk},
        ]}
        channels = load_channels(config)
        assert len(channels) == 2
        assert channels[0].name == "#public"
        assert channels[1].name == "Private"

    def test_empty_hashtag_name_skipped(self):
        config = {"channels": [{"name": "#"}]}
        channels = load_channels(config)
        # "#" is a valid (if weird) hashtag name
        assert len(channels) == 1
        assert channels[0].name == "#"
