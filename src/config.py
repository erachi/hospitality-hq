"""Configuration for Hospitality HQ monitoring system.

Secrets are fetched from AWS SSM Parameter Store (SecureString).
Non-secret config remains in environment variables.
"""

import os
import boto3
from functools import lru_cache


# SSM parameter path prefix
SSM_PREFIX = os.environ.get("SSM_PREFIX", "/hospitality-hq")


@lru_cache(maxsize=1)
def _get_ssm_client():
    """Cached SSM client — created once per Lambda cold start."""
    return boto3.client("ssm")


def _get_secret(param_name: str) -> str:
    """Fetch a SecureString parameter from SSM Parameter Store.

    Falls back to environment variable if SSM fetch fails (for local testing).
    """
    env_key = param_name.upper().replace("-", "_").replace("/", "_")

    try:
        ssm = _get_ssm_client()
        response = ssm.get_parameter(
            Name=f"{SSM_PREFIX}/{param_name}",
            WithDecryption=True,
        )
        return response["Parameter"]["Value"]
    except Exception:
        # Fallback to env var for local testing
        return os.environ.get(env_key, "")


# --- Secrets (fetched from SSM Parameter Store) ---

@lru_cache(maxsize=1)
def get_hospitable_token() -> str:
    return _get_secret("hospitable-api-token")

@lru_cache(maxsize=1)
def get_anthropic_key() -> str:
    return _get_secret("anthropic-api-key")

@lru_cache(maxsize=1)
def get_slack_bot_token() -> str:
    return _get_secret("slack-bot-token")

@lru_cache(maxsize=1)
def get_webhook_secret() -> str:
    return _get_secret("webhook-signing-secret")

@lru_cache(maxsize=1)
def get_slack_signing_secret() -> str:
    return _get_secret("slack-signing-secret")


# --- Non-secret config (environment variables) ---

HOSPITABLE_BASE_URL = "https://public.api.hospitable.com/v2"

CLASSIFY_MODEL = "claude-haiku-4-5-20251001"
DRAFT_MODEL = "claude-sonnet-4-6"

SLACK_CHANNEL_ID = os.environ.get("SLACK_CHANNEL_ID", "")

PROPERTY_UUIDS = [
    uid.strip()
    for uid in os.environ.get(
        "PROPERTY_UUIDS",
        "f8236d9d-988a-4192-9d16-2927b0b9ad8e,3278e9cb-9239-487f-aa51-cbfbaf4b7570",
    ).split(",")
]

DYNAMODB_TABLE = os.environ.get("DYNAMODB_TABLE", "hospitality-hq-messages")
THREAD_MAPPING_TABLE = os.environ.get("THREAD_MAPPING_TABLE", "hospitality-hq-thread-mapping")
THREAD_LOGS_TABLE = os.environ.get("THREAD_LOGS_TABLE", "hospitality-hq-thread-logs")

# Monitoring settings
RESERVATION_LOOKBACK_DAYS = 3
RESERVATION_LOOKAHEAD_DAYS = 30
