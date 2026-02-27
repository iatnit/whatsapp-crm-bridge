"""Application configuration loaded from environment variables."""

from pydantic_settings import BaseSettings
from pathlib import Path


class Settings(BaseSettings):
    # --- WATI ---
    wati_api_endpoint: str = ""   # e.g. https://live-mt-server.wati.io
    wati_tenant_id: str = ""      # tenant ID for V1 API paths
    wati_api_token: str = ""      # Bearer token from WATI dashboard

    @property
    def wati_v1_url(self) -> str:
        """V1 API base: {endpoint}/{tenant_id}"""
        return f"{self.wati_api_endpoint.rstrip('/')}/{self.wati_tenant_id}"

    @property
    def wati_v3_url(self) -> str:
        """V3 API base: {endpoint}"""
        return self.wati_api_endpoint.rstrip("/")

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

    # --- Auto Reply ---
    auto_reply_enabled: bool = True
    auto_reply_cooldown: int = 30          # seconds between replies to same customer
    auto_reply_max_per_hour: int = 10      # max replies per customer per hour
    auto_reply_context_messages: int = 20  # recent messages loaded as context
    auto_reply_max_tokens: int = 500       # max output tokens per reply
    auto_reply_delay: int = 5              # seconds to wait before replying (anti-dup with KnowBot)
    knowledge_base_path: str = "data/knowledge_base.md"

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
