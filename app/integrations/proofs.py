import logging
from datetime import datetime
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import quote
from uuid import uuid4

from fastapi import UploadFile

from app.core.config import settings
from app.core.errors import AppError

logger = logging.getLogger(__name__)
ALLOWED_MIME = {"image/jpeg": "jpg", "image/png": "png"}
MAX_BYTES = 5 * 1024 * 1024


class ProofStorage:
    def __init__(self) -> None:
        self._client: Any | None = None

    @property
    def client(self) -> Any:
        if self._client is None:
            from google.cloud import storage

            self._client = storage.Client(project=settings.google_cloud_project_id or None)
        return self._client

    def upload(self, file: UploadFile, kind: str) -> dict[str, str]:
        extension = ALLOWED_MIME.get(file.content_type or "")
        if not extension:
            raise AppError(
                422,
                "VALIDATION_ERROR",
                "The request is invalid.",
                errors={"proof": ["Only JPG and PNG files are accepted."]},
            )
        content = file.file.read(MAX_BYTES + 1)
        if len(content) > MAX_BYTES:
            raise AppError(
                422,
                "VALIDATION_ERROR",
                "The request is invalid.",
                errors={"proof": ["File size must not exceed 5 MB."]},
            )
        if not settings.google_cloud_storage_bucket:
            raise AppError(503, "PROOF_STORAGE_UNAVAILABLE", "Proof storage is not configured.")
        now = datetime.now(settings.timezone)
        relative = PurePosixPath("proofs", kind, now.strftime("%Y/%m"), f"{uuid4()}.{extension}").as_posix()
        prefix = settings.google_cloud_storage_prefix.strip("/")
        object_name = f"{prefix}/{relative}" if prefix else relative
        try:
            blob = self.client.bucket(settings.google_cloud_storage_bucket).blob(object_name)
            blob.upload_from_string(content, content_type=file.content_type)
        except Exception as exc:
            raise AppError(502, "PROOF_STORAGE_ERROR", "Proof could not be uploaded.") from exc
        url = f"https://storage.googleapis.com/{settings.google_cloud_storage_bucket}/" + "/".join(
            quote(part) for part in object_name.split("/")
        )
        return {"path": relative, "url": url}

    def delete(self, relative_path: str) -> None:
        prefix = settings.google_cloud_storage_prefix.strip("/")
        object_name = f"{prefix}/{relative_path}" if prefix else relative_path
        try:
            self.client.bucket(settings.google_cloud_storage_bucket).blob(object_name).delete()
        except Exception:
            logger.warning("Failed to delete proof from GCS", extra={"path": relative_path}, exc_info=True)


proof_storage = ProofStorage()
