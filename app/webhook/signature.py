"""X-Hub-Signature-256 verification for Meta webhook payloads."""

import hashlib
import hmac

from fastapi import Request, HTTPException


async def verify_signature(request: Request, app_secret: str) -> bytes:
    """Verify the X-Hub-Signature-256 header and return the raw body.

    Meta sends an HMAC-SHA256 signature of the request body using the
    App Secret as the key.  We must validate every POST to /webhook.

    Raises HTTPException(403) when the signature is missing or invalid.
    """
    signature_header = request.headers.get("X-Hub-Signature-256", "")
    if not signature_header.startswith("sha256="):
        raise HTTPException(status_code=403, detail="Missing signature")

    expected_sig = signature_header[7:]  # strip "sha256=" prefix
    body = await request.body()
    computed_sig = hmac.new(
        app_secret.encode(), body, hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(computed_sig, expected_sig):
        raise HTTPException(status_code=403, detail="Invalid signature")

    return body
