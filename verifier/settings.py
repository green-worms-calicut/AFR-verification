"""The service's settings, from the environment: secrets never live in code, files or logs."""

import os
from dataclasses import dataclass, field

import proof_check

ENV = {
    "database_url": "VERIFIER_DATABASE_URL",
    "webhook_secret": "VERIFIER_WEBHOOK_SECRET",  # gw_sales signs its requests with it
    "callback_secret": "VERIFIER_CALLBACK_SECRET",  # we sign our results with it
    "api_key": "VERIFIER_API_KEY",  # sent as X-API-Key with our results
    "gw_sales_url": "GW_SALES_URL",  # results are only posted under this address
    "s3_bucket": "VERIFIER_S3_BUCKET",  # the photos, read by s3Key; AWS credentials and region come the usual AWS ways
}
OPTIONAL = {
    # A client's defaultProject code that makes a disposal a Verra project trip; unset, no Verra checks.
    "verra_project": "VERIFIER_VERRA_PROJECT",
    # While changing the webhook secret: the new one, accepted as well as the old until gw_sales has switched.
    "webhook_secret_next": "VERIFIER_WEBHOOK_SECRET_NEXT",
}
MIN_SECRET = 32  # characters: a secret shorter than this is too easy to guess
SECRETS = ("webhook_secret", "callback_secret", "webhook_secret_next")
LOCAL = ("http://localhost", "http://127.0.0.1")  # where gw_sales may be reached without https: tests on this machine


@dataclass(frozen=True)
class Settings:
    database_url: str = field(repr=False)
    webhook_secret: str = field(repr=False)
    callback_secret: str = field(repr=False)
    api_key: str = field(repr=False)
    gw_sales_url: str
    s3_bucket: str
    typesafe_api_key: str = field(repr=False)
    verra_project: str | None = None
    webhook_secret_next: str | None = field(default=None, repr=False)

    def webhook_secrets(self) -> list[str]:
        """The secrets a request may be signed with: the current one, and the next while switching."""
        return [self.webhook_secret, *([self.webhook_secret_next] if self.webhook_secret_next else [])]

    @classmethod
    def from_env(cls) -> "Settings":
        """Settings from the environment; the TypeSafe key from TYPESAFE_API_KEY or, on a laptop, the keyring."""
        missing = [name for name in ENV.values() if not os.environ.get(name)]
        typesafe_api_key = proof_check.api_key()
        if not typesafe_api_key:
            missing.append("TYPESAFE_API_KEY (or the keyring)")
        if missing:
            raise SystemExit(f"Set {', '.join(missing)} in the environment.")
        settings = cls(**{key: os.environ[name] for key, name in ENV.items()},
                       **{key: os.environ.get(name) or None for key, name in OPTIONAL.items()},
                       typesafe_api_key=typesafe_api_key)
        short = [{**ENV, **OPTIONAL}[key] for key in SECRETS if (v := getattr(settings, key)) is not None and len(v) < MIN_SECRET]
        if short:
            raise SystemExit(f"Make {', '.join(short)} at least {MIN_SECRET} characters (openssl rand -hex 32).")
        if not settings.gw_sales_url.startswith("https://") and not settings.gw_sales_url.startswith(LOCAL):
            raise SystemExit("GW_SALES_URL must start with https://: our results and API key are sent there.")
        return settings
