import uuid

from app.models.classification import (
    Classification,
    ClassificationBatch,
    ClassificationBatchStatus,
)


def _summary_url(project_id) -> str:
    return f"/api/internal/projects/{project_id}/summary"


def test_project_summary_not_found(client, internal_headers):
    response = client.get(_summary_url(uuid.uuid4()), headers=internal_headers)
    assert response.status_code == 404


def test_project_summary_returns_expected_fields(
    client, internal_headers, db_session, project_factory, article_factory
):
    project = project_factory()
    articles = article_factory(project, count=4)
    project.total_files = 1
    project.total_rows = 5
    project.valid_rows = 4
    project.invalid_rows = 1
    project.duplicate_rows = 0
    db_session.commit()

    db_session.add(
        Classification(
            id=uuid.uuid4(),
            article_id=articles[0].id,
            project_id=project.id,
            primary_topic="other",
            communication_category="incidental",
            sentiment="neutral",
            brand_role="incidental_mention",
            confidence=0.3,  # below the low-confidence threshold
            model="deepseek-chat",
            prompt_version="retail-deepseek-v2",
        )
    )
    project.classified_rows = 1
    db_session.add(
        ClassificationBatch(
            id=uuid.uuid4(),
            project_id=project.id,
            status=ClassificationBatchStatus.RUNNING,
            article_count=3,
        )
    )
    db_session.add(
        ClassificationBatch(
            id=uuid.uuid4(),
            project_id=project.id,
            status=ClassificationBatchStatus.FAILED,
            article_count=1,
        )
    )
    db_session.commit()

    response = client.get(_summary_url(project.id), headers=internal_headers)

    assert response.status_code == 200
    body = response.json()
    assert body["project_id"] == str(project.id)
    assert body["total_files"] == 1
    assert body["total_rows"] == 5
    assert body["valid_rows"] == 4
    assert body["invalid_rows"] == 1
    assert body["duplicate_rows"] == 0
    assert body["classified_rows"] == 1
    assert body["unclassified_valid_rows"] == 3
    assert body["classification_percentage"] == 25.0
    assert body["low_confidence_count"] == 1
    assert body["active_batch_count"] == 1
    assert body["failed_batch_count"] == 1
    assert body["last_classification_at"] is not None


def test_project_summary_with_no_classifications_yet(
    client, internal_headers, project_factory, article_factory
):
    project = project_factory()
    article_factory(project, count=2)

    response = client.get(_summary_url(project.id), headers=internal_headers)

    body = response.json()
    assert body["classified_rows"] == 0
    assert body["classification_percentage"] == 0.0
    assert body["last_classification_at"] is None
