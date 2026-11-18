from functools import lru_cache
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "Farm Management Finance API"
    app_env: str = "local"
    app_timezone: str = "Asia/Jakarta"
    database_url: str = "postgresql+psycopg://postgres:postgres@127.0.0.1:5432/fm_finance_finance"
    jwt_secret: str = ""
    jwt_issuer: str = "fm-platform-api"
    jwt_audience: str = "fm-frontend"
    jwt_ttl_hours: int = Field(default=8, ge=1, le=24)
    cors_allowed_origins: Annotated[list[str], NoDecode] = Field(default_factory=list)
    google_cloud_project_id: str = ""
    google_cloud_storage_bucket: str = ""
    google_cloud_storage_prefix: str = ""
    google_application_credentials: str = ""
    log_level: str = "INFO"
    operational_service_url: str = "http://127.0.0.1:8001"
    operational_internal_api_key: str = ""
    operational_timeout_seconds: float = Field(default=5, gt=0, le=60)
    # Where the Excel exports of the monthly cut-offs are stored (app.integrations.export_storage):
    # "local": files under EXPORT_STORAGE_DIR (relative paths are resolved from the working
    #   directory; use an absolute path on servers, on a disk that is backed up);
    # "gcs": objects in the Google Cloud Storage bucket EXPORT_GCS_BUCKET, under EXPORT_GCS_PREFIX
    #   (each defaults to GOOGLE_CLOUD_STORAGE_BUCKET / GOOGLE_CLOUD_STORAGE_PREFIX, the bucket of the
    #   payment proofs), with GOOGLE_APPLICATION_CREDENTIALS or the default credentials.
    # Switching backend affects new files only: every file is read back from where it was written.
    export_storage_backend: Literal["local", "gcs"] = "local"
    export_storage_dir: str = "storage/exports"
    export_gcs_bucket: str = ""
    export_gcs_prefix: str = ""
    # Automatic generation of a cut-off's Excel export (right after the close, then by the hourly
    # monthly_cutoff --pending run) stops after this many failed attempts in a row; a user can still
    # generate it from the frontend (POST /monthly-cutoffs/export).
    export_max_automatic_attempts: int = Field(default=3, ge=1, le=20)
    # A generation still running after this long is considered interrupted (the process died).
    export_generation_timeout_minutes: int = Field(default=15, ge=1, le=24 * 60)
    # A background cut-off/export job (monthly_cutoff_jobs) still running after this long is
    # considered interrupted (its worker died); the month can then be queued again.
    cutoff_job_timeout_minutes: int = Field(default=60, ge=5, le=24 * 60)

    @field_validator("cors_allowed_origins", mode="before")
    @classmethod
    def split_origins(cls, value: object) -> object:
        if isinstance(value, str):
            return [part.strip() for part in value.split(",") if part.strip()]
        return value

    @property
    def timezone(self) -> ZoneInfo:
        return ZoneInfo(self.app_timezone)

    @property
    def export_bucket(self) -> str:
        return self.export_gcs_bucket or self.google_cloud_storage_bucket

    @property
    def export_prefix(self) -> str:
        return (self.export_gcs_prefix or self.google_cloud_storage_prefix).strip("/")

    def validate_export_storage(self) -> None:
        if self.export_storage_backend == "gcs" and not self.export_bucket:
            raise RuntimeError(
                "EXPORT_STORAGE_BACKEND=gcs needs a bucket: set EXPORT_GCS_BUCKET (or GOOGLE_CLOUD_STORAGE_BUCKET)"
            )

    def validate_runtime_secrets(self) -> None:
        if not self.jwt_secret or len(self.jwt_secret) < 32:
            raise RuntimeError("JWT_SECRET must contain at least 32 characters")
        if not self.operational_internal_api_key or len(self.operational_internal_api_key) < 24:
            raise RuntimeError("OPERATIONAL_INTERNAL_API_KEY must contain at least 24 characters")


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
