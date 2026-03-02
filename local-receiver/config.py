"""Local Obsidian receiver configuration."""

from pydantic_settings import BaseSettings
from pathlib import Path


class ReceiverSettings(BaseSettings):
    # Shared secret for HMAC-SHA256 verification (must match server)
    sync_secret: str = ""

    # WATI API token — used to authenticate media downloads
    wati_api_token: str = ""

    # Gemini API key — used for audio transcription
    gemini_api_key: str = ""

    # Obsidian vault CRM path
    crm_base_path: str = str(
        Path.home()
        / "Nutstore Files"
        / "我的坚果云"
        / "LuckyOS"
        / "LOCA-Factory-Brain"
        / "05-Sales Library"
        / "CRM"
    )

    # Phone-to-folder mapping file
    mapping_file: str = "data/phone_to_folder.json"

    # Server
    host: str = "127.0.0.1"
    port: int = 8765

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


settings = ReceiverSettings()
