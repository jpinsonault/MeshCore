"""Tests for config.py — load_channels(), add/remove channel helpers."""

import json
import tempfile
import pytest

import base64
from collector.config import load_channels, add_channel_to_config, remove_channel_from_config
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


class TestLoadHashtagChannels:
    def test_hashtag_name_without_psk(self):
        """Hashtag channel derives key from name."""
        config = {"channels": [{"name": "#test"}]}
        channels = load_channels(config)
        assert len(channels) == 1
        assert channels[0].name == "#test"

    def test_hashtag_with_psk_uses_psk(self):
        """If psk is present, from_psk is used even for #names."""
        config = {"channels": [{"name": "#override", "psk": VALID_PSK}]}
        channels = load_channels(config)
        assert len(channels) == 1
        assert channels[0].secret[:16] == b"\x01" * 16

    def test_non_hashtag_without_psk_skipped(self):
        """Name without '#' and no psk should be skipped."""
        config = {"channels": [{"name": "NoPSK"}]}
        channels = load_channels(config)
        assert len(channels) == 0

    def test_missing_psk_with_hashtag(self):
        """Entry missing 'psk' but starting with '#' should auto-derive."""
        config = {"channels": [{"name": "#meshcore"}]}
        channels = load_channels(config)
        assert len(channels) == 1
        assert channels[0].name == "#meshcore"


class TestAddChannelToConfig:
    def test_add_hashtag_channel(self):
        with tempfile.NamedTemporaryFile(suffix=".json", mode="w", delete=False) as f:
            json.dump({"channels": []}, f)
            path = f.name
        result = add_channel_to_config("#test", path=path)
        assert result is True
        with open(path) as f:
            config = json.load(f)
        assert len(config["channels"]) == 1
        assert config["channels"][0] == {"name": "#test"}

    def test_add_channel_with_psk(self):
        with tempfile.NamedTemporaryFile(suffix=".json", mode="w", delete=False) as f:
            json.dump({"channels": []}, f)
            path = f.name
        result = add_channel_to_config("Private", psk=VALID_PSK, path=path)
        assert result is True
        with open(path) as f:
            config = json.load(f)
        assert config["channels"][0]["psk"] == VALID_PSK

    def test_add_duplicate_returns_false(self):
        with tempfile.NamedTemporaryFile(suffix=".json", mode="w", delete=False) as f:
            json.dump({"channels": [{"name": "#test"}]}, f)
            path = f.name
        result = add_channel_to_config("#test", path=path)
        assert result is False
        with open(path) as f:
            config = json.load(f)
        assert len(config["channels"]) == 1

    def test_add_to_empty_config(self):
        with tempfile.NamedTemporaryFile(suffix=".json", mode="w", delete=False) as f:
            json.dump({}, f)
            path = f.name
        result = add_channel_to_config("#new", path=path)
        assert result is True
        with open(path) as f:
            config = json.load(f)
        assert len(config["channels"]) == 1

    def test_add_preserves_existing(self):
        with tempfile.NamedTemporaryFile(suffix=".json", mode="w", delete=False) as f:
            json.dump({"channels": [{"name": "#existing"}]}, f)
            path = f.name
        add_channel_to_config("#new", path=path)
        with open(path) as f:
            config = json.load(f)
        assert len(config["channels"]) == 2
        names = [ch["name"] for ch in config["channels"]]
        assert "#existing" in names
        assert "#new" in names


class TestRemoveChannelFromConfig:
    def test_remove_existing(self):
        with tempfile.NamedTemporaryFile(suffix=".json", mode="w", delete=False) as f:
            json.dump({"channels": [{"name": "#test"}, {"name": "#keep"}]}, f)
            path = f.name
        remove_channel_from_config("#test", path=path)
        with open(path) as f:
            config = json.load(f)
        assert len(config["channels"]) == 1
        assert config["channels"][0]["name"] == "#keep"

    def test_remove_nonexistent(self):
        with tempfile.NamedTemporaryFile(suffix=".json", mode="w", delete=False) as f:
            json.dump({"channels": [{"name": "#test"}]}, f)
            path = f.name
        remove_channel_from_config("#nonexistent", path=path)
        with open(path) as f:
            config = json.load(f)
        assert len(config["channels"]) == 1

    def test_remove_from_empty(self):
        with tempfile.NamedTemporaryFile(suffix=".json", mode="w", delete=False) as f:
            json.dump({"channels": []}, f)
            path = f.name
        remove_channel_from_config("#test", path=path)
        with open(path) as f:
            config = json.load(f)
        assert len(config["channels"]) == 0
