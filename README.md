# Aquarius Governance

Backend API for the Aquarius DAO on the Stellar network. It manages the full
governance-proposal lifecycle — creation, discussion, weekly voting-slot booking,
on-chain voting via Stellar claimable balances, result tallying with a quorum
check, and (for asset proposals) execution of the outcome through a Soroban
on-chain asset registry.

Live: <https://gov.aqua.network/>

## Place in the Aquarius system

- **Serves** the Aquarius web frontend with proposal, vote, asset-token and
  voting-queue data.
- **Reads** on-chain state and vote claimable balances from **Stellar Horizon**
  and executes asset-registry changes through **Soroban RPC**.
- **Pulls** circulating-supply figures from `cmc.aqua.network` (AQUA) and
  `ice-distributor.aqua.network` (ICE) to compute quorum.

Proposals are voted on-chain: voters send AQUA / `governICE` / `gdICE` as
claimable balances to per-proposal accounts; the service indexes and tallies them.

## Tech stack

Python 3.9 · Django 3.2 · Django REST Framework · Celery (RabbitMQ) · PostgreSQL · Stellar SDK (Horizon + Soroban) · django-quill-editor · Sentry.

## Getting started

Requires Python 3.9, PostgreSQL, and [`pipenv`](https://pipenv.pypa.io/).
RabbitMQ is only needed if you run Celery workers (in dev, tasks run eagerly).

```bash
# 1. Install dependencies (incl. dev tools)
pipenv sync --dev

# 2. Create a local .env (see Configuration). At minimum:
echo 'DATABASE_URL=postgres://postgres:postgres@localhost/aqua_governance' > .env

# 3. Apply migrations
pipenv run python manage.py migrate --noinput

# 4. Run the dev server (settings default to config.settings.dev)
pipenv run python manage.py runserver 0.0.0.0:8000
```

Admin and API are then served on port 8000 (`/open/cms/` for the admin).

## Commands

| Command | What it does |
|---|---|
| `pipenv sync --dev` | Install locked runtime + dev dependencies |
| `pipenv run python manage.py migrate --noinput` | Apply database migrations |
| `pipenv run python manage.py runserver 0.0.0.0:8000` | Run the development server |
| `pipenv run python manage.py test` | Run the Django test suite (`aqua_governance/governance/tests/`) |
| `pipenv run celery -A aqua_governance.taskapp worker -l info` | Run the Celery worker |
| `pipenv run celery -A aqua_governance.taskapp beat -l info` | Run the Celery beat scheduler |
| `pipenv run flake8` | Lint (config in `.flake8`, max line length 120) |
| `pipenv run isort .` | Sort imports (config in `.isort.cfg`) |
| `pipenv run black .` | Format code |

## Configuration

Settings live in `config/settings/` (`base.py` + `dev.py` / `prod.py`). Runtime
config comes from environment variables (loaded from `.env`, which is
git-ignored — **never commit secrets**). `manage.py` defaults to
`config.settings.dev`; production sets `DJANGO_SETTINGS_MODULE=config.settings.prod`.

Common variables:

| Variable | Meaning |
|---|---|
| `DATABASE_URL` | PostgreSQL connection string |
| `HORIZON_URL` | Stellar Horizon endpoint (dev defaults to testnet; prod to `horizon.stellar.org`) |
| `NETWORK_PASSPHRASE` | Stellar network passphrase (public vs testnet) |
| `SOROBAN_RPC_URL` | Soroban RPC endpoint for asset-registry execution |
| `ONCHAIN_ASSET_REGISTRY_CONTRACT_ID` | Soroban asset-registry contract id |
| `ONCHAIN_ASSET_REGISTRY_MANAGER_SECRET` | **Secret** signing key for on-chain execution — set via env only |
| `CELERY_BROKER_URL` | RabbitMQ broker URL (dev defaults to local) |
| `SECRET_KEY` | Django secret key (required in prod) |
| `ALLOWED_HOSTS`, `ADMINS`, `SENTRY_DSN`, `SENTRY_ENVIRONMENT` | Standard prod deployment settings |

Governance parameters (with defaults) include `PROPOSAL_CREATE_OR_UPDATE_COST`
(100,000 AQUA), `PROPOSAL_SUBMIT_COST` (900,000 AQUA), `DISCUSSION_TIME_DAYS` (7),
`EXPIRED_TIME_DAYS` (30) and the default 20% quorum (`percent_for_quorum`).
The AQUA / `governICE` / `gdICE` asset codes and issuers have public mainnet
defaults in `config/settings/base.py`.

## Structure

```
config/
  settings/           # base.py (constants) + dev.py / prod.py overrides
  urls.py             # /api/ → governance urls; /open/cms/ → admin
aqua_governance/
  governance/         # Core app: proposals, votes, history, assets
    models.py         # Proposal, LogVote, HistoryProposal, AssetToken, ProposalQueueSlot
    views.py          # Proposal/AssetProposal/ProposalQueue/LogVote/AssetToken viewsets
    serializers*.py   # v1 (legacy) + v2 serializers
    tasks.py, task_logic/   # Celery tasks (vote indexing, status updates, expiry)
    parser.py         # Claimable-balance → LogVote parsing, vote-key generation
    onchain_hooks/, onchain_actions.py  # Soroban asset-registry execution
    proposal_queue*.py      # Weekly voting-slot booking
    receivers.py      # post_save signal → ETA-scheduled status task
    tests/            # Django TestCase suite
  taskapp/            # Celery app + beat schedule
  utils/              # Stellar/Horizon helpers, payment verification
```

## Pointers

- [`AGENTS.md`](./AGENTS.md) — operating manual and conventions for contributors
  and AI agents (architecture, lifecycle, tasks, models, gotchas). `CLAUDE.md`
  points to the same file.
- Deeper design notes live in the team's internal knowledge base (ask the
  Aquarius team for access).
- Discussion: Aquarius Discord `#governance-voting` — <https://discord.gg/sgzFscHp4C>.
