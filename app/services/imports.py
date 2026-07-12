import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.article import Article, ImportStatus
from app.models.project import Project, ProjectStatus
from app.models.uploaded_file import UploadedFile, UploadedFileStatus
from app.services.excel_parser import ParserError, parse_workbook
from app.services.retailers import infer_retailer


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
        else infer_retailer(uploaded_file.retailer_hint, uploaded_file.original_filename, None)
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
