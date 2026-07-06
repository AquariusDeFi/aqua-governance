import base64
import hashlib
import os

from django.conf import settings

from stellar_sdk import Server, Payment, HashMemo, TransactionEnvelope

from aqua_governance.governance import payment_statuses
from aqua_governance.utils.requests import load_all_records


def is_dev_payment_bypass_enabled():
    if not settings.DEBUG:
        return False

    return os.getenv('PROPOSAL_PAYMENT_BYPASS', '').lower() in {'1', 'true', 'yes', 'on'}


def check_payment(tx_hash, payment_amount=settings.PROPOSAL_COST):
    if is_dev_payment_bypass_enabled():
        return True

    try:
        horizon_server = Server(settings.HORIZON_URL)
        for operation in load_all_records(horizon_server.operations().for_transaction(tx_hash)):
            operation_type = operation.get('type', None)

            if not operation_type or operation_type != 'payment':
                continue

            if operation['asset_code'] == settings.AQUA_ASSET_CODE and operation['asset_issuer'] == settings.AQUA_ASSET_ISSUER and\
                    operation['to'] == settings.AQUA_ASSET_ISSUER and float(operation['amount']) >= payment_amount:
                return True
    except Exception as ex:
        print(ex)

    return False


def check_xdr_payment(transaction_envelope, payment_amount=settings.PROPOSAL_COST):
    for operation in transaction_envelope.transaction.operations:
        if not isinstance(operation, Payment):
            continue
        if operation.asset.code == settings.AQUA_ASSET_CODE and operation.asset.issuer == settings.AQUA_ASSET_ISSUER and \
            operation.destination.account_id == settings.AQUA_ASSET_ISSUER and float(operation.amount) >= payment_amount:
            return True

    return False


def check_proposal_status(transaction_hash, text, payment_amount=settings.PROPOSAL_COST):
    if is_dev_payment_bypass_enabled():
        return payment_statuses.FINE

    horizon_server = Server(settings.HORIZON_URL)
    try:
        transaction_info = horizon_server.transactions().transaction(transaction_hash).call()
    except Exception:
        return payment_statuses.HORIZON_ERROR

    if not transaction_info.get('successful', None):
        return payment_statuses.FAILED_TRANSACTION
    if not check_payment(transaction_hash, payment_amount):
        return payment_statuses.INVALID_PAYMENT

    memo = transaction_info.get('memo', None)
    if not memo:
        return payment_statuses.BAD_MEMO

    text_hash = hashlib.sha256(text.encode('utf-8')).hexdigest()

    if not base64.b64encode(HashMemo(text_hash).memo_hash).decode() == memo:
        return payment_statuses.BAD_MEMO

    return payment_statuses.FINE


def check_transaction_xdr(data, payment_amount=settings.PROPOSAL_COST):
    if is_dev_payment_bypass_enabled():
        return payment_statuses.FINE

    envelope_xdr = data.get('envelope_xdr', None)
    try:
        transaction_envelope = TransactionEnvelope.from_xdr(envelope_xdr, settings.NETWORK_PASSPHRASE)
    except Exception:
        return payment_statuses.HORIZON_ERROR

    if not check_xdr_payment(transaction_envelope, payment_amount):
        return payment_statuses.INVALID_PAYMENT

    memo = transaction_envelope.transaction.memo
    text_hash = hashlib.sha256(data['text'].html.encode('utf-8')).hexdigest()

    if not isinstance(memo, HashMemo) or not HashMemo(text_hash).memo_hash == memo.memo_hash:
        return payment_statuses.BAD_MEMO

    return payment_statuses.FINE
