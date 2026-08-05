#!/usr/bin/env python3
"""Generate a fresh VAPID keypair for Web Push and print it as ready-to-paste .env lines.

Run once per deployment (not per-user — the same keypair identifies this server to every
push service for every subscriber):

    uv run --python 3.12 --with pywebpush python scripts/generate_vapid_keys.py

Paste the output into .env as VAPID_PRIVATE_KEY / VAPID_PUBLIC_KEY (see .env.example).
The private key must stay server-side only; the public key is served to the frontend via
GET /api/push/vapid-public-key (backend/routers/notifications.py) for
`pushManager.subscribe({applicationServerKey: ...})`.
"""
import base64

from py_vapid import Vapid02
from cryptography.hazmat.primitives import serialization


def main() -> None:
    v = Vapid02()
    v.generate_keys()

    # Raw (not PEM/DER) base64url-encoded keys — what pywebpush's webpush() accepts
    # directly as `vapid_private_key`, and what the browser's PushManager.subscribe()
    # expects as `applicationServerKey`.
    priv_raw = v.private_key.private_numbers().private_value.to_bytes(32, "big")
    pub_raw = v.public_key.public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )

    priv_b64 = base64.urlsafe_b64encode(priv_raw).decode().rstrip("=")
    pub_b64 = base64.urlsafe_b64encode(pub_raw).decode().rstrip("=")

    print("# Generated VAPID keypair — paste into .env (see .env.example), then discard")
    print("# this output. The private key must never be committed or shared.")
    print(f"VAPID_PRIVATE_KEY={priv_b64}")
    print(f"VAPID_PUBLIC_KEY={pub_b64}")


if __name__ == "__main__":
    main()
