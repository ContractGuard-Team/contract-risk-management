"""DynamoDB document transport; provision the table separately (PK: job_id, string)."""
from decimal import Decimal
import json
from threading import local


class DynamoDBClient:
    def __init__(self, table_name, *, table=None, region=None):
        self.table_name, self._table, self.region = table_name, table, region
        self._local = local()

    def _connect(self):
        if self._table is not None:
            return self._table
        if not hasattr(self._local, "table"):
            import boto3
            from botocore.config import Config
            # boto3 resource objects are not thread-safe; the heartbeat gets its own resource.
            self._local.table = boto3.resource("dynamodb", region_name=self.region,
                config=Config(connect_timeout=10, read_timeout=30,
                              retries={"mode": "standard", "total_max_attempts": 3})).Table(self.table_name)
        return self._local.table

    @staticmethod
    def _values(values):
        return json.loads(json.dumps(values, allow_nan=False), parse_float=Decimal)

    def get(self, job_id):
        return self._connect().get_item(Key={"job_id": job_id}, ConsistentRead=True).get("Item")

    def put(self, item):
        self._connect().put_item(Item=self._values(item), ConditionExpression="attribute_not_exists(job_id)")

    def update(self, job_id, *, expression, condition, names, values):
        return self._connect().update_item(Key={"job_id": job_id}, UpdateExpression=expression,
            ConditionExpression=condition, ExpressionAttributeNames=names,
            ExpressionAttributeValues=self._values(values), ReturnValues="ALL_NEW")["Attributes"]
