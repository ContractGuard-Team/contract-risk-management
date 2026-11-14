#!/bin/sh
# Install the legal data pipeline on EC2 outside Docker: OS packages, own venv, env file, two systemd services.
#   sudo sh infrastructure/ec2/data_pipeline_setup.sh
# collector: SQS (EventBridge schedule) -> crawl -> originals to S3 AND the same bytes in memory -> OCR -> Supabase + Qdrant
# processor: retries jobs the collector could not finish (downloads again from the web, never S3) + daily refresh
set -eu
APP_DIR=$(cd "$(dirname "$0")/../.." && pwd)
VENV=/opt/contract-risk/data-pipeline/.venv
ENV_FILE="$APP_DIR/.env"  # the project's single env file (section 9)
SERVICE_USER=contract-data

[ "$(id -u)" -eq 0 ] || { echo 'Run as root (sudo).' >&2; exit 1; }

# Tesseract with the Vietnamese model: 2/3 of the source PDFs are scans.
# LibreOffice Writer (headless) converts .doc/.rtf/.odt attachments to PDF before extraction.
if command -v apt-get >/dev/null; then
  apt-get update && apt-get install -y --no-install-recommends python3 python3-venv tesseract-ocr tesseract-ocr-vie \
    libreoffice-writer-nogui fonts-dejavu-core
elif command -v dnf >/dev/null; then
  dnf install -y python3.12 tesseract tesseract-langpack-vie libreoffice-writer || {
    echo 'Install tesseract + Vietnamese data and LibreOffice Writer manually (not in this distro repo), then rerun.' >&2; exit 1; }
fi
tesseract --list-langs 2>/dev/null | grep -qx vie || { echo 'tesseract "vie" language data is missing.' >&2; exit 1; }
command -v soffice >/dev/null || { echo 'LibreOffice (soffice) is missing: .doc/.rtf attachments cannot be read.' >&2; exit 1; }

PYTHON=$(command -v python3.12 || command -v python3)
"$PYTHON" -c 'import sys; assert sys.version_info >= (3, 11), sys.version' || {
  echo 'Python >= 3.11 is required.' >&2; exit 1; }

id "$SERVICE_USER" >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin "$SERVICE_USER"
mkdir -p "$(dirname "$VENV")" /etc/contract-risk
[ -d "$VENV" ] || "$PYTHON" -m venv "$VENV"
"$VENV/bin/pip" install --upgrade pip
"$VENV/bin/pip" install -r "$APP_DIR/infrastructure/requirements/data_pipeline.txt"

if [ ! -f "$ENV_FILE" ]; then
  echo "Create $ENV_FILE first: cp .env.example .env (secrets stay empty: read from AWS Secrets Manager)." >&2
  exit 1
fi
[ -d "$APP_DIR/models" ] || echo "WARNING: $APP_DIR/models missing: run python scripts/download_models.py (HF_HUB_OFFLINE=1 blocks downloads)." >&2
# The service user only needs to read the env file; owner keeps write access.
chgrp "$SERVICE_USER" "$ENV_FILE" && chmod 640 "$ENV_FILE"

for unit in contract-data-collector contract-data-processor; do
  sed -e "s#@APP_DIR@#$APP_DIR#g" -e "s#@VENV@#$VENV#g" -e "s#@ENV_FILE@#$ENV_FILE#g" -e "s#@USER@#$SERVICE_USER#g" \
    "$APP_DIR/infrastructure/ec2/$unit.service" > "/etc/systemd/system/$unit.service"
done
systemctl daemon-reload
systemctl enable contract-data-collector contract-data-processor
echo 'Installed. After running data_pipeline/migrations/001+002 in Supabase (keys come from Secrets Manager):'
echo '  sudo systemctl start contract-data-processor contract-data-collector'
echo '  journalctl -u contract-data-collector -f'
