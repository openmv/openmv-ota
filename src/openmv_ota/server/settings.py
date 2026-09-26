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

    # CVE monitoring: how often the scheduler re-scans every account's live releases
    # against OSV. 0 disables the loop (tests; deployments that trigger scans externally).
    # Daily is the industry norm and comfortably meets an "ongoing monitoring" bar.
    advisory_scan_interval_s: int = 86400

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
    # ...and a ceiling on one IPv6 /64 as a whole (0 = no /64 tier): a /64 is 2**64 addresses,
    # so without it the per-IP limit is one address rotation from meaningless. Higher than the
    # per-IP limit because every device at an IPv6 site shares the /64 (see ratelimit.py).
    checkin_rate_per_prefix_per_min: int = 600
    poll_after_s: int = 3600               # backoff the device is told to wait before polling again
    # Spread the backoff each device is handed by +/- this fraction, so a fleet that checked in
    # together -- a site powering on, an outage clearing -- does not come back in lockstep and
    # arrive as one thundering herd. The device respects whatever value it is told and neither
    # knows nor cares that it was jittered. 0 disables (an exact `poll_after_s` for every device).
    poll_jitter: float = 0.15
    # Upload ceilings. A publish token is a tenant's credential on a SHARED server, and
    # `await upload.read()` is an allocation sized by whoever is uploading -- the same
    # rule the device code is held to ("no allocation sized by anything we do not
    # control"), which the server was quietly breaking. Firmware images are single-digit
    # megabytes; these are generous by comparison and still bounded.
    max_image_bytes: int = 512 * 1024 * 1024
    max_manifest_bytes: int = 1024 * 1024
    max_sbom_bytes: int = 32 * 1024 * 1024

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
    # The datalake's OPERATOR credential, for the one write the update server makes there:
    # purging a forgotten device's data. Optional -- without it, forget leaves data to the
    # datalake's retention. A separate secret again, and a write one, so it lives on the
    # server alone (a viewer grant never carries it).
    # Webhooks: the worker thread's cadence (0 = no worker in this process; a deployment
    # sets it, tests and one-shot tools leave it off), the outbound timeout, and whether
    # endpoints may point at private addresses (a self-host behind its own firewall).
    webhook_interval_s: int = 0
    webhook_timeout_s: int = 10
    webhook_allow_private: bool = False
    datalake_admin_token: str = Field(
        default="",
        validation_alias=AliasChoices("OPENMV_OTA_DATALAKE_ADMIN_TOKEN",
                                      "OPENMV_DATALAKE_ADMIN_TOKEN"))
    # A dashboard's viewer grant (relay watch token + datalake read token) is handed to a
    # browser or a script and cannot be revoked short of rotating a secret, so it lives
    # MINUTES, not the day a sleeping camera's own grant needs. One TTL covers both halves:
    # the grant is one credential for one sitting.
    viewer_token_ttl: int = 300
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
