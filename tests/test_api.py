"""
Comprehensive automated tests for HROne Employee Attendance & Analytics API.
Covers business rules R1 - R10, edge cases, ties, mid-month joiners, overnight shifts,
concurrency constraints, and database explain plans.
"""
import os
import pytest
from datetime import datetime, timezone
from decimal import Decimal
from fastapi.testclient import TestClient

# Ensure tests run against local test database
os.environ["MONGO_URI"] = os.getenv("MONGO_URI", "mongodb://localhost:27017")
os.environ["MONGO_DB"] = "attendance_db_test"

from app.main import (
    app,
    db,
    init_db_indexes,
    compute_attendance_date,
    compute_late_minutes,
    compute_overtime_minutes,
    compute_work_hours,
    round_half_up,
)

client = TestClient(app)


@pytest.fixture(autouse=True, scope="module")
def setup_database():
    """Initializes schema, indexes, and comprehensive edge-case test dataset."""
    db.employees.delete_many({})
    db.attendance_logs.delete_many({})
    init_db_indexes()

    # Seed diverse employees (overnight shift, mid-month joiner, standard shift, zero-log employee)
    db.employees.insert_many([
        {
            "emp_code": "EMP0001",
            "name": "Asha Rao",
            "email": "asha@example.com",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-05",
            "created_at": datetime(2026, 1, 5, 4, 30, tzinfo=timezone.utc),
        },
        {
            "emp_code": "EMP0002",
            "name": "Vikram Shah",
            "email": "vikram@example.com",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-07-13",  # Mid-month joiner
            "created_at": datetime(2026, 7, 13, 4, 0, tzinfo=timezone.utc),
        },
        {
            "emp_code": "EMP0003",
            "name": "Neha Iyer",
            "email": "neha@example.com",
            "department": "Sales",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-02-01",
            "created_at": datetime(2026, 2, 1, 4, 30, tzinfo=timezone.utc),
        },
        {
            "emp_code": "EMP0004",
            "name": "Rohit Das",
            "email": "rohit@example.com",
            "department": "Sales",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-02-01",
            "created_at": datetime(2026, 2, 1, 4, 31, tzinfo=timezone.utc),
        },
        {
            "emp_code": "EMP0005",
            "name": "Meera Nair",
            "email": "meera@example.com",
            "department": "Support",
            "shift_start": "22:00",
            "shift_end": "06:00",  # Overnight shift
            "joined_on": "2026-03-10",
            "created_at": datetime(2026, 3, 10, 4, 30, tzinfo=timezone.utc),
        },
        {
            "emp_code": "EMP0006",
            "name": "Zero Log Emp",
            "email": "zerolog@example.com",
            "department": "Engineering",  # Employee with 0 logs to verify Headcount (R9)
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-01",
            "created_at": datetime(2026, 1, 1, 4, 30, tzinfo=timezone.utc),
        },
    ])
    yield
    db.employees.delete_many({})
    db.attendance_logs.delete_many({})


# --------------------------------------------------------------------------- #
# Unit Tests: Business Rules & Math Helpers
# --------------------------------------------------------------------------- #

def test_r1_overnight_shift_date():
    # 02:00 IST on July 7 is 20:30 UTC on July 6
    # Belongs to shift starting July 6
    dt_utc = datetime(2026, 7, 6, 20, 30, tzinfo=timezone.utc)
    att_date = compute_attendance_date(dt_utc, "22:00", "06:00")
    assert att_date == "2026-07-06"

    # 22:15 IST on July 6 (16:45 UTC on July 6) belongs to July 6
    dt_utc_start = datetime(2026, 7, 6, 16, 45, tzinfo=timezone.utc)
    assert compute_attendance_date(dt_utc_start, "22:00", "06:00") == "2026-07-06"


def test_r2_late_minutes_rules():
    # Shift 09:30 IST. 09:40:00 -> exactly 10 min grace period -> 0 late
    dt_grace = datetime(2026, 7, 6, 4, 10, 0, tzinfo=timezone.utc)
    assert compute_late_minutes(dt_grace, "09:30", "2026-07-06") == 0

    # 09:40:01 -> strictly > 10 min -> floor from shift start = 10 min
    dt_late1 = datetime(2026, 7, 6, 4, 10, 1, tzinfo=timezone.utc)
    assert compute_late_minutes(dt_late1, "09:30", "2026-07-06") == 10

    # 10:15:59 -> 45 min late
    dt_late2 = datetime(2026, 7, 6, 4, 45, 59, tzinfo=timezone.utc)
    assert compute_late_minutes(dt_late2, "09:30", "2026-07-06") == 45


def test_r3_overtime_threshold():
    # Shift end 18:30 IST. Punch out 18:59 IST (29 min OT) -> below 30 min -> 0 OT
    dt_ot29 = datetime(2026, 7, 6, 13, 29, 0, tzinfo=timezone.utc)
    assert compute_overtime_minutes(dt_ot29, "09:30", "18:30", "2026-07-06") == 0

    # Punch out 19:00 IST (30 min OT) -> 30 min
    dt_ot30 = datetime(2026, 7, 6, 13, 30, 0, tzinfo=timezone.utc)
    assert compute_overtime_minutes(dt_ot30, "09:30", "18:30", "2026-07-06") == 30


def test_r4_half_up_rounding():
    # Half-up test: 0.005 should round up to 0.01 (banker's rounding would round down to 0.00)
    assert round_half_up(Decimal("0.005"), 2) == 0.01
    assert round_half_up(Decimal("0.015"), 2) == 0.02


# --------------------------------------------------------------------------- #
# Integration Tests: Endpoints & Concurrency
# --------------------------------------------------------------------------- #

def test_health():
    res = client.get("/health")
    assert res.status_code == 200
    assert res.json() == {"status": "ok"}


def test_create_employee_validation_and_uniqueness():
    payload = {
        "emp_code": "EMP8888",
        "name": "Integration User",
        "email": "user@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-05-01",
    }
    r = client.post("/employees", json=payload)
    assert r.status_code == 201

    # Duplicate -> 409
    r_dup = client.post("/employees", json=payload)
    assert r_dup.status_code == 409

    # Identical shift start and end -> 422
    bad_payload = payload.copy()
    bad_payload["emp_code"] = "EMP8889"
    bad_payload["shift_end"] = "09:30"
    assert client.post("/employees", json=bad_payload).status_code == 422


def test_punch_in_and_race_condition():
    dt = datetime(2026, 7, 6, 3, 58, 0, tzinfo=timezone.utc)
    ms = int(dt.timestamp() * 1000)

    # First punch-in succeeds (201)
    res = client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms, "status": "PRESENT"})
    assert res.status_code == 201
    assert res.json()["date"] == "2026-07-06"

    # Concurrent/second punch-in for same date -> 409 Conflict
    res_dup = client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms + 5000})
    assert res_dup.status_code == 409


def test_punch_out_lifecycle():
    # EMP0001 punches out after 9.12 hours
    dt_out = datetime(2026, 7, 6, 13, 5, 0, tzinfo=timezone.utc)
    ms_out = int(dt_out.timestamp() * 1000)

    res = client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms_out})
    assert res.status_code == 200
    doc = res.json()
    assert doc["work_hours"] == 9.12
    assert doc["half_day"] is False

    # Second punch out -> 409 Conflict
    assert client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms_out}).status_code == 409


def test_regularize_audit_trail():
    # Correct punch_in for EMP0001 on 2026-07-06
    dt_corrected = datetime(2026, 7, 6, 3, 50, 0, tzinfo=timezone.utc)
    ms_corrected = int(dt_corrected.timestamp() * 1000)

    patch_payload = {
        "punch_in": ms_corrected,
        "reason": "Security badge scan delay",
        "regularized_by": "admin.hr",
    }
    r = client.patch("/attendance/EMP0001/2026-07-06", json=patch_payload)
    assert r.status_code == 200
    doc = r.json()
    assert len(doc["history"]) == 1
    assert doc["history"][0]["by"] == "admin.hr"
    assert "punch_in" in doc["history"][0]["changes"]


def test_mid_month_joiner_working_days():
    # EMP0002 joined on 2026-07-13.
    # July 2026 has 23 total working days. Between July 13 and July 31 there are 15 working days.
    res = client.get("/analytics/employees/EMP0002/monthly?month=2026-07")
    assert res.status_code == 200
    assert res.json()["working_days"] == 15


def test_department_summary_includes_zero_log_employee():
    # Engineering department has EMP0001, EMP0002, EMP0006 (0 logs), and EMP8888. Headcount must be 4.
    res = client.get("/analytics/departments/summary?month=2026-07&department=Engineering")
    assert res.status_code == 200
    items = res.json()["items"]
    assert len(items) == 1
    assert items[0]["headcount"] == 4


def test_late_leaderboard_competition_ranking_and_ties():
    # Create tied late records for EMP0003 and EMP0004
    for emp_code in ["EMP0003", "EMP0004"]:
        db.attendance_logs.insert_one({
            "emp_code": emp_code,
            "date": "2026-07-10",
            "status": "PRESENT",
            "punch_in": datetime(2026, 7, 10, 4, 30, tzinfo=timezone.utc),  # 30 min late
            "punch_out": None,
            "work_hours": None,
            "late_minutes": 30,
            "overtime_minutes": 0,
            "half_day": False,
            "history": [],
        })

    res = client.get("/analytics/leaderboard/late?month=2026-07&limit=1")
    assert res.status_code == 200
    items = res.json()["items"]
    # Tied at rank 1: limit applies AFTER ranking, so both must be present!
    assert len(items) == 2
    assert items[0]["rank"] == 1
    assert items[1]["rank"] == 1


def test_department_trend_gap_filling():
    # Check trend for Support across 5 days where no logs exist
    res = client.get("/analytics/departments/Support/trend?from=2026-08-01&to=2026-08-05")
    assert res.status_code == 200
    items = res.json()["items"]
    assert len(items) == 5
    # Weekend days have attendance_rate: None
    assert items[0]["is_working_day"] is False
    assert items[0]["attendance_rate"] is None
    # Weekday with zero logs has rate 0.0
    assert items[2]["is_working_day"] is True
    assert items[2]["attendance_rate"] == 0.0


def test_admin_explain_plans_all_use_ixscan():
    for ep, params in [
        ("attendance_list", {"date_from": "2026-07-01", "date_to": "2026-07-10"}),
        ("employee_monthly", {"emp_code": "EMP0001", "month": "2026-07"}),
        ("department_summary", {"month": "2026-07"}),
        ("late_leaderboard", {"month": "2026-07"}),
        ("department_trend", {"department": "Engineering", "from": "2026-07-01", "to": "2026-07-07"}),
    ]:
        res = client.get(f"/admin/explain/{ep}", params=params)
        assert res.status_code == 200
        text = str(res.json())
        assert "IXSCAN" in text
        assert "COLLSCAN" not in text
