"""Container readiness probe; credentials and response bodies are never printed."""
import os
from urllib.request import urlopen


def main():
    try:
        with urlopen("http://127.0.0.1:" + os.getenv("PORT", "8000") + "/readyz", timeout=3) as response:
            return 0 if response.status == 200 else 1
    except Exception:
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
