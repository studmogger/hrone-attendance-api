"""
Employee Attendance & Analytics API

FastAPI backend backed by MongoDB.
Implements the contract defined in openapi.yaml and business rules R1 - R10.
"""
import os
import math
from contextlib import asynccontextmanager
from datetime import datetime, date, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Annotated, Any, Dict, List, Optional, Union

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query
from pydantic import BaseModel, Field, model_validator
from pymongo import MongoClient, ReturnDocument, ASCENDING, DESCENDING
from pymongo.errors import DuplicateKeyError

# --------------------------------------------------------------------------- #
# Environment & Configuration
# --------------------------------------------------------------------------- #
load_dotenv()

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

client: MongoClient = MongoClient(MONGO_URI, tz_aware=True)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))


def init_db_indexes():
    """Create all required indexes idempotently at startup."""
    emp_indexes = [
        ([("emp_code", ASCENDING)], {"unique": True}),
        ([("department", ASCENDING), ("emp_code", ASCENDING)], {}),
        ([("joined_on", ASCENDING)], {}),
    ]
    att_indexes = [
        ([("emp_code", ASCENDING), ("date", ASCENDING)], {"unique": True}),
        ([("date", DESCENDING), ("emp_code", ASCENDING)], {}),
        ([("emp_code", ASCENDING), ("punch_in", DESCENDING)], {}),
        ([("date", ASCENDING), ("status", ASCENDING)], {}),
        ([("date", ASCENDING), ("late_minutes", ASCENDING)], {}),
    ]
    for keys, kwargs in emp_indexes:
        try:
            db.employees.create_index(keys, **kwargs)
        except Exception:
            pass
    for keys, kwargs in att_indexes:
        try:
            db.attendance_logs.create_index(keys, **kwargs)
        except Exception:
            pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db_indexes()
    yield
    client.close()


openapi_tags = [
    {"name": "system", "description": "System health and readiness probes."},
    {"name": "employees", "description": "Employee directory and identity management."},
    {"name": "attendance", "description": "Punch-in, punch-out, and audit regularizations."},
    {"name": "analytics", "description": "High-performance MongoDB aggregation pipelines."},
    {"name": "admin", "description": "Database query explain probes."},
]

app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan,
    openapi_tags=openapi_tags,
)


# --------------------------------------------------------------------------- #
# Helper Functions & Business Rules (R1 - R8)
# --------------------------------------------------------------------------- #
def truncate_millis(ms: int) -> int:
    """R1: Truncate epoch milliseconds to whole seconds."""
    return (ms // 1000) * 1000


def ms_to_utc_dt(ms: int) -> datetime:
    """Convert epoch milliseconds to a UTC datetime truncated to whole seconds."""
    truncated = truncate_millis(ms)
    return datetime.fromtimestamp(truncated / 1000.0, tz=timezone.utc)


def dt_to_ms(dt: Optional[datetime]) -> Optional[int]:
    """Convert BSON datetime to epoch milliseconds truncated to whole seconds."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return truncate_millis(int(dt.timestamp() * 1000))


def round_half_up(value: Union[float, Decimal], decimals: int) -> float:
    """R8: Rounding of reported numbers: 2 decimals half-up (rates: 4 decimals half-up)."""
    d = Decimal(str(value))
    target = Decimal("10") ** (-decimals)
    return float(d.quantize(target, rounding=ROUND_HALF_UP))


def compute_attendance_date(punch_in_utc: datetime, shift_start: str, shift_end: str) -> str:
    """
    R1: Shift times and attendance dates are IST (UTC+05:30).
    A record's date is the IST calendar date of its punch_in instant.
    Exception - overnight shifts (shift_end <= shift_start):
    a punch-in whose IST time is earlier than shift_end belongs to the shift that started the previous day.
    """
    dt_ist = punch_in_utc.astimezone(IST)
    sh, sm = map(int, shift_start.split(":"))
    eh, em = map(int, shift_end.split(":"))
    is_overnight = (eh < sh) or (eh == sh and em <= sm)

    punch_time = dt_ist.time()
    shift_end_time = time(eh, em)

    if is_overnight and punch_time < shift_end_time:
        att_date = dt_ist.date() - timedelta(days=1)
    else:
        att_date = dt_ist.date()
    return att_date.isoformat()


def compute_late_minutes(punch_in_utc: datetime, shift_start: str, attendance_date_str: str) -> int:
    """
    R2: Grace period 10 minutes. Late only if punch-in is strictly more than 10:00 after shift_start;
    then late_minutes = whole minutes (floor) since shift_start, not since grace period ended. Otherwise 0.
    """
    dt_ist = punch_in_utc.astimezone(IST)
    sh, sm = map(int, shift_start.split(":"))
    att_date = date.fromisoformat(attendance_date_str)
    shift_start_dt = datetime.combine(att_date, time(sh, sm), tzinfo=IST)
    diff_sec = (dt_ist - shift_start_dt).total_seconds()
    if diff_sec > 600:
        return math.floor(diff_sec / 60)
    return 0


def compute_overtime_minutes(punch_out_utc: datetime, shift_start: str, shift_end: str, attendance_date_str: str) -> int:
    """
    R3: Overtime. Whole minutes (floor) from shift_end to punch_out, counted only if that is at least 30; otherwise 0.
    Works across midnight for overnight shifts.
    """
    dt_ist = punch_out_utc.astimezone(IST)
    sh, sm = map(int, shift_start.split(":"))
    eh, em = map(int, shift_end.split(":"))
    is_overnight = (eh < sh) or (eh == sh and em <= sm)
    att_date = date.fromisoformat(attendance_date_str)
    if is_overnight:
        end_date = att_date + timedelta(days=1)
    else:
        end_date = att_date
    shift_end_dt = datetime.combine(end_date, time(eh, em), tzinfo=IST)
    diff_sec = (dt_ist - shift_end_dt).total_seconds()
    minutes = math.floor(diff_sec / 60)
    return minutes if minutes >= 30 else 0


def compute_work_hours(punch_in_utc: datetime, punch_out_utc: datetime) -> float:
    """R4: Work hours. (punch_out - punch_in) in seconds / 3600, rounded to 2 decimals half-up."""
    seconds = Decimal(str((punch_out_utc - punch_in_utc).total_seconds()))
    hours = seconds / Decimal("3600")
    rounded = hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return float(rounded)


def compute_half_day(work_hours: Optional[float]) -> bool:
    """R5: Half day. After punch-out, half_day is true when the rounded work_hours < 4.50."""
    if work_hours is None:
        return False
    return work_hours < 4.50


def get_month_working_days(month_str: str, joined_on_str: str) -> int:
    """
    R7: Working days. Monday - Friday, no holiday calendar;
    counted from joined_on for mid-month joiners.
    """
    y, m = map(int, month_str.split("-"))
    start_date = date(y, m, 1)
    if m == 12:
        end_date = date(y + 1, 1, 1) - timedelta(days=1)
    else:
        end_date = date(y, m + 1, 1) - timedelta(days=1)

    joined_date = date.fromisoformat(joined_on_str)
    calc_start = max(start_date, joined_date)
    if calc_start > end_date:
        return 0

    working_days = 0
    curr = calc_start
    while curr <= end_date:
        if curr.weekday() < 5:  # Monday=0, Friday=4
            working_days += 1
        curr += timedelta(days=1)
    return working_days


# --------------------------------------------------------------------------- #
# Serialization Helpers
# --------------------------------------------------------------------------- #
def format_employee_doc(doc: Dict[str, Any]) -> Dict[str, Any]:
    created_at = doc.get("created_at")
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": dt_to_ms(created_at) if isinstance(created_at, datetime) else created_at,
    }


def format_attendance_doc(doc: Dict[str, Any]) -> Dict[str, Any]:
    history_formatted = []
    for h in doc.get("history", []):
        h_at = dt_to_ms(h.get("at")) if isinstance(h.get("at"), datetime) else h.get("at")
        changes_formatted = {}
        for k, v in h.get("changes", {}).items():
            ch_from = v.get("from")
            ch_to = v.get("to")
            if k in ("punch_in", "punch_out"):
                if isinstance(ch_from, datetime):
                    ch_from = dt_to_ms(ch_from)
                if isinstance(ch_to, datetime):
                    ch_to = dt_to_ms(ch_to)
            changes_formatted[k] = {"from": ch_from, "to": ch_to}
        history_formatted.append({
            "at": h_at,
            "by": h.get("by"),
            "reason": h.get("reason"),
            "changes": changes_formatted,
        })

    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": dt_to_ms(doc.get("punch_in")),
        "punch_out": dt_to_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": history_formatted,
    }


# --------------------------------------------------------------------------- #
# Pydantic Schemas
# --------------------------------------------------------------------------- #
EpochMillis = Annotated[int, Field(strict=True, ge=100000000000, le=4102444800000)]


class EmployeeCreate(BaseModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")

    @model_validator(mode="after")
    def validate_shifts(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMillis] = None
    status: str = Field(default="PRESENT", pattern=r"^(PRESENT|WFH|ON_DUTY)$")


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMillis] = None


class RegularizeRequest(BaseModel):
    status: Optional[str] = Field(default=None, pattern=r"^(PRESENT|ABSENT|LEAVE|WFH|ON_DUTY)$")
    punch_in: Optional[EpochMillis] = None
    punch_out: Optional[EpochMillis] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #

@app.get("/health", tags=["system"])
def health():
    """Returns 200 only if MongoDB answers a ping, otherwise 503."""
    try:
        db.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(status_code=503, detail="Database unavailable")


@app.post("/employees", status_code=201, tags=["employees"])
def create_employee(body: EmployeeCreate):
    """Create an employee. Enforces uniqueness on emp_code via unique database index."""
    now_dt = ms_to_utc_dt(int(datetime.now(timezone.utc).timestamp() * 1000))
    doc = body.model_dump()
    doc["created_at"] = now_dt

    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="emp_code already exists")

    return format_employee_doc(doc)


@app.get("/employees", tags=["employees"])
def list_employees(
    department: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    """List employees sorted by emp_code ascending. Total reflects applied filters."""
    q: Dict[str, Any] = {}
    if department:
        q["department"] = department

    total = db.employees.count_documents(q)
    skip = (page - 1) * page_size
    cursor = db.employees.find(q).sort([("emp_code", ASCENDING)]).skip(skip).limit(page_size)
    items = [format_employee_doc(doc) for doc in cursor]

    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201, tags=["attendance"])
def punch_in(body: PunchInRequest):
    """Records a punch-in. Race-safe via unique compound index on (emp_code, date)."""
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    if body.punched_at is not None:
        punched_ms = truncate_millis(body.punched_at)
        dt_utc = ms_to_utc_dt(punched_ms)
    else:
        now_ms = truncate_millis(int(datetime.now(timezone.utc).timestamp() * 1000))
        dt_utc = ms_to_utc_dt(now_ms)

    att_date = compute_attendance_date(dt_utc, emp["shift_start"], emp["shift_end"])
    late_min = compute_late_minutes(dt_utc, emp["shift_start"], att_date)

    doc = {
        "emp_code": body.emp_code,
        "date": att_date,
        "status": body.status,
        "punch_in": dt_utc,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_min,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="Already punched in for this date")

    return format_attendance_doc(doc)


@app.post("/attendance/punch-out", status_code=200, tags=["attendance"])
def punch_out(body: PunchOutRequest):
    """
    Closes the employee's most recent record whose punch_in is at or before punched_at.
    Computes work_hours (R4), overtime_minutes (R3) and half_day (R5).
    """
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    if body.punched_at is not None:
        punched_ms = truncate_millis(body.punched_at)
        dt_out_utc = ms_to_utc_dt(punched_ms)
    else:
        now_ms = truncate_millis(int(datetime.now(timezone.utc).timestamp() * 1000))
        dt_out_utc = ms_to_utc_dt(now_ms)

    # Find the most recent record whose punch_in <= punched_at
    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": dt_out_utc}},
        sort=[("punch_in", DESCENDING)],
    )

    if not record:
        raise HTTPException(status_code=404, detail="No matching punch-in found")

    if record.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="Record already punched out")

    dt_in_utc = record["punch_in"]
    if dt_in_utc.tzinfo is None:
        dt_in_utc = dt_in_utc.replace(tzinfo=timezone.utc)

    # Validations: punched_at <= punch_in or > 24 hours
    if dt_out_utc <= dt_in_utc:
        raise HTTPException(status_code=422, detail="punch_out must be after punch_in")

    if (dt_out_utc - dt_in_utc).total_seconds() > 24 * 3600:
        raise HTTPException(
            status_code=422,
            detail="punch_out cannot be more than 24 hours after punch_in",
        )

    # Compute derived fields
    work_hrs = compute_work_hours(dt_in_utc, dt_out_utc)
    overtime_min = compute_overtime_minutes(dt_out_utc, emp["shift_start"], emp["shift_end"], record["date"])
    half_day = compute_half_day(work_hrs)

    # Atomic update to guarantee single 200 under concurrent requests
    updated = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": dt_out_utc,
                "work_hours": work_hrs,
                "overtime_minutes": overtime_min,
                "half_day": half_day,
            }
        },
        return_document=ReturnDocument.AFTER,
    )

    if not updated:
        raise HTTPException(status_code=409, detail="Record already punched out")

    return format_attendance_doc(updated)


@app.get("/attendance", tags=["attendance"])
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = Query(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    date_to: Optional[str] = Query(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    status: Optional[str] = Query(default=None, pattern=r"^(PRESENT|ABSENT|LEAVE|WFH|ON_DUTY)$"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    """
    List attendance records sorted by date descending, then emp_code ascending.
    Paging is performed in MongoDB without loading the collection into memory.
    """
    if date_from and date_to and date_from > date_to:
        raise HTTPException(
            status_code=422,
            detail="date_from cannot be after date_to",
        )

    q: Dict[str, Any] = {}
    if emp_code:
        q["emp_code"] = emp_code
    if status:
        q["status"] = status
    if date_from or date_to:
        date_q = {}
        if date_from:
            date_q["$gte"] = date_from
        if date_to:
            date_q["$lte"] = date_to
        q["date"] = date_q

    total = db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size
    cursor = db.attendance_logs.find(q).sort([("date", DESCENDING), ("emp_code", ASCENDING)]).skip(skip).limit(page_size)
    items = [format_attendance_doc(doc) for doc in cursor]

    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}", tags=["attendance"])
def regularize_attendance(
    emp_code: str = Path(...),
    date: str = Path(..., pattern=r"^\d{4}-\d{2}-\d{2}$"),
    body: RegularizeRequest = ...,
):
    """
    Regularize (manually correct) one day's record.
    Recomputes derived fields and appends to history.
    """
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not rec:
        raise HTTPException(status_code=404, detail="Attendance record not found")

    # Current stored values
    curr_status = rec["status"]
    curr_in = rec.get("punch_in")
    curr_out = rec.get("punch_out")
    if curr_in and curr_in.tzinfo is None:
        curr_in = curr_in.replace(tzinfo=timezone.utc)
    if curr_out and curr_out.tzinfo is None:
        curr_out = curr_out.replace(tzinfo=timezone.utc)

    # Determine target status
    new_status = body.status if body.status is not None else curr_status

    if new_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(
                status_code=422,
                detail="Punch times cannot be provided when setting status to ABSENT or LEAVE",
            )
        new_in = None
        new_out = None
        new_work_hours = None
        new_late_minutes = 0
        new_overtime_minutes = 0
        new_half_day = False
    else:
        # Presence status requires a punch_in
        if body.punch_in is not None:
            new_in = ms_to_utc_dt(body.punch_in)
            # R1: punch_in must stay on the record's attendance date
            calc_date = compute_attendance_date(new_in, emp["shift_start"], emp["shift_end"])
            if calc_date != date:
                raise HTTPException(
                    status_code=422,
                    detail=f"punch_in falls on attendance date {calc_date}, expected {date}",
                )
        else:
            new_in = curr_in

        if new_in is None:
            raise HTTPException(
                status_code=422,
                detail="Presence status requires a punch_in",
            )

        if body.punch_out is not None:
            new_out = ms_to_utc_dt(body.punch_out)
        else:
            new_out = curr_out

        if new_out is not None:
            if new_out <= new_in:
                raise HTTPException(
                    status_code=422,
                    detail="punch_out must be after punch_in",
                )
            if (new_out - new_in).total_seconds() > 24 * 3600:
                raise HTTPException(
                    status_code=422,
                    detail="punch_out cannot be more than 24 hours after punch_in",
                )
            new_work_hours = compute_work_hours(new_in, new_out)
            new_overtime_minutes = compute_overtime_minutes(new_out, emp["shift_start"], emp["shift_end"], date)
            new_half_day = compute_half_day(new_work_hours)
        else:
            new_work_hours = None
            new_overtime_minutes = 0
            new_half_day = False

        new_late_minutes = compute_late_minutes(new_in, emp["shift_start"], date)

    # Detect actual changes
    changes_mongo: Dict[str, Any] = {}
    if new_status != curr_status:
        changes_mongo["status"] = {"from": curr_status, "to": new_status}
    if new_in != curr_in:
        changes_mongo["punch_in"] = {"from": curr_in, "to": new_in}
    if new_out != curr_out:
        changes_mongo["punch_out"] = {"from": curr_out, "to": new_out}
    if new_work_hours != rec.get("work_hours"):
        changes_mongo["work_hours"] = {"from": rec.get("work_hours"), "to": new_work_hours}
    if new_late_minutes != rec.get("late_minutes", 0):
        changes_mongo["late_minutes"] = {"from": rec.get("late_minutes", 0), "to": new_late_minutes}
    if new_overtime_minutes != rec.get("overtime_minutes", 0):
        changes_mongo["overtime_minutes"] = {"from": rec.get("overtime_minutes", 0), "to": new_overtime_minutes}
    if new_half_day != rec.get("half_day", False):
        changes_mongo["half_day"] = {"from": rec.get("half_day", False), "to": new_half_day}

    if not changes_mongo:
        raise HTTPException(
            status_code=422,
            detail="Request changes nothing",
        )

    now_utc = ms_to_utc_dt(int(datetime.now(timezone.utc).timestamp() * 1000))
    history_entry = {
        "at": now_utc,
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes_mongo,
    }

    # Optimistic concurrency check: match history length to prevent losing concurrent edits
    curr_history_len = len(rec.get("history", []))
    updated = db.attendance_logs.find_one_and_update(
        {
            "_id": rec["_id"],
            "history": {"$size": curr_history_len},
        },
        {
            "$set": {
                "status": new_status,
                "punch_in": new_in,
                "punch_out": new_out,
                "work_hours": new_work_hours,
                "late_minutes": new_late_minutes,
                "overtime_minutes": new_overtime_minutes,
                "half_day": new_half_day,
            },
            "$push": {"history": history_entry},
        },
        return_document=ReturnDocument.AFTER,
    )

    if not updated:
        raise HTTPException(
            status_code=409,
            detail="Conflict: record was modified concurrently",
        )

    return format_attendance_doc(updated)


# --------------------------------------------------------------------------- #
# Analytics Endpoints (MongoDB Aggregations)
# --------------------------------------------------------------------------- #

@app.get("/analytics/employees/{emp_code}/monthly", tags=["analytics"])
def employee_monthly(
    emp_code: str = Path(...),
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    """Monthly attendance summary for one employee."""
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    working_days = get_month_working_days(month, emp["joined_on"])
    month_start = f"{month}-01"
    month_end = f"{month}-31"

    pipeline = [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": month_start, "$lte": month_end},
            }
        },
        {
            "$project": {
                "status": 1,
                "late_minutes": {"$ifNull": ["$late_minutes", 0]},
                "overtime_minutes": {"$ifNull": ["$overtime_minutes", 0]},
                "half_day": {"$ifNull": ["$half_day", False]},
                "dayOfWeek": {
                    "$dayOfWeek": {
                        "$dateFromString": {"dateString": "$date"}
                    }
                },
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$in": ["$dayOfWeek", [2, 3, 4, 5, 6]]},  # Mon-Fri
                                ]
                            },
                            {"$cond": ["$half_day", 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                },
                "total_late_minutes": {"$sum": "$late_minutes"},
                "total_overtime_minutes": {"$sum": "$overtime_minutes"},
            }
        },
    ]

    agg_res = list(db.attendance_logs.aggregate(pipeline))
    if agg_res:
        row = agg_res[0]
        present_days = float(row.get("present_days", 0.0))
        leave_days = int(row.get("leave_days", 0))
        late_count = int(row.get("late_count", 0))
        total_late = int(row.get("total_late_minutes", 0))
        total_ot = int(row.get("total_overtime_minutes", 0))
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late = 0
        total_ot = 0

    if working_days > 0:
        pct = round_half_up((Decimal(str(present_days)) / Decimal(str(working_days))) * Decimal("100"), 2)
        attendance_pct: Optional[float] = pct
    else:
        attendance_pct = None

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late,
        "total_overtime_minutes": total_ot,
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/departments/summary", tags=["analytics"])
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = Query(default=None),
):
    """Department summary for a month via cross-collection aggregation."""
    y, m = map(int, month.split("-"))
    month_start = f"{month}-01"
    if m == 12:
        month_end = (date(y + 1, 1, 1) - timedelta(days=1)).isoformat()
    else:
        month_end = (date(y, m + 1, 1) - timedelta(days=1)).isoformat()

    match_filter: Dict[str, Any] = {"joined_on": {"$lte": month_end}}
    if department:
        match_filter["department"] = department

    pipeline = [
        {"$match": match_filter},
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "emp_codes": {"$push": "$emp_code"},
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"codes": "$emp_codes"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$in": ["$emp_code", "$$codes"]},
                                    {"$gte": ["$date", month_start]},
                                    {"$lte": ["$date", month_end]},
                                ]
                            }
                        }
                    },
                    {
                        "$project": {
                            "status": 1,
                            "work_hours": 1,
                            "late_minutes": {"$ifNull": ["$late_minutes", 0]},
                            "half_day": {"$ifNull": ["$half_day", False]},
                            "dayOfWeek": {
                                "$dayOfWeek": {
                                    "$dateFromString": {"dateString": "$date"}
                                }
                            },
                        }
                    },
                ],
                "as": "logs",
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": 1,
                "present_days": {
                    "$reduce": {
                        "input": "$logs",
                        "initialValue": 0.0,
                        "in": {
                            "$add": [
                                "$$value",
                                {
                                    "$cond": [
                                        {
                                            "$and": [
                                                {"$in": ["$$this.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                                {"$in": ["$$this.dayOfWeek", [2, 3, 4, 5, 6]]},
                                            ]
                                        },
                                        {"$cond": ["$$this.half_day", 0.5, 1.0]},
                                        0.0,
                                    ]
                                },
                            ]
                        },
                    }
                },
                "work_hours_array": {
                    "$filter": {
                        "input": "$logs",
                        "as": "l",
                        "cond": {
                            "$and": [
                                {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                {"$ne": ["$$l.work_hours", None]},
                            ]
                        },
                    }
                },
                "late_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "l",
                            "cond": {"$gt": ["$$l.late_minutes", 0]},
                        }
                    }
                },
                "total_late_minutes": {
                    "$reduce": {
                        "input": "$logs",
                        "initialValue": 0,
                        "in": {"$add": ["$$value", "$$this.late_minutes"]},
                    }
                },
                "leave_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "l",
                            "cond": {"$eq": ["$$l.status", "LEAVE"]},
                        }
                    }
                },
                "on_duty_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "l",
                            "cond": {"$eq": ["$$l.status", "ON_DUTY"]},
                        }
                    }
                },
            }
        },
        {
            "$project": {
                "department": 1,
                "headcount": 1,
                "present_days": 1,
                "avg_work_hours": {
                    "$cond": [
                        {"$gt": [{"$size": "$work_hours_array"}, 0]},
                        {"$avg": "$work_hours_array.work_hours"},
                        None,
                    ]
                },
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
            }
        },
        {"$sort": {"department": ASCENDING}},
    ]

    items = []
    for row in db.employees.aggregate(pipeline):
        avg_wh = row.get("avg_work_hours")
        items.append({
            "department": row["department"],
            "headcount": int(row["headcount"]),
            "present_days": float(row["present_days"]),
            "avg_work_hours": round_half_up(avg_wh, 2) if avg_wh is not None else None,
            "late_count": int(row["late_count"]),
            "total_late_minutes": int(row["total_late_minutes"]),
            "leave_count": int(row["leave_count"]),
            "on_duty_count": int(row["on_duty_count"]),
        })

    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late", tags=["analytics"])
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(default=10, ge=1, le=50),
    department: Optional[str] = Query(default=None),
):
    """
    Late-comers ranking with ties using standard competition ranking (1, 2, 2, 4).
    Limit applies after ranking.
    """
    month_start = f"{month}-01"
    month_end = f"{month}-31"

    pipeline: List[Dict[str, Any]] = [
        {
            "$match": {
                "date": {"$gte": month_start, "$lte": month_end},
                "late_minutes": {"$gt": 0},
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
    ]

    if department:
        pipeline.append({"$match": {"emp.department": department}})

    pipeline.extend([
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": DESCENDING},
                "output": {
                    "rank": {
                        "$rank": {}  # standard competition ranking
                    }
                },
            }
        },
        {
            "$match": {
                "rank": {"$lte": limit}
            }
        },
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
        {
            "$sort": {
                "total_late_minutes": DESCENDING,
                "emp_code": ASCENDING,
            }
        },
    ])

    items = list(db.attendance_logs.aggregate(pipeline))
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend", tags=["analytics"])
def department_trend(
    department: str = Path(...),
    from_: str = Query(..., alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to: str = Query(..., pattern=r"^\d{4}-\d{2}-\d{2}$"),
):
    """
    Daily attendance trend for a department with gap-filling and a 7-day moving average.
    All processing is performed inside MongoDB.
    """
    dept_exists = db.employees.find_one({"department": department})
    if not dept_exists:
        raise HTTPException(status_code=404, detail="Department not found")

    from_date = date.fromisoformat(from_)
    to_date = date.fromisoformat(to)

    if to_date < from_date:
        raise HTTPException(
            status_code=422,
            detail="'to' date cannot be before 'from' date",
        )

    day_span = (to_date - from_date).days + 1
    if day_span > 92:
        raise HTTPException(
            status_code=422,
            detail="Date range cannot exceed 92 days",
        )

    from_dt = datetime.combine(from_date, time(0, 0), tzinfo=timezone.utc)
    to_dt = datetime.combine(to_date, time(0, 0), tzinfo=timezone.utc)
    to_dt_bounds = to_dt + timedelta(days=1)

    # Aggregation on attendance_logs using $densify for gap-filling and $setWindowFields for 7d moving average
    pipeline = [
        # Match records in range
        {
            "$match": {
                "date": {"$gte": from_, "$lte": to}
            }
        },
        # Join employees to filter by department
        {
            "$lookup": {
                "from": "employees",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
        {"$match": {"emp.department": department}},
        # Aggregate logs by date
        {
            "$group": {
                "_id": "$date",
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$cond": ["$half_day", 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                },
            }
        },
        {
            "$project": {
                "date_dt": {"$dateFromString": {"dateString": "$_id"}},
                "present_count": 1,
                "late_count": 1,
            }
        },
        # Union with a seed document for this department to guarantee at least one document for densify
        {
            "$unionWith": {
                "coll": "employees",
                "pipeline": [
                    {"$match": {"department": department}},
                    {"$limit": 1},
                    {
                        "$project": {
                            "_id": 0,
                            "date_dt": {"$literal": from_dt},
                            "present_count": {"$literal": 0.0},
                            "late_count": {"$literal": 0},
                            "_seed": {"$literal": True},
                        }
                    },
                ],
            }
        },
        # Re-group by date_dt to merge seed with actual records if date matches
        {
            "$group": {
                "_id": "$date_dt",
                "present_count": {"$max": "$present_count"},
                "late_count": {"$max": "$late_count"},
            }
        },
        {
            "$project": {
                "date_dt": "$_id",
                "present_count": 1,
                "late_count": 1,
            }
        },
        # Gap-filling stage using $densify
        {
            "$densify": {
                "field": "date_dt",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [from_dt, to_dt_bounds],
                },
            }
        },
        {
            "$project": {
                "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$date_dt"}},
                "dayOfWeek": {"$dayOfWeek": "$date_dt"},
                "present_count": {"$ifNull": ["$present_count", 0.0]},
                "late_count": {"$ifNull": ["$late_count", 0]},
            }
        },
        # Lookup department employees to compute daily headcount
        {
            "$lookup": {
                "from": "employees",
                "pipeline": [
                    {"$match": {"department": department}},
                    {"$project": {"_id": 0, "joined_on": 1}},
                ],
                "as": "dept_emps",
            }
        },
        {
            "$project": {
                "date": 1,
                "is_working_day": {"$in": ["$dayOfWeek", [2, 3, 4, 5, 6]]},
                "headcount": {
                    "$size": {
                        "$filter": {
                            "input": "$dept_emps",
                            "as": "e",
                            "cond": {"$lte": ["$$e.joined_on", "$date"]},
                        }
                    }
                },
                "present_count": 1,
                "late_count": 1,
            }
        },
        {
            "$project": {
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                "$is_working_day",
                                {"$gt": ["$headcount", 0]},
                            ]
                        },
                        {"$divide": ["$present_count", "$headcount"]},
                        None,
                    ]
                },
            }
        },
        # Compute 7-day moving average over non-null attendance_rate
        {
            "$setWindowFields": {
                "sortBy": {"date": ASCENDING},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {
                            "documents": [-6, "current"],
                        },
                    }
                },
            }
        },
        {"$sort": {"date": ASCENDING}},
    ]

    items = []
    for row in db.attendance_logs.aggregate(pipeline):
        att_rate = row.get("attendance_rate")
        m_avg = row.get("moving_avg_7d")
        items.append({
            "date": row["date"],
            "is_working_day": bool(row["is_working_day"]),
            "headcount": int(row["headcount"]),
            "present_count": float(row["present_count"]),
            "late_count": int(row["late_count"]),
            "attendance_rate": round_half_up(att_rate, 4) if att_rate is not None else None,
            "moving_avg_7d": round_half_up(m_avg, 4) if m_avg is not None else None,
        })

    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Admin Explain Endpoint
# --------------------------------------------------------------------------- #

@app.get("/admin/explain/{endpoint}", tags=["admin"])
def explain_endpoint(
    endpoint: str = Path(..., pattern=r"^(attendance_list|employee_monthly|department_summary|late_leaderboard|department_trend)$"),
    emp_code: Optional[str] = Query(default=None),
    month: Optional[str] = Query(default=None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = Query(default=None),
    limit: int = Query(default=10, ge=1, le=50),
    date_from: Optional[str] = Query(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    date_to: Optional[str] = Query(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    status: Optional[str] = Query(default=None, pattern=r"^(PRESENT|ABSENT|LEAVE|WFH|ON_DUTY)$"),
    from_: Optional[str] = Query(default=None, alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to: Optional[str] = Query(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    """
    Runs explain with executionStats verbosity for the main query or aggregation of the named endpoint,
    built exactly as that endpoint builds it.
    """
    if endpoint == "attendance_list":
        if date_from and date_to and date_from > date_to:
            raise HTTPException(
                status_code=422,
                detail="date_from cannot be after date_to",
            )
        q: Dict[str, Any] = {}
        if emp_code:
            q["emp_code"] = emp_code
        if status:
            q["status"] = status
        if date_from or date_to:
            dq = {}
            if date_from:
                dq["$gte"] = date_from
            if date_to:
                dq["$lte"] = date_to
            q["date"] = dq

        skip = (page - 1) * page_size
        explain_res = db.command({
            "explain": {
                "find": "attendance_logs",
                "filter": q,
                "sort": {"date": DESCENDING, "emp_code": ASCENDING},
                "skip": skip,
                "limit": page_size,
            },
            "verbosity": "executionStats",
        })
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": explain_res,
        }

    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(
                status_code=422,
                detail="emp_code and month are required for employee_monthly explain",
            )
        month_start = f"{month}-01"
        month_end = f"{month}-31"
        pipeline = [
            {"$match": {"emp_code": emp_code, "date": {"$gte": month_start, "$lte": month_end}}},
            {
                "$project": {
                    "status": 1,
                    "late_minutes": {"$ifNull": ["$late_minutes", 0]},
                    "overtime_minutes": {"$ifNull": ["$overtime_minutes", 0]},
                    "half_day": {"$ifNull": ["$half_day", False]},
                    "dayOfWeek": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
                }
            },
            {
                "$group": {
                    "_id": None,
                    "present_days": {
                        "$sum": {
                            "$cond": [
                                {
                                    "$and": [
                                        {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                        {"$in": ["$dayOfWeek", [2, 3, 4, 5, 6]]},
                                    ]
                                },
                                {"$cond": ["$half_day", 0.5, 1.0]},
                                0.0,
                            ]
                        }
                    },
                    "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                    "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
                    "total_late_minutes": {"$sum": "$late_minutes"},
                    "total_overtime_minutes": {"$sum": "$overtime_minutes"},
                }
            },
        ]
        explain_res = db.command({
            "explain": {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {},
            },
            "verbosity": "executionStats",
        })
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": explain_res,
        }

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(
                status_code=422,
                detail="month is required for department_summary explain",
            )
        y, m = map(int, month.split("-"))
        month_start = f"{month}-01"
        if m == 12:
            month_end = (date(y + 1, 1, 1) - timedelta(days=1)).isoformat()
        else:
            month_end = (date(y, m + 1, 1) - timedelta(days=1)).isoformat()

        match_filter: Dict[str, Any] = {"joined_on": {"$lte": month_end}}
        if department:
            match_filter["department"] = department

        pipeline = [
            {"$match": match_filter},
            {
                "$group": {
                    "_id": "$department",
                    "headcount": {"$sum": 1},
                    "emp_codes": {"$push": "$emp_code"},
                }
            },
            {
                "$lookup": {
                    "from": "attendance_logs",
                    "let": {"codes": "$emp_codes"},
                    "pipeline": [
                        {
                            "$match": {
                                "$expr": {
                                    "$and": [
                                        {"$in": ["$emp_code", "$$codes"]},
                                        {"$gte": ["$date", month_start]},
                                        {"$lte": ["$date", month_end]},
                                    ]
                                }
                            }
                        }
                    ],
                    "as": "logs",
                }
            },
        ]
        explain_res = db.command({
            "explain": {
                "aggregate": "employees",
                "pipeline": pipeline,
                "cursor": {},
            },
            "verbosity": "executionStats",
        })
        return {
            "endpoint": endpoint,
            "collection": "employees",
            "explain": explain_res,
        }

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(
                status_code=422,
                detail="month is required for late_leaderboard explain",
            )
        month_start = f"{month}-01"
        month_end = f"{month}-31"
        pipeline = [
            {"$match": {"date": {"$gte": month_start, "$lte": month_end}, "late_minutes": {"$gt": 0}}},
            {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
            {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "emp"}},
            {"$unwind": "$emp"},
        ]
        if department:
            pipeline.append({"$match": {"emp.department": department}})
        pipeline.extend([
            {"$setWindowFields": {"sortBy": {"total_late_minutes": DESCENDING}, "output": {"rank": {"$rank": {}}}}},
            {"$match": {"rank": {"$lte": limit}}},
        ])
        explain_res = db.command({
            "explain": {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {},
            },
            "verbosity": "executionStats",
        })
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": explain_res,
        }

    elif endpoint == "department_trend":
        if not department or not from_ or not to:
            raise HTTPException(
                status_code=422,
                detail="department, from, and to are required for department_trend explain",
            )
        from_date = date.fromisoformat(from_)
        to_date = date.fromisoformat(to)
        if to_date < from_date or (to_date - from_date).days + 1 > 92:
            raise HTTPException(
                status_code=422,
                detail="Invalid date range for department_trend",
            )
        from_dt = datetime.combine(from_date, time(0, 0), tzinfo=timezone.utc)
        to_dt = datetime.combine(to_date, time(0, 0), tzinfo=timezone.utc)
        to_dt_bounds = to_dt + timedelta(days=1)

        pipeline = [
            {"$match": {"date": {"$gte": from_, "$lte": to}}},
            {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "emp"}},
            {"$unwind": "$emp"},
            {"$match": {"emp.department": department}},
            {
                "$group": {
                    "_id": "$date",
                    "present_count": {
                        "$sum": {
                            "$cond": [
                                {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                {"$cond": ["$half_day", 0.5, 1.0]},
                                0.0,
                            ]
                        }
                    },
                    "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
                }
            },
            {
                "$project": {
                    "date_dt": {"$dateFromString": {"dateString": "$_id"}},
                    "present_count": 1,
                    "late_count": 1,
                }
            },
            {
                "$densify": {
                    "field": "date_dt",
                    "range": {"step": 1, "unit": "day", "bounds": [from_dt, to_dt_bounds]},
                }
            },
        ]
        explain_res = db.command({
            "explain": {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {},
            },
            "verbosity": "executionStats",
        })
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": explain_res,
        }
