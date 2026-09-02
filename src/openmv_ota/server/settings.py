"""Server configuration.

Read from ``OPENMV_OTA_*`` environment variables (with the bare ``PORT`` / ``DATABASE_URL``
that PaaS platforms inject
also honored), **or passed programmatically** -- kwargs override the environment, so OpenMV's
website can inject DB/R2/swd-ids config directly (``create_app(ServerSettings(**overrides))``).
ENV is the self-host convenience.
"""

from __future__ import annotations

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

_SECRET_FIELDS = frozenset(
    {"s3_secret_access_key", "s3_access_key_id", "admin_bootstrap_token",
     "swd_ids_verify_token", "capability_secret", "live_token_secret", "datalake_token_secret"}
)


class ServerSettings(BaseSettings):
    # populate_by_name lets the website inject overrides by field name (`ServerSettings(port=...)`)
    # even where a field also has an env alias (PORT / DATABASE_URL).
    model_config = SettingsConfigDict(env_prefix="OPENMV_OTA_", extra="ignore",
                                      populate_by_name=True)

    base_url: str = ""                     # public https origin, for building capability URLs
    host: str = "0.0.0.0"
    port: int = Field(default=8080, validation_alias=AliasChoices("OPENMV_OTA_PORT", "PORT"))

    storage_backend: str = "local"         # "local" (disk, dev) | "s3" (R2/S3, prod)
    storage_location: str = "./ota-storage"
    s3_bucket: str = ""
    s3_endpoint_url: str = ""              # R2/MinIO endpoint
    s3_region: str = ""
    s3_access_key_id: str = ""
    s3_secret_access_key: str = ""

    # PaaS platforms inject a bare DATABASE_URL for managed Postgres; default to a local sqlite file.
    database_url: str = Field(
        default="sqlite:///./ota.db",
        validation_alias=AliasChoices("OPENMV_OTA_DATABASE_URL", "DATABASE_URL"),
    )

    admin_bootstrap_token: str = ""        # seeds the root admin token on first `server init`
    swd_ids_verify_url: str = ""           # the registration dependency -- required to serve
    swd_ids_verify_token: str = ""
    capability_secret: str = ""            # signs the download (capability) tokens; persisted at init if unset
    # TEST-ONLY. Relaxes the server's OFFER-side anti-rollback (rollout.offers_update) so a
    # rollout can offer a release at/below a device's current version -- the one condition a
    # correct server never produces, which is exactly why the DEVICE's own anti-rollback (the
    # real safety boundary, always on) can't otherwise be exercised on real hardware. This is
    # SAFE to expose because it cannot cause a rollback: it only makes the server OFFER a
    # downgrade; every device still rejects it itself (that rejection is what it lets us test).
    # Misuse in production just wastes an offer the fleet declines -- never a downgrade. Off by
    # default; when on, create_app() logs a loud warning and `server check` flags it. Never set
    # it on a production deployment.
    test_offer_downgrades: bool = False
    checkin_rate_per_min: int = 60         # per-IP device check-in rate limit (0 = disabled)
    poll_after_s: int = 3600               # backoff the device is told to wait before polling again
    capability_ttl: int = 3600             # lifetime of an artifact capability token
    # OpenMV Live: when BOTH are set, every registered device's check-in response carries a
    # `live` grant (ready-made relay URLs + a camera token). The secret is shared with the
    # live-relay worker (openmv-cloud services/live-relay), which accepts either env name so
    # one value works fleet-wide.
    live_relay_url: str = Field(           # public origin, e.g. https://live.cloud.openmv.io
        default="",
        validation_alias=AliasChoices("OPENMV_OTA_LIVE_RELAY_URL", "OPENMV_LIVE_RELAY_URL"))
    live_token_secret: str = Field(
        default="",
        validation_alias=AliasChoices("OPENMV_OTA_LIVE_TOKEN_SECRET", "OPENMV_LIVE_TOKEN_SECRET"))
    live_token_ttl: int = 86400            # seconds; outlives a deep-sleep cycle, renewed each check-in
    # OpenMV datalake: when BOTH are set, registered devices get an `ingest` grant each
    # check-in -- a ready-made ingest URL + token for logs/telemetry. Deliberately its
    # OWN secret and TTL, decoupled from Live: the two integrations rotate (and fail)
    # independently, and the datalake service already reads
    # OPENMV_DATALAKE_TOKEN_SECRET as its primary env name.
    datalake_url: str = Field(             # public origin, e.g. https://data.cloud.openmv.io
        default="",
        validation_alias=AliasChoices("OPENMV_OTA_DATALAKE_URL", "OPENMV_DATALAKE_URL"))
    datalake_token_secret: str = Field(
        default="",
        validation_alias=AliasChoices("OPENMV_OTA_DATALAKE_TOKEN_SECRET",
                                      "OPENMV_DATALAKE_TOKEN_SECRET"))
    datalake_token_ttl: int = 86400        # seconds; renewed each check-in, like Live's
    # Browser origins allowed to call this API cross-origin, comma-separated, e.g.
    # OPENMV_OTA_CORS_ALLOW_ORIGINS="https://cloud.openmv.io,https://staging.openmv.io".
    # EMPTY BY DEFAULT, which means no CORS headers at all -- a browser on another origin simply
    # cannot read a response, which is the correct default for an API whose credential is a bearer
    # token. Only a deployment that actually serves a UI from a DIFFERENT origin needs this; a UI
    # served by this same app, or one that proxies through its own backend, must leave it unset.
    # "*" is REFUSED at startup (see create_app): with `allow_credentials` off a wildcard would
    # still let any page on the internet read admin responses using a token it somehow obtained,
    # and an explicit allowlist costs one env var. Starlette would honour a "*" here, so refusing
    # it has to be done by us -- otherwise the obvious thing to type silently opens the API up.
    cors_allow_origins: str = ""
    # uvicorn forwarded-allow-ips: which upstream peers may set X-Forwarded-For. Behind a PaaS proxy
    # set "*" behind a PaaS proxy so the rate limiter sees the real client IP, not the proxy's.
    trusted_proxy_ips: str = "127.0.0.1"

    def missing(self) -> list[str]:
        """Settings required before the server can serve devices (used by ``server check``)."""
        need = []
        if self.storage_backend not in ("local", "s3"):
            need.append("storage_backend (local|s3)")
        if self.storage_backend == "s3" and not self.s3_bucket:
            need.append("s3_bucket")
        # Registration is deliberately NOT required: a self-host that cannot reach
        # OpenMV's registration server still works, degraded to READ-ONLY serving
        # (offers work; no device registry, telemetry, or grants). create_app() and
        # `server check` both say so loudly instead of failing.
        return need

    def summary(self) -> list[str]:
        """Printable ``key = value`` lines with secrets redacted (for ``server check``)."""
        out = []
        for name in type(self).model_fields:
            val = getattr(self, name)
            # The test-only downgrade hook is hidden while off (it is not a normal knob), and
            # shouts when on so a misconfigured deployment can't miss it.
            if name == "test_offer_downgrades":
                if val:
                    out.append("test_offer_downgrades = True  <-- TEST MODE, never in production")
                continue
            if name in _SECRET_FIELDS and val:
                val = "***"
            out.append("%s = %s" % (name, val))
        return out
