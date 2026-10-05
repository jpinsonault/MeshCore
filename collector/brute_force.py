"""
MeshCore Collector — Brute-force channel cracker.

Hashtag channels derive their AES-128 key from SHA-256("#name")[:16].
The channel_hash byte (first byte of the group payload) is cleartext,
so we can reject 255/256 of candidates with a single byte compare
before doing the expensive HMAC-SHA256 MAC verification.

Brute-force strategy per candidate:
  1. PSK = SHA-256("#" + candidate)[:16]
  2. h = SHA-256(PSK)[0]
  3. h != target_hash? → skip (99.6% of candidates)
  4. h == target_hash? → HMAC-SHA256 MAC verify against ciphertext
  5. MAC passes? → AES-128-ECB decrypt, validate plaintext (null term + UTF-8)
  6. Plaintext valid? → cracked

The MAC is only 2 bytes, so false positives occur at ~1/16M candidates.
The plaintext validation (null terminator + valid UTF-8) eliminates them.

Parallelized across all CPU cores via multiprocessing.

Performance (Apple Silicon M-series, Python 3.13+):
  Charset a-z (26 chars):
    3 chars (17.6K):   instant
    4 chars (456K):    <0.1s
    5 chars (11.9M):   ~1.5s
    6 chars (309M):    ~40s
  Charset a-z0-9 (36 chars):
    4 chars (1.7M):    ~0.2s
    5 chars (60M):     ~8s
    6 chars (2.2B):    ~5 min
"""

import hashlib
import hmac as hmac_mod
import os
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from typing import Callable, Optional

CIPHER_KEY_SIZE = 16
CIPHER_MAC_SIZE = 2
CIPHER_BLOCK_SIZE = 16

# Lowercase + digits + hyphen: the full alphabet observed in real hashtag channel
# names (the community catalog uses only [a-z0-9-]). The hyphen adds ~1 char to
# the radix (37 vs 36), a negligible ~3%/char keyspace growth, but lets a single
# brute-force pass reach hyphenated names like #bot-tacoma without relying on the
# rules engine's hyphenated-connector mangling.
DEFAULT_CHARSET = "abcdefghijklmnopqrstuvwxyz0123456789-"


def _crack_chunk(args):
    """Multiprocessing worker: try a range of candidate names.

    Self-contained — all crypto is inlined to avoid cross-process import
    overhead and function-call cost in the hot loop.

    Returns the cracked channel name (str with '#') or None.
    """
    target_hash, mac_and_data, charset, length, start, count, extras = args

    import hashlib
    import hmac as _hmac
    from Crypto.Cipher import AES

    from .crypto import grp_txt_plaintext_ok

    mac = mac_and_data[:2]
    ciphertext = mac_and_data[2:]
    if len(ciphertext) == 0 or len(ciphertext) % 16 != 0:
        return None

    base = len(charset)
    sha256 = hashlib.sha256

    def _decrypts_ok(psk, blob):
        """MAC-verify and strictly validate one extra packet for this channel."""
        mac_e, ct_e = blob[:2], blob[2:]
        if len(ct_e) == 0 or len(ct_e) % 16 != 0:
            return False
        sec = psk + b"\x00" * 16
        if not _hmac.compare_digest(mac_e, _hmac.new(sec, ct_e, hashlib.sha256).digest()[:2]):
            return False
        cph = AES.new(psk, AES.MODE_ECB)
        pt = b"".join(cph.decrypt(ct_e[o:o + 16]) for o in range(0, len(ct_e), 16))
        return grp_txt_plaintext_ok(pt)

    for i in range(count):
        idx = start + i

        # Index → name bytes (big-endian digit extraction)
        n = idx
        name = bytearray(length)
        for pos in range(length - 1, -1, -1):
            name[pos] = charset[n % base]
            n //= base

        full = b"#" + bytes(name)

        # Fast filter: 2x SHA-256, compare 1 byte (rejects 255/256)
        psk = sha256(full).digest()[:16]
        if sha256(psk).digest()[0] != target_hash:
            continue

        # MAC verify — HMAC-SHA256 truncated to 2 bytes
        secret = psk + b"\x00" * 16
        computed = _hmac.new(secret, ciphertext, hashlib.sha256).digest()[:2]
        if not _hmac.compare_digest(mac, computed):
            continue

        # MAC matched — decrypt and strictly validate to rule out a collision.
        # GRP_TXT plaintext: [timestamp(4)][flags(1)][sender: text\0...]
        cipher = AES.new(psk, AES.MODE_ECB)
        plaintext = b""
        for off in range(0, len(ciphertext), 16):
            plaintext += cipher.decrypt(ciphertext[off : off + 16])

        if not grp_txt_plaintext_ok(plaintext):
            continue

        # Cross-check: at least one sibling must also decrypt to valid GRP_TXT.
        # A 2-byte MAC collision won't; and on a shared hash byte, siblings from
        # other channels won't either, so corroboration needs only one same-
        # channel sibling (callers brute-force only when siblings exist).
        if extras and not any(_decrypts_ok(psk, e) for e in extras):
            continue

        return full.decode()

    return None


def brute_force_channel(
    target_hash: int,
    mac_and_data: bytes,
    charset: str = DEFAULT_CHARSET,
    max_length: int = 6,
    num_workers: int = None,
    on_progress: Optional[Callable] = None,
    extra_mac_and_data=None,
    should_stop: Optional[Callable] = None,
) -> Optional[str]:
    """Brute-force a hashtag channel name from an intercepted packet.

    Args:
        target_hash: channel_hash byte from the packet (cleartext, 0-255)
        mac_and_data: [mac(2)][ciphertext...] bytes from the group payload
        charset: character set to search (default: a-z0-9)
        max_length: maximum name length to try (default: 6)
        num_workers: parallel workers (default: CPU count)
        on_progress: callback(length, total, elapsed_sec) after each length
        extra_mac_and_data: other packets' [mac][ct] blobs for the same channel;
            a candidate must decrypt all of them (collision guard)

    Returns:
        Channel name with '#' prefix if cracked, None if exhausted.
    """
    if num_workers is None:
        num_workers = os.cpu_count() or 4

    charset_bytes = charset.encode()
    extras = tuple(extra_mac_and_data or ())
    t0 = time.monotonic()

    for length in range(1, max_length + 1):
        if should_stop is not None and should_stop():
            return None
        total = len(charset) ** length

        # Split into chunks — oversub for load balancing on heterogeneous cores
        n_chunks = max(1, num_workers * 8)
        chunk_size = max(1, total // n_chunks)

        tasks = []
        for chunk_start in range(0, total, chunk_size):
            chunk_count = min(chunk_size, total - chunk_start)
            tasks.append((
                target_hash, mac_and_data, charset_bytes,
                length, chunk_start, chunk_count, extras,
            ))

        with ProcessPoolExecutor(max_workers=num_workers) as executor:
            futures = [executor.submit(_crack_chunk, task) for task in tasks]
            for future in as_completed(futures):
                if should_stop is not None and should_stop():
                    for f in futures:
                        f.cancel()
                    return None
                result = future.result()
                if result is not None:
                    for f in futures:
                        f.cancel()
                    return result

        if on_progress:
            on_progress(length, total, time.monotonic() - t0)

    return None
