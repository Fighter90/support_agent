"""Настройки приложения. Секреты читаются только из окружения или файла .env (в репозиторий не попадают).

Схема настроек перенесена из собственной наработки автора — репозитория methodAgent (GPL-3.0).
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT_DIR = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=('.env', str(ROOT_DIR / '.env')), env_file_encoding='utf-8', extra='ignore')

    environment: str = 'development'
    data_dir: Path = ROOT_DIR / 'data'
    feed_url: str = 'https://store.softline.ru/feeds/Mindbox.yml.gz'
    public_url: str = 'https://methodagent.ru'

    # --- LLM: gigachat | yandex | mock ---
    llm_provider: str = 'gigachat'
    llm_fallback_provider: str = 'yandex'
    gigachat_auth_key: str = ''          # Authorization key (Basic) из личного кабинета Sber Developers
    gigachat_scope: str = 'GIGACHAT_API_B2B'
    gigachat_model: str = 'GigaChat-2-Max'
    gigachat_oauth_url: str = 'https://ngw.devices.sberbank.ru:9443/api/v2/oauth'
    gigachat_api_url: str = 'https://gigachat.devices.sberbank.ru/api/v1'
    gigachat_verify_ssl: bool = True     # в Docker-образ ставится сертификат НУЦ Минцифры
    yandex_api_key: str = ''
    yandex_folder_id: str = 'b1gh8uv9d9ur7e4kmvvi'
    yandex_model: str = 'yandexgpt/latest'
    yandex_api_url: str = 'https://llm.api.cloud.yandex.net/v1'
    llm_timeout_s: float = 60.0
    # Тарифы, ₽ за 1000 токенов (для учёта стоимости запроса; уточняются в docs/cost.xlsx)
    gigachat_price_per_1k: float = 1.95
    yandex_price_per_1k: float = 1.2

    # --- Кабинет менеджера ---
    secret_key: str = 'dev-secret-change-me-please-32chars'
    manager_login: str = 'manager'
    manager_password: str = 'softline2026'
    jury_login: str = 'jury'
    jury_password: str = 'ikuznica2026'
    token_ttl_hours: int = 72

    # --- Агент ---
    max_agent_steps: int = 6
    history_messages: int = 16

    @property
    def catalog_db(self) -> Path:
        return self.data_dir / 'catalog.db'

    @property
    def app_db(self) -> Path:
        return self.data_dir / 'app.db'

    @property
    def quotes_dir(self) -> Path:
        return self.data_dir / 'quotes'


@lru_cache
def get_settings() -> Settings:
    return Settings()
