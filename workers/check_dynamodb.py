"""Read-only EC2 preflight: python -m workers.check_dynamodb."""
import json
import os
from pathlib import Path
from dotenv import dotenv_values
from workers.connectors.dynamodb_tables import DynamoTables


def main():
    for key, value in dotenv_values(Path(__file__).resolve().parents[1] / ".env").items():
        if value and value.strip():
            os.environ.setdefault(key, value)
    for key in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_PROFILE", "AWS_ENDPOINT_URL"):
        if key in os.environ and not os.environ[key].strip():
            del os.environ[key]
    tables = DynamoTables(os.getenv("WORKER_DYNAMODB_TABLE", "contract-risk-jobs"),
        os.getenv("WORKER_DYNAMODB_EVENTS_TABLE", "contract-risk-job-events"), os.getenv("AWS_REGION", "ap-southeast-1"))
    try:
        result = tables.validate_schema()
    except Exception as exc:
        print(json.dumps({"schema_valid": False, "error_type": type(exc).__name__,
            "error_code": getattr(exc, "response", {}).get("Error", {}).get("Code")}))
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
