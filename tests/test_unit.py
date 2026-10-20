import time

import pytest
from pydantic import ValidationError

from campuspay.config import Settings
from campuspay.receiver import verify
from campuspay.schemas import Amount, Transfer
from campuspay.workers import signature


@pytest.mark.parametrize("amount", [0, -1, 1_000_001, True, 1.5, "100", None])
def test_money_rejects_invalid_values(amount):
    with pytest.raises(ValidationError):
        Amount(amount=amount)


@pytest.mark.parametrize("amount", [1, 1_000_000])
def test_money_boundary_values(amount):
    assert Amount(amount=amount).amount == amount


def test_unknown_fields_rejected():
    with pytest.raises(ValidationError):
        Transfer(amount=100, recipient="bob", currency="EUR")


def test_signature_authenticity_tampering_and_age():
    secret, body = "a-long-demo-secret", b'{"eventId":"abc"}'
    now = str(int(time.time()))
    signed = signature(secret, now, body)
    assert verify(secret, now, body, signed)
    assert not verify(secret, now, body + b" ", signed)
    assert not verify("other", now, body, signed)
    assert not verify(secret, str(int(now) - 301), body, signed)
    assert not verify(secret, "invalid", body, signed)


def test_short_demo_password_rejected():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, demo_password="short")


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "http://",
        "http://user:password@localhost/events",
        "http://localhost:99999/events",
    ],
)
def test_invalid_webhook_configuration_rejected(url):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, webhook_url=url)
