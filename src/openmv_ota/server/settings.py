"""Server configuration.

Read from ``OPENMV_OTA_*`` environment variables (with Render's bare ``PORT`` / ``DATABASE_URL``
also honored), **or passed programmatically** -- kwargs override the environment, so OpenMV's
website can inject DB/R2/swd-ids config directly (``create_app(ServerSettings(**overrides))``).
ENV is the self-host convenience.
"""

from __future__ import annotations

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

_SECRET_FIELDS = frozenset(
    {"s3_secret_access_key", "s3_access_key_id", "admin_bootstrap_token",
     "swd_ids_verify_token", "cohort_salt"}
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

    # Render injects a bare DATABASE_URL for its managed Postgres; default to a local sqlite file.
    database_url: str = Field(
        default="sqlite:///./ota.db",
        validation_alias=AliasChoices("OPENMV_OTA_DATABASE_URL", "DATABASE_URL"),
    )

    admin_bootstrap_token: str = ""        # seeds the root admin token on first `server init`
    swd_ids_verify_url: str = ""           # the registration dependency -- required to serve
    swd_ids_verify_token: str = ""
    cohort_salt: str = ""                  # the server HMAC secret (capability tokens); persisted if unset
    checkin_rate_per_min: int = 60         # per-IP device check-in rate limit (0 = disabled)
    poll_after_s: int = 3600               # backoff the device is told to wait before polling again
    capability_ttl: int = 3600             # lifetime of an artifact capability token
    # uvicorn forwarded-allow-ips: which upstream peers may set X-Forwarded-For. Behind a PaaS proxy
    # (Render/Fly) set "*" so the rate limiter sees the real client IP, not the proxy's single IP.
    trusted_proxy_ips: str = "127.0.0.1"
    # Add/correct firmware-board-name -> swd-ids code mappings without a redeploy (JSON in env),
    # e.g. OPENMV_OTA_BOARD_CODE_OVERRIDES='{"ARDUINO_PORTENTA_H7":"H7"}'. Merged over boardmap defaults.
    board_code_overrides: dict[str, str] = {}

    def missing(self) -> list[str]:
        """Settings required before the server can serve devices (used by ``server check``)."""
        need = []
        if self.storage_backend not in ("local", "s3"):
            need.append("storage_backend (local|s3)")
        if self.storage_backend == "s3" and not self.s3_bucket:
            need.append("s3_bucket")
        if not self.swd_ids_verify_url:
            need.append("swd_ids_verify_url")
        if not self.swd_ids_verify_token:
            need.append("swd_ids_verify_token")
        return need

    def summary(self) -> list[str]:
        """Printable ``key = value`` lines with secrets redacted (for ``server check``)."""
        out = []
        for name in type(self).model_fields:
            val = getattr(self, name)
            if name in _SECRET_FIELDS and val:
                val = "***"
            out.append("%s = %s" % (name, val))
        return out
