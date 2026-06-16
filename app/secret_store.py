"""Secret loading.

In Lambda, secrets live in SSM Parameter Store as SecureString parameters under
a prefix (env var ``SSM_PREFIX``, e.g. ``/calorie-bot``) and are fetched once per
cold start, then cached. Locally (tests, dev) they come from environment
variables loaded from ``.env`` — no AWS calls needed.

Named ``secret_store`` rather than ``secrets`` on purpose: a module called
``secrets.py`` would shadow Python's stdlib ``secrets`` module.
"""
import functools
import os

_SSM_PREFIX = os.environ.get("SSM_PREFIX")  # e.g. "/calorie-bot"; unset locally

# logical name -> (SSM param suffix, local env var or None)
_SECRETS = {
    "telegram_bot_token": ("telegram-bot-token", "TELEGRAM_BOT_TOKEN"),
    "telegram_webhook_secret": ("telegram-webhook-secret", "TELEGRAM_WEBHOOK_SECRET"),
    "anthropic_api_key": ("anthropic-api-key", "ANTHROPIC_API_KEY"),
    "google_service_account": ("google-service-account", None),
}


@functools.lru_cache(maxsize=None)
def get_secret(name):
    """Return a secret value, raising if it can't be found."""
    suffix, env_var = _SECRETS[name]
    if env_var and os.environ.get(env_var):
        return os.environ[env_var]
    if _SSM_PREFIX:
        return _ssm_get(f"{_SSM_PREFIX}/{suffix}")
    raise RuntimeError(f"Secret {name!r} unavailable: no {env_var} env var and no SSM_PREFIX")


def get_secret_optional(name):
    """Like get_secret but returns None instead of raising when missing."""
    try:
        return get_secret(name)
    except Exception:
        return None


@functools.lru_cache(maxsize=None)
def _ssm_get(param_name):
    import boto3

    ssm = boto3.client("ssm")
    resp = ssm.get_parameter(Name=param_name, WithDecryption=True)
    return resp["Parameter"]["Value"]
