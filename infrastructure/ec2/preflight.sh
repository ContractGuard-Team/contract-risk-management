#!/bin/sh
# Prove that containers (bridge network) get AWS credentials: IAM role + IMDSv2 hop limit + egress + region.
# A hop limit of 1 lets the host reach IMDS but not containers; fix with:
#   aws ec2 modify-instance-metadata-options --instance-id <id> --http-tokens required --http-put-response-hop-limit 2
set -eu
cd "$(dirname "$0")/../.."
docker compose run --rm --no-deps -T worker python -c "
import boto3, sys
try:
    arn = boto3.client('sts').get_caller_identity()['Arn']
except Exception as exc:
    sys.exit('AWS check failed: ' + type(exc).__name__)
print('AWS identity from container:', arn.split(':')[-1])
" || { echo 'Containers cannot obtain AWS credentials: check the instance role, IMDS hop limit (>=2), AWS_REGION and egress.' >&2; exit 1; }
# The instance role must let the mcp container list/read the reference bucket (read-only connector, 1 key).
docker compose run --rm --no-deps -T mcp python -c "
import os, sys
from mcp_server.s3_reader import S3ReadOnly
if not os.getenv('MCP_S3_BUCKETS'):
    sys.exit(print('MCP S3: MCP_S3_BUCKETS not set, skipped'))
reader = S3ReadOnly()
for bucket in reader.buckets:
    try:
        page = reader.list_objects(bucket, max_keys=1)
    except Exception as exc:
        code = getattr(exc, 'response', {}).get('Error', {}).get('Code', '')
        sys.exit(f'MCP S3 read check failed for {bucket}: {type(exc).__name__} {code}')
    print('MCP S3 read-only access OK:', bucket, '| first page objects:', len(page['objects']))
" || { echo 'Attach infrastructure/aws/mcp-s3-readonly-policy.json to the instance role (plus kms:Decrypt for SSE-KMS).' >&2; exit 1; }
