"""The service's settings, from the environment: secrets never live in code, files or logs."""

import os
from dataclasses import dataclass, field

ENV = {
    "database_url": "VERIFIER_DATABASE_URL",
    "webhook_secret": "VERIFIER_WEBHOOK_SECRET",  # gw_sales signs its requests with it
    "callback_secret": "VERIFIER_CALLBACK_SECRET",  # we sign our results with it
    "api_key": "VERIFIER_API_KEY",  # sent as X-API-Key with our results
    "gw_sales_url": "GW_SALES_URL",  # results are only posted under this address
}


@dataclass(frozen=True)
class Settings:
    database_url: str = field(repr=False)
    webhook_secret: str = field(repr=False)
    callback_secret: str = field(repr=False)
    api_key: str = field(repr=False)
    gw_sales_url: str

    @classmethod
    def from_env(cls) -> "Settings":
        missing = [name for name in ENV.values() if not os.environ.get(name)]
        if missing:
            raise SystemExit(f"Set {', '.join(missing)} in the environment.")
        return cls(**{key: os.environ[name] for key, name in ENV.items()})
