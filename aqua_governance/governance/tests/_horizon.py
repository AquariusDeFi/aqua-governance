"""Offline Horizon doubles for the payment verification tests.

``FakePaymentHorizonServer`` answers the two endpoints ``verify_payment`` touches and
paginates for real, because ``load_all_records`` terminates on an empty page rather than a
short one (the ``len(records) < page_size`` shortcut is commented out pending
stellar/go#5032).  Both endpoints count their calls, so a test can assert that Horizon was
never reached at all.

``FakeLedgerHorizonServer`` is the same double for a test that has to settle several
payments and then confirm them through the API, which one-transaction fake cannot do.
"""
from django.conf import settings

from stellar_sdk.exceptions import NotFoundError


DEFAULT_PAGE_SIZE = 200


def payment_op_record(*, from_account, amount, to=None, asset_code=None, asset_issuer=None,
                      transaction_successful=True, from_muxed=None, source=None,
                      paging_token=None):
    """A Horizon ``payment`` operation record for a classic (non-native) asset."""
    record = {
        'type': 'payment',
        'transaction_successful': transaction_successful,
        'asset_type': 'credit_alphanum4',
        'asset_code': settings.AQUA_ASSET_CODE if asset_code is None else asset_code,
        'asset_issuer': settings.AQUA_ASSET_ISSUER if asset_issuer is None else asset_issuer,
        'from': from_account,
        'to': settings.AQUA_ASSET_ISSUER if to is None else to,
        'amount': amount,
    }
    if from_muxed is not None:
        record['from_muxed'] = from_muxed
    if source is not None:
        record['source_account'] = source
    if paging_token is not None:
        record['paging_token'] = paging_token

    return record


def native_payment_op_record(*, from_account, amount, to=None, transaction_successful=True,
                             paging_token=None):
    """A native-asset payment, which Horizon emits with no ``asset_code``/``asset_issuer``.

    Reading those keys with ``[]`` raises ``KeyError`` on this shape, which is what used to
    abort the whole operation scan through a blanket ``except Exception``.
    """
    record = {
        'type': 'payment',
        'transaction_successful': transaction_successful,
        'asset_type': 'native',
        'from': from_account,
        'to': settings.AQUA_ASSET_ISSUER if to is None else to,
        'amount': amount,
    }
    if paging_token is not None:
        record['paging_token'] = paging_token

    return record


def non_payment_op_record(*, operation_type='create_account', paging_token=None):
    """Any operation the payment scan must skip without reading payment fields."""
    record = {'type': operation_type, 'transaction_successful': True}
    if paging_token is not None:
        record['paging_token'] = paging_token

    return record


def transaction_record(*, successful=True, memo=None, memo_type='hash', transaction_hash=None,
                       inner_hash=None, fee_bump_hash=None):
    """A Horizon ``transactions/{hash}`` record, memo rendered as Horizon renders it."""
    record = {'successful': successful, 'memo_type': memo_type}
    if memo is not None:
        record['memo'] = memo
    if transaction_hash is not None:
        record['hash'] = transaction_hash
    if inner_hash is not None:
        record['inner_transaction'] = {'hash': inner_hash}
    if fee_bump_hash is not None:
        record['fee_bump_transaction'] = {'hash': fee_bump_hash}

    return record


class _TransactionCall:
    def __init__(self, server, transaction_hash):
        self._server = server
        self._transaction_hash = transaction_hash

    def call(self):
        self._server.transaction_calls.append(self._transaction_hash)
        if self._server.transaction_error is not None:
            raise self._server.transaction_error

        return self._server.transaction


class _TransactionsEndpoint:
    def __init__(self, server):
        self._server = server

    def transaction(self, transaction_hash):
        return _TransactionCall(self._server, transaction_hash)


class _OperationsRequestBuilder:
    """Mimics the SDK builder ``load_all_records`` drives: ``limit``/``cursor`` then ``call``."""

    def __init__(self, server, transaction_hash):
        self._server = server
        self._transaction_hash = transaction_hash
        self._limit = DEFAULT_PAGE_SIZE
        self._cursor = None

    def limit(self, page_size):
        self._limit = page_size
        return self

    def cursor(self, cursor):
        self._cursor = cursor
        return self

    def call(self):
        self._server.operation_calls.append(self._transaction_hash)
        if self._server.operations_error is not None:
            raise self._server.operations_error

        records = self._server.operation_records
        if self._cursor is not None:
            tokens = [record['paging_token'] for record in records]
            start = tokens.index(self._cursor) + 1 if self._cursor in tokens else len(records)
            records = records[start:]

        return {'_embedded': {'records': list(records[:self._limit])}}


class _OperationsEndpoint:
    def __init__(self, server):
        self._server = server

    def for_transaction(self, transaction_hash):
        return _OperationsRequestBuilder(self._server, transaction_hash)


class FakePaymentHorizonServer:
    """A Horizon stand-in for ``verify_payment``, with per-endpoint call counting."""

    def __init__(self, *, transaction=None, operations=(), transaction_error=None,
                 operations_error=None):
        self.transaction = transaction if transaction is not None else transaction_record()
        self.operation_records = [
            dict(record, paging_token=record.get('paging_token') or 'page-{0:06d}'.format(index))
            for index, record in enumerate(operations)
        ]
        self.transaction_error = transaction_error
        self.operations_error = operations_error
        self.transaction_calls = []
        self.operation_calls = []

    def transactions(self):
        return _TransactionsEndpoint(self)

    def operations(self):
        return _OperationsEndpoint(self)


class _NotFoundResponse:
    """The minimum ``NotFoundError`` reads out of a response object."""

    status_code = 404
    text = '{}'
    headers = {}

    def __init__(self, url):
        self.url = url

    def json(self):
        return {}


def transaction_not_found_error(transaction_hash):
    """What Horizon answers for a transaction nobody broadcast."""
    return NotFoundError(_NotFoundResponse(
        'https://horizon.example/transactions/{0}'.format(transaction_hash),
    ))


class _MissingTransactionCall:
    def __init__(self, transaction_hash):
        self._transaction_hash = transaction_hash

    def call(self):
        raise transaction_not_found_error(self._transaction_hash)


class _MissingOperationsRequestBuilder:
    def __init__(self, transaction_hash):
        self._transaction_hash = transaction_hash

    def limit(self, page_size):
        return self

    def cursor(self, cursor):
        return self

    def call(self):
        raise transaction_not_found_error(self._transaction_hash)


class _LedgerTransactionsEndpoint:
    def __init__(self, ledger):
        self._ledger = ledger

    def transaction(self, transaction_hash):
        self._ledger.transaction_calls.append(transaction_hash)
        entry = self._ledger.entry_for(transaction_hash)
        if entry is None:
            return _MissingTransactionCall(transaction_hash)

        return entry.transactions().transaction(transaction_hash)


class _LedgerOperationsEndpoint:
    def __init__(self, ledger):
        self._ledger = ledger

    def for_transaction(self, transaction_hash):
        self._ledger.operation_calls.append(transaction_hash)
        entry = self._ledger.entry_for(transaction_hash)
        if entry is None:
            return _MissingOperationsRequestBuilder(transaction_hash)

        return entry.operations().for_transaction(transaction_hash)


class FakeLedgerHorizonServer:
    """A Horizon stand-in that answers for many transactions at once, keyed by hash.

    Every settled transaction gets its own :class:`FakePaymentHorizonServer`, so pagination
    and record shapes stay identical to the single-transaction fake.  A hash the ledger has
    never seen answers 404, which is what Horizon does for a transaction nobody broadcast -
    the state an attacker's invented hash is really in.
    """

    def __init__(self):
        self.entries = {}
        self.transaction_calls = []
        self.operation_calls = []

    def entry_for(self, transaction_hash):
        if not isinstance(transaction_hash, str):
            return None
        return self.entries.get(transaction_hash.lower())

    def add(self, transaction_hash, *, transaction, operations=()):
        self.entries[transaction_hash.lower()] = FakePaymentHorizonServer(
            transaction=transaction,
            operations=operations,
        )
        return transaction_hash

    def settle_payment(self, transaction_hash, *, memo, from_account, amount,
                       successful=True, memo_type='hash', extra_operations=(), **payment_kwargs):
        """Record one AQUA burn as Horizon would report it once the transaction settled."""
        operations = list(extra_operations)
        operations.append(payment_op_record(
            from_account=from_account,
            amount=amount,
            **payment_kwargs,
        ))
        return self.add(
            transaction_hash,
            transaction=transaction_record(
                successful=successful,
                memo=memo,
                memo_type=memo_type,
                transaction_hash=transaction_hash.lower(),
            ),
            operations=operations,
        )

    def transactions(self):
        return _LedgerTransactionsEndpoint(self)

    def operations(self):
        return _LedgerOperationsEndpoint(self)
