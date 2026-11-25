"""HTTPS backend runner. Importing this file never starts a server."""
import os
from pathlib import Path
import ssl

from app.desktop_config import DesktopConfig
from app.desktop_security import DesktopOriginGuard


def config_path() -> Path:
    return Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / "HOnY" / "desktop.json"


def create_server(config: DesktopConfig):
    from app.desktop_certificate import validate_certificate
    validate_certificate(config, min_remaining_days=0)
    os.chdir(config.project_root)
    # Must be set before importing app.config, whose Settings reads .env once.
    os.environ["APP_ENV"] = "production"
    import uvicorn
    from app.main import app
    from app.config import settings

    if settings.app_env != "production":
        raise RuntimeError("Desktop service requires a fresh production-settings process.")

    return uvicorn.Server(uvicorn.Config(
        DesktopOriginGuard(app, config.origin), host=config.bind_ipv4, port=config.port,
        ssl_certfile=str(config.certificate), ssl_keyfile=str(config.private_key),
        ssl_version=ssl.PROTOCOL_TLS_SERVER, ssl_ciphers="TLSv1.2:!aNULL:!eNULL",
        proxy_headers=False, forwarded_allow_ips="", server_header=False,
        access_log=False, log_level="warning", timeout_graceful_shutdown=25,
    ))


def main():
    create_server(DesktopConfig.load(config_path())).run()


if __name__ == "__main__":
    main()
