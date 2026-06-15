from datetime import timedelta

import environ
# Build paths inside the project like this: root(...)
from corsheaders.defaults import default_headers
from dateutil.relativedelta import relativedelta
from stellar_sdk import Network

env = environ.Env()

root = environ.Path(__file__) - 3
apps_root = root.path('aqua_governance')

BASE_DIR = root()

environ.Env.read_env()


# Base configurations
# --------------------------------------------------------------------------

ROOT_URLCONF = 'config.urls'
WSGI_APPLICATION = 'config.wsgi.application'

DEFAULT_AUTO_FIELD = 'django.db.models.fields.BigAutoField'


# Application definition
# --------------------------------------------------------------------------

DJANGO_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'django.contrib.sites',
    'django.contrib.sitemaps',
]

THIRD_PARTY_APPS = [
    'corsheaders',
    'django_quill',
]

LOCAL_APPS = [
    'aqua_governance.taskapp',
    'aqua_governance.governance',
]

INSTALLED_APPS = DJANGO_APPS + THIRD_PARTY_APPS + LOCAL_APPS


# Middleware configurations
# --------------------------------------------------------------------------

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'corsheaders.middleware.CorsMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.contrib.sites.middleware.CurrentSiteMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]


# Template configurations
# --------------------------------------------------------------------------

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [
            root('aqua_governance', 'templates'),
        ],
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
            'loaders': [
                'django.template.loaders.filesystem.Loader',
                'django.template.loaders.app_directories.Loader',
            ],
        },
    },
]


# Fixture configurations
# --------------------------------------------------------------------------

FIXTURE_DIRS = [
    root('aqua_governance', 'fixtures'),
]


# Password validation
# https://docs.djangoproject.com/en/1.9/ref/settings/#auth-password-validators
# --------------------------------------------------------------------------

AUTH_PASSWORD_VALIDATORS = [
    {
        'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator',
    },
]


# Internationalization
# https://docs.djangoproject.com/en/1.9/topics/i18n/
# --------------------------------------------------------------------------

LANGUAGE_CODE = 'en-us'

TIME_ZONE = 'UTC'

USE_I18N = True

USE_L10N = True

USE_TZ = True

SITE_ID = 1


# Static files (CSS, JavaScript, Images)
# https://docs.djangoproject.com/en/1.9/howto/static-files/
# --------------------------------------------------------------------------

STATIC_URL = '/static/'
STATIC_ROOT = env('STATIC_ROOT', default=root('static'))

STATICFILES_FINDERS = (
    'django.contrib.staticfiles.finders.AppDirectoriesFinder',
    'django.contrib.staticfiles.finders.FileSystemFinder',
)

STATICFILES_DIRS = [
    root('aqua_governance', 'assets'),
]

MEDIA_URL = '/media/'
MEDIA_ROOT = root('media')


# Celery configuration
# --------------------------------------------------------------------------

CELERY_ENABLED = env.bool('CELERY_ENABLED', default=True)

if CELERY_ENABLED:

    CELERY_ACCEPT_CONTENT = ['json']
    CELERY_TASK_SERIALIZER = 'json'
    CELERY_TASK_IGNORE_RESULT = True


# Rest framework configuration
# http://www.django-rest-framework.org/api-guide/settings/
# --------------------------------------------------------------------------

REST_FRAMEWORK = {
    'PAGE_SIZE': 30,
    'DEFAULT_PAGINATION_CLASS': 'rest_framework.pagination.PageNumberPagination',
    'DEFAULT_RENDERER_CLASSES': [
        'rest_framework.renderers.JSONRenderer',
    ],
}

# CORS headers settings
# ---------------------
CORS_ORIGIN_ALLOW_ALL = True
CORS_ALLOW_HEADERS = list(default_headers)


# QUILL settings
# ---------------------
QUILL_CONFIGS = {
    'default': {
        'theme': 'snow',
        'modules': {
            'syntax': True,
            'toolbar': [
                [
                    {'header': []},
                    'bold', 'italic', 'underline',
                ],
                [{'list': 'ordered'}, {'list': 'bullet'}],
                ['link'],
                ['clean'],
            ],
        },
    },
}

# AQUA info
# ---------------------

AQUA_ASSET_CODE = env('AQUA_ASSET_CODE', default='AQUA')
AQUA_ASSET_ISSUER = env('AQUA_ASSET_ISSUER', default='GBNZILSTVQZ4R7IKQDGHYGY2QXL5QOFJYQMXPKWRRM5PAV7Y4M67AQUA')
AQUA_CIRCULATING_URL = env('AQUA_CIRCULATING_URL', default='https://cmc.aqua.network/api/coins/?q=circulating')

GOVERNANCE_ICE_ASSET_CODE = env('GOVERNANCE_ICE_ASSET_CODE', default='governICE')
GOVERNANCE_ICE_ASSET_ISSUER = env('GOVERNANCE_ICE_ASSET_ISSUER', default='GAXSGZ2JM3LNWOO4WRGADISNMWO4HQLG4QBGUZRKH5ZHL3EQBGX73ICE')

GDICE_ASSET_CODE = env('GDICE_ASSET_CODE', default='gdICE')
GDICE_ASSET_ISSUER = env('GDICE_ASSET_ISSUER', default='GAXSGZ2JM3LNWOO4WRGADISNMWO4HQLG4QBGUZRKH5ZHL3EQBGX73ICE')
ICE_CIRCULATING_URL = env('ICE_CIRCULATING_URL', default='https://ice-distributor.aqua.network/api/distributions/stats/')

PROPOSAL_COST = env.int('PROPOSAL_COST', default=1000000)  # TODO: remove it
PROPOSAL_SUBMIT_COST = env.int('PROPOSAL_SUBMIT_COST', default=900000)
PROPOSAL_CREATE_OR_UPDATE_COST = env.int('PROPOSAL_CREATE_OR_UPDATE_COST', default=100000)

EXPIRED_TIME = timedelta(days=env.int('EXPIRED_TIME_DAYS', default=30))
DISCUSSION_TIME = timedelta(days=env.int('DISCUSSION_TIME_DAYS', default=7))
DEFAULT_MIN_VOTING_DURATION_DAYS = env.int('DEFAULT_MIN_VOTING_DURATION_DAYS', default=7)
ASSET_MIN_VOTING_DURATION_DAYS = env.int('ASSET_MIN_VOTING_DURATION_DAYS', default=7)
ASSET_QUEUE_GAP_SECONDS = env.int('ASSET_QUEUE_GAP_SECONDS', default=1)
PROPOSAL_QUEUE_BOOKING_HORIZON_WEEKS = env.int('PROPOSAL_QUEUE_BOOKING_HORIZON_WEEKS', default=6)
HORIZON_URL = env('HORIZON_URL', default='https://horizon.stellar.org')
NETWORK_PASSPHRASE = env('NETWORK_PASSPHRASE', default=Network.public_network().network_passphrase)

# Soroban / onchain hooks
# --------------------------------------------------------------------------
SOROBAN_RPC_URL = env('SOROBAN_RPC_URL', default='')
ONCHAIN_ASSET_REGISTRY_CONTRACT_ID = env('ONCHAIN_ASSET_REGISTRY_CONTRACT_ID', default='')
ONCHAIN_ASSET_REGISTRY_MANAGER_SECRET = env('ONCHAIN_ASSET_REGISTRY_MANAGER_SECRET', default='')
ASSET_PROPOSAL_TRANSITION_ADVISORY_LOCK_ID = env.int(
    'ASSET_PROPOSAL_TRANSITION_ADVISORY_LOCK_ID',
    default=env.int('ASSET_SUBMIT_ADVISORY_LOCK_ID', default=94127051),
)
ONCHAIN_SOROBAN_BASE_FEE = env.int('ONCHAIN_SOROBAN_BASE_FEE', default=100000)
ONCHAIN_SOROBAN_TIMEOUT = env.int('ONCHAIN_SOROBAN_TIMEOUT', default=120)
ONCHAIN_EXECUTION_LEASE_SECONDS = env.int('ONCHAIN_EXECUTION_LEASE_SECONDS', default=300)
ONCHAIN_TX_MAX_POLLS = env.int('ONCHAIN_TX_MAX_POLLS', default=20)
ONCHAIN_TX_POLL_INTERVAL_SECONDS = env.int('ONCHAIN_TX_POLL_INTERVAL_SECONDS', default=3)

# Discord info
# --------------------------------------------------------------------------

DEFAULT_DISCORD_URL = 'https://discord.com/channels/862710317825392660/1046931670458187836'
