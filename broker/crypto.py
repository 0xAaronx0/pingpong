"""Signature verification for pingpong broker requests.

See docs/PROTOCOL.md §1.1. The broker only ever *verifies* Ed25519 signatures;
it never holds private keys and never touches the X25519 sealed contact blobs.
"""
from __future__ import annotations

import base64
import hashlib

from nacl.exceptions import BadSignatureError
from nacl.signing import VerifyKey


def b64url_decode(s: str) -> bytes:
    """Decode unpadded base64url (callers may or may not include padding)."""
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def canonical(method: str, path: str, body: bytes, timestamp: str, nonce: str) -> bytes:
    body_hash = hashlib.sha256(body).hexdigest()
    return f"{method}\n{path}\n{body_hash}\n{timestamp}\n{nonce}".encode()


def verify_raw(agent_id: str, signature_b64: str, message: bytes) -> bool:
    """Verify an Ed25519 signature by `agent_id` over arbitrary canonical bytes
    (used for the data signatures of PROTOCOL §1.2)."""
    try:
        VerifyKey(b64url_decode(agent_id)).verify(message, b64url_decode(signature_b64))
        return True
    except (BadSignatureError, ValueError, TypeError):
        return False


def verify(
    agent_id: str,
    signature_b64: str,
    method: str,
    path: str,
    body: bytes,
    timestamp: str,
    nonce: str,
) -> bool:
    """True iff `signature_b64` is a valid Ed25519 signature by `agent_id`
    over the canonical request string. `agent_id` is base64url(ed25519_pub)."""
    try:
        vk = VerifyKey(b64url_decode(agent_id))
        vk.verify(canonical(method, path, body, timestamp, nonce), b64url_decode(signature_b64))
        return True
    except (BadSignatureError, ValueError, TypeError):
        return False
