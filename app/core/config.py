from functools import lru_cache

from dotenv import load_dotenv
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# Load `.env` into the process environment as early as possible (import-time).
# pydantic-settings reads `.env` into the Settings object, but several
# best-effort integrations (SMTP email, SMS providers) read raw `os.getenv`.
# Without this, those credentials never reach `os.getenv` and delivery is
# silently skipped even when `.env` is fully configured. `override=False`
# keeps real OS env vars (prod/containers) authoritative over `.env`.
load_dotenv(override=False)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    app_env: str = "development"
    log_level: str = "INFO"

    database_url: str
    database_url_sync: str | None = None

    jwt_secret: str = Field(..., min_length=16)
    jwt_algorithm: str = "HS256"
    access_token_ttl_minutes: int = 720

    supabase_url: str | None = None
    supabase_service_role_key: str | None = None
    supabase_incident_bucket: str = "incident-attachments"

    cors_origins: str = "http://localhost:3000"

    # ── Module Entitlement & Licensing System ──────────────────────────────
    # Path to the signed .lic file. Offline-validated locally; no phone-home.
    # When unset, the validator looks for `licence.lic` in the backend root.
    # NOTE: this only tells the app WHERE the licence is — it can never GRANT
    # entitlements. Only the signed licence does (build prompt §5.3).
    licence_file_path: str | None = None
    # Alternative to the file: the full signed licence token, supplied via the
    # LICENCE_TOKEN env var. Robust for cloud/container backends with an
    # ephemeral filesystem (Vercel/Dokploy) where an uploaded file wouldn't
    # survive a restart. The file (if present) takes precedence.
    licence_token: str | None = None
    # Days before expiry that flip the status to EXPIRING_SOON (banner window).
    licence_warn_days: int = 14
    # Re-validate the licence on this cadence (seconds). Catches expiry roll-over
    # and clock-tamper between boots without a restart.
    licence_recheck_seconds: int = 3600
    # P2-1 background scheduler. Default off (opt-in) so a shared dev DB isn't
    # mutated by interval jobs; on-prem deployments set SCHEDULER_ENABLED=true.
    scheduler_enabled: bool = False

    # ─── PTW closed-loop: FLRA policy ────────────────────────────────────
    # FLRA is an optional sub-flow per permit (closed-loop rebuild). Instance-
    # level config matches the per-customer-instance deployment model:
    #   PTW_FLRA_REQUIRED_DEFAULT=true       → every permit requires an FLRA
    #   PTW_FLRA_REQUIRED_TYPES=HOT_WORK,CONFINED_SPACE
    #                                        → only these types require it
    # The resolved value is snapshotted onto Permit.flraRequired at creation.
    ptw_flra_required_default: bool = False
    ptw_flra_required_types: str = ""

    def ptw_flra_required_for(self, permit_type: str) -> bool:
        types = {t.strip().upper() for t in self.ptw_flra_required_types.split(",") if t.strip()}
        if types:
            return permit_type.upper() in types
        return self.ptw_flra_required_default

    # Security: echo the password-reset OTP back in the forgot-password response
    # for QA when there is no email gateway. OFF by default and must be opted in
    # explicitly — so even a misconfigured APP_ENV can never leak an OTP. Never
    # enable in any internet-reachable environment.
    expose_dev_otp: bool = False

    # ── Email (SMTP) ────────────────────────────────────────────────────────
    # Typed access to the same values `.env` exposes. The best-effort email
    # sender prefers these (falls back to os.getenv for backwards-compat).
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_user: str | None = None
    smtp_pass: str | None = None
    email_from: str | None = None

    # AI agents (Anthropic Claude). Optional — when unset, the
    # workflow-rule agents (Pattern A: triage / lessons) log a warning
    # and fall through gracefully so the workflow keeps working. The
    # user-initiated agent platform (Pattern B) cannot proceed without
    # the key and surfaces an ERRORED invocation if it's missing.
    anthropic_api_key: str | None = None
    # Default model for Pattern A agents.
    anthropic_model: str = "claude-haiku-4-5-20251001"
    # Escalation model used by Pattern B agents when low confidence or
    # explicit user request triggers a deeper analysis. Configured at
    # the agent level (Agent.escalationModelId), but this acts as the
    # platform-wide hint for newly-seeded agents.
    anthropic_escalation_model: str = "claude-opus-4-7"
    # Tool-loop iteration cap and per-turn output cap for Pattern B
    # agents. Surface here so the operations dashboard can tune them
    # without code edits.
    agent_max_tool_iterations: int = 8
    agent_max_tokens_per_turn: int = 4096

    @property
    def cors_origins_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def is_production(self) -> bool:
        return self.app_env.lower() == "production"

    # Driver normalisation. SQLAlchemy picks a dialect from the URL prefix —
    # if the user pastes the bare `postgresql://...` URL from Supabase's
    # connection-string panel, the async engine rejects it because the
    # default psycopg2 dialect is sync-only. We rewrite the prefix so the
    # right driver is always selected.
    @property
    def async_database_url(self) -> str:
        return _force_driver(self.database_url, "asyncpg")

    @property
    def sync_database_url(self) -> str:
        return _force_driver(self.database_url_sync or self.database_url, "psycopg2")


def _force_driver(url: str, driver: str) -> str:
    """Rewrite the URL's driver prefix to match `driver`. Accepts:
      postgres://...                  (legacy Heroku-style)
      postgresql://...                (no driver — SQLAlchemy default)
      postgresql+asyncpg://...
      postgresql+psycopg2://...
    """
    target = f"postgresql+{driver}://"
    url = url.strip()
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://") :]
    if url.startswith("postgresql+asyncpg://") or url.startswith("postgresql+psycopg2://"):
        # Replace whatever driver they wrote with the one this caller wants
        rest = url.split("://", 1)[1]
        return target + rest
    if url.startswith("postgresql://"):
        return target + url[len("postgresql://") :]
    return url  # let SQLAlchemy raise if the scheme is something else entirely


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
