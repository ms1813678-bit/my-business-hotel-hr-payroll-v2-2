"""Strict single-origin boundary used only by the opt-in HTTPS service."""
from urllib.parse import urlsplit


class DesktopOriginGuard:
    def __init__(self, app, origin: str):
        self.app = app
        self.origin = origin
        self.authority = urlsplit(origin).netloc.encode("ascii")

    async def __call__(self, scope, receive, send):
        if scope["type"] not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return
        headers = scope.get("headers", [])
        hosts = [v for k, v in headers if k.lower() == b"host"]
        origins = [v for k, v in headers if k.lower() == b"origin"]
        referers = [v for k, v in headers if k.lower() == b"referer"]
        valid = scope.get("scheme") in {"https", "wss"} and hosts == [self.authority]
        if origins:
            valid = valid and origins == [self.origin.encode("ascii")]
        # Legacy form navigation may have Referer but no Origin. Never accept
        # an unsafe request lacking both, even when SameSite sends cookies.
        if (scope.get("method", "GET") not in {"GET", "HEAD", "OPTIONS"}
                or scope["type"] == "websocket") and not origins:
            valid = valid and len(referers) == 1
            if valid:
                try:
                    ref = urlsplit(referers[0].decode("ascii"))
                    valid = f"{ref.scheme}://{ref.netloc}" == self.origin and not ref.username
                except (UnicodeError, ValueError):
                    valid = False
        if not valid:
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
            else:
                await send({"type": "http.response.start", "status": 403,
                            "headers": [(b"content-type", b"text/plain; charset=utf-8"),
                                        (b"cache-control", b"no-store")]})
                await send({"type": "http.response.body", "body": b"Request origin rejected."})
            return
        async def private_response(event):
            if event["type"] == "http.response.start":
                # No browser caching of HR responses. This does not erase
                # downloads/cookies/existing profiles or prove WebView state.
                headers = [(key, value) for key, value in event.get("headers", [])
                           if key.lower() not in {b"cache-control", b"pragma", b"expires"}]
                event = {**event, "headers": [*headers,
                    (b"cache-control", b"no-store, private"),
                    (b"pragma", b"no-cache"), (b"expires", b"0")]}
            await send(event)
        await self.app(scope, receive, private_response)
