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
def sample_classification():
    """A sample classification result."""
    return {
        "category": "URGENT_MAINTENANCE",
        "urgency": "HIGH",
        "summary": "AC not working, guest reports high temperature",
    }
