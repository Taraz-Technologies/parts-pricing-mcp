import logging
import re

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    digikey_client_id: str = ""
    digikey_client_secret: SecretStr = SecretStr("")
    digikey_api_base: str = "https://api.digikey.com"
    digikey_site: str = "US"
    digikey_language: str = "en"
    digikey_currency: str = "USD"
    digikey_daily_limit: int = 1000
    digikey_per_minute: int = 100
    digikey_concurrency: int = 4

    mouser_search_api_key: SecretStr = SecretStr("")
    mouser_api_base: str = "https://api.mouser.com"
    mouser_daily_limit: int = 1000
    mouser_per_minute: int = 25
    mouser_batch_size: int = 10

    quota_reserve: int = 50
    cache_ttl_hours: float = 12
    notfound_ttl_hours: float = 24
    search_ttl_hours: float = 6
    max_wait_seconds: float = 15
    request_timeout_seconds: float = 20
    bom_max_lines: int = 300
    data_dir: str = "./data"
    console_api_keys: SecretStr = SecretStr("")  # "planner:KEY,prc:KEY,mcp:KEY"
    log_level: str = "INFO"

    def console_keys(self) -> dict[str, str]:
        """key -> client name."""
        out = {}
        for pair in self.console_api_keys.get_secret_value().split(","):
            name, _, key = pair.strip().partition(":")
            if name and key:
                out[key.strip()] = name.strip()
        return out


_API_KEY_RX = re.compile(r"apiKey=[^&\s\"']+")


def redact(text: str) -> str:
    return _API_KEY_RX.sub("apiKey=***", text)


class RedactFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        if "apiKey=" in msg:
            record.msg, record.args = redact(msg), None
        return True


_filter = RedactFilter()
_base_factory = logging.getLogRecordFactory()


def _redacting_factory(*args, **kwargs):
    record = _base_factory(*args, **kwargs)
    _filter.filter(record)
    return record


def setup_logging(level: str = "INFO") -> None:
    # A filter on the root logger never sees records propagated from child loggers
    # (httpx, uvicorn, ours), so redaction runs at record creation for every logger.
    logging.setLogRecordFactory(_redacting_factory)
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger().addFilter(_filter)
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)
