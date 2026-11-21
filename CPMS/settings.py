from pathlib import Path
from environs import Env
from corsheaders.defaults import default_headers

BASE_DIR = Path(__file__).resolve().parent.parent

env = Env()
env.read_env(BASE_DIR / '.env')


SECRET_KEY = 'django-insecure-3bo%j#oo@tfu+bj!n&l#5_3@f+r4n$=1nvv0%4(@l_u-g@2*b3'

DEBUG = True

ALLOWED_HOSTS = ["*"]


INSTALLED_APPS = [
    "corsheaders",

    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',

    'django_extensions',
    "managementsys.apps.ManagementsysConfig",
    "rest_framework",
]

MIDDLEWARE = [
    "corsheaders.middleware.CorsMiddleware",
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
    'managementsys.discord_alerts.DiscordErrorAlertMiddleware',
    'managementsys.activity_log.ActivityLogMiddleware',
]

ACTIVITY_LOG_ENABLED = env.bool('ACTIVITY_LOG_ENABLED', True)

CORS_ALLOW_ALL_ORIGINS = True
CORS_ALLOW_HEADERS = list(default_headers) + ["x-branch-id"]

ROOT_URLCONF = 'CPMS.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'CPMS.wsgi.application'


DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.postgresql',
        'NAME': env.str('DB_NAME', 'cpms'),
        'USER': env.str('DB_USER', 'postgres'),
        'PASSWORD': env.str('DB_PASSWORD', 'seesaw'),
        'HOST': env.str('DB_HOST', 'localhost'),
        'PORT': env.int('DB_PORT', 5432),
    },
    'external': {
        'ENGINE': 'django.db.backends.postgresql',
        'NAME': env.str('EXTERNAL_DB_NAME', 'ipos'),
        'USER': env.str('EXTERNAL_DB_USER', 'postgres'),
        'PASSWORD': env.str('EXTERNAL_DB_PASSWORD', 'seesaw'),
        'HOST': env.str('EXTERNAL_DB_HOST', 'localhost'),
        'PORT': env.int('EXTERNAL_DB_PORT', 5432),
    }
}


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


LANGUAGE_CODE = 'en-us'

TIME_ZONE = 'UTC'

USE_I18N = True

USE_TZ = True


STATIC_URL = 'static/'

MEDIA_URL = '/media/'
MEDIA_ROOT = BASE_DIR / 'media'


DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

DISCORD_WEBHOOK_URL       = env.str('DISCORD_WEBHOOK_URL', '')
DISCORD_ALERT_STATUSES    = env.str('DISCORD_ALERT_STATUSES', '404,500-599')
DISCORD_ALERT_COOLDOWN    = env.int('DISCORD_ALERT_COOLDOWN', 60)
DISCORD_ALERT_ENV_NAME    = env.str('DISCORD_ALERT_ENV_NAME', 'CPMS dev')
DISCORD_ALERT_MENTION     = env.str('DISCORD_ALERT_MENTION', '')
DISCORD_ALERT_INCLUDE_BODY = env.bool('DISCORD_ALERT_INCLUDE_BODY', True)
DISCORD_ALERT_IGNORE_PATHS = env.list('DISCORD_ALERT_IGNORE_PATHS', ['/favicon.ico', '/static/'])

CPMS_VERCEL_URL    = env.str('CPMS_VERCEL_URL', 'https://cpms-dashboard-api.vercel.app')
CPMS_INGEST_SECRET = env.str('CPMS_INGEST_SECRET', '')

CPMS_RESERVATION_DURATION_MINUTES = env.int('CPMS_RESERVATION_DURATION_MINUTES', 30)
CPMS_RESERVATION_BRANCH_ID = env.int('CPMS_RESERVATION_BRANCH_ID', 0) or None

REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': [
        'managementsys.auth_backend.AppUserAuthentication',
    ],
    'DEFAULT_PERMISSION_CLASSES': [
        'managementsys.auth_backend.IsAppAuthenticated',
    ],
}
