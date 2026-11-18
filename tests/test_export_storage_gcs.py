"""Checks for the Google Cloud Storage backend of the export storage
(app/integrations/export_storage.py) and for choosing the backend with EXPORT_STORAGE_BACKEND.

Google Cloud Storage is replaced by an in-memory fake of the few client calls used (the same calls
were checked against the real library and an emulator). With ``STORAGE_EMULATOR_HOST`` set (e.g.
``gcp-storage-emulator start --port 9023 --default-bucket fm-finance-exports``),
``test_with_the_real_client_library`` also runs against it.
"""

import os
from pathlib import Path

import pytest
import test_monthly_cutoff as scenario
from fastapi.testclient import TestClient
from google.api_core.exceptions import Forbidden, NotFound
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_monthly_cutoff import MONTH, month_params

from app.core.config import settings
from app.db.models import MonthlyCutoffExport
from app.features.monthly_cutoff_export import XLSX, ExportGenerationError, generate_export
from app.features.monthly_cutoff_service import run_cutoff
from app.integrations import export_storage as storage_module
from app.integrations.export_storage import (
    ExportFileMissing,
    ExportStorageError,
    GcsExportStorage,
    LocalExportStorage,
    export_storage,
    parse_gcs_location,
)

month = scenario.month
client = scenario.client

pytestmark = pytest.mark.usefixtures("operational_month_end")  # closing a month asks operational

EXPORT_URL = "/api/v1/monthly-cutoffs/export"


# --- A fake of the google-cloud-storage client ----------------------------------------------------


class FakeBlob:
    def __init__(self, gcs: FakeGcs, bucket: str, name: str) -> None:
        self.gcs, self.bucket, self.name = gcs, bucket, name
        self.cache_control: str | None = None

    def upload_from_string(self, data: bytes, content_type: str, timeout: float, retry: object) -> None:
        if self.gcs.fail_with:
            raise self.gcs.fail_with
        if self.bucket not in self.gcs.buckets:
            raise NotFound(f"bucket {self.bucket}")
        self.gcs.objects[(self.bucket, self.name)] = (data, content_type, self.cache_control)
        self.gcs.calls.append(("upload", self.bucket, self.name, timeout, retry is not None))

    def download_as_bytes(self, timeout: float, retry: object) -> bytes:
        if self.gcs.fail_with:
            raise self.gcs.fail_with
        try:
            return self.gcs.objects[(self.bucket, self.name)][0]
        except KeyError:
            raise NotFound(f"{self.bucket}/{self.name}") from None


class FakeBucket:
    def __init__(self, gcs: FakeGcs, name: str) -> None:
        self.gcs, self.name = gcs, name

    def blob(self, name: str) -> FakeBlob:
        return FakeBlob(self.gcs, self.name, name)


class FakeGcs:
    def __init__(self, *buckets: str) -> None:
        self.buckets = set(buckets)
        self.objects: dict[tuple[str, str], tuple[bytes, str, str | None]] = {}
        self.calls: list[tuple[object, ...]] = []
        self.fail_with: Exception | None = None

    def bucket(self, name: str) -> FakeBucket:
        return FakeBucket(self, name)


@pytest.fixture
def gcs(monkeypatch: pytest.MonkeyPatch) -> FakeGcs:
    """New exports go to the bucket "fm-exports" under the prefix "finance", in a fake GCS."""
    fake = FakeGcs("fm-exports", "fm-proofs")
    monkeypatch.setattr(storage_module, "_gcs_client", fake)
    monkeypatch.setattr(settings, "export_storage_backend", "gcs")
    monkeypatch.setattr(settings, "export_gcs_bucket", "fm-exports")
    monkeypatch.setattr(settings, "export_gcs_prefix", "finance")
    return fake


# --- Choosing the backend -----------------------------------------------------------------------


def test_the_backend_is_chosen_by_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core.config import Settings

    assert Settings.model_fields["export_storage_backend"].default == "local", "local by default"
    assert isinstance(export_storage(), LocalExportStorage)

    monkeypatch.setattr(settings, "export_storage_backend", "gcs")
    monkeypatch.setattr(settings, "export_gcs_bucket", "")  # whatever the environment says
    monkeypatch.setattr(settings, "export_gcs_prefix", "")
    monkeypatch.setattr(settings, "google_cloud_storage_bucket", "fm-proofs")
    monkeypatch.setattr(settings, "google_cloud_storage_prefix", "/prod/")
    storage = export_storage()
    assert isinstance(storage, GcsExportStorage)
    assert (storage.bucket, storage.prefix) == ("fm-proofs", "prod"), "defaults: the proofs' bucket and prefix"

    monkeypatch.setattr(settings, "export_gcs_bucket", "fm-exports")
    monkeypatch.setattr(settings, "export_gcs_prefix", "finance/exports")
    storage = export_storage()
    assert (storage.bucket, storage.prefix) == ("fm-exports", "finance/exports")

    assert isinstance(export_storage("local"), LocalExportStorage), "a file is read with its own backend"
    with pytest.raises(ValueError):
        export_storage("s3")


def test_gcs_without_a_bucket_stops_the_api_from_starting(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.main import app

    monkeypatch.setattr(settings, "export_storage_backend", "gcs")
    monkeypatch.setattr(settings, "export_gcs_bucket", "")
    monkeypatch.setattr(settings, "google_cloud_storage_bucket", "")

    with pytest.raises(RuntimeError, match="EXPORT_STORAGE_BACKEND=gcs needs a bucket"), TestClient(app):
        pass
    with pytest.raises(ExportStorageError, match="no Google Cloud Storage bucket configured"):
        export_storage().save("a.xlsx", b"x", XLSX)  # and a worker cannot write anywhere


def test_the_credentials_file_from_the_settings_is_used(monkeypatch: pytest.MonkeyPatch) -> None:
    """GOOGLE_APPLICATION_CREDENTIALS in .env only reaches the settings, not the library: passed on."""
    from google.cloud import storage

    made: list[tuple[str, object]] = []
    monkeypatch.setattr(storage_module, "_gcs_client", None)
    monkeypatch.delenv("STORAGE_EMULATOR_HOST", raising=False)
    monkeypatch.setattr(settings, "google_cloud_project_id", "fm-project")
    monkeypatch.setattr(settings, "google_application_credentials", "/secrets/finance-sa.json")
    monkeypatch.setattr(
        storage.Client,
        "from_service_account_json",
        classmethod(lambda cls, path, project: made.append((path, project)) or "client-from-key"),
    )

    assert storage_module.gcs_client() == "client-from-key"
    assert storage_module.gcs_client() == "client-from-key", "one client per process"
    assert made == [("/secrets/finance-sa.json", "fm-project")]


# --- The GCS backend ----------------------------------------------------------------------------


def test_save_and_load(gcs: FakeGcs) -> None:
    storage = export_storage()

    location = storage.save("monthly-cutoffs/2026-07/cutoff-3.xlsx", b"one", XLSX)
    again = storage.save("monthly-cutoffs/2026-07/cutoff-3.xlsx", b"two", XLSX)  # replaced

    assert location == again == "gs://fm-exports/finance/monthly-cutoffs/2026-07/cutoff-3.xlsx"
    assert gcs.objects[("fm-exports", "finance/monthly-cutoffs/2026-07/cutoff-3.xlsx")] == (b"two", XLSX, "no-store")
    assert storage.load(location) == b"two"
    assert gcs.calls[0] == ("upload", "fm-exports", "finance/monthly-cutoffs/2026-07/cutoff-3.xlsx", 60, True)
    assert GcsExportStorage("fm-exports").save("a.xlsx", b"x", XLSX) == "gs://fm-exports/a.xlsx", "no prefix"


def test_a_file_is_read_where_it_was_written(gcs: FakeGcs, monkeypatch: pytest.MonkeyPatch) -> None:
    location = export_storage().save("a.xlsx", b"x", XLSX)
    monkeypatch.setattr(settings, "export_gcs_bucket", "fm-proofs")  # the configuration changes later

    assert export_storage().load(location) == b"x"


def test_gcs_failures(gcs: FakeGcs) -> None:
    storage = export_storage()
    with pytest.raises(ExportFileMissing):
        storage.load("gs://fm-exports/finance/missing.xlsx")
    with pytest.raises(ExportStorageError, match="could not upload gs://nope/a.xlsx"):
        GcsExportStorage("nope").save("a.xlsx", b"x", XLSX)
    gcs.fail_with = Forbidden("the service account may not write")
    with pytest.raises(ExportStorageError, match="may not write"):
        storage.save("a.xlsx", b"x", XLSX)
    with pytest.raises(ExportStorageError, match="may not write"):
        storage.load("gs://fm-exports/a.xlsx")
    for key in ("../a.xlsx", "/a.xlsx", "a//b.xlsx", ""):
        with pytest.raises(ValueError):
            storage.save(key, b"x", XLSX)
    for location in ("fm-exports/a.xlsx", "gs://fm-exports", "gs:///a.xlsx", "s3://b/a.xlsx"):
        with pytest.raises(ValueError):
            parse_gcs_location(location)


# --- Exports on GCS, end to end -----------------------------------------------------------------


def test_an_export_stored_in_gcs_and_downloaded(
    db: Session,
    month: dict[str, int],
    client: TestClient,
    gcs: FakeGcs,
    export_storage_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_cutoff(period=MONTH, session=db)

    outcome = generate_export(MONTH, session=db)

    location = f"gs://fm-exports/finance/monthly-cutoffs/{MONTH.label}/cutoff-{outcome.cutoff_id}.xlsx"
    assert outcome.storage_key == location
    record = db.scalar(select(MonthlyCutoffExport))
    assert (record.storage_backend, record.storage_key) == ("gcs", location)
    assert not any(export_storage_dir.rglob("*.xlsx")), "nothing on the local disk"
    content, content_type, cache = gcs.objects[parse_gcs_location(location)]
    assert (content[:2], content_type, cache, len(content)) == (b"PK", XLSX, "no-store", record.size_bytes)

    response = client.get(EXPORT_URL, params=month_params())
    assert (response.status_code, response.content) == (200, content)

    # Back to local storage: new files go to the disk, this one is still read from GCS.
    monkeypatch.setattr(settings, "export_storage_backend", "local")
    assert client.get(EXPORT_URL, params=month_params()).content == content
    regenerated = generate_export(MONTH, session=db)
    assert regenerated.storage_key == f"monthly-cutoffs/{MONTH.label}/cutoff-{outcome.cutoff_id}.xlsx"
    assert (export_storage_dir / regenerated.storage_key).exists()


def test_gcs_unavailable(db: Session, month: dict[str, int], client: TestClient, gcs: FakeGcs) -> None:
    run_cutoff(period=MONTH, session=db)
    gcs.fail_with = Forbidden("the service account may not write")

    with pytest.raises(ExportGenerationError) as failed:
        generate_export(MONTH, session=db)

    assert failed.value.reason.startswith("ExportStorageError: could not upload gs://fm-exports/finance/")
    export = client.get(scenario.URL, params=month_params()).json()["data"]["export"]
    assert (export["status"], export["reason"]) == ("FAILED", failed.value.reason), "the list shows why"

    gcs.fail_with = None
    generate_export(MONTH, session=db)
    gcs.fail_with = Forbidden("the service account may not read")
    response = client.get(EXPORT_URL, params=month_params())
    assert (response.status_code, response.json()["code"]) == (503, "EXPORT_STORAGE_UNAVAILABLE")


@pytest.mark.skipif(not os.environ.get("STORAGE_EMULATOR_HOST"), reason="no Cloud Storage emulator (STORAGE_EMULATOR_HOST)")
def test_with_the_real_client_library(monkeypatch: pytest.MonkeyPatch) -> None:
    bucket = os.environ.get("EXPORT_TEST_GCS_BUCKET", "fm-finance-exports")
    monkeypatch.setattr(storage_module, "_gcs_client", None)
    monkeypatch.setattr(settings, "google_application_credentials", "")
    storage = GcsExportStorage(bucket, "test-run")

    location = storage.save("monthly-cutoffs/2026-07/cutoff-1.xlsx", b"PK-real", XLSX)

    assert location == f"gs://{bucket}/test-run/monthly-cutoffs/2026-07/cutoff-1.xlsx"
    assert storage.load(location) == b"PK-real"
    blob = storage_module.gcs_client().bucket(bucket).get_blob("test-run/monthly-cutoffs/2026-07/cutoff-1.xlsx")
    assert (blob.content_type, blob.cache_control) == (XLSX, "no-store")
    with pytest.raises(ExportFileMissing):
        storage.load(f"gs://{bucket}/test-run/missing.xlsx")
