"""Two-table DynamoDB transport; uses the SDK credential chain (EC2 IAM role)."""
from decimal import Decimal
from threading import local

from boto3.dynamodb.types import TypeDeserializer, TypeSerializer
from botocore.config import Config


def native(value):
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, dict):
        return {key: native(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [native(item) for item in value]
    return value


class DynamoTables:
    def __init__(self, jobs_table, events_table, region="ap-southeast-1", *, client=None):
        if not jobs_table or not events_table or jobs_table == events_table:
            raise ValueError("Configure distinct jobs and events DynamoDB tables")
        self.jobs_table, self.events_table, self.region = jobs_table, events_table, region
        self._client, self._local = client, local()

    @property
    def client(self):
        if self._client is not None:
            return self._client
        if not hasattr(self._local, "client"):
            import boto3
            self._local.client = boto3.client("dynamodb", region_name=self.region, config=Config(
                connect_timeout=5, read_timeout=15, retries={"mode": "standard", "total_max_attempts": 3}))
        return self._local.client

    @staticmethod
    def encode(values):
        serializer = TypeSerializer()
        return {key: serializer.serialize(native(value)) for key, value in values.items()}

    @staticmethod
    def decode(values):
        deserializer = TypeDeserializer()
        return {key: deserializer.deserialize(value) for key, value in values.items()}

    def get(self, table, key):
        item = self.client.get_item(TableName=table, Key=self.encode(key), ConsistentRead=True).get("Item")
        return self.decode(item) if item else None

    def transact(self, operations):
        encoded = []
        for operation in operations:
            kind, arguments = next(iter(operation.items()))
            arguments = dict(arguments)
            for key in ("Item", "Key", "ExpressionAttributeValues"):
                if key in arguments:
                    arguments[key] = self.encode(arguments[key])
            encoded.append({kind: arguments})
        self.client.transact_write_items(TransactItems=encoded)

    def event(self, job_id, event_id):
        return self.get(self.events_table, {"job_id": job_id, "event_id": event_id})

    def event_put(self, item):
        return {"Put": {"TableName": self.events_table, "Item": item,
                        "ConditionExpression": "attribute_not_exists(job_id) AND attribute_not_exists(event_id)"}}

    def validate_schema(self):
        for table, expected in ((self.jobs_table, [("job_id", "HASH")]),
                                (self.events_table, [("job_id", "HASH"), ("event_id", "RANGE")])):
            description = self.client.describe_table(TableName=table)["Table"]
            actual = {(key["AttributeName"], key["KeyType"]) for key in description["KeySchema"]}
            types = {field["AttributeName"]: field["AttributeType"] for field in description["AttributeDefinitions"]}
            if actual != set(expected) or any(types[name] != "S" for name, _ in expected):
                raise ValueError(f"DynamoDB key schema does not match the worker contract: {table}")
        return {"region": self.region, "jobs_table": self.jobs_table, "events_table": self.events_table,
                "schema_valid": True}
