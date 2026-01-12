from __future__ import annotations

import logging
from dataclasses import dataclass

from py_clob_client.client import ClobClient

from polymarket_bot.config import Settings

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ApiCreds:
    api_key: str
    api_secret: str
    api_passphrase: str


def build_clob_client(settings: Settings) -> tuple[ClobClient, ApiCreds]:
    """Create a CLOB client and derive/create API creds.

    Supports both:
    - Magic/proxy funding (signature_type=1 + funder required)
    - EOA (signature_type=0)

    Returns a trading-ready client + API creds (used for websocket auth).
    """

    if not settings.poly_private_key:
        raise ValueError("POLY_PRIVATE_KEY is required")

    if settings.poly_signature_type in (1, 2) and not settings.poly_funder_address:
        raise ValueError("POLY_FUNDER_ADDRESS is required for proxy/safe signature types")

    funder: str | None = settings.poly_funder_address

    # Note: py_clob_client uses `key=` for the private key.
    if settings.poly_signature_type in (1, 2):
        assert funder is not None
        client = ClobClient(
            settings.poly_host,
            key=settings.poly_private_key,
            chain_id=settings.poly_chain_id,
            signature_type=settings.poly_signature_type,
            funder=funder,
        )
    else:
        client = ClobClient(
            settings.poly_host,
            key=settings.poly_private_key,
            chain_id=settings.poly_chain_id,
        )

    creds_raw = client.derive_api_key()

    # Some versions return a dict-like, others a pydantic-ish model. Normalize.
    if hasattr(creds_raw, "model_dump"):
        creds: dict = creds_raw.model_dump()  # type: ignore[assignment]
    elif isinstance(creds_raw, dict):
        creds = creds_raw
    elif hasattr(creds_raw, "__dict__"):
        creds = dict(getattr(creds_raw, "__dict__"))
    else:
        raise RuntimeError(f"Unexpected api creds type from derive_api_key(): {type(creds_raw)}")

    # py_clob_client returns keys under various casings depending on version.
    api_key = creds.get("apiKey") or creds.get("api_key")
    api_secret = creds.get("secret") or creds.get("api_secret")
    api_passphrase = creds.get("passphrase") or creds.get("api_passphrase")

    if not api_key or not api_secret or not api_passphrase:
        raise RuntimeError(f"Unexpected api creds shape from derive_api_key(): {creds}")

    log.info("Derived API key for websocket + trading.")

    return client, ApiCreds(api_key=api_key, api_secret=api_secret, api_passphrase=api_passphrase)
