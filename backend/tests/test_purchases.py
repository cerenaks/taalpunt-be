"""App Store purchases (FR-001 step 2), checked end to end with Apple's library.

The tests make their own certificate chain shaped like Apple's (root, intermediate and leaf with
Apple's marker extensions) and sign transactions with it, so the real signature and chain checks run.
"""
import base64
import json
import uuid
from datetime import datetime, timedelta, timezone

import jwt
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from taalpunt_api import appstore, handlers, store

ALICE = str(uuid.uuid4())
BOB = str(uuid.uuid4())
QUARTERLY = "nl.taalpunt.app.premium.quarterly"
NOW = datetime.now(timezone.utc)


def _ms(d: datetime) -> int:
    return int(d.timestamp() * 1000)


def _cert(name, key, issuer_name, issuer_key, ca, marker=None):
    b = (x509.CertificateBuilder()
         .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)]))
         .issuer_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, issuer_name)]))
         .public_key(key.public_key())
         .serial_number(x509.random_serial_number())
         .not_valid_before(NOW - timedelta(days=30))
         .not_valid_after(NOW + timedelta(days=365))
         .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True)
         .add_extension(x509.KeyUsage(digital_signature=not ca, key_cert_sign=ca, crl_sign=ca, content_commitment=False,
                                      key_encipherment=False, data_encipherment=False, key_agreement=False,
                                      encipher_only=False, decipher_only=False), critical=True)
         .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
         .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(issuer_key.public_key()), critical=False))
    if marker:
        b = b.add_extension(x509.UnrecognizedExtension(x509.ObjectIdentifier(marker), b"\x05\x00"), critical=False)
    return b.sign(issuer_key, hashes.SHA256())


class FakeApple:
    """Signs App Store data the way Apple does: ES256 with the three certificates in x5c."""

    def __init__(self):
        root_key, mid_key, self.leaf_key = (ec.generate_private_key(ec.SECP256R1()) for _ in range(3))
        root = _cert("Test Root", root_key, "Test Root", root_key, ca=True)
        mid = _cert("Test WWDR", mid_key, "Test Root", root_key, ca=True, marker="1.2.840.113635.100.6.2.1")
        leaf = _cert("Test Signing", self.leaf_key, "Test WWDR", mid_key, ca=False, marker="1.2.840.113635.100.6.11.1")
        self.root_der = root.public_bytes(serialization.Encoding.DER)
        self.x5c = [base64.b64encode(c.public_bytes(serialization.Encoding.DER)).decode() for c in (leaf, mid, root)]

    def sign(self, payload: dict) -> str:
        return jwt.encode(payload, self.leaf_key, algorithm="ES256", headers={"x5c": self.x5c})

    def transaction(self, token=ALICE, otid="1000", expires=NOW + timedelta(days=90), signed=NOW, **over) -> str:
        tx = {
            "transactionId": str(uuid.uuid4().int)[:16], "originalTransactionId": otid,
            "bundleId": "nl.taalpunt.app", "productId": QUARTERLY, "type": "Auto-Renewable Subscription",
            "purchaseDate": _ms(signed), "expiresDate": _ms(expires), "signedDate": _ms(signed),
            "environment": "Sandbox", "inAppOwnershipType": "PURCHASED", "appAccountToken": token,
        }
        tx.update(over)
        return self.sign({k: v for k, v in tx.items() if v is not None})

    def notification(self, kind, transaction, renewal=None, environment="Sandbox") -> str:
        data = {"environment": environment, "bundleId": "nl.taalpunt.app", "appAppleId": 1234567890,
                "signedTransactionInfo": transaction}
        if renewal:
            data["signedRenewalInfo"] = renewal
        return self.sign({"notificationType": kind, "notificationUUID": str(uuid.uuid4()), "version": "2.0",
                          "signedDate": _ms(NOW), "data": data})


@pytest.fixture
def apple(monkeypatch):
    fake = FakeApple()
    monkeypatch.setenv("BUNDLE_ID", "nl.taalpunt.app")
    monkeypatch.setenv("APP_APPLE_ID", "1234567890")
    appstore.use_test_roots([fake.root_der])
    yield fake
    appstore.use_test_roots(None)


def buy(sub, signed, transfer=None):
    body = {"signed_transaction": signed}
    if transfer is not None:
        body["transfer"] = transfer
    ev = {"routeKey": "POST /me/purchases", "headers": {}, "body": json.dumps(body),
          "requestContext": {"authorizer": {"jwt": {"claims": {"sub": sub, "token_use": "access"}}}}}
    r = handlers.api(ev, None)
    return r["statusCode"], json.loads(r["body"])


def notify(signed_payload):
    r = handlers.appstore_notifications({"body": json.dumps({"signedPayload": signed_payload})}, None)
    return r["statusCode"], json.loads(r["body"])


# ---------- the app reports a purchase ----------

def test_buying_gives_premium_until_the_period_ends(table, apple):
    status, body = buy(ALICE, apple.transaction(expires=NOW + timedelta(days=90)))
    assert status == 200
    assert body["premium"]["active"] and body["premium"]["sources"] == ["apple"]
    assert body["premium"]["until"][:10] == (NOW + timedelta(days=90)).date().isoformat()
    assert store.apple_owner("1000") == ALICE


def test_an_expired_subscription_gives_no_premium(table, apple):
    status, body = buy(ALICE, apple.transaction(expires=NOW - timedelta(days=1)))
    assert status == 200 and body["premium"]["active"] is False


def test_a_refunded_purchase_gives_no_premium(table, apple):
    status, body = buy(ALICE, apple.transaction(revocationDate=_ms(NOW - timedelta(hours=1)), revocationReason=0))
    assert status == 200 and body["premium"]["active"] is False


def test_someone_elses_subscription_needs_a_restore_to_move(table, apple):
    assert buy(ALICE, apple.transaction(token=ALICE))[0] == 200
    status, body = buy(BOB, apple.transaction(token=ALICE))
    assert status == 409 and body["error"] == "other_account"
    assert store.get_premium(BOB)["active"] is False
    # Restore purchases on Bob's account moves it there; Alice loses it.
    status, body = buy(BOB, apple.transaction(token=ALICE), transfer=True)
    assert status == 200 and body["premium"]["active"]
    assert store.get_premium(ALICE)["active"] is False
    assert store.apple_owner("1000") == BOB


def test_a_purchase_without_an_account_token_goes_to_whoever_reports_it(table, apple):
    status, body = buy(BOB, apple.transaction(token=None))
    assert status == 200 and body["premium"]["active"]


@pytest.mark.parametrize("over, code", [
    ({"bundleId": "com.someone.else"}, "invalid_purchase"),
    ({"productId": "com.other.product"}, "unknown_product"),
])
def test_other_apps_and_products_are_refused(table, apple, over, code):
    status, body = buy(ALICE, apple.transaction(**over))
    assert status == 400 and body["error"] == code
    assert store.get_premium(ALICE)["active"] is False


def test_a_transaction_not_signed_by_apple_is_refused(table, apple):
    forged = FakeApple().transaction()  # a valid-looking chain, but not from the trusted root
    assert buy(ALICE, forged)[1]["error"] == "invalid_purchase"
    head, payload, sig = apple.transaction().split(".")
    tampered = json.loads(base64.urlsafe_b64decode(payload + "=="))
    tampered["expiresDate"] = _ms(NOW + timedelta(days=9999))
    edited = base64.urlsafe_b64encode(json.dumps(tampered).encode()).decode().rstrip("=")
    assert buy(ALICE, f"{head}.{edited}.{sig}")[1]["error"] == "invalid_purchase"
    assert buy(ALICE, "not a jws")[1]["error"] == "invalid_purchase"
    assert store.get_premium(ALICE)["active"] is False


def test_xcode_test_purchases_only_on_the_mock_server(table, apple, monkeypatch):
    unsigned = jwt.encode({"originalTransactionId": "9", "transactionId": "9", "bundleId": "nl.taalpunt.app",
                           "productId": QUARTERLY, "expiresDate": _ms(NOW + timedelta(days=1)),
                           "signedDate": _ms(NOW), "environment": "Xcode"}, "x" * 32, algorithm="HS256")
    assert buy(ALICE, unsigned)[1]["error"] == "invalid_purchase"
    monkeypatch.setenv("APPSTORE_ACCEPT_XCODE", "true")
    assert buy(ALICE, unsigned)[1]["premium"]["active"]


def test_real_purchases_wait_for_the_app_apple_id(table, apple, monkeypatch):
    monkeypatch.setenv("APP_APPLE_ID", "")
    appstore.use_test_roots([apple.root_der])
    status, body = buy(ALICE, apple.transaction(environment="Production"))
    assert status == 503 and body["error"] == "not_ready"


def test_an_older_report_does_not_undo_a_newer_one(table, apple):
    buy(ALICE, apple.transaction(expires=NOW + timedelta(days=90), signed=NOW))
    buy(ALICE, apple.transaction(expires=NOW - timedelta(days=1), signed=NOW - timedelta(days=95)))
    assert store.get_premium(ALICE)["active"]


# ---------- Apple's server notifications ----------

def test_renewal_notification_extends_premium(table, apple):
    buy(ALICE, apple.transaction(expires=NOW + timedelta(days=1)))
    renewed = apple.transaction(expires=NOW + timedelta(days=91), signed=NOW + timedelta(seconds=5))
    assert notify(apple.notification("DID_RENEW", renewed)) == (200, {"ok": True})
    assert store.get_premium(ALICE)["until"][:10] == (NOW + timedelta(days=91)).date().isoformat()


def test_notification_before_the_app_reports_uses_the_account_token(table, apple):
    assert notify(apple.notification("SUBSCRIBED", apple.transaction(token=BOB, otid="2000")))[0] == 200
    assert store.get_premium(BOB)["active"] and store.apple_owner("2000") == BOB


def test_expiry_and_refund_notifications_end_premium(table, apple):
    buy(ALICE, apple.transaction())
    expired = apple.transaction(expires=NOW - timedelta(minutes=1), signed=NOW + timedelta(seconds=5))
    notify(apple.notification("EXPIRED", expired))
    assert store.get_premium(ALICE)["active"] is False
    buy(BOB, apple.transaction(token=BOB, otid="3000"))
    refunded = apple.transaction(token=BOB, otid="3000", signed=NOW + timedelta(seconds=5),
                                 revocationDate=_ms(NOW), revocationReason=0)
    notify(apple.notification("REFUND", refunded))
    assert store.get_premium(BOB)["active"] is False


def test_notifications_follow_a_moved_subscription(table, apple):
    buy(ALICE, apple.transaction(token=ALICE))
    buy(BOB, apple.transaction(token=ALICE), transfer=True)
    renewed = apple.transaction(token=ALICE, expires=NOW + timedelta(days=180), signed=NOW + timedelta(seconds=5))
    notify(apple.notification("DID_RENEW", renewed))
    assert store.get_premium(BOB)["active"] and not store.get_premium(ALICE)["active"]


def test_billing_grace_period_keeps_premium(table, apple):
    buy(ALICE, apple.transaction(expires=NOW + timedelta(days=1)))
    lapsed = apple.transaction(expires=NOW - timedelta(hours=1), signed=NOW + timedelta(seconds=5))
    renewal = apple.sign({"originalTransactionId": "1000", "productId": QUARTERLY, "environment": "Sandbox",
                          "gracePeriodExpiresDate": _ms(NOW + timedelta(days=6)), "signedDate": _ms(NOW)})
    notify(apple.notification("DID_FAIL_TO_RENEW", lapsed, renewal))
    assert store.get_premium(ALICE)["active"]


def test_test_notification_and_forgeries(table, apple):
    assert notify(apple.sign({"notificationType": "TEST", "notificationUUID": "x", "version": "2.0",
                              "data": {"environment": "Sandbox", "bundleId": "nl.taalpunt.app"}}))[0] == 200
    forged = FakeApple()
    status, body = notify(forged.notification("SUBSCRIBED", forged.transaction(token=BOB)))
    assert status == 400 and not store.get_premium(BOB)["active"]
    assert notify("garbage")[0] == 400


# ---------- deleting the account ----------

def test_deleting_the_account_removes_the_subscription_link(table, apple, monkeypatch):
    buy(ALICE, apple.transaction())

    class FakeCognito:
        def admin_delete_user(self, **kw):
            pass

    import boto3 as real_boto3
    orig = real_boto3.client
    monkeypatch.setattr(real_boto3, "client", lambda name, *a, **k: FakeCognito() if name == "cognito-idp" else orig(name, *a, **k))
    monkeypatch.setenv("USER_POOL_ID", "eu-north-1_test")
    ev = {"requestContext": {"authorizer": {"jwt": {"claims": {"sub": ALICE, "token_use": "access"}}}}}
    assert handlers.delete_account(ev, None)["statusCode"] == 200
    assert table.scan()["Items"] == []
