from datetime import date

from app.models.article import RetailerConfidence, RetailerReviewStatus
from app.models.classification import ClassificationReviewStatus


def _approve_url(project_id, classification_id) -> str:
    return f"/projects/{project_id}/classifications/{classification_id}/approve"


def _bulk_approve_url(project_id) -> str:
    return f"/projects/{project_id}/classifications/bulk-approve"


def _correct_url(project_id, classification_id) -> str:
    return f"/projects/{project_id}/classifications/{classification_id}/correct"


def _review_url(project_id) -> str:
    return f"/projects/{project_id}?tab=review"


def _correction_payload(**overrides):
    payload = {
        "primary_topic": "crisis_controversy",
        "secondary_topic": "regulation",
        "communication_category": "reactive_crisis",
        "sentiment": "negative",
        "brand_role": "secondary_mention",
        "story_key": "Corrected story",
    }
    payload.update(overrides)
    return payload


# --- queue membership --------------------------------------------------------


def test_low_confidence_classification_appears_in_review_queue(
    authenticated_client, db_session, project_factory, article_factory, classification_factory
):
    project = project_factory()
    article = article_factory(project, count=1, title="Needs Human Attention")[0]
    classification_factory(article, confidence=0.25)  # defaults to review_status=pending

    response = authenticated_client.get(_review_url(project.id))

    assert response.status_code == 200
    assert "Needs Human Attention" in response.text
    assert "No classification results need review." not in response.text


def test_approved_classification_does_not_reappear_in_queue(
    authenticated_client, db_session, project_factory, article_factory, classification_factory
):
    project = project_factory()
    article = article_factory(project, count=1, title="Already Approved Article")[0]
    classification_factory(
        article, confidence=0.97, review_status=ClassificationReviewStatus.APPROVED
    )

    response = authenticated_client.get(_review_url(project.id))

    assert response.status_code == 200
    assert "Already Approved Article" not in response.text
    assert "No classification results need review." in response.text


# --- approve ------------------------------------------------------------------


def test_approve_individual_removes_it_from_the_queue(
    authenticated_client, db_session, project_factory, article_factory, classification_factory
):
    project = project_factory()
    article = article_factory(project, count=1)[0]
    classification = classification_factory(article, confidence=0.3)  # starts pending

    response = authenticated_client.post(_approve_url(project.id, classification.id), follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == f"/projects/{project.id}?tab=review"

    db_session.rollback()
    db_session.refresh(classification)
    assert classification.review_status == ClassificationReviewStatus.APPROVED
    assert classification.reviewed_at is not None

    follow_up = authenticated_client.get(_review_url(project.id))
    assert "No classification results need review." in follow_up.text


def test_approve_requires_authentication(client, project_factory, article_factory, classification_factory, db_session):
    project = project_factory()
    article = article_factory(project, count=1)[0]
    classification = classification_factory(article)

    response = client.post(_approve_url(project.id, classification.id), follow_redirects=False)

    assert response.status_code == 307
    assert response.headers["location"] == "/login"


def test_approve_rejects_classification_from_another_project(
    authenticated_client, db_session, project_factory, article_factory, classification_factory
):
    project_a = project_factory(name="Approve Scope A")
    project_b = project_factory(name="Approve Scope B")
    article_b = article_factory(project_b, count=1)[0]
    classification_b = classification_factory(article_b, confidence=0.3)  # starts pending

    response = authenticated_client.post(_approve_url(project_a.id, classification_b.id))

    assert response.status_code == 404
    db_session.rollback()
    db_session.refresh(classification_b)
    assert classification_b.review_status == ClassificationReviewStatus.PENDING


# --- bulk approve ---------------------------------------------------------------


def test_bulk_approve_selected(
    authenticated_client, db_session, project_factory, article_factory, classification_factory
):
    project = project_factory()
    articles = article_factory(project, count=2)
    classifications = [classification_factory(a, confidence=0.3) for a in articles]  # start pending

    response = authenticated_client.post(
        _bulk_approve_url(project.id),
        data={"classification_ids": [str(c.id) for c in classifications]},
        follow_redirects=False,
    )

    assert response.status_code == 303
    db_session.rollback()
    for classification in classifications:
        db_session.refresh(classification)
        assert classification.review_status == ClassificationReviewStatus.APPROVED
        assert classification.reviewed_at is not None


def test_bulk_approve_with_no_selection_is_rejected(authenticated_client, project_factory):
    project = project_factory()

    response = authenticated_client.post(_bulk_approve_url(project.id), data={})

    assert response.status_code == 422


# --- correct --------------------------------------------------------------------


def test_edit_and_save_correction_updates_fields_and_preserves_original_ai_values(
    authenticated_client, db_session, project_factory, article_factory, classification_factory
):
    project = project_factory()
    article = article_factory(project, count=1)[0]
    classification = classification_factory(
        article,
        primary_topic="store_expansion",
        secondary_topic=None,
        communication_category="corporate",
        sentiment="positive",
        brand_role="primary_focus",
        story_key=None,
        confidence=0.88,
        rationale_ro="Titlul anunta o deschidere de magazin.",
    )

    response = authenticated_client.post(
        _correct_url(project.id, classification.id),
        data=_correction_payload(),
        follow_redirects=False,
    )

    assert response.status_code == 303
    db_session.rollback()
    db_session.refresh(classification)

    assert classification.primary_topic == "crisis_controversy"
    assert classification.secondary_topic == "regulation"
    assert classification.communication_category == "reactive_crisis"
    assert classification.sentiment == "negative"
    assert classification.brand_role == "secondary_mention"
    assert classification.story_key == "Corrected story"
    assert classification.review_status == ClassificationReviewStatus.CORRECTED
    assert classification.reviewed_at is not None

    # Read-only fields never touched.
    assert classification.confidence == 0.88
    assert classification.rationale_ro == "Titlul anunta o deschidere de magazin."

    # Original AI output preserved verbatim.
    assert classification.original_ai_labels == {
        "primary_topic": "store_expansion",
        "secondary_topic": None,
        "communication_category": "corporate",
        "sentiment": "positive",
        "brand_role": "primary_focus",
        "story_key": None,
    }


def test_second_correction_never_overwrites_the_original_ai_snapshot(
    authenticated_client, db_session, project_factory, article_factory, classification_factory
):
    project = project_factory()
    article = article_factory(project, count=1)[0]
    classification = classification_factory(
        article,
        primary_topic="store_expansion",
        secondary_topic=None,
        communication_category="corporate",
        sentiment="positive",
        brand_role="primary_focus",
        story_key=None,
        confidence=0.81,
        rationale_ro="Reasoning from the AI, never touched by any correction.",
    )
    original_snapshot = {
        "primary_topic": "store_expansion",
        "secondary_topic": None,
        "communication_category": "corporate",
        "sentiment": "positive",
        "brand_role": "primary_focus",
        "story_key": None,
    }

    first = authenticated_client.post(
        _correct_url(project.id, classification.id),
        data=_correction_payload(primary_topic="crisis_controversy"),
        follow_redirects=False,
    )
    assert first.status_code == 303

    db_session.rollback()
    db_session.refresh(classification)
    assert classification.primary_topic == "crisis_controversy"
    assert classification.original_ai_labels == original_snapshot

    second = authenticated_client.post(
        _correct_url(project.id, classification.id),
        data=_correction_payload(primary_topic="other", story_key="Second correction"),
        follow_redirects=False,
    )
    assert second.status_code == 303

    db_session.rollback()
    db_session.refresh(classification)
    assert classification.primary_topic == "other"
    assert classification.story_key == "Second correction"
    assert classification.review_status == ClassificationReviewStatus.CORRECTED

    # Still the very first AI output (not the first correction's values),
    # untouched by the second correction, and read-only fields never moved.
    assert classification.original_ai_labels == original_snapshot
    assert classification.confidence == 0.81
    assert classification.rationale_ro == "Reasoning from the AI, never touched by any correction."


def test_correction_rejects_invalid_taxonomy_value(
    authenticated_client, db_session, project_factory, article_factory, classification_factory
):
    project = project_factory()
    article = article_factory(project, count=1)[0]
    classification = classification_factory(
        article, primary_topic="store_expansion", confidence=0.3
    )  # starts pending

    response = authenticated_client.post(
        _correct_url(project.id, classification.id),
        data=_correction_payload(primary_topic="not_a_real_topic"),
    )

    assert response.status_code == 422
    db_session.rollback()
    db_session.refresh(classification)
    assert classification.primary_topic == "store_expansion"
    assert classification.review_status == ClassificationReviewStatus.PENDING
    assert classification.original_ai_labels is None


def test_correction_rejects_invalid_sentiment(
    authenticated_client, db_session, project_factory, article_factory, classification_factory
):
    project = project_factory()
    article = article_factory(project, count=1)[0]
    classification = classification_factory(article)

    response = authenticated_client.post(
        _correct_url(project.id, classification.id),
        data=_correction_payload(sentiment="furious"),
    )

    assert response.status_code == 422


def test_correction_requires_authentication(client, project_factory, article_factory, classification_factory):
    project = project_factory()
    article = article_factory(project, count=1)[0]
    classification = classification_factory(article)

    response = client.post(
        _correct_url(project.id, classification.id), data=_correction_payload(), follow_redirects=False
    )

    assert response.status_code == 307
    assert response.headers["location"] == "/login"


def test_correction_rejects_classification_from_another_project(
    authenticated_client, db_session, project_factory, article_factory, classification_factory
):
    project_a = project_factory(name="Correct Scope A")
    project_b = project_factory(name="Correct Scope B")
    article_b = article_factory(project_b, count=1)[0]
    classification_b = classification_factory(article_b, primary_topic="store_expansion")

    response = authenticated_client.post(
        _correct_url(project_a.id, classification_b.id), data=_correction_payload()
    )

    assert response.status_code == 404
    db_session.rollback()
    db_session.refresh(classification_b)
    assert classification_b.primary_topic == "store_expansion"


# --- brand assignment review keeps working, separately -------------------------


def _needs_review_article(article_factory, project, **overrides):
    defaults = dict(
        count=1,
        retailer="unknown",
        title="Some story",
        source="Some source",
        publication_date=date(2026, 4, 1),
        retailer_review_status=RetailerReviewStatus.NEEDS_REVIEW,
        retailer_confidence=RetailerConfidence.NEEDS_REVIEW,
        retailer_raw_value="Local Shop XYZ",
    )
    defaults.update(overrides)
    return article_factory(project, **defaults)[0]


def test_brand_assignment_review_continues_to_work(
    authenticated_client, db_session, project_factory, article_factory, uploaded_file_factory
):
    project = project_factory()
    uf = uploaded_file_factory(project, original_filename="Ambiguous.xlsx")
    _needs_review_article(article_factory, project, uploaded_file_id=uf.id, title="Needs Brand")

    response = authenticated_client.get(_review_url(project.id))

    assert response.status_code == 200
    assert "Ambiguous.xlsx" in response.text
    assert "Needs Brand" in response.text
    assert "Brand assignment review" in response.text


def test_classification_review_and_brand_review_sections_are_distinct(
    authenticated_client, db_session, project_factory, article_factory, classification_factory, uploaded_file_factory
):
    project = project_factory()
    classified_article = article_factory(project, count=1, title="Pending Classification Article")[0]
    classification_factory(classified_article, confidence=0.3)  # starts pending

    uf = uploaded_file_factory(project, original_filename="NeedsBrand.xlsx")
    _needs_review_article(article_factory, project, uploaded_file_id=uf.id, title="Needs Brand Row")

    response = authenticated_client.get(_review_url(project.id))

    assert response.status_code == 200
    assert "Classification review" in response.text
    assert "Brand assignment review" in response.text
    assert "Pending Classification Article" in response.text
    assert "Needs Brand Row" in response.text


# --- empty states are separate ---------------------------------------------------


def test_empty_states_are_separate_messages(authenticated_client, project_factory):
    project = project_factory()

    response = authenticated_client.get(_review_url(project.id))

    assert response.status_code == 200
    assert "No classification results need review." in response.text
    assert "No brand assignments need review." in response.text
