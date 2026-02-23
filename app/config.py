"""Application configuration loaded from environment variables."""

from pydantic_settings import BaseSettings
from pathlib import Path


class Settings(BaseSettings):
    # --- Meta / WhatsApp ---
    meta_verify_token: str = ""
    meta_app_secret: str = ""
    whatsapp_access_token: str = ""
    whatsapp_phone_number_id: str = ""

    # --- LLM ---
    anthropic_api_key: str = ""
    gemini_api_key: str = ""
    llm_provider: str = "gemini"  # "anthropic" or "gemini"

    # --- Feishu ---
    feishu_app_id: str = "cli_a9f0a37109b81cc6"
    feishu_app_secret: str = ""

    # Feishu Bitable IDs
    feishu_app_token: str = "XYeCby15ga5CDKsX57YcFL1Hnce"
    feishu_table_customers: str = "tbl4kQe0MeodIGGD"
    feishu_table_followup: str = "tblcftbYX7E0cEUo"

    # --- App ---
    log_level: str = "INFO"
    daily_analysis_hour: int = 23
    daily_analysis_minute: int = 0

    # --- Paths ---
    data_dir: Path = Path("data")
    db_path: Path = Path("data/whatsapp.db")
    media_dir: Path = Path("data/media")
    customers_json: Path = Path("data/crm_customers.json")

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


settings = Settings()
