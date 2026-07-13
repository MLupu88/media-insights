import uuid
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.article import Article, ImportStatus, RetailerReviewStatus
from app.models.import_batch import ImportBatch, ImportBatchStatus
from app.models.project import Project, ProjectStatus
from app.models.uploaded_file import UploadedFile, UploadedFileStatus
from app.services.excel_parser import ParserError, parse_workbook
from app.services.retailers import assign_retailer


def _load_seen_fingerprints(db: Session, project_id: uuid.UUID) -> dict[str, uuid.UUID]:
    stmt = (
        select(Article.fingerprint, Article.id)
        .where(Article.project_id == project_id)
        .order_by(Article.created_at.asc(), Article.id.asc())
    )
    seen: dict[str, uuid.UUID] = {}
    for fingerprint, article_id in db.execute(stmt):
        seen.setdefault(fingerprint, article_id)
    return seen


def recompute_project_totals(db: Session, project_id: uuid.UUID) -> None:
    totals = db.execute(
        select(
            func.count(UploadedFile.id),
            func.coalesce(func.sum(UploadedFile.row_count), 0),
            func.coalesce(func.sum(UploadedFile.valid_row_count), 0),
            func.coalesce(func.sum(UploadedFile.invalid_row_count), 0),
            func.coalesce(func.sum(UploadedFile.duplicate_row_count), 0),
        ).where(UploadedFile.project_id == project_id)
    ).one()
    total_files, total_rows, valid_rows, invalid_rows, duplicate_rows = totals

    project = db.get(Project, project_id)
    if project is None:
        return

    project.total_files = total_files
    project.total_rows = total_rows
    project.valid_rows = valid_rows
    project.invalid_rows = invalid_rows
    project.duplicate_rows = duplicate_rows

    if total_files > 0 and project.status == ProjectStatus.CREATED:
        project.status = ProjectStatus.IMPORTED

    db.commit()


def import_uploaded_file(db: Session, uploaded_file: UploadedFile) -> None:
    uploaded_file.status = UploadedFileStatus.PROCESSING
    uploaded_file.error_message = None
    db.commit()

    try:
        parse_result = parse_workbook(
            uploaded_file.stored_path,
            uploaded_file.original_filename,
            uploaded_file.retailer_hint,
            uploaded_file.retailer_hint_confirmed,
        )
    except ParserError as exc:
        uploaded_file.status = UploadedFileStatus.FAILED
        uploaded_file.error_message = str(exc)
        db.commit()
        recompute_project_totals(db, uploaded_file.project_id)
        return
    except Exception as exc:  # noqa: BLE001 - any parsing failure must not crash the request
        uploaded_file.status = UploadedFileStatus.FAILED
        uploaded_file.error_message = f"Unexpected error while parsing: {exc}"
        db.commit()
        recompute_project_totals(db, uploaded_file.project_id)
        return

    seen_fingerprints = _load_seen_fingerprints(db, uploaded_file.project_id)

    valid_count = 0
    invalid_count = 0
    duplicate_count = 0
    articles: list[Article] = []

    for parsed_row in parse_result.rows:
        is_duplicate = parsed_row.fingerprint in seen_fingerprints
        duplicate_of_id = seen_fingerprints.get(parsed_row.fingerprint) if is_duplicate else None
        article_id = uuid.uuid4()

        articles.append(
            Article(
                id=article_id,
                project_id=uploaded_file.project_id,
                uploaded_file_id=uploaded_file.id,
                original_row_number=parsed_row.original_row_number,
                retailer=parsed_row.retailer,
                medium=parsed_row.medium,
                publication_date=parsed_row.publication_date,
                title=parsed_row.title,
                article_url=parsed_row.article_url,
                mediatrust_url=parsed_row.mediatrust_url,
                source=parsed_row.source,
                subject=parsed_row.subject,
                audience=parsed_row.audience,
                ave=parsed_row.ave,
                sentiment_original=parsed_row.sentiment_original,
                importance_original=parsed_row.importance_original,
                author=parsed_row.author,
                county=parsed_row.county,
                source_audience=parsed_row.source_audience,
                subfolder_1=parsed_row.subfolder_1,
                subfolder_2=parsed_row.subfolder_2,
                raw_json=parsed_row.raw_json,
                fingerprint=parsed_row.fingerprint,
                is_duplicate=is_duplicate,
                duplicate_of_article_id=duplicate_of_id,
                import_status=parsed_row.import_status,
                import_error=parsed_row.import_error,
                retailer_confidence=parsed_row.retailer_confidence,
                retailer_review_status=parsed_row.retailer_review_status,
                retailer_raw_value=parsed_row.retailer_raw_value,
            )
        )

        if not is_duplicate:
            seen_fingerprints[parsed_row.fingerprint] = article_id
        else:
            duplicate_count += 1

        if parsed_row.import_status == ImportStatus.INVALID:
            invalid_count += 1
        else:
            valid_count += 1

    db.add_all(articles)

    uploaded_file.row_count = len(articles)
    uploaded_file.valid_row_count = valid_count
    uploaded_file.invalid_row_count = invalid_count
    uploaded_file.duplicate_row_count = duplicate_count
    uploaded_file.workbook_sheet = parse_result.workbook_sheet
    uploaded_file.detected_retailer = (
        parse_result.rows[0].retailer
        if parse_result.rows
        else assign_retailer(
            None,
            retailer_hint=uploaded_file.retailer_hint,
            retailer_hint_confirmed=uploaded_file.retailer_hint_confirmed,
            filename=uploaded_file.original_filename,
            dominant_value=None,
        ).value
    )
    uploaded_file.status = UploadedFileStatus.COMPLETED
    db.commit()

    recompute_project_totals(db, uploaded_file.project_id)


def retry_import(db: Session, uploaded_file: UploadedFile) -> None:
    if uploaded_file.status != UploadedFileStatus.FAILED:
        raise ValueError("Only failed imports can be retried.")

    db.query(Article).filter(Article.uploaded_file_id == uploaded_file.id).delete()
    uploaded_file.row_count = 0
    uploaded_file.valid_row_count = 0
    uploaded_file.invalid_row_count = 0
    uploaded_file.duplicate_row_count = 0
    uploaded_file.error_message = None
    db.commit()

    import_uploaded_file(db, uploaded_file)


# --- ImportBatch lifecycle -------------------------------------------------
#
# The batch row is created and committed as the very first write of the
# request, before any file is parsed, and finalized once at the end — each
# commit touches only the batch's own fields, since every other write in
# the loop (UploadedFile/Article creation, project totals) is already
# committed by `import_uploaded_file` before control returns here. A hard
# process crash between those two commits simply leaves the row visibly at
# `processing` with `completed_at IS NULL` forever — the honest signal
# required, with no watchdog/sweeper needed to produce it.


def start_import_batch(db: Session, project_id: uuid.UUID) -> ImportBatch:
    batch = ImportBatch(project_id=project_id, status=ImportBatchStatus.PROCESSING)
    db.add(batch)
    db.commit()
    db.refresh(batch)
    return batch


def record_batch_file_result(batch: ImportBatch, uploaded_file: UploadedFile) -> None:
    """Accumulates one successfully-processed file's outcome into the
    batch's in-memory counters. Does not commit — the caller commits once,
    at finalization, so the batch's row-level counters never partially
    reflect an in-progress request.
    """
    batch.files_processed += 1
    if uploaded_file.status == UploadedFileStatus.FAILED:
        batch.files_rejected += 1
    else:
        batch.files_accepted += 1
    batch.total_rows += uploaded_file.row_count
    batch.valid_rows += uploaded_file.valid_row_count
    batch.invalid_rows += uploaded_file.invalid_row_count
    batch.duplicate_rows += uploaded_file.duplicate_row_count


def record_batch_rejected_file(batch: ImportBatch) -> None:
    """A file rejected before an `UploadedFile` row even existed (wrong
    extension, over the size limit) — still counted, with no row-level
    counters to add.
    """
    batch.files_processed += 1
    batch.files_rejected += 1


def finalize_import_batch(db: Session, batch: ImportBatch, error_reasons: list[str]) -> None:
    needs_review_rows = db.execute(
        select(func.count(Article.id))
        .select_from(Article)
        .join(UploadedFile, Article.uploaded_file_id == UploadedFile.id)
        .where(UploadedFile.import_batch_id == batch.id)
        .where(Article.retailer_review_status == RetailerReviewStatus.NEEDS_REVIEW)
    ).scalar_one()
    batch.needs_review_rows = needs_review_rows

    if batch.files_processed > 0 and batch.files_rejected == batch.files_processed:
        batch.status = ImportBatchStatus.FAILED
    elif batch.files_rejected > 0:
        batch.status = ImportBatchStatus.PARTIALLY_COMPLETED
    else:
        batch.status = ImportBatchStatus.COMPLETED

    batch.completed_at = datetime.now(timezone.utc)
    batch.error_summary = (
        "; ".join(error_reasons) if error_reasons and batch.status != ImportBatchStatus.COMPLETED else None
    )
    db.commit()


def fail_import_batch(db: Session, batch_id: uuid.UUID, error_summary: str) -> None:
    """Safety net for a Python-level exception that escapes the per-file
    loop entirely (not a hard process crash — those are, by construction,
    never caught by anything and simply leave the batch at `processing`).
    """
    db.rollback()
    batch = db.get(ImportBatch, batch_id)
    if batch is not None:
        batch.status = ImportBatchStatus.FAILED
        batch.completed_at = datetime.now(timezone.utc)
        batch.error_summary = error_summary
        db.commit()
