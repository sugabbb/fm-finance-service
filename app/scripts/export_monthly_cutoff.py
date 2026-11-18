"""Generates (again) the Excel workbook of a closed month, for the finance team.

The workbook is normally generated right after the close by ``app.scripts.monthly_cutoff``, and
retried by its hourly ``--pending`` run up to ``EXPORT_MAX_AUTOMATIC_ATTEMPTS`` failures in a row.
Use this to generate it after that, or to regenerate it (e.g. after the export storage was moved
or the file was lost); it works whatever the failed attempts (like the frontend's
``POST /monthly-cutoffs/export``)::

    python -m app.scripts.export_monthly_cutoff --month 2026-07

It exports the month's reported cut-off, replacing that cut-off's previous workbook. The file goes
to the export storage (``EXPORT_STORAGE_BACKEND``/``EXPORT_STORAGE_DIR``) and is downloaded with
``GET /api/v1/monthly-cutoffs/export?year=&month=``.

Exit codes: ``0`` success, ``1`` the export failed (recorded with its reason) or is being generated
already, ``2`` invalid arguments or a month that has not been closed.
"""

import argparse
import logging
import sys
from collections.abc import Sequence

from app.core.config import settings
from app.features.monthly_cutoff_export import (
    CutoffNotFoundError,
    ExportGenerationError,
    ExportInProgressError,
    generate_export,
)
from app.scripts.sync_cash_statements import month

logger = logging.getLogger("app.scripts.export_monthly_cutoff")

EXIT_OK, EXIT_FAILED, EXIT_USAGE = 0, 1, 2


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.scripts.export_monthly_cutoff",
        description="Generate the Excel workbook of a closed month's monthly cut-off.",
    )
    parser.add_argument("--month", type=month, required=True, help="YYYY-MM, a month that has been closed")
    args = parser.parse_args(argv)

    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        outcome = generate_export(args.month)
    except CutoffNotFoundError as error:
        logger.error("%s", error)
        return EXIT_USAGE
    except (ExportInProgressError, ExportGenerationError) as error:
        logger.error("%s", error)
        return EXIT_FAILED
    except Exception:
        logger.exception("monthly cut-off export %s failed", args.month.label)
        return EXIT_FAILED
    logger.info(
        "monthly cut-off export %s: cut-off %s saved as %s (%s bytes, storage key %s)",
        outcome.period.label,
        outcome.cutoff_id,
        outcome.file_name,
        outcome.size_bytes,
        outcome.storage_key,
    )
    for warning in outcome.warnings:
        logger.warning("monthly cut-off export %s: %s", outcome.period.label, warning)
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
