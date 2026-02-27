"""
Over-the-air system tests (single-board and two-board).

Single-board tests use the collector's ``collector send`` for transmit + echo.
Two-board tests: sender board transmits real LoRa messages via ``collector send``,
collector board captures them over the air. Both boards run the same repeater
firmware. Python host verifies frames arrive and are decoded correctly.

Run:
    MESHCORE_PORT=/dev/cu.usbserial-0001 \
    MESHCORE_SENDER_PORT=/dev/cu.usbserial-5 \
    python -m pytest collector/tests/system/test_radio.py -v
"""

import random
import secrets
import string
import threading
import time

import pytest

from collector.brute_force import brute_force_channel
from collector.cracker import ChannelCracker
from collector.crypto import Channel, extract_group_payload, try_decode_group_message
from collector.protocol import (
    FRAME_TYPE_ADVERTISEMENT,
    FRAME_TYPE_RX_RAW,
    PAYLOAD_TYPE_GRP_TXT,
)

pytestmark = pytest.mark.system

RADIO_TIMEOUT = 15  # seconds — generous for LoRa latency


class TestCollectorSend:
    """Single-board tests for `collector send` — validates the full pipeline:
    CLI parse → encrypt → sendFlood → logRxRaw echo → frame → decrypt → store.

    The message IS transmitted over LoRa (sendFlood), but we verify the echo
    path back through logRxRaw, not reception by a second board.

    Only requires MESHCORE_PORT (single board).
    """

    def test_send_echoes_through_pipeline(self, hw, unique_tag):
        """collector send → firmware encrypts + sendFlood + logRxRaw echo → host decrypts."""
        core, fc = hw
        channel = Channel.from_hashtag("#test")
        core.set_channels([channel])

        success = core.send_channel_message("#test", "systest", unique_tag)
        assert success, "send_channel_message returned False"

        msg = fc.wait_for_channel_message(
            lambda m: unique_tag in m.text,
            timeout=RADIO_TIMEOUT,
        )
        assert msg is not None, f"Channel message with tag {unique_tag} not echoed back"
        assert msg.sender == "systest"
        assert unique_tag in msg.text

        # Verify stored in SQLite
        time.sleep(0.5)
        rows = core.store.get_channel_messages(channel_name="#test", limit=50)
        matching = [r for r in rows if unique_tag in (r.get("text") or "")]
        assert len(matching) >= 1, "Sent message not found in channel_messages table"


class TestSendAndCrack:
    """Full-loop test: send on a random channel → crack → decrypt.

    Neither board has foreknowledge of the channel name. The collector picks
    a random hashtag, sends a message, and the cracker discovers the channel
    by dictionary attack on the echoed packet.

    Only requires MESHCORE_PORT (single board). If a second board is present
    and relaying, the test also verifies real over-the-air reception (a second
    RX_RAW frame with real SNR, distinct from the synthetic -50 echo).

    Flow:
        1. Pick random channel name, e.g. #systest_a3f7b2c1
        2. Set up cracker with a wordlist containing the name (among decoys)
        3. Collector sends message — firmware encrypts, sendFlood, logRxRaw echo
        4. Echo arrives as RX_RAW (SNR=-50, synthetic) — cracker gets notified
        5. Cracker cracks the channel name, retroactively decrypts
        6. Assert: message decrypted, matches original text, stored in SQLite
        7. Bonus: if relay arrives (second board), verify real radio SNR
    """

    def test_send_crack_full_loop(self, hw, unique_tag):
        """Random channel → send → crack → decrypt — no foreknowledge."""
        core, fc = hw
        core.send_screen_text("Cracking...")

        # 1. Random channel name — neither board knows it
        random_word = f"systest_{secrets.token_hex(4)}"
        channel_name = f"#{random_word}"

        # 2. Decoy wordlist + the real channel name buried inside
        decoys = [f"decoy_{secrets.token_hex(3)}_{i}" for i in range(50)]
        wordlist = decoys[:25] + [random_word] + decoys[25:]

        # 3. Set up cracker with NO pre-configured channels
        #    Mark any channels already in the store as "known" so the cracker
        #    ignores stale packets from prior tests and only cracks new ones.
        existing_channels = []
        for s in core._store.get_channel_summary():
            try:
                existing_channels.append(Channel.from_hashtag(s["channel_name"]))
            except Exception:
                pass
        core.set_channels([])
        cracker = ChannelCracker(core._store, existing_channels)
        cracker._build_hash_table(wordlist)

        # Wire cracker discovery → core's live channel list.
        # Track all discoveries; wait for our specific random channel.
        target_discovered = threading.Event()
        discovered_info = {}

        def on_discovered(name, decoded_count):
            core.add_channel(Channel.from_hashtag(name))
            if name == channel_name:
                discovered_info["name"] = name
                discovered_info["decoded"] = decoded_count
                target_discovered.set()

        cracker.on_channel_discovered = on_discovered
        core.set_cracker(cracker)
        cracker.start()

        try:
            # 4. Send the message — collector has no channels, cracker is listening
            message_text = f"full_loop_{unique_tag}"
            success = core.send_channel_message(channel_name, "radiotx", message_text)
            assert success, "send_channel_message returned False"

            # 5. Wait for cracker to discover our specific random channel
            assert target_discovered.wait(timeout=RADIO_TIMEOUT), (
                f"Cracker did not crack {channel_name} within {RADIO_TIMEOUT}s"
            )
            assert discovered_info["name"] == channel_name
            assert discovered_info["decoded"] >= 1

            # 6. Wait for the relay — a second RX_RAW/GRP_TXT with real SNR (not -50)
            relay_frame = fc.wait_for_frame(
                lambda f: (
                    f["type"] == FRAME_TYPE_RX_RAW
                    and f.get("parsed", {}).get("payload_type") == PAYLOAD_TYPE_GRP_TXT
                    and f.get("parsed", {}).get("snr", -50) != -50.0
                ),
                timeout=RADIO_TIMEOUT,
            )
            # The relay may or may not arrive depending on the sender board's
            # proximity and relay behaviour. We assert it optimistically but
            # don't fail the entire test if radio conditions prevent it.
            if relay_frame:
                real_snr = relay_frame["parsed"]["snr"]
                assert real_snr != -50.0, "Relay frame has synthetic SNR — not a real radio reception"

            # 7. Verify the cracked message is stored correctly in SQLite
            time.sleep(0.5)
            rows = core.store.get_channel_messages(channel_name=channel_name, limit=50)
            matching = [r for r in rows if unique_tag in (r.get("text") or "")]
            assert len(matching) >= 1, (
                f"Decrypted message not found in channel_messages for {channel_name}"
            )
            row = matching[0]
            assert row["sender"] == "radiotx"
            assert unique_tag in row["text"]
            core.send_screen_text("PASS")

        except Exception:
            core.send_screen_text("FAIL")
            raise
        finally:
            cracker.stop()
            time.sleep(1)
            core.send_screen_text()  # clear


@pytest.mark.radio
class TestInterceptAndCrack:
    """True blind intercept-and-crack: two independent boards, no shared state.

    The sender board transmits an encrypted message on a random hashtag channel
    via ``collector send``. The collector board intercepts the ciphertext over
    LoRa and has NO channels configured — it doesn't know the key. A cracker
    loaded with candidate hashtag names (the real one buried among decoys)
    performs a dictionary attack on the intercepted packet, discovers the key,
    and decrypts the message.

    This is a real over-the-air intercept and crack: real AES-128 encryption,
    real LoRa radio, real dictionary attack. Neither board has foreknowledge
    of the other's channel or message.
    """

    def test_intercept_and_crack_over_the_air(self, hw, sender, unique_tag):
        """Sender transmits → LoRa → collector intercepts → cracker cracks → decrypted."""
        core, fc = hw

        # Show test status on the collector's OLED
        core.send_screen_text("Cracking...")

        # 1. Collector has NO channels — completely blind
        core.set_channels([])

        # 2. Pick a random hashtag channel name — neither board knows it ahead of time
        random_word = f"radiotx_{secrets.token_hex(4)}"
        channel_name = f"#{random_word}"

        # 3. Build cracker wordlist: real name buried among 50 decoys
        decoys = [f"decoy_{secrets.token_hex(3)}_{i}" for i in range(50)]
        wordlist = decoys[:25] + [random_word] + decoys[25:]

        # 4. Set up cracker with NO known channels
        existing_channels = []
        for s in core._store.get_channel_summary():
            try:
                existing_channels.append(Channel.from_hashtag(s["channel_name"]))
            except Exception:
                pass
        cracker = ChannelCracker(core._store, existing_channels)
        cracker._build_hash_table(wordlist)

        target_discovered = threading.Event()
        discovered_info = {}

        def on_discovered(name, decoded_count):
            core.add_channel(Channel.from_hashtag(name))
            if name == channel_name:
                discovered_info["name"] = name
                discovered_info["decoded"] = decoded_count
                target_discovered.set()

        cracker.on_channel_discovered = on_discovered
        core.set_cracker(cracker)
        cracker.start()

        try:
            # 5. Sender board transmits over LoRa — collector knows nothing
            message_text = f"intercept_{unique_tag}"
            sender.write(
                f"collector send {channel_name} radiotx {message_text}\r".encode()
            )

            # 6. Wait for the cracker to intercept and crack
            assert target_discovered.wait(timeout=RADIO_TIMEOUT), (
                f"Cracker did not crack {channel_name} within {RADIO_TIMEOUT}s"
            )
            assert discovered_info["name"] == channel_name
            assert discovered_info["decoded"] >= 1

            # 7. Verify the intercepted message was decrypted correctly
            time.sleep(0.5)
            rows = core.store.get_channel_messages(channel_name=channel_name, limit=50)
            matching = [r for r in rows if unique_tag in (r.get("text") or "")]
            assert len(matching) >= 1, "Cracked message not found in channel_messages"
            assert matching[0]["sender"] == "radiotx"
            assert unique_tag in matching[0]["text"]

            core.send_screen_text("PASS")

        except Exception:
            core.send_screen_text("FAIL")
            raise
        finally:
            cracker.stop()
            time.sleep(2)
            core.send_screen_text()  # clear


@pytest.mark.radio
class TestOverTheAir:
    """End-to-end radio tests: sender → air → collector → Python."""

    def test_message_captured_as_rx_raw(self, hw, sender, unique_tag):
        """Send a message from sender board, verify collector gets an RX_RAW frame."""
        core, fc = hw

        sender.write(
            f"collector send #radiotest sender {unique_tag}\r".encode()
        )

        frame = fc.wait_for_frame(
            lambda f: (
                f["type"] == FRAME_TYPE_RX_RAW
                and f.get("parsed", {}).get("payload_type") == PAYLOAD_TYPE_GRP_TXT
            ),
            timeout=RADIO_TIMEOUT,
        )
        assert frame is not None, "No RX_RAW/GRP_TXT frame received over the air"
        assert frame["parsed"]["raw_len"] > 0

    def test_message_decrypted(self, hw, sender, unique_tag):
        """Send a message, verify collector decrypts it and stores in SQLite."""
        core, fc = hw
        channel = Channel.from_hashtag("#radiotest")
        core.set_channels([channel])

        sender.write(
            f"collector send #radiotest sender {unique_tag}\r".encode()
        )

        msg = fc.wait_for_channel_message(
            lambda m: unique_tag in m.text,
            timeout=RADIO_TIMEOUT,
        )
        assert msg is not None, f"Channel message with tag {unique_tag} not received"
        assert msg.sender, "Sender name is empty"
        assert unique_tag in msg.text

        # Verify stored in SQLite
        time.sleep(0.5)
        rows = core.store.get_channel_messages(channel_name="#radiotest", limit=50)
        matching = [r for r in rows if unique_tag in (r.get("text") or "")]
        assert len(matching) >= 1, "Decrypted message not found in channel_messages table"

    def test_advertisement_captured(self, hw, sender):
        """Trigger an advertisement from the sender, verify collector receives it."""
        core, fc = hw

        sender.write(b"advert\r")

        frame = fc.wait_for_frame(
            lambda f: f["type"] == FRAME_TYPE_ADVERTISEMENT,
            timeout=RADIO_TIMEOUT,
        )
        assert frame is not None, "No ADVERTISEMENT frame received over the air"
        parsed = frame["parsed"]
        assert "pub_key" in parsed
        assert len(parsed["pub_key"]) == 32, f"pub_key wrong length: {len(parsed['pub_key'])}"


@pytest.mark.radio
class TestBruteForce:
    """True brute-force crack over the air: no wordlist, no hints, pure compute.

    The sender board transmits on a random 4-character hashtag channel.
    The collector board intercepts the encrypted packet over LoRa.
    The brute forcer tries all 26^4 = 456,976 lowercase names in parallel
    across all CPU cores. No dictionary, no hints — just SHA-256 grinding.
    """

    def test_brute_force_over_the_air(self, hw, sender, unique_tag):
        """Sender → LoRa → intercept → brute-force all 4-char names → decrypted."""
        core, fc = hw
        core.send_screen_text("Brute force...")
        core.set_channels([])

        # Random 4-char lowercase channel name — nobody knows it
        name = "".join(random.choices(string.ascii_lowercase, k=4))
        channel_name = f"#{name}"

        # Sender board transmits over LoRa
        message_text = f"bf_{unique_tag}"
        sender.write(
            f"collector send {channel_name} bfsender {message_text}\r".encode()
        )

        # Collector intercepts encrypted packet over the air
        frame = fc.wait_for_frame(
            lambda f: (
                f["type"] == FRAME_TYPE_RX_RAW
                and f.get("parsed", {}).get("payload_type") == PAYLOAD_TYPE_GRP_TXT
            ),
            timeout=RADIO_TIMEOUT,
        )
        assert frame is not None, "No RX_RAW received over the air"

        # Extract ciphertext — we have the encrypted bytes, nothing else
        raw = frame["parsed"]["raw"]
        extracted = extract_group_payload(raw)
        assert extracted is not None, "Failed to parse group payload"

        # Brute force — try all 26^4 = 456,976 lowercase names
        t0 = time.monotonic()
        result = brute_force_channel(
            extracted["channel_hash"],
            extracted["mac_and_data"],
            charset=string.ascii_lowercase,
            max_length=4,
        )
        elapsed = time.monotonic() - t0

        assert result == channel_name, (
            f"Brute force returned {result}, expected {channel_name}"
        )

        # Decrypt and verify the message content
        channel = Channel.from_hashtag(result)
        msg = try_decode_group_message(raw, [channel], frame["received_at"])
        assert msg is not None, "Decryption failed after brute force"
        assert msg.sender == "bfsender"
        assert unique_tag in msg.text

        core.send_screen_text(f"BF {elapsed:.1f}s")
        time.sleep(2)
        core.send_screen_text()
