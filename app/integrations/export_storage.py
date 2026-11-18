"""Where generated files (the Excel exports of the monthly cut-offs) are kept.

Files are saved under *keys*: relative POSIX paths such as ``monthly-cutoffs/2026-07/cutoff-12.xlsx``.
Saving returns the file's *location*, which is what gets recorded (with the backend) and read back
later, so a file stays readable where it was written after the configuration changes.

``EXPORT_STORAGE_BACKEND`` picks where new files go:

``local``
    Files under ``EXPORT_STORAGE_DIR`` on the server's disk. Location: the key (relative to the
    directory).
``gcs``
    Objects in the Google Cloud Storage bucket ``EXPORT_GCS_BUCKET`` under ``EXPORT_GCS_PREFIX``
    (defaults: ``GOOGLE_CLOUD_STORAGE_BUCKET`` / ``GOOGLE_CLOUD_STORAGE_PREFIX``). Location:
    ``gs://<bucket>/<object>``. Credentials: the service account key file
    ``GOOGLE_APPLICATION_CREDENTIALS`` if set (also when it is only in ``.env``), otherwise the
    default credentials of the environment (e.g. the service account of the VM / Cloud Run
    service). Objects are private (the bucket's access control) and served through the API's
    download endpoint. For local development, ``STORAGE_EMULATOR_HOST`` points the client at an
    emulator.

To add a backend, implement :class:`ExportStorage` and register it in :func:`export_storage`.
"""

import os
from pathlib import Path, PurePosixPath
from typing import Any, Protocol
from uuid import uuid4

from app.core.config import settings

LOCAL, GCS = "local", "gcs"
GCS_SCHEME = "gs://"
GCS_TIMEOUT_SECONDS = 60


class ExportFileMissing(LookupError):
    """There is no file at the location (deleted, or the storage directory changed)."""


class ExportStorageError(RuntimeError):
    """The storage could not be reached or refused the operation (e.g. permissions)."""


class ExportStorage(Protocol):
    backend: str

    def save(self, key: str, content: bytes, content_type: str) -> str:
        """Stores ``content`` under ``key``, replacing any previous file, atomically; returns the
        location to record (and give to :meth:`load`)."""

    def load(self, location: str) -> bytes:
        """The file at ``location``; :class:`ExportFileMissing` if there is none."""


def checked_key(key: str) -> PurePosixPath:
    path = PurePosixPath(key)
    if not key or path.is_absolute() or any(part in ("", ".", "..") for part in key.split("/")):
        raise ValueError(f"invalid storage key {key!r}")
    return path


# --- Local disk ---------------------------------------------------------------------------------


class LocalExportStorage:
    backend = LOCAL

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def path(self, key: str) -> Path:
        return self.root.joinpath(*checked_key(key).parts)

    def save(self, key: str, content: bytes, content_type: str = "") -> str:
        target = self.path(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        # Written next to the target, then renamed over it: a reader never sees half a file.
        temporary = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
        try:
            temporary.write_bytes(content)
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        return key

    def load(self, location: str) -> bytes:
        try:
            return self.path(location).read_bytes()
        except FileNotFoundError as exc:
            raise ExportFileMissing(location) from exc
        except OSError as exc:
            raise ExportStorageError(f"could not read {location}: {exc}") from exc


# --- Google Cloud Storage -----------------------------------------------------------------------

_gcs_client: Any = None


def gcs_client() -> Any:
    """One client per process, created on first use (the ``gcs`` backend only)."""
    global _gcs_client
    if _gcs_client is None:
        from google.cloud import storage  # type: ignore[import-untyped]

        project = settings.google_cloud_project_id or None
        credentials_file = settings.google_application_credentials
        if credentials_file and not os.environ.get("STORAGE_EMULATOR_HOST"):
            # Read from settings, so a path set only in .env works too (the library only looks at
            # the process environment).
            _gcs_client = storage.Client.from_service_account_json(credentials_file, project=project)
        else:
            _gcs_client = storage.Client(project=project)
    return _gcs_client


def parse_gcs_location(location: str) -> tuple[str, str]:
    """``gs://bucket/object`` -> ``(bucket, object)``."""
    bucket, _, name = location.removeprefix(GCS_SCHEME).partition("/")
    if not location.startswith(GCS_SCHEME) or not bucket or not name:
        raise ValueError(f"invalid Google Cloud Storage location {location!r}")
    return bucket, name


class GcsExportStorage:
    backend = GCS

    def __init__(self, bucket: str, prefix: str = "") -> None:
        self.bucket = bucket
        self.prefix = prefix.strip("/")

    def object_name(self, key: str) -> str:
        relative = checked_key(key).as_posix()
        return f"{self.prefix}/{relative}" if self.prefix else relative

    def save(self, key: str, content: bytes, content_type: str) -> str:
        if not self.bucket:
            raise ExportStorageError(
                "no Google Cloud Storage bucket configured: set EXPORT_GCS_BUCKET (or GOOGLE_CLOUD_STORAGE_BUCKET)"
            )
        name = self.object_name(key)
        location = f"{GCS_SCHEME}{self.bucket}/{name}"
        from google.cloud.storage.retry import DEFAULT_RETRY  # type: ignore[import-untyped]

        try:
            blob = gcs_client().bucket(self.bucket).blob(name)
            blob.cache_control = "no-store"  # finance data: never kept by a cache
            # An upload replaces the object atomically. Retried on transient errors: writing the same
            # key again is harmless (the library only retries uploads by default with preconditions).
            blob.upload_from_string(content, content_type=content_type, timeout=GCS_TIMEOUT_SECONDS, retry=DEFAULT_RETRY)
        except Exception as exc:
            raise ExportStorageError(f"could not upload {location}: {exc}") from exc
        return location

    def load(self, location: str) -> bytes:
        bucket, name = parse_gcs_location(location)
        from google.api_core.exceptions import NotFound
        from google.cloud.storage.retry import DEFAULT_RETRY

        try:
            content: bytes = (
                gcs_client().bucket(bucket).blob(name).download_as_bytes(timeout=GCS_TIMEOUT_SECONDS, retry=DEFAULT_RETRY)
            )
        except NotFound as exc:
            raise ExportFileMissing(location) from exc
        except Exception as exc:
            raise ExportStorageError(f"could not download {location}: {exc}") from exc
        return content


def export_storage(backend: str | None = None) -> ExportStorage:
    """The storage of ``backend``: where new files go by default (``EXPORT_STORAGE_BACKEND``), or
    the backend a file was written with, to read it."""
    backend = backend or settings.export_storage_backend
    if backend == LOCAL:
        return LocalExportStorage(settings.export_storage_dir)
    if backend == GCS:
        return GcsExportStorage(settings.export_bucket, settings.export_prefix)
    raise ValueError(f"unknown export storage backend {backend!r}")
