"""
Launcher. Run: python run.py
Default binds to 127.0.0.1. Set HOST=0.0.0.0 to listen on all interfaces
(requires AUTH_USERNAME + AUTH_PASSWORD).
"""
import os
import sys
import uvicorn


def _is_loopback(host: str) -> bool:
    h = (host or "").strip().lower()
    return h in ("127.0.0.1", "localhost", "::1")


if __name__ == "__main__":
    host = os.environ.get("HOST") or os.environ.get("APP_HOST") or "127.0.0.1"
    port = int(os.environ.get("APP_PORT", os.environ.get("PORT", "8000")))
    user = (os.environ.get("AUTH_USERNAME") or "").strip()
    pwd = (os.environ.get("AUTH_PASSWORD") or "").strip()
    if not _is_loopback(host) and not (user and pwd):
        print(
            "Refusing to bind to non-loopback host without auth. "
            "Set AUTH_USERNAME and AUTH_PASSWORD, or use HOST=127.0.0.1.",
            file=sys.stderr,
        )
        sys.exit(1)
    uvicorn.run("backend.main:app", host=host, port=port, reload=False)
