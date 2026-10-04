"""Tests for .env loading and the remote CLI's stream handling (no hardware)."""

import os
import struct

from collector import envfile
from collector.remote import RemoteCLI


class TestEnvFile:
    def test_parses_and_does_not_override(self, tmp_path, monkeypatch):
        (tmp_path / ".env").write_text(
            "# comment\nMESHCORE_HOST=node.local\nexport MESHCORE_PASSWORD='p w'\nEMPTY=\nJUNK\n",
            encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("MESHCORE_HOST", raising=False)
        monkeypatch.delenv("MESHCORE_PASSWORD", raising=False)
        monkeypatch.setenv("EMPTY", "kept")
        assert envfile.load_env() == tmp_path / ".env"
        assert os.environ["MESHCORE_HOST"] == "node.local"
        assert os.environ["MESHCORE_PASSWORD"] == "p w"
        assert os.environ["EMPTY"] == "kept"

    def test_existing_env_wins(self, tmp_path, monkeypatch):
        (tmp_path / ".env").write_text("MESHCORE_HOST=from-file\n", encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("MESHCORE_HOST", "from-env")
        envfile.load_env()
        assert os.environ["MESHCORE_HOST"] == "from-env"


def _frame(body):
    return bytes([0xC0]) + struct.pack("<H", len(body)) + body


class TestTakeText:
    def _cli(self, buf):
        cli = object.__new__(RemoteCLI)   # no network
        cli._buf = buf
        return cli

    def test_strips_collector_frames(self):
        cli = self._cli(b"ver\r\n" + _frame(b"\xd3" + b"\x01" * 20) + b"  -> v1\r\n")
        assert cli._take_text() == b"ver\r\n  -> v1\r\n"
        assert cli._buf == b""

    def test_keeps_partial_frame_for_later(self):
        frame = _frame(b"\xd0" + b"\x02" * 10)
        cli = self._cli(b"abc" + frame[:5])
        assert cli._take_text() == b"abc"
        assert cli._buf == frame[:5]
        cli._buf += frame[5:] + b"xyz"
        assert cli._take_text() == b"xyz"
