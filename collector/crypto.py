"""
MeshCore Collector — Channel decryption.

Reimplements the C++ MACThenDecrypt (AES-128-ECB + HMAC-SHA256 MAC) in Python
so the host can decode group channel messages against user-configured PSKs.

Wire format for GRP_TXT/GRP_DATA packets (Packet::writeTo):
  [header(1)] [transport_codes(4)?] [path_len(1)] [path(N)] [payload...]

Payload layout (createGroupDatagram):
  [channel_hash(1)] [mac(2)] [encrypted_data...]

Decrypted data for GRP_TXT (sendGroupMessage):
  [timestamp(4 LE)] [flags(1)] [sender: text\0]
"""

import base64
import hashlib
import hmac
import struct
from dataclasses import dataclass
from typing import Optional

from Crypto.Cipher import AES

# Constants matching MeshCore C++
CIPHER_KEY_SIZE = 16
CIPHER_BLOCK_SIZE = 16
CIPHER_MAC_SIZE = 2
PATH_HASH_SIZE = 1
PUB_KEY_SIZE = 32

PAYLOAD_TYPE_GRP_TXT = 0x05
PAYLOAD_TYPE_GRP_DATA = 0x06

ROUTE_TYPE_TRANSPORT_FLOOD = 0x00
ROUTE_TYPE_FLOOD = 0x01
ROUTE_TYPE_DIRECT = 0x02
ROUTE_TYPE_TRANSPORT_DIRECT = 0x03


@dataclass
class Channel:
    """A configured group channel with its PSK-derived secret and hash."""
    name: str
    secret: bytes   # 32 bytes (zero-padded if 16-byte PSK)
    hash: int       # 1-byte channel hash (first byte of SHA-256 of raw PSK)

    @classmethod
    def from_hashtag(cls, name: str) -> "Channel":
        """Construct a Channel from a hashtag channel name.

        Matches firmware derivation for public hashtag channels:
        - Normalize name to include '#' prefix
        - PSK = SHA-256("#name")[:16]
        - Hash = SHA-256(psk)[:1]
        - Secret = psk + 16 zero bytes
        """
        if not name.startswith("#"):
            name = f"#{name}"
        psk = hashlib.sha256(name.encode("utf-8")).digest()[:CIPHER_KEY_SIZE]
        channel_hash = hashlib.sha256(psk).digest()[0]
        secret = psk + b"\x00" * (PUB_KEY_SIZE - len(psk))
        return cls(name=name, secret=secret, hash=channel_hash)

    @classmethod
    def from_psk(cls, name: str, psk_base64: str) -> "Channel":
        """Construct a Channel from a base64-encoded PSK.

        Matches BaseChatMesh::addChannel key derivation:
        - Decode base64 PSK (must be 16 or 32 bytes)
        - Zero-pad to 32 bytes if 16 bytes
        - Hash = SHA-256(raw_psk)[:1]
        """
        raw = base64.b64decode(psk_base64)
        if len(raw) not in (16, 32):
            raise ValueError(f"PSK must be 16 or 32 bytes, got {len(raw)}")

        # Channel hash is SHA-256 of the raw PSK, truncated to 1 byte
        h = hashlib.sha256(raw).digest()
        channel_hash = h[0]

        # Secret is zero-padded to 32 bytes
        secret = raw + b"\x00" * (PUB_KEY_SIZE - len(raw))

        return cls(name=name, secret=secret, hash=channel_hash)


def extract_group_payload(raw_packet: bytes) -> Optional[dict]:
    """Parse a raw mesh packet to extract group message fields.

    Args:
        raw_packet: The complete raw packet bytes (as from Packet::writeTo)

    Returns:
        Dict with 'channel_hash' and 'mac_and_data', or None if not a GRP_TXT/GRP_DATA.
    """
    if len(raw_packet) < 3:
        return None

    header = raw_packet[0]
    route_type = header & 0x03
    payload_type = (header >> 2) & 0x0F

    if payload_type not in (PAYLOAD_TYPE_GRP_TXT, PAYLOAD_TYPE_GRP_DATA):
        return None

    i = 1
    # Transport codes present for TRANSPORT_FLOOD (0x00) and TRANSPORT_DIRECT (0x03)
    if route_type == ROUTE_TYPE_TRANSPORT_FLOOD or route_type == ROUTE_TYPE_TRANSPORT_DIRECT:
        i += 4  # 2x uint16_t

    if i >= len(raw_packet):
        return None

    path_len = raw_packet[i]
    i += 1
    i += path_len  # skip path bytes

    if i >= len(raw_packet):
        return None

    # Payload starts here: [channel_hash(1)][mac(2)][encrypted_data...]
    channel_hash = raw_packet[i]
    i += PATH_HASH_SIZE

    if i + CIPHER_MAC_SIZE >= len(raw_packet):
        return None

    mac_and_data = raw_packet[i:]

    return {
        "channel_hash": channel_hash,
        "mac_and_data": mac_and_data,
        "payload_type": payload_type,
    }


def mac_then_decrypt(secret: bytes, mac_and_data: bytes) -> Optional[bytes]:
    """Python port of Utils::MACThenDecrypt.

    Verifies the 2-byte HMAC-SHA256 MAC, then AES-128-ECB decrypts.

    Args:
        secret: 32-byte channel secret
        mac_and_data: [mac(2)][ciphertext...] bytes

    Returns:
        Decrypted bytes, or None if MAC verification fails or data is too short.
    """
    if len(mac_and_data) <= CIPHER_MAC_SIZE:
        return None

    mac = mac_and_data[:CIPHER_MAC_SIZE]
    ciphertext = mac_and_data[CIPHER_MAC_SIZE:]

    # Ciphertext must be a multiple of block size
    if len(ciphertext) == 0 or len(ciphertext) % CIPHER_BLOCK_SIZE != 0:
        return None

    # Verify HMAC-SHA256 truncated to 2 bytes
    computed = hmac.new(secret, ciphertext, hashlib.sha256).digest()[:CIPHER_MAC_SIZE]
    if not hmac.compare_digest(mac, computed):
        return None

    # AES-128-ECB decrypt (key is first 16 bytes of secret)
    cipher = AES.new(secret[:CIPHER_KEY_SIZE], AES.MODE_ECB)
    plaintext = b""
    for offset in range(0, len(ciphertext), CIPHER_BLOCK_SIZE):
        block = ciphertext[offset:offset + CIPHER_BLOCK_SIZE]
        plaintext += cipher.decrypt(block)

    return plaintext


def encrypt_then_mac(secret: bytes, plaintext: bytes) -> bytes:
    """Python port of Utils::encryptThenMAC (for testing).

    AES-128-ECB encrypt with zero-padding, then HMAC-SHA256 MAC.

    Returns:
        [mac(2)][ciphertext...] bytes
    """
    # Pad plaintext to block boundary with zeros
    padded = plaintext
    remainder = len(padded) % CIPHER_BLOCK_SIZE
    if remainder != 0:
        padded = padded + b"\x00" * (CIPHER_BLOCK_SIZE - remainder)

    # AES-128-ECB encrypt
    cipher = AES.new(secret[:CIPHER_KEY_SIZE], AES.MODE_ECB)
    ciphertext = b""
    for offset in range(0, len(padded), CIPHER_BLOCK_SIZE):
        block = padded[offset:offset + CIPHER_BLOCK_SIZE]
        ciphertext += cipher.encrypt(block)

    # HMAC-SHA256 truncated to 2 bytes
    mac = hmac.new(secret, ciphertext, hashlib.sha256).digest()[:CIPHER_MAC_SIZE]

    return mac + ciphertext


@dataclass
class GroupMessage:
    """A decoded group channel message."""
    timestamp: int        # sender's RTC timestamp (uint32 LE)
    sender: str           # "name" from "name: message" format
    text: str             # message text
    channel_name: str     # channel name from config
    channel_hash: int     # 1-byte channel hash
    raw_timestamp: float  # received_at from frame


def try_decode_group_message(
    raw_packet: bytes,
    channels: list,
    received_at: float,
) -> Optional[GroupMessage]:
    """Try to decode a group message from a raw packet.

    Extracts the group payload, matches channel hash, tries decryption,
    and parses the plaintext.

    Args:
        raw_packet: Complete raw packet bytes
        channels: List of Channel objects to try
        received_at: Timestamp when the packet was received

    Returns:
        GroupMessage if successful, None otherwise.
    """
    extracted = extract_group_payload(raw_packet)
    if not extracted:
        return None

    if extracted["payload_type"] != PAYLOAD_TYPE_GRP_TXT:
        return None

    ch_hash = extracted["channel_hash"]
    mac_and_data = extracted["mac_and_data"]

    # Try each channel with matching hash
    for channel in channels:
        if channel.hash != ch_hash:
            continue

        plaintext = mac_then_decrypt(channel.secret, mac_and_data)
        if plaintext is None:
            continue

        # Parse decrypted GRP_TXT: [timestamp(4 LE)][flags(1)][sender: text\0]
        if len(plaintext) < 6:
            continue

        timestamp = struct.unpack("<I", plaintext[0:4])[0]
        flags = plaintext[4]
        if (flags >> 2) != 0:  # only plain text messages (type 0)
            continue

        # The rest is "sender: text" null-terminated (with possible zero padding)
        text_bytes = plaintext[5:]
        # Find null terminator
        null_idx = text_bytes.find(0)
        if null_idx >= 0:
            text_bytes = text_bytes[:null_idx]

        try:
            text_str = text_bytes.decode("utf-8")
        except UnicodeDecodeError:
            continue

        # Split "sender: message"
        colon_idx = text_str.find(": ")
        if colon_idx > 0:
            sender = text_str[:colon_idx]
            message = text_str[colon_idx + 2:]
        else:
            sender = "?"
            message = text_str

        return GroupMessage(
            timestamp=timestamp,
            sender=sender,
            text=message,
            channel_name=channel.name,
            channel_hash=channel.hash,
            raw_timestamp=received_at,
        )

    return None
