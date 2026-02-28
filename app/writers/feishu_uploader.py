"""Feishu Drive file upload utilities for attaching media to Bitable records."""

import logging
import mimetypes
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

BASE_URL = "https://open.feishu.cn/open-apis"


async def upload_media_to_feishu(
    file_path: str,
    token: str,
    parent_type: str = "bitable_file",
    parent_node: str = "",
) -> str | None:
    """Upload a file to Feishu Drive using the upload_all endpoint.

    Suitable for files under 20MB. Uses multipart/form-data.

    Args:
        file_path: Local path to the file.
        token: Feishu tenant_access_token.
        parent_type: Drive parent type (default: "bitable_file").
        parent_node: Parent node token (the app_token of the Bitable).

    Returns file_token on success, None on failure.
    """
    path = Path(file_path)
    if not path.exists():
        logger.error("File not found for Feishu upload: %s", file_path)
        return None

    file_size = path.stat().st_size
    if file_size > 20 * 1024 * 1024:
        logger.warning("File too large for upload (>20MB): %s (%d bytes)", file_path, file_size)
        return None

    mime_type = mimetypes.guess_type(str(path))[0] or "application/octet-stream"

    url = f"{BASE_URL}/drive/v1/medias/upload_all"
    headers = {"Authorization": f"Bearer {token}"}

    async with httpx.AsyncClient(timeout=30) as client:
        try:
            with open(file_path, "rb") as f:
                resp = await client.post(
                    url,
                    headers=headers,
                    data={
                        "file_name": path.name,
                        "parent_type": parent_type,
                        "parent_node": parent_node,
                        "size": str(file_size),
                    },
                    files={"file": (path.name, f, mime_type)},
                )
        except httpx.RequestError as e:
            logger.error("Feishu upload request failed for %s: %s", file_path, e)
            return None

    data = resp.json()
    if data.get("code") != 0:
        logger.error(
            "Feishu upload error for %s: code=%s msg=%s",
            file_path, data.get("code"), data.get("msg"),
        )
        return None

    file_token = data.get("data", {}).get("file_token")
    logger.info("Feishu uploaded %s → file_token=%s", path.name, file_token)
    return file_token


async def upload_files_for_bitable(
    file_paths: list[str],
    token: str,
    app_token: str,
) -> list[dict]:
    """Upload multiple files and return Bitable attachment field value.

    Args:
        file_paths: List of local file paths.
        token: Feishu tenant_access_token.
        app_token: Feishu Bitable app_token (used as parent_node).

    Returns list of {"file_token": "xxx"} dicts for the attachment field.
    Skips files that fail to upload.
    """
    attachments = []
    for fp in file_paths:
        file_token = await upload_media_to_feishu(
            file_path=fp,
            token=token,
            parent_type="bitable_file",
            parent_node=app_token,
        )
        if file_token:
            attachments.append({"file_token": file_token})
    return attachments
