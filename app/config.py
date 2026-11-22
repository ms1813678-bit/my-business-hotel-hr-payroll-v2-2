from pydantic import model_validator
from sqlalchemy.engine import make_url
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "M.Y. Business Hotel HR"
    app_env: str = "development"
    # Interactive API docs (/docs, /openapi.json) stay off unless the
    # administrator explicitly enables them for troubleshooting.
    app_expose_docs: bool = False
    app_host: str = "127.0.0.1"
    app_port: int = 8000
    database_url: str
    session_secret: str
    # Optional HTTP endpoint that receives login alert events (lockout and
    # successful sign-in) as JSON POSTs, e.g. a WhatsApp forwarding gateway.
    login_alert_webhook_url: str = ""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @model_validator(mode="after")
    def validate_security_settings(self):
        url = self.database_url.strip()
        if not url or "change_me" in url.lower():
            raise ValueError("DATABASE_URL must be explicitly configured; CHANGE_ME placeholders are forbidden.")
        if not url.startswith("postgresql+psycopg://"):
            raise ValueError("DATABASE_URL must use the installed PostgreSQL psycopg driver.")
        try:
            database_host = make_url(url).host
        except Exception as exc:
            raise ValueError("DATABASE_URL is not a valid PostgreSQL connection URL.") from exc
        if (database_host or "").casefold() not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError(
                "DATABASE_URL must connect to PostgreSQL on this computer only "
                "(localhost, 127.0.0.1, or ::1)."
            )
        self.database_url = url
        secret = self.session_secret.strip()
        if len(secret) < 32 or secret == "CHANGE_THIS_TO_A_LONG_RANDOM_SECRET" or secret.lower().startswith("change_me"):
            raise ValueError("SESSION_SECRET must be a unique random value of at least 32 characters.")
        self.session_secret = secret
        return self


settings = Settings()
