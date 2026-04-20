"""Shared test fixtures for Hospitality HQ tests."""

import os
import sys
import pytest
import boto3
from moto import mock_aws

# Add src to path so tests can import modules
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

# Set test environment variables before importing config
os.environ["SSM_PREFIX"] = "/hospitality-hq-test"
os.environ["SLACK_CHANNEL_ID"] = "C_TEST_CHANNEL"
os.environ["PROPERTY_UUIDS"] = "test-prop-uuid-1,test-prop-uuid-2"
os.environ["DYNAMODB_TABLE"] = "hospitality-hq-messages-test"
os.environ["THREAD_MAPPING_TABLE"] = "hospitality-hq-thread-mapping-test"
os.environ["THREAD_LOGS_TABLE"] = "hospitality-hq-thread-logs-test"
os.environ["TASKS_BUCKET"] = "hospitality-hq-tasks-test"
os.environ["TASKS_CHANNEL_ID"] = "C_TEST_TASKS"
os.environ["EXPENSES_BUCKET"] = "hospitality-hq-expenses-test"
os.environ["EXPENSES_CHANNEL_ID"] = "C_TEST_EXPENSES"
os.environ["AWS_DEFAULT_REGION"] = "us-east-1"
os.environ["AWS_ACCESS_KEY_ID"] = "testing"
os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"


@pytest.fixture
def ssm_with_secrets():
    """Create mock SSM parameters with test secrets."""
    with mock_aws():
        client = boto3.client("ssm", region_name="us-east-1")
        prefix = "/hospitality-hq-test"

        client.put_parameter(
            Name=f"{prefix}/hospitable-api-token",
            Value="test-hospitable-token",
            Type="SecureString",
        )
        client.put_parameter(
            Name=f"{prefix}/anthropic-api-key",
            Value="test-anthropic-key",
            Type="SecureString",
        )
        client.put_parameter(
            Name=f"{prefix}/slack-bot-token",
            Value="xoxb-test-slack-token",
            Type="SecureString",
        )
        yield client


@pytest.fixture
def dynamodb_table():
    """Create mock DynamoDB table."""
    with mock_aws():
        dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
        table = dynamodb.create_table(
            TableName="hospitality-hq-messages-test",
            KeySchema=[
                {"AttributeName": "reservation_uuid", "KeyType": "HASH"},
                {"AttributeName": "message_id", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "reservation_uuid", "AttributeType": "S"},
                {"AttributeName": "message_id", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        table.wait_until_exists()
        yield table


@pytest.fixture
def thread_mapping_table():
    """Mock DynamoDB thread mapping table."""
    with mock_aws():
        dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
        table = dynamodb.create_table(
            TableName="hospitality-hq-thread-mapping-test",
            KeySchema=[{"AttributeName": "thread_ts", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "thread_ts", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        table.wait_until_exists()
        yield table


@pytest.fixture
def thread_logs_table():
    """Mock DynamoDB thread logs table."""
    with mock_aws():
        dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
        table = dynamodb.create_table(
            TableName="hospitality-hq-thread-logs-test",
            KeySchema=[
                {"AttributeName": "reservation_uuid", "KeyType": "HASH"},
                {"AttributeName": "log_id", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "reservation_uuid", "AttributeType": "S"},
                {"AttributeName": "log_id", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        table.wait_until_exists()
        yield table


@pytest.fixture
def all_thread_tables():
    """Create all tables needed by the thread handler (messages + mapping + logs)."""
    with mock_aws():
        dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
        messages = dynamodb.create_table(
            TableName="hospitality-hq-messages-test",
            KeySchema=[
                {"AttributeName": "reservation_uuid", "KeyType": "HASH"},
                {"AttributeName": "message_id", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "reservation_uuid", "AttributeType": "S"},
                {"AttributeName": "message_id", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        mapping = dynamodb.create_table(
            TableName="hospitality-hq-thread-mapping-test",
            KeySchema=[{"AttributeName": "thread_ts", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "thread_ts", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        logs = dynamodb.create_table(
            TableName="hospitality-hq-thread-logs-test",
            KeySchema=[
                {"AttributeName": "reservation_uuid", "KeyType": "HASH"},
                {"AttributeName": "log_id", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "reservation_uuid", "AttributeType": "S"},
                {"AttributeName": "log_id", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        messages.wait_until_exists()
        mapping.wait_until_exists()
        logs.wait_until_exists()
        yield {"messages": messages, "mapping": mapping, "logs": logs}


@pytest.fixture
def sample_reservation():
    """A sample Hospitable reservation object."""
    return {
        "id": "res-uuid-123",
        "property_id": "test-prop-uuid-1",
        "property_name": "Villa Bougainvillea",
        "checkin": "2026-04-20",
        "checkout": "2026-04-25",
        "status": "accepted",
        "guest": {
            "first_name": "Jane",
            "full_name": "Jane Smith",
        },
    }


@pytest.fixture
def sample_guest_message():
    """A sample guest message from Hospitable."""
    return {
        "id": "msg-001",
        "body": "Hi, the AC isn't working and it's really hot in here.",
        "sender_type": "guest",
        "source": "platform",
        "created_at": "2026-04-20T22:15:00Z",
    }


@pytest.fixture
def sample_host_message():
    """A sample host message from Hospitable."""
    return {
        "id": "msg-002",
        "body": "Hi Jane! Sorry about that, let me look into it right away.",
        "sender_type": "host",
        "source": "manual",
        "created_at": "2026-04-20T22:20:00Z",
    }


@pytest.fixture
def tasks_bucket():
    """Mock S3 bucket seeded with properties + users config.

    TaskStore caches config per process via lru_cache, so tests that need
    different seed data between invocations should clear that cache.
    """
    import json

    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket="hospitality-hq-tasks-test")

        properties = [
            {
                "id": "prop-palm",
                "name": "The Palm Club",
                "slug": "palm",
                "timezone": "America/Phoenix",
            },
            {
                "id": "prop-villa",
                "name": "Villa Bougainvillea",
                "slug": "villa",
                "timezone": "America/Phoenix",
            },
        ]
        users = [
            {"id": "vj", "slack_user_id": "UVJ", "display_name": "VJ"},
            {"id": "maggie", "slack_user_id": "UMAGGIE", "display_name": "Maggie"},
        ]
        s3.put_object(
            Bucket="hospitality-hq-tasks-test",
            Key="config/properties.json",
            Body=json.dumps(properties).encode("utf-8"),
        )
        s3.put_object(
            Bucket="hospitality-hq-tasks-test",
            Key="config/users.json",
            Body=json.dumps(users).encode("utf-8"),
        )

        # Reset the lru_cache on TaskStore methods so each test sees its own
        # fixture state. TaskStore is instantiated per test, so the cached
        # methods are bound to instance — but lru_cache on bound methods
        # caches on the class; clear defensively.
        try:
            from task_store import TaskStore

            for attr in ("load_properties", "load_users"):
                fn = getattr(TaskStore, attr, None)
                if fn and hasattr(fn, "cache_clear"):
                    fn.cache_clear()
        except ImportError:
            pass

        yield s3


@pytest.fixture
def expenses_bucket():
    """Mock S3 bucket for the expense workflow, with Object Lock enabled.

    Categories and merchant patterns are bundled with the Lambda (seed/),
    not stored in S3 — so this fixture only creates the bucket and
    leaves seeding to the test.
    """
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(
            Bucket="hospitality-hq-expenses-test",
            ObjectLockEnabledForBucket=True,
        )
        yield s3


@pytest.fixture
def sample_classification():
    """A sample classification result."""
    return {
        "category": "URGENT_MAINTENANCE",
        "urgency": "HIGH",
        "summary": "AC not working, guest reports high temperature",
    }


