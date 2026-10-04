"""Tests for the default public channel auto-decode."""

import hashlib
import base64
import struct
import time

import pytest

from collector.crypto import (
    DEFAULT_PUBLIC_PSK_B64,
    DEFAULT_PUBLIC_CHANNEL_NAME,
    Channel,
    default_public_channel,
    encrypt_then_mac,
    mac_then_decrypt,
    try_decode_group_message,
    PAYLOAD_TYPE_GRP_TXT,
)


class TestDefaultPublicChannel:
    def test_returns_channel_object(self):
        ch = default_public_channel()
        assert isinstance(ch, Channel)

    def test_channel_name(self):
        ch = default_public_channel()
        assert ch.name == "Public"

    def test_psk_matches_known_hex(self):
        """The base64 PSK should decode to the known MeshCore public key."""
        raw = base64.b64decode(DEFAULT_PUBLIC_PSK_B64)
        assert raw.hex() == "8b3387e9c5cdea6ac9e5edbaa115cd72"

    def test_secret_is_32_bytes(self):
        ch = default_public_channel()
        assert len(ch.secret) == 32

    def test_secret_starts_with_psk(self):
        ch = default_public_channel()
        raw = base64.b64decode(DEFAULT_PUBLIC_PSK_B64)
        assert ch.secret[:16] == raw

    def test_secret_zero_padded(self):
        ch = default_public_channel()
        assert ch.secret[16:] == b"\x00" * 16

    def test_hash_matches_sha256_of_psk(self):
        ch = default_public_channel()
        raw = base64.b64decode(DEFAULT_PUBLIC_PSK_B64)
        expected_hash = hashlib.sha256(raw).digest()[0]
        assert ch.hash == expected_hash

    def test_from_psk_equivalent(self):
        """default_public_channel() should match Channel.from_psk()."""
        ch1 = default_public_channel()
        ch2 = Channel.from_psk("Public", DEFAULT_PUBLIC_PSK_B64)
        assert ch1.hash == ch2.hash
        assert ch1.secret == ch2.secret

    def test_roundtrip_encrypt_decrypt(self):
        """A message encrypted with the public PSK should decrypt successfully."""
        ch = default_public_channel()
        # Build a GRP_TXT plaintext: [timestamp(4)][flags(1)][sender: text\0]
        ts = struct.pack("<I", 1700000000)
        flags = b"\x00"
        text = b"alice: hello world\x00"
        plaintext = ts + flags + text
        mac_and_data = encrypt_then_mac(ch.secret, plaintext)
        result = mac_then_decrypt(ch.secret, mac_and_data)
        assert result is not None
        assert result[:len(plaintext)] == plaintext

    def test_decode_group_message_with_public_channel(self):
        """A synthetic GRP_TXT packet should decode using the public channel."""
        ch = default_public_channel()
        # Build plaintext
        ts = struct.pack("<I", 1700000000)
        flags = b"\x00"
        text = b"bob: testing 123\x00"
        plaintext = ts + flags + text
        mac_and_data = encrypt_then_mac(ch.secret, plaintext)

        # Build raw packet: header(1) + path_len(1) + channel_hash(1) + mac_and_data
        header = (PAYLOAD_TYPE_GRP_TXT << 2) | 0x01  # FLOOD route
        raw_packet = bytes([header, 0, ch.hash]) + mac_and_data

        msg = try_decode_group_message(raw_packet, [ch], time.time())
        assert msg is not None
        assert msg.sender == "bob"
        assert msg.text == "testing 123"
        assert msg.channel_name == "Public"

    def test_wrong_psk_fails_decrypt(self):
        """A different PSK should not decrypt messages from the public channel."""
        ch = default_public_channel()
        wrong = Channel.from_hashtag("#wrong")

        ts = struct.pack("<I", 1700000000)
        plaintext = ts + b"\x00" + b"alice: hi\x00"
        mac_and_data = encrypt_then_mac(ch.secret, plaintext)

        result = mac_then_decrypt(wrong.secret, mac_and_data)
        assert result is None
