from datetime import date

import pytest
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError

from app.models.project import Project
from app.schemas.project import ProjectCreate
from app.services.projects import create_project

# --- ProjectCreate: the four supply combinations ------------------------------


def test_quarter_only_is_valid():
    data = ProjectCreate(name="Q2 Coverage", quarter="2026-Q2")
    assert data.quarter == "2026-Q2"
    assert data.period_start is None
    assert data.period_end is None


def test_date_range_only_is_valid():
    data = ProjectCreate(
        name="Six Week Campaign", period_start=date(2026, 4, 1), period_end=date(2026, 6, 30)
    )
    assert data.quarter is None
    assert data.period_start == date(2026, 4, 1)
    assert data.period_end == date(2026, 6, 30)


def test_quarter_plus_date_range_is_valid():
    data = ProjectCreate(
        name="Q2 With Precise Range",
        quarter="2026-Q2",
        period_start=date(2026, 4, 1),
        period_end=date(2026, 6, 30),
    )
    assert data.quarter == "2026-Q2"
    assert data.period_start == date(2026, 4, 1)
    assert data.period_end == date(2026, 6, 30)


def test_neither_quarter_nor_dates_is_rejected():
    with pytest.raises(ValidationError, match="Provide either a quarter"):
        ProjectCreate(name="Nothing Provided")


def test_blank_quarter_with_no_dates_is_rejected():
    with pytest.raises(ValidationError, match="Provide either a quarter"):
        ProjectCreate(name="Blank Quarter", quarter="")


# --- half-open / reversed ranges ----------------------------------------------


def test_half_open_date_range_start_only_is_rejected():
    with pytest.raises(ValidationError, match="must both be provided or both be omitted"):
        ProjectCreate(name="Half Open Start", period_start=date(2026, 4, 1))


def test_half_open_date_range_end_only_is_rejected():
    with pytest.raises(ValidationError, match="must both be provided or both be omitted"):
        ProjectCreate(name="Half Open End", period_end=date(2026, 6, 30))


def test_reversed_date_range_is_rejected():
    with pytest.raises(ValidationError, match="period_end must not be before period_start"):
        ProjectCreate(name="Reversed", period_start=date(2026, 6, 30), period_end=date(2026, 4, 1))


def test_equal_start_and_end_dates_are_valid():
    data = ProjectCreate(name="One Day Campaign", period_start=date(2026, 4, 1), period_end=date(2026, 4, 1))
    assert data.period_start == data.period_end


def test_invalid_quarter_format_is_still_rejected():
    with pytest.raises(ValidationError, match="Quarter must be in the format"):
        ProjectCreate(name="Bad Quarter", quarter="Q2-2026")


# --- create_project service persists the new fields --------------------------


def test_create_project_persists_period_and_client_fields(db_session):
    data = ProjectCreate(
        name="Campaign With Client",
        period_start=date(2026, 4, 1),
        period_end=date(2026, 6, 30),
        client_name="Auchan Romania",
    )
    project = create_project(db_session, data)
    db_session.refresh(project)

    assert project.quarter is None
    assert project.period_start == date(2026, 4, 1)
    assert project.period_end == date(2026, 6, 30)
    assert project.client_name == "Auchan Romania"


def test_existing_quarter_only_projects_are_unaffected(project_factory, db_session):
    project = project_factory(name="Legacy Quarter Project", quarter="2026-Q2")
    db_session.refresh(project)

    assert project.quarter == "2026-Q2"
    assert project.period_start is None
    assert project.period_end is None
    assert project.client_name is None


# --- DB-level CHECK constraint: the backstop for a path bypassing Pydantic ----


def test_db_check_constraint_rejects_neither_quarter_nor_dates(db_session):
    project = Project(name="Bypasses Pydantic")
    db_session.add(project)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_db_check_constraint_rejects_half_open_range(db_session):
    project = Project(name="Bypasses Pydantic Half Open", period_start=date(2026, 4, 1))
    db_session.add(project)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_db_check_constraint_rejects_reversed_range(db_session):
    project = Project(
        name="Bypasses Pydantic Reversed",
        period_start=date(2026, 6, 30),
        period_end=date(2026, 4, 1),
    )
    db_session.add(project)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_db_check_constraint_allows_a_valid_date_range_with_no_quarter(db_session):
    project = Project(
        name="Valid Range Project", period_start=date(2026, 4, 1), period_end=date(2026, 6, 30)
    )
    db_session.add(project)
    db_session.commit()  # must not raise
    assert project.quarter is None


# --- web form route: still requires a period, since it has no date UI yet ----


def test_create_project_via_form_rejects_blank_quarter_with_no_dates(
    authenticated_client, db_session
):
    response = authenticated_client.post(
        "/projects",
        data={"name": "No Period Provided", "quarter": ""},
        follow_redirects=False,
    )
    assert response.status_code == 422
    assert db_session.query(Project).filter_by(name="No Period Provided").first() is None
