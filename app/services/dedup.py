"""Shared, deterministic fingerprint-dedup primitives.

Used identically by import-time dedup (`imports.py`) and correction-time
re-canonicalization (`review.py`) so the two paths can never
independently drift — neither on *which row wins* for a shared
fingerprint, nor on *how concurrent access is serialized*.
"""

import uuid

from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from app.models.article import Article
from app.models.project import Project


def order_by_canonical(stmt: Select) -> Select:
    """The shared tie-break rule — earliest `created_at`, then `id` —
    applied to a SELECT over `Article`.
    """
    return stmt.order_by(Article.created_at.asc(), Article.id.asc())


def canonical_sort_key(article: Article) -> tuple:
    """The same rule, for sorting already-fetched ORM rows in Python."""
    return (article.created_at, article.id)


def reconcile_fingerprint_group(rows: list[Article]) -> Article:
    """Given `Article` rows that all share one fingerprint,
    deterministically designates exactly one as canonical (earliest
    `created_at`, then `id`) and marks every other row a duplicate
    pointing at it — mutating every row in `rows`, including the
    canonical itself (`is_duplicate=False`, `duplicate_of_article_id=None`).
    Returns the canonical row.

    This is the one place "which row wins" is decided; both import-time
    dedup and correction-time re-canonicalization call it so they can
    never disagree on the same group.
    """
    ordered = sorted(rows, key=canonical_sort_key)
    canonical = ordered[0]
    canonical.is_duplicate = False
    canonical.duplicate_of_article_id = None
    for row in ordered[1:]:
        row.is_duplicate = True
        row.duplicate_of_article_id = canonical.id
    return canonical


def lock_project_for_dedup(db: Session, project_id: uuid.UUID) -> None:
    """Project-scoped transaction lock, required before any
    fingerprint-dependent scan or mutation.

    Held until the caller's transaction commits or rolls back — this is
    what actually prevents two concurrent dedup-changing operations (an
    import and a Review correction, or two concurrent corrections) on the
    same project from both scanning before either mutates and racing to
    create two canonicals for the same fingerprint.
    """
    db.execute(select(Project.id).where(Project.id == project_id).with_for_update())
