"""Employee Attendance & Analytics API. Run: uvicorn app.main:app --port 8000."""
import calendar
import json
import os
import re
from contextlib import asynccontextmanager
from datetime import date as Date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Annotated, Literal

from bson import Decimal128, json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import AfterValidator, BaseModel, Field, field_validator, model_validator
from pymongo import IndexModel, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()
client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True, serverSelectionTimeoutMS=3000, connectTimeoutMS=3000,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]
UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
PRESENCE = ("PRESENT", "WFH", "ON_DUTY")
RECORD_FIELDS = (
    "status", "punch_in", "punch_out", "work_hours", "late_minutes",
    "overtime_minutes", "half_day",
)


def create_indexes():
    db.employees.create_indexes([
        IndexModel([("emp_code", 1)], unique=True, name="employee_code"),
        IndexModel([("department", 1), ("joined_on", 1), ("emp_code", 1)],
                   name="employee_department"),
        IndexModel([("joined_on", 1), ("department", 1)], name="employee_joined"),
    ])
    db.attendance_logs.create_indexes([
        IndexModel([("emp_code", 1), ("date", -1)], unique=True,
                   name="attendance_employee_day"),
        IndexModel([("date", -1), ("emp_code", 1)], name="attendance_day"),
        IndexModel([("status", 1), ("date", -1), ("emp_code", 1)],
                   name="attendance_status_day"),
        IndexModel([("emp_code", 1), ("punch_in", -1)], name="attendance_employee_punch"),
    ])


@asynccontextmanager
async def lifespan(app):
    create_indexes()
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


@app.exception_handler(PyMongoError)
async def database_error(request: Request, exc: PyMongoError):
    return JSONResponse(status_code=503, content={"detail": "Database unavailable"})


def invalid(field, message, location="body"):
    raise RequestValidationError([{"type": "value_error", "loc": [location, field], "msg": message}])


def valid_date(value: str) -> str:
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        raise ValueError("Use YYYY-MM-DD")
    Date.fromisoformat(value)
    return value


def valid_month(value: str) -> str:
    valid_date(value + "-01")
    return value


DateString = Annotated[str, AfterValidator(valid_date)]
MonthString = Annotated[str, AfterValidator(valid_month)]
ShiftTime = Annotated[str, Field(pattern=r"^([01][0-9]|2[0-3]):[0-5][0-9]$")]
EpochMillis = Annotated[int, Field(strict=True, ge=100000000000, le=4102444800000)]
Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]
Page = Annotated[int, Query(ge=1)]
PageSize = Annotated[int, Query(ge=1, le=100)]
Limit = Annotated[int, Query(ge=1, le=50)]


class EmployeeIn(BaseModel):
    emp_code: Annotated[str, Field(pattern=r"^EMP[0-9]{4,6}$")]
    name: Annotated[str, Field(min_length=1, max_length=100)]
    email: Annotated[str, Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")]
    department: Annotated[str, Field(min_length=1, max_length=50)]
    shift_start: ShiftTime = "09:30"
    shift_end: ShiftTime = "18:30"
    joined_on: DateString

    @model_validator(mode="after")
    def different_shift_times(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: EpochMillis | None = None

    @field_validator("punched_at", mode="before")
    @classmethod
    def supplied_timestamp(cls, value):
        if value is None:
            raise ValueError("Omit punched_at to use the current time")
        return value


class PunchInIn(PunchOutIn):
    status: PresenceStatus = "PRESENT"


class RegularizeIn(BaseModel):
    status: Status | None = None
    punch_in: EpochMillis | None = None
    punch_out: EpochMillis | None = None
    reason: Annotated[str, Field(min_length=5, max_length=200)]
    regularized_by: Annotated[str, Field(min_length=1, max_length=50)]

    @field_validator("status", "punch_in", "punch_out", mode="before")
    @classmethod
    def supplied_value(cls, value):
        if value is None:
            raise ValueError("Omit a field to leave it unchanged; null is not accepted")
        return value


def instant(value=None):
    return (EPOCH + timedelta(seconds=value // 1000) if value is not None
            else datetime.now(UTC).replace(microsecond=0))


def half_up(value, places=2):
    return float(Decimal(value).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP))


def attendance_day(punch_in, employee):
    local = punch_in.astimezone(IST)
    day = local.date()
    if employee["shift_end"] <= employee["shift_start"] and local.strftime("%H:%M") < employee["shift_end"]:
        day -= timedelta(days=1)
    return day.isoformat()


def shift_instant(day, clock):
    return datetime.fromisoformat(f"{day}T{clock}:00").replace(tzinfo=IST)


def compute_late_minutes(punch_in, shift_start, date_str):
    seconds = (punch_in - shift_instant(date_str, shift_start)).total_seconds()
    return int(seconds // 60) if seconds > 600 else 0


def compute_work_hours(punch_in, punch_out):
    seconds = int((punch_out - punch_in).total_seconds())
    return half_up(Decimal(seconds) / 3600)


def compute_overtime(punch_out, shift_start, shift_end, date_str):
    end = shift_instant(date_str, shift_end)
    if shift_end <= shift_start:
        end += timedelta(days=1)
    minutes = int((punch_out - end).total_seconds() // 60)
    return minutes if minutes >= 30 else 0


def derived_fields(record, employee):
    values = {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    if record["status"] in PRESENCE:
        values["late_minutes"] = compute_late_minutes(
            record["punch_in"], employee["shift_start"], record["date"])
        if record.get("punch_out") is not None:
            values["work_hours"] = compute_work_hours(record["punch_in"], record["punch_out"])
            values["half_day"] = values["work_hours"] < 4.50
            values["overtime_minutes"] = compute_overtime(
                record["punch_out"], employee["shift_start"], employee["shift_end"], record["date"])
    return values


def api_value(value):
    if isinstance(value, datetime):
        utc = value.replace(tzinfo=UTC) if value.tzinfo is None else value
        return (utc - EPOCH) // timedelta(milliseconds=1)
    if isinstance(value, Decimal128):
        return float(value.to_decimal())
    if isinstance(value, dict):
        return {k: api_value(v) for k, v in value.items() if k != "_id"}
    if isinstance(value, list):
        return [api_value(v) for v in value]
    return value


def attendance_response(record):
    return api_value({"punch_in": None, "punch_out": None, "work_hours": None,
                      "late_minutes": 0, "overtime_minutes": 0, "half_day": False,
                      "history": [], **record})


def employee_or_404(emp_code):
    employee = db.employees.find_one({"emp_code": emp_code})
    if employee is None:
        raise HTTPException(404, "Employee not found")
    return employee


def record_version(record):
    # Compare values and audit length so a stale calculation cannot overwrite another edit.
    return {"_id": record["_id"], **{key: record.get(key) for key in RECORD_FIELDS},
            "$expr": {"$eq": [{"$size": {"$ifNull": ["$history", []]}}, len(record.get("history", []))]}}


def validate_duration(punch_in, punch_out):
    if punch_out <= punch_in or punch_out - punch_in > timedelta(hours=24):
        invalid("punch_out", "Punch-out must be after punch-in and within 24 hours")


@app.get("/health")
def health():
    db.command("ping")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = {**body.model_dump(), "created_at": instant()}
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists") from None
    return api_value(doc)


@app.get("/employees")
def list_employees(department: str | None = None, page: Page = 1, page_size: PageSize = 20):
    query = {"department": department} if department is not None else {}
    items = list(db.employees.find(query, {"_id": 0}).sort("emp_code", 1)
                 .skip((page - 1) * page_size).limit(page_size))
    return {"items": api_value(items), "total": db.employees.count_documents(query),
            "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    employee = employee_or_404(body.emp_code)
    ts = instant(body.punched_at)
    doc = {"emp_code": body.emp_code, "date": attendance_day(ts, employee),
           "status": body.status, "punch_in": ts, "punch_out": None, "history": []}
    doc.update(derived_fields(doc, employee))
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "Already punched in for this date") from None
    return attendance_response(doc)


def attendance_query(emp_code, date_from, date_to, status, page, page_size):
    if date_from and date_to and date_from > date_to:
        invalid("date_to", "date_to must be on or after date_from", "query")
    query = {}
    if emp_code is not None:
        query["emp_code"] = emp_code
    if status is not None:
        query["status"] = status
    if date_from or date_to:
        query["date"] = {}
        if date_from:
            query["date"]["$gte"] = date_from
        if date_to:
            query["date"]["$lte"] = date_to
    hint = ("attendance_employee_day" if emp_code is not None else
            "attendance_status_day" if status is not None else "attendance_day")
    return {"find": "attendance_logs", "filter": query, "projection": {"_id": 0},
            "sort": {"date": -1, "emp_code": 1}, "skip": (page - 1) * page_size,
            "limit": page_size, "hint": hint}


@app.get("/attendance")
def list_attendance(emp_code: str | None = None, date_from: DateString | None = None,
                    date_to: DateString | None = None, status: Status | None = None,
                    page: Page = 1, page_size: PageSize = 20):
    query = attendance_query(emp_code, date_from, date_to, status, page, page_size)
    docs = db.attendance_logs.find(query["filter"], query["projection"]).sort(list(query["sort"].items()))
    docs = docs.hint(query["hint"]).skip(query["skip"]).limit(query["limit"])
    return {"items": [attendance_response(doc) for doc in docs],
            "total": db.attendance_logs.count_documents(query["filter"], hint=query["hint"]),
            "page": page, "page_size": page_size}


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    employee = employee_or_404(body.emp_code)
    ts = instant(body.punched_at)
    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$ne": None, "$lte": ts}},
        sort=[("punch_in", -1)], hint="attendance_employee_punch")
    if record is None:
        # Distinguish a reversed punch from an employee with no punch-in at all.
        if db.attendance_logs.find_one({"emp_code": body.emp_code, "punch_in": {"$gt": ts}}):
            invalid("punched_at", "Punch-out must be after punch-in")
        raise HTTPException(404, "No punch-in found")
    if record.get("punch_out") is not None:
        raise HTTPException(409, "Already punched out")
    punched_in = record["punch_in"].replace(microsecond=0)
    validate_duration(punched_in, ts)
    values = derived_fields({**record, "punch_in": punched_in, "punch_out": ts}, employee)
    values.pop("late_minutes")
    updated = db.attendance_logs.find_one_and_update(
        record_version(record), {"$set": {"punch_in": punched_in, "punch_out": ts, **values}},
        return_document=ReturnDocument.AFTER)
    if updated is None:
        raise HTTPException(409, "Attendance changed; retry the request")
    return attendance_response(updated)


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: DateString, body: RegularizeIn):
    employee = employee_or_404(emp_code)
    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if record is None:
        raise HTTPException(404, "Attendance record not found")
    updated = {**record}
    supplied = body.model_fields_set
    if "status" in supplied:
        updated["status"] = body.status
    if updated["status"] not in PRESENCE:
        if supplied & {"punch_in", "punch_out"}:
            invalid("status", "ABSENT and LEAVE cannot have punch times")
        updated.update(punch_in=None, punch_out=None)
    else:
        for key in ("punch_in", "punch_out"):
            if key in supplied:
                updated[key] = instant(getattr(body, key))
            elif updated.get(key) is not None:
                updated[key] = updated[key].replace(microsecond=0)
        if updated.get("punch_in") is None:
            invalid("punch_in", "Presence requires a punch-in")
        if attendance_day(updated["punch_in"], employee) != date:
            invalid("punch_in", "Punch-in must stay on the attendance date")
        if updated.get("punch_out") is not None:
            validate_duration(updated["punch_in"], updated["punch_out"])
    updated.update(derived_fields(updated, employee))
    defaults = {"late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    changes = {key: {"from": record.get(key, defaults.get(key)), "to": updated.get(key)}
               for key in RECORD_FIELDS if record.get(key, defaults.get(key)) != updated.get(key)}
    if not changes:
        invalid("body", "Correction changes nothing")
    entry = {"at": instant(), "by": body.regularized_by, "reason": body.reason, "changes": changes}
    result = db.attendance_logs.find_one_and_update(
        record_version(record), {"$set": {key: updated.get(key) for key in RECORD_FIELDS},
                                 "$push": {"history": entry}}, return_document=ReturnDocument.AFTER)
    if result is None:
        raise HTTPException(409, "Attendance changed; retry the correction")
    return attendance_response(result)


# Analytics operate on stored metrics; punch timestamps are deliberately not recomputed.
def month_bounds(month):
    first = Date.fromisoformat(month + "-01")
    last = first.replace(day=calendar.monthrange(first.year, first.month)[1])
    return first.isoformat(), last.isoformat()


def rounded_number(expression, places=2):
    # All reported metrics are nonnegative. MongoDB $round uses ties-to-even.
    scale = 10 ** places
    return {"$toDouble": {"$divide": [
        {"$floor": {"$add": [{"$multiply": [{"$toDecimal": expression}, scale]}, Decimal128("0.5")]}},
        scale,
    ]}}


def working_day(expression):
    return {"$in": [{"$dayOfWeek": expression}, [2, 3, 4, 5, 6]]}


def presence_weight():
    return {"$cond": [{"$in": ["$status", list(PRESENCE)]},
                      {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1]}, 0]}


def attendance_totals():
    completed = {"$and": [{"$in": ["$status", list(PRESENCE)]},
                            {"$ne": [{"$ifNull": ["$work_hours", None]}, None]}]}
    return {"$group": {
        "_id": None,
        "present_days": {"$sum": {"$cond": [working_day({"$toDate": "$date"}), presence_weight(), 0]}},
        "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
        "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
        "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
        "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
        "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        "work_sum": {"$sum": {"$cond": [completed, {"$toDecimal": "$work_hours"}, 0]}},
        "work_count": {"$sum": {"$cond": [completed, 1, 0]}},
    }}


def monthly_logs(first, last):
    return {"$lookup": {
        "from": "attendance_logs", "localField": "emp_code", "foreignField": "emp_code",
        "pipeline": [{"$match": {"date": {"$gte": first, "$lte": last}}}, attendance_totals()],
        "as": "totals",
    }}


def aggregation_query(collection, pipeline, hint):
    return {"aggregate": collection, "pipeline": pipeline, "cursor": {},
            "hint": hint, "allowDiskUse": True}


def run_aggregation(query):
    return list(db[query["aggregate"]].aggregate(
        query["pipeline"], hint=query["hint"], allowDiskUse=query["allowDiskUse"]))


def employee_monthly_query(emp_code, month):
    employee_or_404(emp_code)
    first, last = month_bounds(month)

    start = {"$max": [{"$toDate": first}, {"$toDate": "$joined_on"}]}
    days = {"$max": [0, {"$add": [1, {"$dateDiff": {
        "startDate": start, "endDate": {"$toDate": last}, "unit": "day"}}]}]}
    workdays = {"$size": {"$filter": {
        "input": {"$range": [0, {"$toInt": days}]}, "as": "offset",
        "cond": working_day({"$add": [start, {"$multiply": ["$$offset", 86400000]}]}),
    }}}
    fields = ("present_days", "leave_days", "late_count", "total_late_minutes", "total_overtime_minutes")
    pipeline = [
        {"$match": {"emp_code": emp_code}}, monthly_logs(first, last),
        {"$set": {"totals": {"$arrayElemAt": ["$totals", 0]}, "working_days": workdays}},
        {"$project": {
            "_id": 0, "emp_code": 1, "month": {"$literal": month}, "working_days": 1,
            **{key: {"$ifNull": ["$totals." + key, 0]} for key in fields},
            "attendance_pct": {"$cond": [{"$gt": ["$working_days", 0]}, rounded_number({"$divide": [
                {"$multiply": [{"$toDecimal": {"$ifNull": ["$totals.present_days", 0]}}, 100]},
                "$working_days"]}), None]},
        }},
    ]
    return aggregation_query("employees", pipeline, "employee_code")


def department_summary_query(month, department):
    first, last = month_bounds(month)
    match = {"joined_on": {"$lte": last}}
    if department is not None:
        match["department"] = department
    fields = ("present_days", "late_count", "total_late_minutes", "leave_days", "on_duty_count", "work_sum", "work_count")
    pipeline = [
        {"$match": match}, monthly_logs(first, last),
        {"$set": {"totals": {"$arrayElemAt": ["$totals", 0]}}},
        {"$group": {"_id": "$department", "headcount": {"$sum": 1},
                    **{key: {"$sum": {"$ifNull": ["$totals." + key, 0]}} for key in fields}}},
        {"$project": {
            "_id": 0, "department": "$_id", "headcount": 1, "present_days": 1,
            "late_count": 1, "total_late_minutes": 1, "leave_count": "$leave_days", "on_duty_count": 1,
            "avg_work_hours": {"$cond": [{"$gt": ["$work_count", 0]},
                                         rounded_number({"$divide": ["$work_sum", "$work_count"]}), None]},
        }},
        {"$sort": {"department": 1}},
    ]
    return aggregation_query("employees", pipeline,
                             "employee_department" if department is not None else "employee_joined")


def late_leaderboard_query(month, department, limit):
    first, last = month_bounds(month)
    pipeline = [
        {"$match": {"date": {"$gte": first, "$lte": last}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code",
                     "pipeline": [{"$match": {"department": department}}] if department is not None else [],
                     "as": "employee"}},
        {"$unwind": "$employee"},
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},
        {"$project": {"_id": 0, "emp_code": "$_id", "name": "$employee.name", "department": "$employee.department",
                      "total_late_minutes": 1, "late_count": 1, "rank": 1}},
        {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
    ]
    return aggregation_query("attendance_logs", pipeline, "attendance_day")


def department_trend_query(department, first, last):
    count = (Date.fromisoformat(last) - Date.fromisoformat(first)).days + 1
    if count < 1 or count > 92:
        invalid("to", "Range must contain 1 to 92 calendar days", "query")
    if db.employees.find_one({"department": department}, {"_id": 1}) is None:
        raise HTTPException(404, "Department not found")
    pipeline = [
        {"$match": {"department": department}}, {"$limit": 1},
        # A database-generated calendar also covers ranges with no attendance at all.
        {"$project": {"_id": 0, "offset": {"$range": [0, count]}}}, {"$unwind": "$offset"},
        {"$set": {"day": {"$add": [{"$toDate": first}, {"$multiply": ["$offset", 86400000]}]}}},
        {"$set": {"date": {"$dateToString": {"date": "$day", "format": "%Y-%m-%d"}},
                  "is_working_day": working_day("$day")}},
        {"$lookup": {
            "from": "employees", "let": {"date": "$date"}, "as": "staff",
            "pipeline": [{"$match": {"department": department, "$expr": {"$lte": ["$joined_on", "$$date"]}}},
                         {"$count": "headcount"}],
        }},
        {"$lookup": {
            "from": "attendance_logs", "let": {"date": "$date"}, "as": "attendance",
            "pipeline": [
                {"$match": {"$expr": {"$eq": ["$date", "$$date"]}}},
                {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code",
                             "pipeline": [{"$match": {"department": department}}], "as": "employee"}},
                {"$unwind": "$employee"},
                {"$group": {"_id": None, "present_count": {"$sum": presence_weight()},
                            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}}}},
            ],
        }},
        {"$set": {"headcount": {"$ifNull": [{"$arrayElemAt": ["$staff.headcount", 0]}, 0]},
                  "present_count": {"$ifNull": [{"$arrayElemAt": ["$attendance.present_count", 0]}, 0]},
                  "late_count": {"$ifNull": [{"$arrayElemAt": ["$attendance.late_count", 0]}, 0]}}},
        {"$set": {"attendance_rate": {"$cond": [
            {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
            rounded_number({"$divide": [{"$toDecimal": "$present_count"}, "$headcount"]}, 4), None]}}},
        {"$setWindowFields": {"sortBy": {"date": 1}, "output": {
            "moving_avg_7d": {"$avg": {"$toDecimal": "$attendance_rate"}, "window": {"documents": [-6, 0]}}}}},
        {"$project": {"_id": 0, "date": 1, "is_working_day": 1, "headcount": 1,
                      "present_count": 1, "late_count": 1, "attendance_rate": 1,
                      "moving_avg_7d": rounded_number("$moving_avg_7d", 4)}},
        {"$sort": {"date": 1}},
    ]
    return aggregation_query("employees", pipeline, "employee_department")


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: MonthString):
    return api_value(run_aggregation(employee_monthly_query(emp_code, month))[0])


@app.get("/analytics/departments/summary")
def department_summary(month: MonthString, department: str | None = None):
    return {"month": month, "items": api_value(run_aggregation(department_summary_query(month, department)))}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: MonthString, department: str | None = None, limit: Limit = 10):
    return {"month": month, "items": api_value(run_aggregation(late_leaderboard_query(month, department, limit)))}


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, date_from: Annotated[DateString, Query(alias="from")],
                     date_to: Annotated[DateString, Query(alias="to")]):
    return {"department": department, "items": api_value(run_aggregation(
        department_trend_query(department, date_from, date_to)))}


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"],
    emp_code: str | None = None, month: MonthString | None = None, department: str | None = None,
    limit: Limit = 10, date_from: DateString | None = None, date_to: DateString | None = None,
    status: Status | None = None, page: Page = 1, page_size: PageSize = 20,
    trend_from: Annotated[DateString | None, Query(alias="from")] = None,
    trend_to: Annotated[DateString | None, Query(alias="to")] = None,
):
    def required(value, name):
        if value is None:
            invalid(name, "Field required", "query")
        return value

    if endpoint == "attendance_list":
        query = attendance_query(emp_code, date_from, date_to, status, page, page_size)
    elif endpoint == "employee_monthly":
        query = employee_monthly_query(required(emp_code, "emp_code"), required(month, "month"))
    elif endpoint == "department_summary":
        query = department_summary_query(required(month, "month"), department)
    elif endpoint == "late_leaderboard":
        query = late_leaderboard_query(required(month, "month"), department, limit)
    else:
        query = department_trend_query(required(department, "department"),
                                       required(trend_from, "from"), required(trend_to, "to"))
    plan = db.command("explain", query, verbosity="executionStats")
    return {"endpoint": endpoint, "collection": query.get("find", query.get("aggregate")),
            "explain": json.loads(json_util.dumps(plan))}
