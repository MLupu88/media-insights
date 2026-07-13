"""Manual brand-review workflow (reporting-scope Phase C).

Transaction ownership lives at the API/route layer — every function here
uses `db.flush()`, never `db.commit()`, so a single route-level commit
covers article correction, canonical promotion/re-pointing, and every
level of counter recomputation as one atomic unit, with a clean rollback
path on any failure.

`reassign_article_brand`/`bulk_reassign_article_brand` reuse the exact
same canonical-selection primitive import time uses
(`app.services.dedup.reconcile_fingerprint_group`) and acquire the same
project-scoped lock (`app.services.dedup.lock_project_for_dedup`) before
any fingerprint-dependent scan, so import-time and correction-time dedup
can never independently drift or race each other.

Trusted-mapping confirmation (`confirm_brand_mapping`/`clear_brand_mapping`)
is a deliberately separate, explicit action that row corrections never
touch, however many rows end up on the same brand.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.article import Article, ImportStatus, RetailerConfidence, RetailerReviewStatus
from app.models.import_batch import ImportBatch
from app.models.uploaded_file import UploadedFile
from app.services.dedup import lock_project_for_dedup, reconcile_fingerprint_group
from app.services.excel_parser import compute_fingerprint
from app.services.imports import recompute_project_totals
from app.services.retailers import CANONICAL_RETAILERS


class ReviewServiceError(Exception):
    """Base for every user-facing validation error this module raises —
    the route layer catches this specifically (never a bare `Exception`)
    to distinguish "reject with a clean message" from "something broke."
    """


class ArticleNotFoundError(ReviewServiceError):
    pass


class UploadedFileNotFoundError(ReviewServiceError):
    pass


class InvalidBrandError(ReviewServiceError):
    def __init__(self, brand: str):
        self.brand = brand
        super().__init__(f"'{brand}' is not a supported canonical brand.")


# --- read-only: the Review tab's data -----------------------------------------


@dataclass
class ReviewFileGroup:
    uploaded_file: UploadedFile
    articles: list[Article]


def get_review_groups(db: Session, project_id: uuid.UUID) -> list[ReviewFileGroup]:
    articles = (
        db.execute(
            select(Article)
            .where(
                Article.project_id == project_id,
                Article.retailer_review_status == RetailerReviewStatus.NEEDS_REVIEW,
            )
            .order_by(Article.original_row_number.asc())
        )
        .scalars()
        .all()
    )
    if not articles:
        return []

    grouped: dict[uuid.UUID, list[Article]] = {}
    for article in articles:
        grouped.setdefault(article.uploaded_file_id, []).append(article)

    uploaded_files = {
        uf.id: uf
        for uf in db.execute(select(UploadedFile).where(UploadedFile.id.in_(grouped.keys()))).scalars()
    }

    groups = [
        ReviewFileGroup(uploaded_file=uploaded_files[uploaded_file_id], articles=group_articles)
        for uploaded_file_id, group_articles in grouped.items()
        if uploaded_file_id in uploaded_files
    ]
    groups.sort(key=lambda g: g.uploaded_file.original_filename)
    return groups


def count_needs_review(db: Session, project_id: uuid.UUID) -> int:
    return db.execute(
        select(func.count(Article.id)).where(
            Article.project_id == project_id,
            Article.retailer_review_status == RetailerReviewStatus.NEEDS_REVIEW,
        )
    ).scalar_one()


# --- validation helpers ---------------------------------------------------


def _validate_brand(brand: str) -> None:
    if brand not in CANONICAL_RETAILERS:
        raise InvalidBrandError(brand)


def _fetch_articles_or_raise(
    db: Session, project_id: uuid.UUID, article_ids: list[uuid.UUID]
) -> dict[uuid.UUID, Article]:
    rows = db.execute(
        select(Article).where(Article.project_id == project_id, Article.id.in_(article_ids))
    ).scalars()
    found = {row.id: row for row in rows}
    missing = [str(aid) for aid in article_ids if aid not in found]
    if missing:
        raise ArticleNotFoundError(
            f"Article(s) not found in project {project_id}: {', '.join(missing)}"
        )
    return found


# --- candidate gathering + deterministic locking ---------------------------


def _compute_new_fingerprint(article: Article, new_retailer: str) -> str:
    return compute_fingerprint(
        new_retailer, article.title, article.source, article.publication_date, article.article_url
    )


def _gather_candidate_ids(
    db: Session, project_id: uuid.UUID, article: Article, new_fingerprint: str
) -> dict[uuid.UUID, datetime]:
    """Every row that could be touched by correcting `article` to
    `new_fingerprint`: itself, its former duplicates (if it's currently
    canonical), and whatever already shares the new fingerprint. Returned
    with `created_at` so the caller can lock them in global order.
    """
    candidates: dict[uuid.UUID, datetime] = {article.id: article.created_at}

    for row_id, created_at in db.execute(
        select(Article.id, Article.created_at).where(
            Article.project_id == project_id, Article.duplicate_of_article_id == article.id
        )
    ):
        candidates[row_id] = created_at

    for row_id, created_at in db.execute(
        select(Article.id, Article.created_at).where(
            Article.project_id == project_id,
            Article.fingerprint == new_fingerprint,
            Article.id != article.id,
        )
    ):
        candidates[row_id] = created_at

    return candidates


def _lock_rows_in_order(db: Session, candidates: dict[uuid.UUID, datetime]) -> dict[uuid.UUID, Article]:
    """Locks every candidate row one at a time, in ascending
    `(created_at, id)` order. A single multi-row `SELECT ... FOR UPDATE
    ORDER BY ...` does not guarantee Postgres acquires the underlying row
    locks in that order (the sort can happen after the scan) — separate,
    sequential single-row lock statements are what actually guarantees it.
    Since `(created_at, id)` is a fixed total order over every article,
    any two transactions whose candidate sets overlap acquire locks on
    the shared rows in the same relative order, so no wait-cycle (hence
    no deadlock) can form between them.
    """
    ordered_ids = sorted(candidates, key=lambda row_id: (candidates[row_id], row_id))
    locked: dict[uuid.UUID, Article] = {}
    for row_id in ordered_ids:
        locked[row_id] = db.execute(
            select(Article).where(Article.id == row_id).with_for_update()
        ).scalar_one()
    return locked


def _apply_fingerprint_reconciliation(
    article: Article, new_retailer: str, new_fingerprint: str, locked_by_id: dict[uuid.UUID, Article]
) -> set[uuid.UUID]:
    """Mutates `article` and any sibling rows in `locked_by_id` it
    interacts with. Every row referenced here must already be locked.
    """
    affected_uploaded_file_ids = {article.uploaded_file_id}
    old_fingerprint = article.fingerprint

    if new_fingerprint == old_fingerprint:
        article.retailer = new_retailer
        article.retailer_review_status = RetailerReviewStatus.CONFIRMED
        article.retailer_confidence = RetailerConfidence.MANUAL_CORRECTION
        return affected_uploaded_file_ids

    # If this article was itself canonical, its former duplicates need a
    # new canonical promoted among themselves before its fingerprint
    # changes out from under them.
    if not article.is_duplicate:
        siblings = [
            row
            for row_id, row in locked_by_id.items()
            if row_id != article.id and row.duplicate_of_article_id == article.id
        ]
        if siblings:
            reconcile_fingerprint_group(siblings)
            for row in siblings:
                affected_uploaded_file_ids.add(row.uploaded_file_id)

    # Resolve this article's status under the NEW fingerprint against
    # whatever else already shares it — one call to the same shared
    # primitive decides the canonical among the combined group.
    existing_for_new_fp = [
        row
        for row_id, row in locked_by_id.items()
        if row_id != article.id and row.fingerprint == new_fingerprint
    ]
    if existing_for_new_fp:
        reconcile_fingerprint_group([article, *existing_for_new_fp])
        for row in existing_for_new_fp:
            affected_uploaded_file_ids.add(row.uploaded_file_id)
    else:
        article.is_duplicate = False
        article.duplicate_of_article_id = None

    article.retailer = new_retailer
    article.fingerprint = new_fingerprint
    article.retailer_review_status = RetailerReviewStatus.CONFIRMED
    article.retailer_confidence = RetailerConfidence.MANUAL_CORRECTION

    return affected_uploaded_file_ids


# --- public correction API --------------------------------------------------


def reassign_article_brand(
    db: Session, project_id: uuid.UUID, article_id: uuid.UUID, new_retailer: str
) -> set[uuid.UUID]:
    """Corrects one article's brand. Flush-only — the caller commits."""
    _validate_brand(new_retailer)
    lock_project_for_dedup(db, project_id)

    article_preview = db.execute(
        select(Article).where(Article.id == article_id, Article.project_id == project_id)
    ).scalar_one_or_none()
    if article_preview is None:
        raise ArticleNotFoundError(f"Article {article_id} not found in project {project_id}.")

    new_fingerprint = _compute_new_fingerprint(article_preview, new_retailer)
    candidates = _gather_candidate_ids(db, project_id, article_preview, new_fingerprint)
    locked_by_id = _lock_rows_in_order(db, candidates)

    affected = _apply_fingerprint_reconciliation(
        locked_by_id[article_preview.id], new_retailer, new_fingerprint, locked_by_id
    )
    db.flush()
    return affected


def bulk_reassign_article_brand(
    db: Session, project_id: uuid.UUID, article_ids: list[uuid.UUID], new_retailer: str
) -> set[uuid.UUID]:
    """Corrects several articles to the same brand as one transaction:
    validate everything first, gather the union of every affected
    fingerprint group across the whole selection, lock that full union
    once in global order, then apply every correction — never a loop
    that independently gathers and locks per row. Flush-only.
    """
    _validate_brand(new_retailer)
    if not article_ids:
        raise ReviewServiceError("No rows were selected.")

    unique_ids = list(dict.fromkeys(article_ids))
    previews = _fetch_articles_or_raise(db, project_id, unique_ids)

    lock_project_for_dedup(db, project_id)

    new_fingerprints: dict[uuid.UUID, str] = {}
    all_candidates: dict[uuid.UUID, datetime] = {}
    for article_id, preview in previews.items():
        new_fp = _compute_new_fingerprint(preview, new_retailer)
        new_fingerprints[article_id] = new_fp
        all_candidates.update(_gather_candidate_ids(db, project_id, preview, new_fp))

    locked_by_id = _lock_rows_in_order(db, all_candidates)

    affected: set[uuid.UUID] = set()
    ordered_target_ids = sorted(previews, key=lambda i: (locked_by_id[i].created_at, i))
    for article_id in ordered_target_ids:
        affected |= _apply_fingerprint_reconciliation(
            locked_by_id[article_id], new_retailer, new_fingerprints[article_id], locked_by_id
        )

    db.flush()
    return affected


# --- counter recomputation (flush-only) -------------------------------------


def _recompute_uploaded_file_counts(db: Session, uploaded_file_id: uuid.UUID) -> None:
    uploaded_file = db.get(UploadedFile, uploaded_file_id)
    if uploaded_file is None:
        return
    total, valid, invalid, duplicate = db.execute(
        select(
            func.count(Article.id),
            func.count(Article.id).filter(Article.import_status == ImportStatus.VALID),
            func.count(Article.id).filter(Article.import_status == ImportStatus.INVALID),
            func.count(Article.id).filter(Article.is_duplicate.is_(True)),
        ).where(Article.uploaded_file_id == uploaded_file_id)
    ).one()
    uploaded_file.row_count = total or 0
    uploaded_file.valid_row_count = valid or 0
    uploaded_file.invalid_row_count = invalid or 0
    uploaded_file.duplicate_row_count = duplicate or 0


def _recompute_import_batch_counts(db: Session, import_batch_id: uuid.UUID) -> None:
    batch = db.get(ImportBatch, import_batch_id)
    if batch is None:
        return
    totals = db.execute(
        select(
            func.coalesce(func.sum(UploadedFile.row_count), 0),
            func.coalesce(func.sum(UploadedFile.valid_row_count), 0),
            func.coalesce(func.sum(UploadedFile.invalid_row_count), 0),
            func.coalesce(func.sum(UploadedFile.duplicate_row_count), 0),
        ).where(UploadedFile.import_batch_id == import_batch_id)
    ).one()
    batch.total_rows, batch.valid_rows, batch.invalid_rows, batch.duplicate_rows = totals

    batch.needs_review_rows = db.execute(
        select(func.count(Article.id))
        .select_from(Article)
        .join(UploadedFile, Article.uploaded_file_id == UploadedFile.id)
        .where(
            UploadedFile.import_batch_id == import_batch_id,
            Article.retailer_review_status == RetailerReviewStatus.NEEDS_REVIEW,
        )
    ).scalar_one()


def recompute_counters_no_commit(
    db: Session, project_id: uuid.UUID, uploaded_file_ids: set[uuid.UUID]
) -> None:
    """Refreshes UploadedFile -> Project -> ImportBatch rollups for every
    file touched by a correction, as part of the caller's single open
    transaction. Flush-only.
    """
    batch_ids: set[uuid.UUID] = set()
    for uploaded_file_id in uploaded_file_ids:
        _recompute_uploaded_file_counts(db, uploaded_file_id)
        uploaded_file = db.get(UploadedFile, uploaded_file_id)
        if uploaded_file is not None and uploaded_file.import_batch_id is not None:
            batch_ids.add(uploaded_file.import_batch_id)

    # `recompute_project_totals`/`_recompute_import_batch_counts` each
    # aggregate FROM `UploadedFile` via a fresh SELECT — with autoflush
    # off (this app's session default), that SELECT would otherwise see
    # the pre-update values from the DB, not the mutations just made
    # above in this same transaction.
    db.flush()

    recompute_project_totals(db, project_id, commit=False)

    for batch_id in batch_ids:
        _recompute_import_batch_counts(db, batch_id)

    db.flush()


# --- trusted file-level mapping ---------------------------------------------


def confirm_brand_mapping(
    db: Session, project_id: uuid.UUID, uploaded_file_id: uuid.UUID, brand: str
) -> UploadedFile:
    """The one explicit action that may ever set
    `retailer_hint_confirmed = True` — never a side effect of correcting
    individual rows, however many end up on the same brand.
    """
    _validate_brand(brand)

    uploaded_file = db.execute(
        select(UploadedFile).where(
            UploadedFile.id == uploaded_file_id, UploadedFile.project_id == project_id
        )
    ).scalar_one_or_none()
    if uploaded_file is None:
        raise UploadedFileNotFoundError(f"File {uploaded_file_id} not found in project {project_id}.")

    uploaded_file.retailer_hint = brand
    uploaded_file.retailer_hint_confirmed = True
    db.flush()
    return uploaded_file


def clear_brand_mapping(db: Session, project_id: uuid.UUID, uploaded_file_id: uuid.UUID) -> UploadedFile:
    uploaded_file = db.execute(
        select(UploadedFile).where(
            UploadedFile.id == uploaded_file_id, UploadedFile.project_id == project_id
        )
    ).scalar_one_or_none()
    if uploaded_file is None:
        raise UploadedFileNotFoundError(f"File {uploaded_file_id} not found in project {project_id}.")

    uploaded_file.retailer_hint = None
    uploaded_file.retailer_hint_confirmed = False
    db.flush()
    return uploaded_file
