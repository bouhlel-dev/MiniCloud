"""Generate SSH key pairs in the formats the OpenSSH client expects.

Only the fingerprint is calculated here: the key material itself is handed to
cryptography's OpenSSH serialiser, so the private half arrives in the same
``openssh-key-v1`` container that ``ssh-keygen`` writes.
"""

import base64
import binascii
import hashlib
import struct

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)

KEY_TYPE = "ssh-ed25519"


def _field(payload: bytes) -> bytes:
    return struct.pack(">I", len(payload)) + payload


def public_blob(public_key: bytes) -> bytes:
    """The wire encoding shared by authorized_keys and ssh-keygen fingerprints."""
    return _field(KEY_TYPE.encode("ascii")) + _field(public_key)


def fingerprint_blob(blob: bytes) -> str:
    return f"SHA256:{base64.b64encode(hashlib.sha256(blob).digest()).decode('ascii').rstrip('=')}"


def fingerprint(public_key: bytes) -> str:
    return fingerprint_blob(public_blob(public_key))


def fingerprint_of(public_key_line: str) -> str:
    """The ssh-keygen fingerprint of an authorized_keys line, or an empty string."""
    try:
        blob = base64.b64decode(public_key_line.split(" ")[1], validate=True)
    except (binascii.Error, IndexError, ValueError):
        return ""
    return fingerprint_blob(blob)


def generate_key_pair(label: str) -> dict:
    """Return a fresh ed25519 pair; the caller shows the private key and never stores it."""
    key = Ed25519PrivateKey.generate()
    comment = " ".join((label or "").split())
    private_key = key.private_bytes(
        encoding=Encoding.PEM,
        format=PrivateFormat.OpenSSH,
        encryption_algorithm=NoEncryption(),
    ).decode("ascii")
    public_line = key.public_key().public_bytes(encoding=Encoding.OpenSSH, format=PublicFormat.OpenSSH).decode("ascii")
    if comment:
        public_line = f"{public_line} {comment}"
    raw_public = key.public_key().public_bytes(encoding=Encoding.Raw, format=PublicFormat.Raw)
    return {
        "public_key": public_line,
        "private_key": private_key,
        "fingerprint": fingerprint(raw_public),
    }
