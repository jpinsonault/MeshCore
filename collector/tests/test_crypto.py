"""Tests for the channel decryption module."""

import base64
import hashlib
import struct
import pytest

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from collector.crypto import (
    Channel,
    GroupMessage,
    extract_group_payload,
    mac_then_decrypt,
    encrypt_then_mac,
    try_decode_group_message,
    PAYLOAD_TYPE_GRP_TXT,
    PAYLOAD_TYPE_GRP_DATA,
    ROUTE_TYPE_FLOOD,
    ROUTE_TYPE_DIRECT,
    ROUTE_TYPE_TRANSPORT_FLOOD,
    ROUTE_TYPE_TRANSPORT_DIRECT,
    CIPHER_BLOCK_SIZE,
    CIPHER_MAC_SIZE,
    PATH_HASH_SIZE,
)


# --- Helpers ---

# Well-known test PSK (16 bytes, base64-encoded)
TEST_PSK_16 = base64.b64encode(b"\x01" * 16).decode()
# Well-known test PSK (32 bytes, base64-encoded)
TEST_PSK_32 = base64.b64encode(b"\x02" * 32).decode()


def _make_channel(name="Test", psk=None):
    """Create a test channel."""
    return Channel.from_psk(name, psk or TEST_PSK_16)


def _make_grp_txt_packet(channel, text, route_type=ROUTE_TYPE_FLOOD, path=b"", timestamp=1700000000):
    """Build a raw GRP_TXT packet matching firmware wire format."""
    # Build plaintext: [timestamp(4 LE)][flags(1)][text\0]
    plaintext = struct.pack("<I", timestamp) + b"\x00" + text.encode("utf-8") + b"\x00"

    # Encrypt
    mac_and_data = encrypt_then_mac(channel.secret, plaintext)

    # Build payload: [channel_hash(1)][mac_and_data]
    payload = bytes([channel.hash]) + mac_and_data

    # Build packet: [header(1)][transport_codes?][path_len(1)][path][payload]
    header = (PAYLOAD_TYPE_GRP_TXT << 2) | route_type
    parts = bytes([header])

    if route_type in (0x00, 0x03):  # transport codes
        parts += b"\x00\x00\x00\x00"

    parts += bytes([len(path)]) + path + payload
    return parts


# --- Channel.from_psk ---

class TestChannelFromPsk:
    def test_16_byte_psk(self):
        ch = Channel.from_psk("Public", TEST_PSK_16)
        assert ch.name == "Public"
        assert len(ch.secret) == 32
        assert ch.secret[16:] == b"\x00" * 16  # zero-padded
        raw = base64.b64decode(TEST_PSK_16)
        expected_hash = hashlib.sha256(raw).digest()[0]
        assert ch.hash == expected_hash

    def test_32_byte_psk(self):
        ch = Channel.from_psk("Private", TEST_PSK_32)
        assert ch.name == "Private"
        assert len(ch.secret) == 32
        assert ch.secret == b"\x02" * 32
        raw = base64.b64decode(TEST_PSK_32)
        expected_hash = hashlib.sha256(raw).digest()[0]
        assert ch.hash == expected_hash

    def test_invalid_psk_length(self):
        bad_psk = base64.b64encode(b"\x03" * 8).decode()
        with pytest.raises(ValueError, match="16 or 32"):
            Channel.from_psk("Bad", bad_psk)

    def test_invalid_base64(self):
        with pytest.raises(Exception):
            Channel.from_psk("Bad", "not-valid-base64!!!")


# --- extract_group_payload ---

class TestExtractGroupPayload:
    def test_flood_packet(self):
        ch = _make_channel()
        raw = _make_grp_txt_packet(ch, "hello", route_type=ROUTE_TYPE_FLOOD)
        result = extract_group_payload(raw)
        assert result is not None
        assert result["channel_hash"] == ch.hash
        assert result["payload_type"] == PAYLOAD_TYPE_GRP_TXT
        assert len(result["mac_and_data"]) > CIPHER_MAC_SIZE

    def test_transport_flood_packet(self):
        ch = _make_channel()
        raw = _make_grp_txt_packet(ch, "hello", route_type=ROUTE_TYPE_TRANSPORT_FLOOD)
        result = extract_group_payload(raw)
        assert result is not None
        assert result["channel_hash"] == ch.hash

    def test_with_path(self):
        ch = _make_channel()
        raw = _make_grp_txt_packet(ch, "hello", path=b"\xaa\xbb\xcc")
        result = extract_group_payload(raw)
        assert result is not None
        assert result["channel_hash"] == ch.hash

    def test_non_grp_returns_none(self):
        # Header with payload_type=TXT_MSG (0x02)
        header = (0x02 << 2) | ROUTE_TYPE_FLOOD
        raw = bytes([header, 0x00]) + b"\x00" * 20
        assert extract_group_payload(raw) is None

    def test_too_short_returns_none(self):
        assert extract_group_payload(b"\x14") is None
        assert extract_group_payload(b"") is None
        assert extract_group_payload(b"\x14\x00") is None

    def test_grp_data_extracted(self):
        # Build a GRP_DATA header
        header = (PAYLOAD_TYPE_GRP_DATA << 2) | ROUTE_TYPE_FLOOD
        raw = bytes([header, 0x00, 0xAA, 0xBB]) + b"\x00" * 16
        result = extract_group_payload(raw)
        assert result is not None
        assert result["payload_type"] == PAYLOAD_TYPE_GRP_DATA


# --- mac_then_decrypt ---

class TestMacThenDecrypt:
    def test_round_trip(self):
        secret = b"\x01" * 16 + b"\x00" * 16
        plaintext = b"Hello, world!\x00\x00\x00"  # pad to 16 bytes
        encrypted = encrypt_then_mac(secret, plaintext)
        decrypted = mac_then_decrypt(secret, encrypted)
        assert decrypted is not None
        assert decrypted[:len(plaintext)] == plaintext

    def test_round_trip_multi_block(self):
        secret = b"\x05" * 32
        plaintext = b"A" * 33  # > 2 blocks
        encrypted = encrypt_then_mac(secret, plaintext)
        decrypted = mac_then_decrypt(secret, encrypted)
        assert decrypted is not None
        # Plaintext padded to 48 bytes (3 blocks)
        assert decrypted[:33] == plaintext

    def test_wrong_mac(self):
        secret = b"\x01" * 16 + b"\x00" * 16
        encrypted = encrypt_then_mac(secret, b"test data here!!")
        # Corrupt the MAC
        corrupted = bytes([encrypted[0] ^ 0xFF]) + encrypted[1:]
        assert mac_then_decrypt(secret, corrupted) is None

    def test_wrong_key(self):
        secret1 = b"\x01" * 32
        secret2 = b"\x02" * 32
        encrypted = encrypt_then_mac(secret1, b"secret message!!")
        assert mac_then_decrypt(secret2, encrypted) is None

    def test_too_short(self):
        secret = b"\x01" * 32
        assert mac_then_decrypt(secret, b"\x00\x01") is None
        assert mac_then_decrypt(secret, b"\x00") is None
        assert mac_then_decrypt(secret, b"") is None

    def test_non_block_aligned_ciphertext(self):
        secret = b"\x01" * 32
        # MAC (2 bytes) + non-aligned ciphertext (5 bytes)
        assert mac_then_decrypt(secret, b"\x00" * 7) is None


# --- try_decode_group_message ---

class TestTryDecodeGroupMessage:
    def test_full_round_trip(self):
        ch = _make_channel(name="Public")
        raw = _make_grp_txt_packet(ch, "Alice: Hello everyone!", timestamp=1700000000)
        msg = try_decode_group_message(raw, [ch], 1700000000.5)
        assert msg is not None
        assert isinstance(msg, GroupMessage)
        assert msg.sender == "Alice"
        assert msg.text == "Hello everyone!"
        assert msg.channel_name == "Public"
        assert msg.channel_hash == ch.hash
        assert msg.timestamp == 1700000000
        assert msg.raw_timestamp == 1700000000.5

    def test_wrong_hash_no_match(self):
        ch = _make_channel(name="Public")
        raw = _make_grp_txt_packet(ch, "Alice: Hello!")

        # Create a channel with different hash
        other = _make_channel(name="Other", psk=TEST_PSK_32)
        if other.hash == ch.hash:
            # Very unlikely, but handle it
            return
        msg = try_decode_group_message(raw, [other], 1.0)
        assert msg is None

    def test_multiple_channels_correct_match(self):
        ch1 = _make_channel(name="Chan1", psk=TEST_PSK_16)
        ch2 = _make_channel(name="Chan2", psk=TEST_PSK_32)
        raw = _make_grp_txt_packet(ch1, "Bob: Testing")
        msg = try_decode_group_message(raw, [ch2, ch1], 1.0)
        assert msg is not None
        assert msg.channel_name == "Chan1"
        assert msg.sender == "Bob"
        assert msg.text == "Testing"

    def test_non_grp_returns_none(self):
        # Build a TXT_MSG packet header
        header = (0x02 << 2) | ROUTE_TYPE_FLOOD
        raw = bytes([header, 0x00]) + b"\x00" * 30
        msg = try_decode_group_message(raw, [_make_channel()], 1.0)
        assert msg is None

    def test_message_without_colon_separator(self):
        ch = _make_channel()
        # Message without "sender: " format
        raw = _make_grp_txt_packet(ch, "no colon here")
        msg = try_decode_group_message(raw, [ch], 1.0)
        assert msg is not None
        assert msg.sender == "?"
        assert msg.text == "no colon here"

    def test_empty_channels_list(self):
        ch = _make_channel()
        raw = _make_grp_txt_packet(ch, "Alice: Hello!")
        msg = try_decode_group_message(raw, [], 1.0)
        assert msg is None


# --- Edge cases / error paths ---

class TestExtractGroupPayloadEdgeCases:
    def test_transport_direct_route(self):
        """TRANSPORT_DIRECT (0x03) has 4-byte transport codes like TRANSPORT_FLOOD."""
        ch = _make_channel()
        raw = _make_grp_txt_packet(ch, "hello", route_type=ROUTE_TYPE_TRANSPORT_DIRECT)
        result = extract_group_payload(raw)
        assert result is not None
        assert result["channel_hash"] == ch.hash

    def test_direct_route_no_transport_codes(self):
        """DIRECT (0x02) has no transport codes."""
        ch = _make_channel()
        raw = _make_grp_txt_packet(ch, "hello", route_type=ROUTE_TYPE_DIRECT)
        result = extract_group_payload(raw)
        assert result is not None
        assert result["channel_hash"] == ch.hash

    def test_path_len_exceeds_packet(self):
        """Path length claims more bytes than remain — should return None."""
        header = (PAYLOAD_TYPE_GRP_TXT << 2) | ROUTE_TYPE_FLOOD
        # header + path_len=200 but only 3 bytes of actual data
        raw = bytes([header, 200]) + b"\x00" * 3
        result = extract_group_payload(raw)
        assert result is None

    def test_payload_too_short_after_channel_hash(self):
        """Payload has channel_hash but no MAC/ciphertext."""
        header = (PAYLOAD_TYPE_GRP_TXT << 2) | ROUTE_TYPE_FLOOD
        # header + path_len=0 + channel_hash + 1 byte (not enough for MAC)
        raw = bytes([header, 0x00, 0xAA, 0xBB])
        result = extract_group_payload(raw)
        assert result is None

    def test_zero_length_path(self):
        """Path length = 0 is valid."""
        ch = _make_channel()
        raw = _make_grp_txt_packet(ch, "test", route_type=ROUTE_TYPE_FLOOD, path=b"")
        result = extract_group_payload(raw)
        assert result is not None

    def test_max_path_bytes(self):
        """64-byte path (MAX_PATH_SIZE) should still parse."""
        ch = _make_channel()
        path = b"\xaa" * 64
        raw = _make_grp_txt_packet(ch, "test", path=path)
        result = extract_group_payload(raw)
        assert result is not None


class TestTryDecodeEdgeCases:
    def test_non_zero_flags_skipped(self):
        """Only flags with (flags >> 2) == 0 are decoded. Non-zero type should be skipped."""
        ch = _make_channel()
        # Build plaintext with flags byte = 0x04 (type 1, not plain text)
        plaintext = struct.pack("<I", 1700000000) + b"\x04" + b"Alice: Hello!\x00"
        mac_and_data = encrypt_then_mac(ch.secret, plaintext)
        payload = bytes([ch.hash]) + mac_and_data
        header = (PAYLOAD_TYPE_GRP_TXT << 2) | ROUTE_TYPE_FLOOD
        raw = bytes([header, 0x00]) + payload
        msg = try_decode_group_message(raw, [ch], 1.0)
        assert msg is None

    def test_invalid_utf8_in_message(self):
        """Invalid UTF-8 bytes in message text should not crash — returns None."""
        ch = _make_channel()
        # Build plaintext with invalid UTF-8 in the text part
        plaintext = struct.pack("<I", 1700000000) + b"\x00" + b"\xff\xfe\xfd\x00"
        mac_and_data = encrypt_then_mac(ch.secret, plaintext)
        payload = bytes([ch.hash]) + mac_and_data
        header = (PAYLOAD_TYPE_GRP_TXT << 2) | ROUTE_TYPE_FLOOD
        raw = bytes([header, 0x00]) + payload
        msg = try_decode_group_message(raw, [ch], 1.0)
        assert msg is None  # UTF-8 decode fails, skipped

    def test_plaintext_too_short_for_message(self):
        """Plaintext with only 5 bytes (timestamp + flags, no text) should be skipped."""
        ch = _make_channel()
        plaintext = struct.pack("<I", 1700000000) + b"\x00"  # exactly 5 bytes
        mac_and_data = encrypt_then_mac(ch.secret, plaintext)
        payload = bytes([ch.hash]) + mac_and_data
        header = (PAYLOAD_TYPE_GRP_TXT << 2) | ROUTE_TYPE_FLOOD
        raw = bytes([header, 0x00]) + payload
        msg = try_decode_group_message(raw, [ch], 1.0)
        # 5 bytes is minimum — decrypted block is 16 bytes, text is empty null-terminated
        assert msg is not None
        assert msg.text == ""

    def test_hash_collision_wrong_key_skipped(self):
        """Two channels with same hash but different keys — wrong key fails MAC, right one succeeds."""
        ch1 = _make_channel(name="Real")
        # Manually create a channel with the same hash but different secret
        ch2 = Channel(name="Fake", secret=b"\xFF" * 32, hash=ch1.hash)

        raw = _make_grp_txt_packet(ch1, "Alice: secret message")
        # Put fake channel first — MAC check should fail, then real channel succeeds
        msg = try_decode_group_message(raw, [ch2, ch1], 1.0)
        assert msg is not None
        assert msg.channel_name == "Real"
        assert msg.sender == "Alice"

    def test_grp_data_not_decoded_as_text(self):
        """GRP_DATA payload type should not be decoded (only GRP_TXT is)."""
        ch = _make_channel()
        plaintext = struct.pack("<I", 1700000000) + b"\x00" + b"data\x00"
        mac_and_data = encrypt_then_mac(ch.secret, plaintext)
        payload = bytes([ch.hash]) + mac_and_data
        header = (PAYLOAD_TYPE_GRP_DATA << 2) | ROUTE_TYPE_FLOOD
        raw = bytes([header, 0x00]) + payload
        msg = try_decode_group_message(raw, [ch], 1.0)
        assert msg is None  # GRP_DATA is filtered out

    def test_sender_with_colon_in_name(self):
        """Sender name containing ': ' — first colon wins."""
        ch = _make_channel()
        raw = _make_grp_txt_packet(ch, "Alice: Bob: weird message")
        msg = try_decode_group_message(raw, [ch], 1.0)
        assert msg is not None
        assert msg.sender == "Alice"
        assert msg.text == "Bob: weird message"

    def test_unicode_message(self):
        """UTF-8 encoded message (emoji, CJK) should decode correctly."""
        ch = _make_channel()
        raw = _make_grp_txt_packet(ch, "Alice: Hello \u2764\ufe0f")
        msg = try_decode_group_message(raw, [ch], 1.0)
        assert msg is not None
        assert msg.sender == "Alice"
        assert "\u2764" in msg.text
