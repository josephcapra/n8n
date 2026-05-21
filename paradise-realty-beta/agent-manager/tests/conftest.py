"""Shared test fixtures."""

from __future__ import annotations

import base64

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from agentmgr.state_store import InMemoryStateStore


@pytest.fixture
def store() -> InMemoryStateStore:
    return InMemoryStateStore()


@pytest.fixture
def keypair() -> tuple[Ed25519PrivateKey, str]:
    """Return (private_key, public_key_b64) — a fresh approval key pair."""
    private_key = Ed25519PrivateKey.generate()
    public_b64 = base64.b64encode(
        private_key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
    ).decode()
    return private_key, public_b64
