"""Tests for config.py — especially load_channels()."""

import pytest

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import base64
from collector.config import load_channels
from collector.crypto import Channel


# Valid 16-byte PSK
VALID_PSK = base64.b64encode(b"\x01" * 16).decode()
VALID_PSK_32 = base64.b64encode(b"\x02" * 32).decode()


class TestLoadChannels:
    def test_valid_channel(self):
        config = {"channels": [{"name": "Public", "psk": VALID_PSK}]}
        channels = load_channels(config)
        assert len(channels) == 1
        assert channels[0].name == "Public"
        assert isinstance(channels[0], Channel)

    def test_multiple_valid_channels(self):
        config = {"channels": [
            {"name": "Public", "psk": VALID_PSK},
            {"name": "Private", "psk": VALID_PSK_32},
        ]}
        channels = load_channels(config)
        assert len(channels) == 2
        assert channels[0].name == "Public"
        assert channels[1].name == "Private"

    def test_empty_channels_list(self):
        config = {"channels": []}
        assert load_channels(config) == []

    def test_no_channels_key(self):
        config = {}
        assert load_channels(config) == []

    def test_channels_key_is_none(self):
        """channels=None should not crash — treated as empty."""
        config = {"channels": None}
        assert load_channels(config) == []

    def test_missing_name(self):
        """Entry missing 'name' should be skipped."""
        config = {"channels": [{"psk": VALID_PSK}]}
        assert load_channels(config) == []

    def test_missing_psk(self):
        """Entry missing 'psk' should be skipped."""
        config = {"channels": [{"name": "Public"}]}
        assert load_channels(config) == []

    def test_empty_name(self):
        """Empty name string should be skipped."""
        config = {"channels": [{"name": "", "psk": VALID_PSK}]}
        assert load_channels(config) == []

    def test_empty_psk(self):
        """Empty PSK string should be skipped."""
        config = {"channels": [{"name": "Public", "psk": ""}]}
        assert load_channels(config) == []

    def test_invalid_base64_psk(self):
        """Bad base64 PSK should be skipped, not crash."""
        config = {"channels": [{"name": "Bad", "psk": "not-valid-base64!!!"}]}
        assert load_channels(config) == []

    def test_wrong_length_psk(self):
        """PSK that decodes to wrong length (8 bytes) should be skipped."""
        bad_psk = base64.b64encode(b"\x03" * 8).decode()
        config = {"channels": [{"name": "Bad", "psk": bad_psk}]}
        assert load_channels(config) == []

    def test_mixed_valid_and_invalid(self):
        """Valid entries survive alongside invalid ones."""
        bad_psk = base64.b64encode(b"\x03" * 8).decode()
        config = {"channels": [
            {"name": "Good1", "psk": VALID_PSK},
            {"name": "Bad", "psk": bad_psk},
            {"psk": VALID_PSK},  # missing name
            {"name": "Good2", "psk": VALID_PSK_32},
        ]}
        channels = load_channels(config)
        assert len(channels) == 2
        assert channels[0].name == "Good1"
        assert channels[1].name == "Good2"

    def test_entry_is_not_a_dict(self):
        """Non-dict entries in the list should be skipped."""
        config = {"channels": ["not a dict", 42, None, {"name": "Ok", "psk": VALID_PSK}]}
        channels = load_channels(config)
        assert len(channels) == 1
        assert channels[0].name == "Ok"
