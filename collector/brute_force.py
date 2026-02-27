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

DEFAULT_CHARSET = "abcdefghijklmnopqrstuvwxyz0123456789"


def _crack_chunk(args):
    """Multiprocessing worker: try a range of candidate names.

    Self-contained — all crypto is inlined to avoid cross-process import
    overhead and function-call cost in the hot loop.

    Returns the cracked channel name (str with '#') or None.
    """
    target_hash, mac_and_data, charset, length, start, count = args

    import hashlib
    import hmac as _hmac
    from Crypto.Cipher import AES

    mac = mac_and_data[:2]
    ciphertext = mac_and_data[2:]
    if len(ciphertext) == 0 or len(ciphertext) % 16 != 0:
        return None

    base = len(charset)
    sha256 = hashlib.sha256

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

        # MAC matched — decrypt and validate plaintext to rule out collision.
        # GRP_TXT plaintext: [timestamp(4)][flags(1)][sender: text\0...]
        cipher = AES.new(psk, AES.MODE_ECB)
        plaintext = b""
        for off in range(0, len(ciphertext), 16):
            plaintext += cipher.decrypt(ciphertext[off : off + 16])

        # Must have at least 6 bytes (4 timestamp + 1 flags + 1 null)
        # and contain a null terminator after the header.
        if len(plaintext) < 6:
            continue
        text_part = plaintext[5:]
        null_idx = text_part.find(0)
        if null_idx < 0:
            continue
        try:
            text_part[:null_idx].decode("utf-8")
        except UnicodeDecodeError:
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
) -> Optional[str]:
    """Brute-force a hashtag channel name from an intercepted packet.

    Args:
        target_hash: channel_hash byte from the packet (cleartext, 0-255)
        mac_and_data: [mac(2)][ciphertext...] bytes from the group payload
        charset: character set to search (default: a-z0-9)
        max_length: maximum name length to try (default: 6)
        num_workers: parallel workers (default: CPU count)
        on_progress: callback(length, total, elapsed_sec) after each length

    Returns:
        Channel name with '#' prefix if cracked, None if exhausted.
    """
    if num_workers is None:
        num_workers = os.cpu_count() or 4

    charset_bytes = charset.encode()
    t0 = time.monotonic()

    for length in range(1, max_length + 1):
        total = len(charset) ** length

        # Split into chunks — oversub for load balancing on heterogeneous cores
        n_chunks = max(1, num_workers * 8)
        chunk_size = max(1, total // n_chunks)

        tasks = []
        for chunk_start in range(0, total, chunk_size):
            chunk_count = min(chunk_size, total - chunk_start)
            tasks.append((
                target_hash, mac_and_data, charset_bytes,
                length, chunk_start, chunk_count,
            ))

        with ProcessPoolExecutor(max_workers=num_workers) as executor:
            futures = [executor.submit(_crack_chunk, task) for task in tasks]
            for future in as_completed(futures):
                result = future.result()
                if result is not None:
                    for f in futures:
                        f.cancel()
                    return result

        if on_progress:
            on_progress(length, total, time.monotonic() - t0)

    return None
