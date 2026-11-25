"""HOnY window only. It never starts or stops the backend service."""
import locale
import os
import urllib.request
from pathlib import Path

from app.desktop_config import DesktopConfig
from app.desktop_server import config_path


def check_backend(origin: str) -> None:
    # Standard CA validation and hostname checking. No IP/localhost fallback.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(origin + "/health", timeout=8) as response:
        if response.status != 200 or response.url != origin + "/health":
            raise RuntimeError("Backend health check failed.")


def run_window(origin: str) -> None:
    import webview
    check_backend(origin)
    webview.settings["IGNORE_SSL_ERRORS"] = False
    webview.settings["ALLOW_DOWNLOADS"] = True
    # No Python-to-JavaScript API bridge and no local HTTP server.
    window = webview.create_window("HOnY", None, width=1280, height=850,
                          min_size=(900, 600), background_color="#F5F3EE")
    state = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "HOnY" / "WebView2"
    state.mkdir(parents=True, exist_ok=True)
    from app.desktop_webview_guard import WebView2OriginGuard
    guard = WebView2OriginGuard(origin)
    webview.start(func=guard.start, args=(window,), gui="edgechromium", debug=False,
                  private_mode=False, storage_path=str(state))


def startup_failure_message(language: str) -> str:
    messages = {
        "ar": "تعذر الاتصال بخدمة HOnY الآمنة. تحقق من تشغيل الخدمة والشهادة واتصال الشبكة.",
        "en": "Cannot connect to the secure HOnY service. Check the service, certificate and network.",
        "zh": "无法连接到安全的 HOnY 服务。请检查服务、证书和网络。",
    }
    return messages.get(language, messages["ar"])


def main():
    try:
        run_window(DesktopConfig.load_origin(config_path()))
    except Exception:  # noqa: BLE001 - show one secret-free native startup failure
        # One language per dialog; do not mix Arabic/English/Chinese UI copy.
        import ctypes
        language = (locale.getlocale()[0] or "ar").split("_")[0]
        ctypes.windll.user32.MessageBoxW(None, startup_failure_message(language), "HOnY", 0x10)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
