"""Create a local root .env from .env.example (same layout) without displaying or overwriting secrets."""
from pathlib import Path
import re
import secrets


def main():
    root = Path(__file__).resolve().parents[1]
    destination = root / ".env"
    if destination.exists():
        raise SystemExit(".env already exists; preserve its values and update from .env.example manually.")
    local = {"APP_ENV": "local", "MODEL_PROVIDER": "fixture", "WORKER_ENABLED": "false",
             "LANGFUSE_ENABLED": "false"}
    # API keys are never written here: services fetch them from Secrets Manager via the Config service,
    # so even a local run needs AWS credentials that can read API_KEYS_SECRET_ID.
    lines = ["# LOCAL root .env (gitignored, never commit). Fixture model, worker idle. No API keys in this file."]
    for line in (root / ".env.example").read_text(encoding="utf-8").splitlines():
        match = re.match(r"^([A-Z0-9_]+)=(.*)$", line)
        if match:
            key, value = match.groups()
            if key.startswith("CONFIG_TOKEN_"):
                value = secrets.token_urlsafe(32)
            line = f"{key}={local.get(key, value)}"
        lines.append(line)
    with destination.open("x", encoding="utf-8") as stream:
        stream.write("\n".join(lines) + "\n")
    print("Created ignored root .env with unique local tokens; fixture model enabled, SQS polling disabled.")


if __name__ == "__main__":
    main()
