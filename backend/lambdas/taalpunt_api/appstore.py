"""App Store subscriptions (FR-001 step 2).

Two ways in, both carrying data signed by Apple (JWS):
- POST /me/purchases: the app sends a StoreKit 2 transaction after buying, restoring or a renewal it saw.
- POST /appstore/notifications: App Store Server Notifications V2 (renewals, expiry, refunds, ...).

Each signed object is checked with Apple's App Store Server Library: the certificate chain up to the
Apple Root CA - G3 in certs/ (with Apple's online revocation check), the app's bundle id and, for real
purchases, its App Store app id. Sandbox purchases (TestFlight and App Review) are accepted too.

A subscription belongs to one learner: the one whose user id the app put in the purchase as
appAccountToken. Restoring on another TaalPunt account moves it there (`transfer`), so nobody is stuck
paying for an account they can't reach; the automatic sync on launch never moves it.
"""
from __future__ import annotations

import base64
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import store

log = logging.getLogger()

PRODUCTS = frozenset({"nl.taalpunt.app.premium.quarterly", "nl.taalpunt.app.premium.monthly"})
ENVIRONMENTS = ("Production", "Sandbox")
# Purchases in Xcode's StoreKit testing aren't signed by Apple. Only the local mock server accepts them.
XCODE = "Xcode"
ROOT_CERTS = (Path(__file__).parent / "certs" / "AppleRootCA-G3.cer",)

_verifiers: dict[str, Any] = {}
# Tests sign with their own root certificate (and skip the online revocation check).
_test_roots: list[bytes] | None = None


class PurchaseError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def use_test_roots(roots: list[bytes] | None) -> None:
    global _test_roots
    _test_roots = roots
    _verifiers.clear()


def _iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat(timespec="seconds")


def _peek(jws: Any) -> dict:
    """The payload before verification, only to pick the environment's verifier."""
    if not isinstance(jws, str) or jws.count(".") != 2 or len(jws) > 64 * 1024:
        raise PurchaseError(400, "invalid_purchase", "This isn't a signed App Store transaction.")
    part = jws.split(".")[1]
    try:
        payload = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
    except (ValueError, UnicodeDecodeError) as exc:
        raise PurchaseError(400, "invalid_purchase", "This isn't a signed App Store transaction.") from exc
    if not isinstance(payload, dict):
        raise PurchaseError(400, "invalid_purchase", "This isn't a signed App Store transaction.")
    return payload


def _verifier(environment: Any):
    from appstoreserverlibrary.models.Environment import Environment
    from appstoreserverlibrary.signed_data_verifier import SignedDataVerifier

    xcode_ok = environment == XCODE and os.environ.get("APPSTORE_ACCEPT_XCODE") == "true"
    if environment not in ENVIRONMENTS and not xcode_ok:
        raise PurchaseError(400, "invalid_purchase", "Unknown App Store environment.")
    if environment not in _verifiers:
        app_apple_id = os.environ.get("APP_APPLE_ID") or None
        if environment == "Production" and not app_apple_id:
            log.error("APP_APPLE_ID is not set; App Store purchases can't be checked")
            raise PurchaseError(503, "not_ready", "Purchases can't be checked right now. Try again later.")
        roots = _test_roots if _test_roots is not None else [p.read_bytes() for p in ROOT_CERTS]
        _verifiers[environment] = SignedDataVerifier(
            roots, _test_roots is None, Environment(environment),
            os.environ.get("BUNDLE_ID", "nl.taalpunt.app"), int(app_apple_id) if app_apple_id else None)
    return _verifiers[environment]


def _verified(check, jws: str):
    from appstoreserverlibrary.signed_data_verifier import VerificationException, VerificationStatus

    try:
        return check(jws)
    except VerificationException as err:
        if err.status == VerificationStatus.RETRYABLE_VERIFICATION_FAILURE:
            raise PurchaseError(503, "try_again", "Apple couldn't be reached. Try again later.") from err
        log.warning("App Store data failed verification: %s", err.status.name)
        raise PurchaseError(400, "invalid_purchase", "This App Store transaction couldn't be verified.") from err


def _transaction(jws: str, environment: str):
    tx = _verified(_verifier(environment).verify_and_decode_signed_transaction, jws)
    if tx.productId not in PRODUCTS or not tx.originalTransactionId:
        raise PurchaseError(400, "unknown_product", "This isn't a TaalPunt Premium subscription.")
    return tx


def _until(tx, renewal=None) -> str:
    """End of Premium from this transaction: the refund time if Apple refunded it, otherwise the end of
    the paid period (or of Apple's billing grace period, when that is longer)."""
    if tx.revocationDate:
        return _iso(tx.revocationDate)
    grace = renewal.gracePeriodExpiresDate if renewal is not None else None
    return _iso(max(tx.expiresDate or 0, grace or 0))


def _store(sub: str, tx, renewal=None) -> None:
    applied = store.put_apple_entitlement(
        sub, until=_until(tx, renewal), original_transaction_id=tx.originalTransactionId,
        transaction_id=tx.transactionId or "", product_id=tx.productId,
        environment=tx.rawEnvironment or "", signed_date=int(tx.signedDate or 0))
    if not applied:
        log.info("ignored an older App Store update for %s", tx.originalTransactionId)


def claim(sub: str, body: Any) -> dict:
    """POST /me/purchases: {"signed_transaction": "<JWS>", "transfer": false}. Returns the Premium status."""
    if not isinstance(body, dict):
        raise PurchaseError(400, "invalid_purchase", "Body must be an object.")
    jws = body.get("signed_transaction")
    tx = _transaction(jws, _peek(jws).get("environment"))
    otid = tx.originalTransactionId
    owner = store.apple_owner(otid) or (tx.appAccountToken or "").lower() or sub
    if owner != sub:
        if body.get("transfer") is not True:
            raise PurchaseError(409, "other_account",
                                "This App Store subscription is linked to another TaalPunt account.")
        log.info("moving App Store subscription %s to another account", otid)
        store.end_apple_entitlement(owner, otid)
    store.set_apple_owner(otid, sub)
    _store(sub, tx)
    return store.get_premium(sub)


def notification(body: Any) -> str:
    """POST /appstore/notifications: {"signedPayload": "<JWS>"}. Returns what was done, for the log."""
    if not isinstance(body, dict):
        raise PurchaseError(400, "invalid_notification", "Body must be an object.")
    payload = body.get("signedPayload")
    data = _peek(payload).get("data")
    if not isinstance(data, dict):
        return "ignored: no transaction"  # for example a summary of a subscription price change
    environment = data.get("environment")
    note = _verified(_verifier(environment).verify_and_decode_notification, payload)
    kind = note.rawNotificationType
    if kind == "TEST":
        return "test"
    if not note.data or not note.data.signedTransactionInfo:
        return f"ignored: {kind} without a transaction"
    tx = _transaction(note.data.signedTransactionInfo, environment)
    renewal = None
    if note.data.signedRenewalInfo:
        renewal = _verified(_verifier(environment).verify_and_decode_renewal_info, note.data.signedRenewalInfo)
    otid = tx.originalTransactionId
    owner = store.apple_owner(otid)
    if not owner:
        owner = (tx.appAccountToken or "").lower()
        if not owner:
            log.warning("App Store notification %s for %s has no account", kind, otid)
            return f"ignored: {kind} without an account"
        store.set_apple_owner(otid, owner)
    _store(owner, tx, renewal)
    return f"applied: {kind}"
