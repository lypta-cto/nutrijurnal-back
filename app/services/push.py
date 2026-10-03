"""
Sending a Web Push notification, and making the keys that sign one.

The browser gives us an endpoint on its push service (Google's for Chrome,
Mozilla's, Apple's) and two keys; pywebpush encrypts the message for that
browser and signs the request with our VAPID key. The endpoint is a URL the
browser handed us, so it is only ever accepted on the known push services —
otherwise "subscribe" would be a way to make the API send requests anywhere.
"""

from __future__ import annotations

import base64
import json
import logging
from typing import Literal
from urllib.parse import urlparse

from app.core.config import settings

logger = logging.getLogger(__name__)

# The push services browsers actually use, by host suffix
PUSH_HOSTS = (
    "fcm.googleapis.com",
    "android.googleapis.com",
    "push.services.mozilla.com",
    "push.apple.com",
    "notify.windows.com",
)

Outcome = Literal["sent", "gone", "failed"]


def is_push_endpoint(url: str) -> bool:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    return parsed.scheme == "https" and any(
        host == suffix or host.endswith(f".{suffix}") for suffix in PUSH_HOSTS
    )


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def new_vapid_keys() -> tuple[str, str]:
    """(public, private), both URL-safe base64 of the raw P-256 key — the
    public one is what the browser's `applicationServerKey` wants, the private
    one is what pywebpush signs with."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    key = ec.generate_private_key(ec.SECP256R1())
    private = key.private_numbers().private_value.to_bytes(32, "big")
    public = key.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    return _b64url(public), _b64url(private)


def send(endpoint: str, p256dh: str, auth: str, message: dict) -> Outcome:
    """One message to one browser. Blocking (pywebpush uses requests), so the
    scheduler runs it in a thread. "gone" means the browser unsubscribed or
    the subscription expired: the caller forgets it."""
    from pywebpush import WebPushException, webpush

    try:
        webpush(
            subscription_info={"endpoint": endpoint, "keys": {"p256dh": p256dh, "auth": auth}},
            data=json.dumps(message),
            vapid_private_key=settings.VAPID_PRIVATE_KEY,
            # A fresh dict each time: pywebpush fills in the audience per endpoint
            vapid_claims={"sub": settings.VAPID_SUBJECT},
            ttl=3600,
            timeout=10,
        )
    except WebPushException as error:
        status = error.response.status_code if error.response is not None else None
        if status in (404, 410):
            return "gone"
        logger.warning("Push to %s failed: %s", urlparse(endpoint).hostname, status)
        return "failed"
    except Exception:  # noqa: BLE001 — one bad browser must not stop the rest
        logger.warning("Push to %s failed", urlparse(endpoint).hostname, exc_info=True)
        return "failed"
    return "sent"
