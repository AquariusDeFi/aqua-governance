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

There is no in-repo CI; these gates are the contributor's responsibility.
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
                        │ Proposal.save() → post_save signal
                        ▼
┌────────────────────────────────────────────────────────────┐
│                      SIGNAL SYSTEM                          │
│  receivers.py: FieldTracker detects end_at change           │
│    → task_update_proposal_status.apply_async(eta=end_at)    │
└───────────────────────┬────────────────────────────────────┘
                        │
                        ▼
┌────────────────────────────────────────────────────────────┐
│                   CELERY BEAT TASKS                         │
│                                                              │
│  task_update_active_proposals (every 5 min)                  │
│    └→ task_update_proposal_results                           │
│         ├→ task_update_votes       (index CBs from Horizon) │
│         └→ _update_proposal_final_results (sum + supply)    │
│                                                              │
│  task_check_expired_proposals (every 24h)                    │
│  check_proposals_with_bad_horizon_error (every 10 min)       │
│  task_update_votes (every 10 min, for VOTED proposals)       │
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
    │   ├── models.py          # Proposal, LogVote, HistoryProposal
    │   ├── views.py           # ProposalViewSet (v2), ProposalsView (v1), LogVoteView
    │   ├── serializers.py     # v1 serializers (legacy)
    │   ├── serializers_v2.py  # ProposalCreate/Update/Submit/Detail/List serializers
    │   ├── serializer_fields.py  # QuillField: bridges QuillField model → HTML string
    │   ├── filters.py         # DRF filter backends (status, owner, vote_owner)
    │   ├── pagination.py      # CustomPageNumberPagination (adds ?limit= param)
    │   ├── tasks.py           # All Celery tasks + _make_new_vote/_make_updated_vote helpers
    │   ├── parser.py          # generate_vote_key, parse_vote (CB → LogVote)
    │   ├── receivers.py       # post_save signal → apply_async(eta=end_at)
    │   ├── exceptions.py      # ClaimableBalanceParsingError, GenerateGrouKeyException
    │   ├── admin.py           # Django admin configuration
    │   └── urls.py            # Router registrations for all ViewSets
    ├── utils/
    │   ├── payments.py        # check_proposal_status (on-chain), check_transaction_xdr (offline)
    │   ├── requests.py        # load_all_records (Horizon cursor-pagination helper)
    │   ├── signals.py         # DisableSignals context manager
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
  [draft=True]  ←── offline XDR check (check_transaction_xdr)
      │               FINE → payment_status=FINE
      │               else → hide=True, payment_status=<error>
      │
      ▼
 action=TO_CREATE → Proposal.check_transaction() (retry task or /check_payment)
      │                  verifies on-chain → draft=False, action=NONE
      ▼
 [DISCUSSION] ←── must wait DISCUSSION_TIME (7 days) before submit
      │
      │  (POST /api/proposal/{id}/submit/)
      ▼
 action=TO_SUBMIT → Proposal.check_transaction() → proposal_status=VOTING
      │                  sets start_at, end_at, action=NONE
      │
      ├── (end_at reached via ETA signal task) ──→ [VOTED]
      │
      └── (30 days inactive in DISCUSSION) ──→ [EXPIRED]
```

### Payment Verification — Two Paths

**Path 1: Offline XDR check** (`check_transaction_xdr` in `utils/payments.py`)
Called immediately when a proposal is created or updated (no Horizon round-trip).
1. Parse `envelope_xdr` with `TransactionEnvelope.from_xdr()`
2. Scan operations for a `Payment` of ≥ cost AQUA to `AQUA_ASSET_ISSUER`
3. Verify memo: `HashMemo(SHA256(text.html))` matches XDR memo
4. Returns `FINE | INVALID_PAYMENT | BAD_MEMO | HORIZON_ERROR`

**Path 2: On-chain check** (`check_proposal_status` in `utils/payments.py`)
Called by `Proposal.check_transaction()`, triggered via retry task or `/check_payment` endpoint.
1. Fetch transaction from Horizon by `transaction_hash`
2. Verify `transaction_info['successful']`
3. Call `check_payment()`: scans operations for valid AQUA payment
4. Verify memo hash matches `SHA256(proposal_text.html)`
5. Returns `FINE | HORIZON_ERROR | FAILED_TRANSACTION | INVALID_PAYMENT | BAD_MEMO`

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

**Tracker:** `voting_time_tracker = FieldTracker(fields=['end_at'])` — used by post_save signal.

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

| Task | Schedule | Purpose |
|------|----------|---------|
| `task_update_active_proposals` | Every 5 min | Re-indexes votes for all VOTING proposals |
| `task_check_expired_proposals` | Every 24h | Marks DISCUSSION → EXPIRED after 30 days inactive |
| `check_proposals_with_bad_horizon_error` | Every 10 min | Retries Horizon payment check for `HORIZON_ERROR` proposals |
| `task_update_votes` | Every 10 min | Re-indexes votes for all VOTED proposals |

### Signal-Triggered

| Signal condition | Task | ETA |
|-----------------|------|-----|
| `Proposal.end_at` changed (detected by FieldTracker in post_save) | `task_update_proposal_status` | `proposal.end_at` |

`task_update_proposal_status` checks `end_at <= now + 5s`, sets `proposal_status=VOTED`, then calls `task_update_proposal_results(freezing_amount=True)`.

### Task Call Chain

```
task_update_active_proposals
  → task_update_proposal_results(proposal_id, freezing_amount=False)
      → task_update_votes(proposal_id, False)       # indexes CBs, no vote freeze
      → _update_proposal_final_results(proposal_id)  # sums + fetches supply

task_update_proposal_status  [signal-triggered at end_at]
  → task_update_proposal_results(proposal_id, freezing_amount=True)
      → task_update_votes(proposal_id, True)         # indexes CBs, sets voted_amount
      → _update_proposal_final_results(proposal_id)  # final tally
```

---

## 7. API Endpoints

### URL Structure

| URL prefix | ViewSet | Version | Notes |
|-----------|---------|---------|-------|
| `api/proposals/` | ProposalsView | v1 legacy | List + retrieve + create; filtered to `created_at ≤ 2022-04-15` |
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
| `submit_proposal` | POST | `/api/proposal/{id}/submit/` | Submit a DISCUSSION proposal to VOTING; requires ≥7 day discussion |
| `check_proposal_payment` | POST | `/api/proposal/{id}/check_payment/` | Re-verify payment on-chain via Horizon |

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
| `ProposalCreateSerializer` | POST /proposal/ | Sets `draft=True`, `action=TO_CREATE`; calls `check_transaction_xdr` |
| `ProposalUpdateSerializer` | PUT /proposal/{id}/ | Sets `action=TO_UPDATE`; uses `new_*` fields |
| `SubmitSerializer` | POST /proposal/{id}/submit/ | Sets `action=TO_SUBMIT`; validates `new_start_at`, `new_end_at` |
| `ProposalDetailSerializer` | GET /proposal/{id}/ | Includes `history_proposal` (non-hidden) |
| `ProposalListSerializer` | GET /proposal/ | Includes `logvote_set` |

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
| `PROPOSAL_CREATE_OR_UPDATE_COST` | 100,000 AQUA |
| `PROPOSAL_SUBMIT_COST` | 900,000 AQUA |
| `PROPOSAL_COST` | 1,000,000 (legacy constant, TODO: remove) |
| `DISCUSSION_TIME` | `timedelta(days=7)` — minimum discussion before submit |
| `EXPIRED_TIME` | `timedelta(days=30)` — auto-expire DISCUSSION proposals |
| `NETWORK_PASSPHRASE` | Stellar Public Network passphrase |

### External URLs

| Setting | URL |
|---------|-----|
| `AQUA_CIRCULATING_URL` | `https://cmc.aqua.network/api/coins/?q=circulating` |
| `ICE_CIRCULATING_URL` | `https://ice-distributor.aqua.network/api/distributions/stats/` |
| `DEFAULT_DISCORD_URL` | `https://discord.com/channels/862710317825392660/1046931670458187836` |

---

## 9. Important Patterns and Gotchas

1. **No authentication**: All API endpoints are `AllowAny`. "Ownership" is verified by checking the XDR source account matches `proposed_by` in `_check_owner_permissions()`. No session or token auth.

2. **QuillField serializer quirk**: `serializer_fields.QuillField.get_attribute()` hardcodes `instance.text.html` regardless of the field name. This works for `text` fields but must be overridden for `new_text`. `to_internal_value` wraps input HTML in a `Quill` object with empty delta.

3. **Hardcoded `id=65` exclusion**: `ProposalViewSet` base queryset has `.exclude(id=65)`. `TestProposalViewSet` overrides the queryset without this exclusion. Historical artifact — do not remove without checking data.

4. **Legacy v1 date cutoff**: `ProposalsView` (v1) hardcodes `created_at__lte=datetime(2022, 4, 15)`. Any proposal created after this date is invisible via the v1 API.

5. **`DisableSignals` pattern**: `_update_proposal_final_results` wraps `proposal.save()` in `DisableSignals('aqua_governance.governance.receivers.save_final_result', sender=Proposal)` to prevent re-triggering the ETA scheduling signal when only updating vote result fields.

6. **Staged `new_*` update pattern**: Updates/submits do not apply immediately. Fields are staged in `new_title`, `new_text`, `new_transaction_hash`, `new_envelope_xdr`, `new_start_at`, `new_end_at`, with `action` set. `check_transaction()` is called later (retry task or `/check_payment` endpoint) to promote them.

7. **`GenerateGrouKeyException` typo**: The exception class name is intentionally `GenerateGrouKeyException` (missing 'p'). It is imported consistently across the codebase — don't rename it without updating all imports.

8. **`task_update_proposal_status` 5-second tolerance**: Uses `end_at <= timezone.now() + timedelta(seconds=5)` to handle slight scheduling delays. The task is ETA-scheduled so it may arrive slightly after the exact `end_at`.

9. **`freezing_amount` flag**: When `True` (called at voting end), `voted_amount` is set to the current CB amount. When `False` (called during active voting), `voted_amount` stays `None`. This freezes the vote count at the moment voting closed.

10. **`partial_update` disabled**: `ProposalViewSet.partial_update()` delegates to `self.update()`, ignoring the partial flag. There is no PATCH-only path.

11. **`_update_proposal_final_results` uses `update_fields`**: Only saves `['vote_for_result', 'vote_against_result', 'aqua_circulating_supply', 'ice_circulating_supply']`. This combined with `DisableSignals` prevents the post_save signal from re-scheduling ETA tasks.

12. **Legacy `PROPOSAL_COST`**: The constant `PROPOSAL_COST = 1000000` in settings is only used by the legacy `check_payment()` and `check_xdr_payment()` functions. All current code uses `PROPOSAL_CREATE_OR_UPDATE_COST` (100K) and `PROPOSAL_SUBMIT_COST` (900K).

---

## 10. Development Setup

```bash
# Install dependencies
pipenv sync --dev

# Configure environment (copy and edit)
echo 'export DATABASE_URL="postgres://username:password@localhost/aqua_governance"' > .env

# Apply migrations
pipenv run python manage.py migrate --noinput

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

External knowledge base at `../aquarius-knowledge/repos/aqua-governance/`:
- `Overview.md` — High-level project overview and tech stack
- `Models.md` — Complete model field reference with constraints and behaviors
- `Tasks.md` — Celery task details with pipeline diagrams
- `API.md` — API endpoint reference with filter and serializer details
- `Business Logic.md` — Payment validation flows, vote aggregation, signal system
- `aqua-governance MOC.md` — Map of Contents
