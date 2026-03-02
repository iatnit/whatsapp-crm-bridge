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

    # --- Webhook ---
    webhook_secret: str = ""  # if set, require ?token= on webhook URL
    admin_token: str = ""     # if set, require Authorization: Bearer <token> on admin endpoints

    # --- Feishu ---
    feishu_app_id: str = ""
    feishu_app_secret: str = ""

    # Feishu Bitable IDs
    feishu_app_token: str = ""
    feishu_table_customers: str = ""
    feishu_table_followup: str = ""

    # --- Feishu Bot ---
    feishu_bot_enabled: bool = False
    feishu_bot_allowed_users: str = ""          # comma-separated open_id list
    feishu_bot_verification_token: str = ""
    feishu_bot_reply_webhook: str = ""      # group bot webhook for replies

    # --- HubSpot ---
    hubspot_access_token: str = ""   # Private App access token
    hubspot_enabled: bool = False    # 主开关

    # --- Auto Reply ---
    auto_reply_enabled: bool = True
    auto_reply_cooldown: int = 30          # seconds between replies to same customer
    auto_reply_max_per_hour: int = 10      # max replies per customer per hour
    auto_reply_context_messages: int = 20  # recent messages loaded as context
    auto_reply_max_tokens: int = 500       # max output tokens per reply
    auto_reply_delay: int = 60             # seconds to wait before replying (give Lucky time to respond)
    auto_reply_human_pause: int = 1800     # seconds to pause AI after human replies (default 30min)
    knowledge_base_path: str = "data/knowledge_base.md"

    # --- Obsidian Sync ---
    obsidian_sync_url: str = ""          # e.g. https://obsidian-sync.zhangyun.xyz
    obsidian_sync_secret: str = ""       # shared HMAC-SHA256 secret
    obsidian_sync_enabled: bool = False

    # --- Notion ---
    notion_token: str = ""             # Notion Integration Secret (ntn_xxx)
    notion_report_db_id: str = ""      # Database ID for daily CEO report

    # --- Notifications ---
    feishu_webhook_url: str = ""       # Feishu group bot webhook for daily reminders
    reminder_hour: int = 9             # daily reminder time (CST hour, 0-23)
    reminder_minute: int = 0

    # --- App ---
    log_level: str = "INFO"
    daily_analysis_hour: int = 23
    daily_analysis_minute: int = 0
    pipeline_interval_hours: int = 1   # run pipeline every N hours (0 = disabled, use daily cron only)
    pipeline_concurrency: int = 3      # max concurrent conversation analyses

    # --- Paths ---
    data_dir: Path = Path("data")
    db_path: Path = Path("data/whatsapp.db")
    media_dir: Path = Path("data/media")
    customers_json: Path = Path("data/crm_customers.json")

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


settings = Settings()
