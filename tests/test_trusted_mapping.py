from datetime import date

from app.models.article import RetailerConfidence, RetailerReviewStatus


def test_confirm_brand_mapping_sets_hint_and_confirmed_flag(
    authenticated_client, db_session, project_factory, uploaded_file_factory
):
    project = project_factory()
    uploaded_file = uploaded_file_factory(project)
    assert uploaded_file.retailer_hint_confirmed is False

    response = authenticated_client.post(
        f"/projects/{project.id}/files/{uploaded_file.id}/confirm-brand-mapping",
        data={"brand": "Auchan"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == f"/projects/{project.id}?tab=files"

    db_session.refresh(uploaded_file)
    assert uploaded_file.retailer_hint == "Auchan"
    assert uploaded_file.retailer_hint_confirmed is True


def test_confirm_brand_mapping_rejects_unsupported_brand(
    authenticated_client, db_session, project_factory, uploaded_file_factory
):
    project = project_factory()
    uploaded_file = uploaded_file_factory(project)

    response = authenticated_client.post(
        f"/projects/{project.id}/files/{uploaded_file.id}/confirm-brand-mapping",
        data={"brand": "Not A Real Brand"},
        follow_redirects=False,
    )
    assert response.status_code == 422

    db_session.refresh(uploaded_file)
    assert uploaded_file.retailer_hint_confirmed is False


def test_confirm_brand_mapping_rejects_file_from_another_project(
    authenticated_client, db_session, project_factory, uploaded_file_factory
):
    owner_project = project_factory(name="Mapping Owner")
    other_project = project_factory(name="Mapping Other")
    uploaded_file = uploaded_file_factory(owner_project)

    response = authenticated_client.post(
        f"/projects/{other_project.id}/files/{uploaded_file.id}/confirm-brand-mapping",
        data={"brand": "Auchan"},
        follow_redirects=False,
    )
    assert response.status_code == 404

    db_session.refresh(uploaded_file)
    assert uploaded_file.retailer_hint_confirmed is False


def test_trusted_mapping_can_be_cleared(
    authenticated_client, db_session, project_factory, uploaded_file_factory
):
    project = project_factory()
    uploaded_file = uploaded_file_factory(
        project, retailer_hint="Auchan", retailer_hint_confirmed=True
    )

    response = authenticated_client.post(
        f"/projects/{project.id}/files/{uploaded_file.id}/clear-brand-mapping",
        follow_redirects=False,
    )
    assert response.status_code == 303

    db_session.refresh(uploaded_file)
    assert uploaded_file.retailer_hint is None
    assert uploaded_file.retailer_hint_confirmed is False


def test_trusted_mapping_can_be_replaced_with_a_different_brand(
    authenticated_client, db_session, project_factory, uploaded_file_factory
):
    project = project_factory()
    uploaded_file = uploaded_file_factory(
        project, retailer_hint="Auchan", retailer_hint_confirmed=True
    )

    response = authenticated_client.post(
        f"/projects/{project.id}/files/{uploaded_file.id}/confirm-brand-mapping",
        data={"brand": "Carrefour"},
        follow_redirects=False,
    )
    assert response.status_code == 303

    db_session.refresh(uploaded_file)
    assert uploaded_file.retailer_hint == "Carrefour"
    assert uploaded_file.retailer_hint_confirmed is True


def test_single_row_correction_never_confirms_the_file_hint(
    authenticated_client, db_session, project_factory, article_factory, uploaded_file_factory
):
    project = project_factory()
    uploaded_file = uploaded_file_factory(project)
    article = article_factory(
        project, count=1, retailer="unknown", title="Story", source="Source",
        publication_date=date(2026, 4, 1), uploaded_file_id=uploaded_file.id,
        retailer_review_status=RetailerReviewStatus.NEEDS_REVIEW,
        retailer_confidence=RetailerConfidence.NEEDS_REVIEW,
    )[0]

    authenticated_client.post(
        f"/projects/{project.id}/articles/{article.id}/assign-brand",
        data={"brand": "Auchan"},
        follow_redirects=False,
    )

    db_session.refresh(uploaded_file)
    assert uploaded_file.retailer_hint_confirmed is False
    assert uploaded_file.retailer_hint is None


def test_bulk_correction_of_every_row_in_a_file_still_never_confirms_the_hint(
    authenticated_client, db_session, project_factory, article_factory, uploaded_file_factory
):
    """Explicitly proves the safety correction: correcting ALL of a
    file's flagged rows to one brand — even every single one — must
    never be treated as an implicit "trust this file" signal.
    """
    project = project_factory()
    uploaded_file = uploaded_file_factory(project)
    rows = [
        article_factory(
            project, count=1, retailer="unknown", title=f"Story {i}", source="Source",
            publication_date=date(2026, 4, 1), uploaded_file_id=uploaded_file.id,
            fingerprint=f"fp-{i}", retailer_review_status=RetailerReviewStatus.NEEDS_REVIEW,
            retailer_confidence=RetailerConfidence.NEEDS_REVIEW,
        )[0]
        for i in range(4)
    ]

    authenticated_client.post(
        f"/projects/{project.id}/articles/bulk-assign-brand",
        data={"article_ids": [str(r.id) for r in rows], "brand": "Auchan"},
        follow_redirects=False,
    )

    for row in rows:
        db_session.refresh(row)
        assert row.retailer == "Auchan"

    db_session.refresh(uploaded_file)
    assert uploaded_file.retailer_hint_confirmed is False
    assert uploaded_file.retailer_hint is None
