# AGENTS.md — Aqua Governance

Quick-start reference for AI agents working in this codebase.

---

## 1. What This Project Does

Aqua Governance is the backend API for the Aquarius DAO voting system on the Stellar blockchain. It manages:

- **Proposal lifecycle**: creation → discussion → voting → voted/expired
- **On-chain voting**: voters send AQUA/governICE/gdICE tokens as Stellar claimable balances to unique per-proposal accounts
- **Payment verification**: verifies AQUA payments for proposal creation (100K AQUA) and submission for voting (900K AQUA)
- **Vote aggregation**: periodically indexes claimable balances from Horizon, groups them by voter key, and tallies results

**Live:** https://gov.aqua.network/ | **Repo:** https://github.com/AquariusDeFi/aqua-governance

Beyond general governance proposals, the service also handles **asset governance**
(`ADD_ASSET` / `REMOVE_ASSET` proposals executed on-chain through a Soroban asset
registry) and a **weekly voting-queue** that books fixed voting slots.

---

## 1a. Verification Gates

Run before considering any change done, in this order. Requires the dev
dependencies (`pipenv sync --dev`) and a reachable PostgreSQL:

```bash
pipenv run flake8                 # lint (.flake8, max line length 120)
pipenv run isort --check-only .   # import order (.isort.cfg)
pipenv run python manage.py migrate --noinput   # migrations apply cleanly
pipenv run python manage.py test                # Django TestCase suite
```

These gates are the contributor's responsibility.
Settings default to `config.settings.dev`; the suite runs against it.

---

## 1b. Conventions

- **Style/lint:** flake8 (max line length 120) with the plugin set in `Pipfile`;
  `isort` + `black` for imports/formatting. Match `.editorconfig`.
- **Quotes/commas:** single quotes and trailing commas are enforced by flake8
  plugins — follow the surrounding code.
- **Settings:** all constants and asset/cost/timing/URL config live in
  `config/settings/base.py`, read via `django-environ`. Add new config as an
  `env(...)` with a sensible default; never hard-code secrets.
- **App layout:** business logic stays in the `governance` app; Celery wiring in
  `taskapp`; shared Stellar/Horizon helpers in `utils`. v2 serializers in
  `serializers_v2.py`, legacy v1 in `serializers.py`.
- **Tests:** Django `TestCase` + DRF `APIClient` under
  `aqua_governance/governance/tests/`; use the existing `_factories` helpers.

## 1c. Do Not Change Without Agreement

- **Migrations** in `aqua_governance/governance/migrations/` — never edit applied
  migrations; add new ones.
- **On-chain execution** (`onchain_hooks/`, `onchain_actions.py`) and the
  asset-registry contract wiring — touches real funds/secrets.
- The `GenerateGrouKeyException` typo and the hardcoded `id=65` / v1 date cutoff
  behaviours (see §9) — historical data artifacts.
- Vote-key format and indexing pipeline (§4) — changing it desyncs stored votes.
- **The payment-authorization invariant (§4).** A transition is applied only when
  Horizon attests a payment whose payer equals `proposed_by`, whose memo commits
  to that transition, and none of whose resolvable hashes has been claimed
  before; the `ConsumedTransaction` claim is the last statement of the same
  database transaction that applies the transition. Do not move the claim, do not
  relax the payer check, and do not reintroduce authorization at the staging
  layer — staging is unauthenticated by design and promotion is the gate.

---

## 2. Architecture

```
┌────────────────────────────────────────────────────────────┐
│                         API LAYER                           │
│  GET/POST/PUT  →  DRF ViewSets  →  PostgreSQL               │
│                                                              │
│  /api/proposal/          (v2: full CRUD + custom actions)   │
│  /api/proposals/         (v1: legacy, date-capped)          │
│  /api/votes-for-proposal/ (vote listing with filters)       │
│  /open/cms/              (Django admin)                      │
└───────────────────────┬────────────────────────────────────┘
                        │
                        ▼
┌────────────────────────────────────────────────────────────┐
│                   CELERY BEAT TASKS                         │
│  (polling only — there are no post_save signal receivers)   │
│                                                              │
│  task_update_active_proposals (every 5 min)                  │
│    └→ task_update_proposal_results                           │
│         ├→ task_update_votes       (index CBs from Horizon) │
│         └→ update_proposal_final_results (sum + supply)     │
│                                                              │
│  task_sync_proposal_statuses_by_time (every 1 min)           │
│  task_check_pending_proposal_payments (every 1 min)          │
│  task_check_expired_proposals (every 24h)                    │
│  task_update_votes (every 10 min, for VOTED proposals)       │
│  task_poll_submitted_onchain_executions (every 1 min)        │
│  task_retry_failed_onchain_executions (every 10 min)         │
└───────────────────────┬────────────────────────────────────┘
                        │
                        ▼
┌────────────────────────────────────────────────────────────┐
│                  STELLAR HORIZON + EXTERNAL                  │
│  Horizon: fetch claimable balances, verify transactions      │
│  cmc.aqua.network: AQUA circulating supply                   │
│  ice-distributor.aqua.network: ICE circulating supply        │
└────────────────────────────────────────────────────────────┘
```

---

## 3. Project Structure

```
aqua-governance/
├── config/
│   ├── settings/
│   │   ├── base.py           # All constants: assets, costs, timing, URLs
│   │   ├── dev.py            # Dev overrides (DEBUG, DB, HORIZON_URL)
│   │   └── prod.py           # Production overrides
│   └── urls.py               # Root: /api/ → governance.urls; /open/cms/ → admin
└── aqua_governance/
    ├── governance/            # Core app
    │   ├── models.py          # Proposal, ConsumedTransaction, LogVote, HistoryProposal, ...
    │   ├── views.py           # ProposalViewSet (v2), ProposalsView (v1), LogVoteView
    │   ├── serializers.py     # v1 read serializers (legacy, list/detail only)
    │   ├── serializers_v2.py  # ProposalCreate/AssetProposalCreate/Update/Submit/Detail/List
    │   ├── serializer_fields.py  # QuillField, TransactionHashField
    │   ├── transitions.py     # Create/Update/SubmitTransition: the verified transition
    │   ├── proposal_transactions.py  # check_transaction: the promotion paths
    │   ├── consumed_transactions.py  # claim_transaction_hashes (the payment ledger)
    │   ├── consumed_transaction_backfill.py  # shared by migration 0031 and the command
    │   ├── db_locks.py        # advisory locks: asset transition, payment sweep
    │   ├── filters.py         # DRF filter backends (status, owner, vote_owner)
    │   ├── pagination.py      # CustomPageNumberPagination (adds ?limit= param)
    │   ├── tasks.py           # All Celery tasks
    │   ├── task_logic/        # vote indexing + proposal finalization helpers
    │   ├── parser.py          # generate_vote_key, parse_vote (CB → LogVote)
    │   ├── exceptions.py      # ClaimableBalanceParsingError, TransactionAlreadyConsumed, ...
    │   ├── management/commands/  # backfill_consumed_transactions, rearm_proposal_payment_check
    │   ├── admin.py           # Django admin configuration
    │   └── urls.py            # Router registrations for all ViewSets
    ├── utils/
    │   ├── memo.py            # canonical + legacy memo grammar, MemoExpectation
    │   ├── payments.py        # verify_payment (Horizon), inspect_envelope (offline advisory)
    │   ├── requests.py        # load_all_records (Horizon cursor-pagination helper)
    │   └── stellar/
    │       └── asset.py       # parse_asset_string helper
    └── taskapp/
        └── __init__.py        # Celery app instance + beat schedule (crontab definitions)
```

---

## 4. Key Concepts

### Proposal Lifecycle State Machine

```
(POST /api/proposal/)
      │
      ▼
  [draft=True]  ←── offline XDR inspection (inspect_envelope) — ADVISORY ONLY
      │               the verdict is reported to the client; it authorizes nothing
      │
      ▼
 action=TO_CREATE → check_transaction() (beat sweep or /check_payment/)
      │                  Horizon verifies → draft=False, action=NONE, hash claimed
      ▼
 [DISCUSSION] ←── must wait DISCUSSION_TIME (7 days) before submit
      │
      │  (POST /api/proposal/{id}/submit/)
      ▼
 action=TO_SUBMIT → check_transaction() → proposal_status=QUEUED/VOTING
      │                  sets start_at, end_at, action=NONE, books the queue slot
      │
      ├── (end_at reached, task_sync_proposal_statuses_by_time) ──→ [VOTED]
      │
      └── (30 days inactive in DISCUSSION) ──→ [EXPIRED]
```

### Payment Verification — one gate, and one advisory check

**The payment is the authorization.** There is no session, token or signature auth
anywhere in this service, and the envelope a client posts is unsigned and therefore
worthless as a credential (Horizon will also serve the *signed* envelope of any past
transaction to anyone). What authorizes a transition is the AQUA payment Horizon
attests, checked at promotion time. Everything in the HTTP request — the envelope,
the staged `new_*` copy — is a claim to be checked.

**The gate: `verify_payment` (`utils/payments.py`), called from
`proposal_transactions.check_transaction()`** via the beat sweep or
`POST /api/proposal/{id}/check_payment/`. Four checks, all required:

- **Check A — payer.** The AQUA payment *operation*'s own source (muxed `M…` folded to
  its `G…`) must equal `proposal.proposed_by`. Operation level, not transaction level:
  a transaction can pay 1 AQUA from the victim and the rest from the attacker.
- **Check B — single use.** Every hash the transaction resolves under (including a
  fee-bump's inner and outer hash) is claimed in `ConsumedTransaction`, as the **last**
  statement of the transaction that applies the transition. A payment authorizes exactly
  one transition, ever; a rollback releases the claim with it.
- **Check C — memo.** `utils/memo.py` builds a `MemoExpectation` for the transition being
  applied. Accepted formats, canonical first: `sha256('AQUA-GOV|v1|<PURPOSE>|…')` over
  per-field inner digests, or — only while `PROPOSAL_LEGACY_MEMO_ACCEPTED` is true — the
  legacy `sha256(text_html)`. The memo type must be `hash`.
- **Payment shape.** Exactly the asset, destination and exact `Decimal` amount for the
  purpose — `PROPOSAL_CREATE_OR_UPDATE_COST` or `PROPOSAL_SUBMIT_COST`. A 900,000
  payment does not satisfy a 100,000 obligation.

Verdicts are `FINE | HORIZON_ERROR | FAILED_TRANSACTION | INVALID_PAYMENT | BAD_MEMO`.
Only `HORIZON_ERROR` is retryable; a terminal verdict records
`payment_check_rejected_hash` so the sweep stops re-asking Horizon about it.

**The advisory check: `inspect_envelope` (`utils/payments.py`)**, called by the create /
update / submit serializers at staging time. It parses the posted `envelope_xdr`
offline and returns the same status vocabulary so an honest client learns *before
signing* that its envelope is wrong. It is **not** a security control and no transition
depends on it — anyone can post any envelope. Same for
`ProposalViewSet._reject_declared_owner_mismatch`: it compares the envelope's declared
source to `proposed_by` purely to give a wrong-wallet client a legible error.

### Vote Key Format

```
"{proposal_id}|{vote_choice}|{account_issuer}|{asset_code}|{sorted(time_list)}"
```

- `account_issuer`: claimant destination that has an `abs_before` predicate (the voter's account)
- `time_list`: list of `abs_before` timestamps from claimants; sorted to ensure deterministic key
- Multiple claimable balances from the same voter/proposal/asset/period share a key
- Groups are sorted by amount DESC; largest CB gets `group_index=0`

### Vote Indexing Pipeline (`task_update_votes`)

```
Phase 1 — Group CBs by vote_key:
  For proposal → fetch all CBs from Horizon (vote_for_issuer + vote_against_issuer accounts)
  For each CB: generate_vote_key() → group into raw_vote_groups dict
  (GenerateGrouKeyException skipped with warning)

Phase 2 — Sort + Process each group:
  Sort CBs by amount DESC (largest first = group_index 0)
  For each (vote_key, group_index) entry:
    Find existing LogVote by (key, group_index):
      → found:  _make_updated_vote() → update_log_vote list
      → not found: _make_new_vote() → fetch Horizon ops for created_at/original_amount
          Check duplicate by claimable_balance_id (hide=False):
          → dup found: _make_updated_vote() → update_log_vote list
          → no dup:    → new_log_vote list

Phase 3 — Mark claimed:
  Any existing vote whose (key, group_index) not in indexed_vote_keys_and_index
  → vote.claimed = True → claimed_log_vote list

Phase 4 — Bulk DB operations:
  LogVote.objects.bulk_create(new_log_vote)
  LogVote.objects.bulk_update(update_log_vote, [claimable_balance_id, amount, ...])
  LogVote.objects.bulk_update(claimed_log_vote, ["claimed"])
```

---

## 5. Models

### Proposal

| Field | Type | Notes |
|-------|------|-------|
| proposed_by | CharField(56) | Creator's Stellar public key |
| title | CharField(256) | |
| text | QuillField | Rich HTML; serialized as plain HTML via QuillField serializer |
| version | PositiveSmallIntegerField | Incremented on each verified update |
| vote_for_issuer | CharField(56) | Auto-generated random Stellar keypair on first save |
| vote_against_issuer | CharField(56) | Auto-generated random Stellar keypair on first save |
| proposal_status | Choice | DISCUSSION / QUEUED / VOTING / VOTED / EXPIRED |
| proposal_type | Choice | GENERAL / ADD_ASSET / REMOVE_ASSET (asset types grouped as `ASSET_PROPOSAL_TYPES`) |
| payment_status | Choice | FINE / HORIZON_ERROR / BAD_MEMO / INVALID_PAYMENT / FAILED_TRANSACTION |
| payment_check_rejected_hash | CharField(64, null) | The pending hash a terminal verdict was recorded for; `''` means "there was no hash". Takes the row out of the beat sweep and dedups the operator alert. Cleared by every promotion, and by `manage.py rearm_proposal_payment_check` |
| status | Choice | Legacy (TODO: remove) |
| action | Choice | TO_CREATE / TO_UPDATE / TO_SUBMIT / NONE |
| transaction_hash | CharField(64, unique) | Current/creation payment tx hash |
| new_transaction_hash | CharField(64, unique) | Pending update/submit tx hash |
| envelope_xdr | TextField | Current transaction XDR |
| new_envelope_xdr | TextField | Pending update/submit XDR |
| new_title / new_text | CharField/QuillField | Staged update values (pending approval) |
| new_start_at / new_end_at | DateTimeField | Staged submit values |
| start_at / end_at | DateTimeField | Active voting window |
| vote_for_result | DecimalField(20,7) | Aggregated FOR total |
| vote_against_result | DecimalField(20,7) | Aggregated AGAINST total |
| aqua_circulating_supply | DecimalField | AQUA supply snapshot at last update |
| ice_circulating_supply | DecimalField | ICE supply snapshot at last update |
| percent_for_quorum | PositiveSmallIntegerField | Default 20 (= 20% quorum required) |
| hide | BooleanField | Soft delete (excluded from all public endpoints) |
| draft | BooleanField | True until creation payment verified |
| is_simple_proposal | BooleanField | Reserved for future custom voting options |
| discord_channel_url/name | URL/CharField | Discussion channel metadata |
| discord_username | CharField(64) | Submitter's Discord handle |

### ConsumedTransaction

The append-only payment ledger. One row per transaction hash, forever; written as the
last statement of the transaction that applies a transition, so a hash is burned if and
only if a transition really happened.

| Field | Type | Notes |
|-------|------|-------|
| transaction_hash | CharField(64, unique) | Lowercase. Uniqueness is on the hash **alone** — a composite key with `purpose` would let one payment be spent as a create *and* a submit |
| proposal | FK(Proposal, SET_NULL, null) | Never CASCADE: superusers can delete proposals, and a cascade would un-burn their payments |
| purpose | Choice | CREATE / UPDATE / SUBMIT / LEGACY (`LEGACY` = backfilled by migration `0031`) |
| payer | CharField(56, null) | The verified on-chain payer, for forensics; discarded to NULL if it is not 56 chars |
| created_at | DateTimeField | auto_now_add |

The pk is a surrogate `AutoField`: with `transaction_hash` as a natural pk, `save()`
would UPDATE over an existing claim instead of raising.

Claim through `consumed_transactions.claim_transaction_hashes()` — never by creating rows
by hand. It must be the **last** lock a transaction takes.

### LogVote

| Field | Type | Notes |
|-------|------|-------|
| claimable_balance_id | CharField(72) | Stellar CB ID |
| proposal | FK(Proposal, CASCADE) | |
| vote_choice | Choice | `vote_for` / `vote_against` |
| asset_code | Choice | AQUA / governICE / gdICE |
| account_issuer | CharField(56) | Voter's Stellar account |
| key | CharField(170) | Composite vote key (see §4) |
| group_index | IntegerField | Position in sorted CB group (0 = largest amount) |
| amount | DecimalField(20,7) | Current CB amount |
| original_amount | DecimalField(20,7) | Amount when CB was first created |
| voted_amount | DecimalField(20,7) | Frozen at voting end (`freezing_amount=True`) |
| claimed | BooleanField | CB claimed back by voter; excluded from active counts |
| hide | BooleanField | Soft exclusion (spam / invalid / duplicate) |
| transaction_link | URLField | Horizon transactions URL for this CB |
| created_at | DateTimeField | CB creation timestamp |

**Unique constraint:** `unique_together = [['hide', 'claimable_balance_id']]` — allows one active + one hidden row per CB ID.

### HistoryProposal

| Field | Type | Notes |
|-------|------|-------|
| version | PositiveSmallIntegerField | Version number snapshotted |
| title / text | CharField/QuillField | Content at that version |
| transaction_hash | CharField(64, unique) | Payment tx for that version |
| envelope_xdr | TextField | XDR for that version |
| proposal | FK(Proposal, CASCADE) | Parent proposal |
| hide | BooleanField | Hidden history entries (submit snapshot is hidden) |
| created_at | DateTimeField | When this version was active |

---

## 6. Celery Tasks

### Beat Schedule

Defined in `aqua_governance/taskapp/__init__.py`.

| Task | Schedule | Purpose |
|------|----------|---------|
| `task_update_active_proposals` | Every 5 min | Re-indexes votes for all VOTING proposals |
| `task_sync_proposal_statuses_by_time` | Every 1 min | VOTING → VOTED at `end_at`; expires stale DISCUSSION rows; starts due QUEUED proposals |
| `task_check_pending_proposal_payments` | Every 1 min | The payment sweep: `check_transaction()` for every non-hidden row with a pending action |
| `task_check_expired_proposals` | Every 24h | Marks DISCUSSION → EXPIRED after 30 days inactive |
| `task_update_votes` | Every 10 min | Re-indexes votes for all VOTED proposals |
| `task_poll_submitted_onchain_executions` | Every 1 min | Polls Soroban for submitted asset-registry executions |
| `task_retry_failed_onchain_executions` | Every 10 min | Re-attempts FAILED/PENDING on-chain executions |

There are **no** signal receivers and no ETA-scheduled tasks: state advances by polling in
`task_sync_proposal_statuses_by_time`.

### The payment sweep

`task_check_pending_proposal_payments` is the unattended caller of `check_transaction()`,
so three properties of it are load-bearing:

- **A session-level advisory lock** (`PROPOSAL_PAYMENT_SWEEP_ADVISORY_LOCK_ID`, taken in
  `db_locks.py`) makes a tick skip while the previous one is still running. At up to three
  Horizon round-trips per row the sweep can outlast its own 60 s period. The lock is
  session-level, not `pg_advisory_xact_lock`: the sweep must not hold a transaction open
  across Horizon calls, and its id must differ from the asset-transition lock, which the
  promotions themselves take.
- **Per-row `try/except`**, because promotion now raises for real — a rejected claim, a
  deadlock, a programming error. One poisoned row must not stop the rows behind it.
- **`.order_by('id')` plus a terminal-rejection filter**: rows whose pending hash equals
  `payment_check_rejected_hash` are excluded in SQL, so a permanently-failing row stops
  costing Horizon traffic. Re-arm one with `manage.py rearm_proposal_payment_check`.

### Task Call Chain

```
task_update_active_proposals
  → task_update_proposal_results(proposal_id, freezing_amount=False)
      → task_update_votes(proposal_id, False)       # indexes CBs, no vote freeze
      → update_proposal_final_results(proposal_id)  # sums + fetches supply

task_sync_proposal_statuses_by_time  [every minute; end_at reached]
  → task_update_proposal_results(proposal_id, freezing_amount=True)
      → task_update_votes(proposal_id, True)        # indexes CBs, sets voted_amount
      → update_proposal_final_results(proposal_id)  # final tally, then on-chain hook

task_check_pending_proposal_payments  [every minute]
  → proposal_transactions.check_transaction(proposal)   # per row, exceptions isolated
      → utils.payments.verify_payment(...)              # Horizon: payer, amount, memo
      → consumed_transactions.claim_transaction_hashes(...)   # last statement
```

---

## 7. API Endpoints

### URL Structure

| URL prefix | ViewSet | Version | Notes |
|-----------|---------|---------|-------|
| `api/proposals/` | ProposalsView | v1 legacy | List + retrieve **only** (POST returns 405); filtered to `created_at ≤ 2022-04-15` |
| `api/proposal/` | ProposalViewSet | v2 current | Full CRUD + submit + check_payment; excludes `id=65` |
| `api/test/proposal/` | TestProposalViewSet | test | Same as v2 without `id=65` exclusion; TODO: remove |
| `api/votes-for-proposal/` | LogVoteView | both | Vote listing only |
| `api/asset-proposal/` | AssetProposalViewSet | v2 | Asset-governance proposals (`ADD_ASSET` / `REMOVE_ASSET`) |
| `api/asset-tokens/` | AssetTokenView | v2 | On-chain asset-token registry |
| `api/proposal-queue/` | ProposalQueueViewSet | v2 | Weekly voting-slot booking / queue state |
| `open/cms/` | Django Admin | — | Staff interface |

Registered in `aqua_governance/governance/urls.py`.

### ProposalViewSet (v2) Custom Actions

| Action | Method | URL | Description |
|--------|--------|-----|-------------|
| `submit_proposal` | POST | `/api/proposal/{id}/submit/` | Stage a submit (`action=TO_SUBMIT`); requires ≥7 day discussion. Applies nothing on its own |
| `check_proposal_payment` | POST | `/api/proposal/{id}/check_payment/` | The promotion gate: verify the payment on Horizon and apply the staged transition. Takes no body |

### Filter Query Parameters

| Endpoint | Param | Values | Effect |
|---------|-------|--------|--------|
| `/api/proposal/` | `status` | `discussion` / `voting` / `voted` / `expired` | Filter by `proposal_status` |
| `/api/proposal/` | `owner_public_key` | Stellar public key | Filter by `proposed_by` |
| `/api/proposal/` | `vote_owner_public_key` | Stellar public key | Filter proposals voted on by account |
| `/api/proposal/` | `active` | any truthy value | With `vote_owner_public_key`: show proposals with *unclaimed* votes; without it: shows `claimed=False` |
| `/api/votes-for-proposal/` | `owner_public_key` | Stellar public key | Filter votes by `account_issuer` |
| `/api/votes-for-proposal/` | `proposal_id` | integer | Filter votes by proposal |
| Any | `ordering` | field names | Override sort order |
| Any | `limit` | integer | Override page size (default 30) |

### Serializer Classes (v2)

| Serializer | Used for | Key behaviors |
|-----------|----------|---------------|
| `ProposalCreateSerializer` | POST /proposal/ | Sets `draft=True`, `action=TO_CREATE`; `inspect_envelope` for the advisory verdict only |
| `AssetProposalCreateSerializer` | POST /asset-proposal/ | Same, plus the asset triple and narrative fields |
| `ProposalUpdateSerializer` | PUT/PATCH /proposal/{id}/ | Stages `new_*`; `proposed_by` and `text` are read-only, and `transaction_hash` / `envelope_xdr` are neither writable nor returned |
| `SubmitSerializer` | POST /proposal/{id}/submit/ | Stages `action=TO_SUBMIT`; validates `new_start_at`, `new_end_at` against the weekly queue |
| `ProposalDetailSerializer` | GET /proposal/{id}/ | Includes `history_proposal` (non-hidden) |
| `ProposalListSerializer` | GET /proposal/ | Includes `logvote_set` |

All three staging serializers reject a `transaction_hash` already held by another
proposal's hash column (`__iexact`) or already present in `ConsumedTransaction` — a
pre-payment 400 rather than an unrecoverable promotion failure. `serializers.py` (v1)
holds **read** serializers only; it no longer has a create path.

### `get_queryset()` Dynamic Filtering (ProposalViewSet)

| Action | Extra filter |
|--------|-------------|
| `retrieve`, `list` | No extra filter (EXPIRED proposals visible) |
| all other actions | `.exclude(proposal_status=EXPIRED)` |
| `submit_proposal` | `.filter(proposal_status=DISCUSSION, last_updated_at__lte=now-7days)` |
| `update`, `partial_update` | `.filter(proposal_status=DISCUSSION)` |
| `check_proposal_payment` | `.exclude(action=NONE)` (only proposals with pending action) |
| default | `.filter(draft=False)` |

---

## 8. Key Settings

### Stellar Assets

| Setting | Value |
|---------|-------|
| `AQUA_ASSET_CODE` | `AQUA` |
| `AQUA_ASSET_ISSUER` | `GBNZILSTVQZ4R7IKQDGHYGY2QXL5QOFJYQMXPKWRRM5PAV7Y4M67AQUA` |
| `GOVERNANCE_ICE_ASSET_CODE` | `governICE` |
| `GOVERNANCE_ICE_ASSET_ISSUER` | `GAXSGZ2JM3LNWOO4WRGADISNMWO4HQLG4QBGUZRKH5ZHL3EQBGX73ICE` |
| `GDICE_ASSET_CODE` | `gdICE` |
| `GDICE_ASSET_ISSUER` | `GAXSGZ2JM3LNWOO4WRGADISNMWO4HQLG4QBGUZRKH5ZHL3EQBGX73ICE` |

### Costs and Timing

| Setting | Value |
|---------|-------|
| `PROPOSAL_CREATE_OR_UPDATE_COST` | 100,000 AQUA — the exact amount a create or update payment must carry |
| `PROPOSAL_SUBMIT_COST` | 900,000 AQUA — the exact amount a submit payment must carry |
| `PROPOSAL_COST` | 1,000,000 — dead. Its only remaining reference is an unreached fallback branch in `payments._resolve_payment_amount`; every caller passes an explicit amount (TODO: remove) |
| `DISCUSSION_TIME` | `timedelta(days=7)` — minimum discussion before submit |
| `EXPIRED_TIME` | `timedelta(days=30)` — auto-expire DISCUSSION proposals |
| `NETWORK_PASSPHRASE` | Stellar Public Network passphrase |

Amounts are compared exactly, as `Decimal`. A 900,000 payment does not satisfy a
100,000 obligation and vice versa.

### Payment Verification

| Setting | Value |
|---------|-------|
| `PROPOSAL_LEGACY_MEMO_ACCEPTED` | `True`. Whether `sha256(text_html)` is still accepted alongside the canonical `AQUA-GOV\|v1\|…` memo. Flip to `False` for v3 — an env flip with a deploy-free rollback, and the step that can strand an in-flight payment |
| `PROPOSAL_PAYMENT_CHECK_THROTTLE_RATE` | `''` (off), wired to the `proposal_payment_check` DRF throttle scope. **No `CACHES` is configured**, so any rate is per-process locmem multiplied by the worker count — point `CACHES['default']` at Redis before enabling it |

### Advisory Lock IDs

Both are PostgreSQL advisory locks and both must stay distinct.

| Setting | Default | Scope |
|---------|---------|-------|
| `ASSET_PROPOSAL_TRANSITION_ADVISORY_LOCK_ID` | 94127051 | `pg_advisory_xact_lock`, held for one asset-proposal transition |
| `PROPOSAL_PAYMENT_SWEEP_ADVISORY_LOCK_ID` | 94127052 | `pg_try_advisory_lock` (session), held for a whole sweep run. Reusing the transition id would block every staging and promotion for the duration |

### External URLs

| Setting | URL |
|---------|-----|
| `AQUA_CIRCULATING_URL` | `https://cmc.aqua.network/api/coins/?q=circulating` |
| `ICE_CIRCULATING_URL` | `https://ice-distributor.aqua.network/api/distributions/stats/` |
| `DEFAULT_DISCORD_URL` | `https://discord.com/channels/862710317825392660/1046931670458187836` |

---

## 9. Important Patterns and Gotchas

1. **No authentication, and no ownership check either.** All API endpoints are `AllowAny`, and *nothing* in the request proves ownership: the posted envelope is unsigned, so any caller can name any source account, and Horizon hands out the signed envelope of any past transaction on request. `ProposalViewSet._reject_declared_owner_mismatch()` (formerly `_check_owner_permissions()`, a name that no longer exists) compares the declared envelope source to `proposed_by` **as an input-consistency check only** — it exists to give an honest client with the wrong wallet connected a legible 403. Do not put a security decision behind it. Authorization is the on-chain payment, checked at promotion time (§4). Consequently, **staging is open**: anyone can overwrite another account's `new_title` / `new_text` / `new_transaction_hash`. That is a known, documented residual — the fix is that promotion refuses to apply what the payment does not attest, not a check at the staging layer.

2. **QuillField serializer quirk**: `serializer_fields.QuillField.get_attribute()` hardcodes `instance.text.html` regardless of the field name. This works for `text` fields but must be overridden for `new_text`. `to_internal_value` wraps input HTML in a `Quill` object with empty delta.

3. **Hardcoded `id=65` exclusion**: `ProposalViewSet` base queryset has `.exclude(id=65)`. `TestProposalViewSet` overrides the queryset without this exclusion. Historical artifact — do not remove without checking data.

4. **Legacy v1 date cutoff**: `ProposalsView` (v1) hardcodes `created_at__lte=datetime(2022, 4, 15)`. Any proposal created after this date is invisible via the v1 API.

5. **`Proposal.save()` makes an outbound HTTP call on insert**: it fetches `ICE_CIRCULATING_URL` with **no timeout**, and `AssetProposalCreateSerializer.create` runs that save inside `transaction.atomic()`. One hung request therefore holds a write transaction open. Every test that creates a `Proposal` must wrap it in `_factories.patch_ice_circulating_supply()`.

6. **Staged `new_*` update pattern**: Updates/submits do not apply immediately. Fields are staged in `new_title`, `new_text`, `new_transaction_hash`, `new_envelope_xdr`, `new_start_at`, `new_end_at`, with `action` set. `check_transaction()` promotes them later (beat sweep or `/check_payment/`) — and promotes the transition it *verified*, never the staged columns re-read after the fact, since staging is unauthenticated and can change in between.

7. **`GenerateGrouKeyException` typo**: The exception class name is intentionally `GenerateGrouKeyException` (missing 'p'). It is imported consistently across the codebase — don't rename it without updating all imports.

8. **One VOTING proposal at a time**: `_start_due_scheduled_proposals` takes the asset-transition advisory lock, re-reads each candidate `select_for_update()`, checks `Proposal.has_active_voting_proposal_conflict()`, and `break`s after starting one. The global voting invariant lives in that loop, not in a constraint.

9. **`freezing_amount` flag**: When `True` (called at voting end), `voted_amount` is set to the current CB amount. When `False` (called during active voting), `voted_amount` stays `None`. This freezes the vote count at the moment voting closed.

10. **`partial_update` disabled**: `ProposalViewSet.partial_update()` delegates to `self.update()`, ignoring the partial flag. There is no PATCH-only path.

11. **`update_proposal_final_results` uses `update_fields`**: it saves only `['vote_for_result', 'vote_against_result', 'vote_abstain_result', 'ice_circulating_supply']`, then dispatches the on-chain hook if the ICE supply fetch was fresh. It lives in `task_logic/proposal_finalization.py`, not in `tasks.py`.

12. **Legacy `PROPOSAL_COST` is dead**: the four functions that used to read it (`check_payment`, `check_xdr_payment`, `check_proposal_status`, `check_transaction_xdr`) no longer exist. Every caller now passes an explicit `PROPOSAL_CREATE_OR_UPDATE_COST` (100K) or `PROPOSAL_SUBMIT_COST` (900K), compared exactly, so the constant is reachable only through an unexercised fallback branch in `payments._resolve_payment_amount`.

13. **A hash is spent once, globally**: `ConsumedTransaction` is keyed on `transaction_hash` alone and is never updated. Never write it directly — go through `claim_transaction_hashes()`, and keep the claim as the **last** statement of the applying transaction so a rollback releases it. Deleting a proposal leaves its claims behind (`SET_NULL`) on purpose.

14. **Operator commands** (`manage.py`, no other management commands exist):
    - `backfill_consumed_transactions [--dry-run]` — rebuilds the ledger from `Proposal.transaction_hash` and `HistoryProposal.transaction_hash`, skipping in-flight `TO_CREATE` rows. Idempotent; the same code migration `0031` runs. **Mandatory once after any deploy of this app that follows the migration.**
    - `rearm_proposal_payment_check <id> --action <ACTION> [--unhide]` — clears `payment_check_rejected_hash`, resets `payment_status`, re-sets `action`. The only remedy for a terminal verdict, since `action` and `payment_status` are in `ProposalAdmin.readonly_fields`. Logs the before/after at ERROR; does **not** un-burn a claimed hash and does not restore `draft`.

---

## 10. Development Setup

```bash
# Install dependencies
pipenv sync --dev

# Configure environment (copy and edit)
echo 'export DATABASE_URL="postgres://username:password@localhost/aqua_governance"' > .env

# Apply migrations
pipenv run python manage.py migrate --noinput

# Rebuild the payment ledger (mandatory once after a deploy that lands 0031;
# idempotent, so it is safe to re-run at any time)
pipenv run python manage.py backfill_consumed_transactions

# Run development server
pipenv run python manage.py runserver 0.0.0.0:8000

# Run Celery worker (separate terminal)
pipenv run celery -A aqua_governance.taskapp worker -l info

# Run Celery beat scheduler (separate terminal)
pipenv run celery -A aqua_governance.taskapp beat -l info
```

Settings module defaults to `config.settings.dev`. Set `DJANGO_SETTINGS_MODULE` to override.

---

## 11. Related Docs

Deeper design notes (overview, models, tasks, API, business logic) are kept in
the Aquarius team's internal knowledge base — ask the team for access.
