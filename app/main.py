"""
Employee Attendance & Analytics API
Run from repository root: uvicorn app.main:app --port 8000
"""
import os
import re
import calendar
from datetime import datetime, date, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Literal, Any

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Path as ApiPath
from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator
from pymongo import MongoClient, ASCENDING, DESCENDING
from pymongo.errors import DuplicateKeyError, PyMongoError
from bson import json_util
from bson.json_util import dumps

load_dotenv()

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
PRESENCE = {"PRESENT", "WFH", "ON_DUTY"}
ALL_STATUS = PRESENCE | {"ABSENT", "LEAVE"}
EPOCH_MIN, EPOCH_MAX = 100_000_000_000, 4_102_444_800_000

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")
client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"), tz_aware=True, serverSelectionTimeoutMS=3000)
db = client[os.getenv("MONGO_DB", "attendance_db")]
employees = db["employees"]
logs = db["attendance_logs"]

def now_utc():
    return datetime.now(UTC).replace(microsecond=0)

def dt_from_ms(value: int) -> datetime:
    if type(value) is not int or not EPOCH_MIN <= value <= EPOCH_MAX:
        raise HTTPException(422, "timestamp must be an integer epoch-millisecond value in range")
    return datetime.fromtimestamp(value // 1000, tz=UTC)

def ms_from_dt(value):
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return int(value.timestamp()) * 1000

def trunc_dt(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).replace(microsecond=0)

def rounded(value, places=2):
    if value is None:
        return None
    q = Decimal("1").scaleb(-places)
    return float(Decimal(str(value)).quantize(q, rounding=ROUND_HALF_UP))

def date_parse(s: str) -> date:
    try:
        d = date.fromisoformat(s)
        if d.isoformat() != s:
            raise ValueError()
        return d
    except Exception:
        raise HTTPException(422, "date must be YYYY-MM-DD")

def month_bounds(month: str):
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month or ""):
        raise HTTPException(422, "month must be YYYY-MM")
    y, m = map(int, month.split("-"))
    first = date(y, m, 1)
    last = date(y, m, calendar.monthrange(y, m)[1])
    return first.isoformat(), last.isoformat(), first, last

def working_days(start: date, end: date) -> int:
    if end < start: return 0
    count = 0
    cur = start
    while cur <= end:
        if cur.weekday() < 5: count += 1
        cur += timedelta(days=1)
    return count

def shift_datetimes(emp, punch_in):
    local = punch_in.astimezone(IST)
    shift_start_t = time.fromisoformat(emp["shift_start"])
    shift_end_t = time.fromisoformat(emp["shift_end"])
    overnight = shift_end_t <= shift_start_t
    att_date = local.date()
    if overnight and local.time().replace(tzinfo=None) < shift_end_t:
        att_date -= timedelta(days=1)
    start_date = att_date
    shift_start = datetime.combine(start_date, shift_start_t, IST)
    shift_end_date = start_date + (timedelta(days=1) if overnight else timedelta())
    shift_end = datetime.combine(shift_end_date, shift_end_t, IST)
    return att_date.isoformat(), shift_start.astimezone(UTC), shift_end.astimezone(UTC)

def derive(emp, status, pin, pout):
    if status not in PRESENCE:
        return {"late_minutes": 0, "work_hours": None, "overtime_minutes": 0, "half_day": False}
    if pin is None:
        raise HTTPException(422, "presence status requires punch_in")
    pin = trunc_dt(pin)
    att_date, shift_start, shift_end = shift_datetimes(emp, pin)
    late_delta = (pin - shift_start).total_seconds()
    late = int(late_delta // 60) if late_delta > 600 else 0
    if pout is None:
        return {"late_minutes": late, "work_hours": None, "overtime_minutes": 0, "half_day": False}
    pout = trunc_dt(pout)
    secs = (pout - pin).total_seconds()
    if secs <= 0 or secs > 86400:
        raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")
    hours = rounded(Decimal(int(secs)) / Decimal(3600), 2)
    overtime_delta = (pout - shift_end).total_seconds()
    overtime = int(overtime_delta // 60) if overtime_delta >= 1800 else 0
    return {"late_minutes": late, "work_hours": hours, "overtime_minutes": overtime, "half_day": hours < 4.50}

def ensure_indexes():
    employees.create_index([("emp_code", ASCENDING)], unique=True, name="uq_employee_code")
    employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)], name="ix_employee_department_joined")
    logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="uq_attendance_employee_date")
    logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="ix_attendance_date_employee")
    logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING), ("status", ASCENDING)], name="ix_attendance_employee_date_status")
    logs.create_index([("date", ASCENDING), ("status", ASCENDING), ("late_minutes", DESCENDING)], name="ix_attendance_date_status_late")

@app.on_event("startup")
def startup():
    ensure_indexes()

def clean_employee(doc):
    if not doc: return None
    return {k: (ms_from_dt(v) if k == "created_at" else v) for k, v in doc.items() if k != "_id"}

def clean_record(doc):
    if not doc: return None
    out = {k: v for k, v in doc.items() if k != "_id"}
    for k in ("punch_in", "punch_out"):
        out[k] = ms_from_dt(out.get(k))
    out["late_minutes"] = int(out.get("late_minutes", 0) or 0)
    out["overtime_minutes"] = int(out.get("overtime_minutes", 0) or 0)
    out["half_day"] = bool(out.get("half_day", False))
    out["history"] = out.get("history") or []
    for entry in out["history"]:
        entry["at"] = ms_from_dt(entry.get("at"))
        for change in (entry.get("changes") or {}).values():
            for side in ("from", "to"):
                if isinstance(change.get(side), datetime):
                    change[side] = ms_from_dt(change[side])
    return out

class EmployeeCreate(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: date
    @model_validator(mode="after")
    def shifts_differ(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self

class PunchIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str
    punched_at: Optional[StrictInt] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"
    @field_validator("punched_at")
    @classmethod
    def valid_ms(cls, v):
        if v is not None and not EPOCH_MIN <= v <= EPOCH_MAX: raise ValueError("invalid epoch milliseconds")
        return v

class PunchOut(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str
    punched_at: Optional[StrictInt] = None
    @field_validator("punched_at")
    @classmethod
    def valid_ms(cls, v):
        if v is not None and not EPOCH_MIN <= v <= EPOCH_MAX: raise ValueError("invalid epoch milliseconds")
        return v

class Regularize(BaseModel):
    model_config = ConfigDict(extra="ignore")
    status: Optional[Literal["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"]] = None
    punch_in: Optional[StrictInt] = None
    punch_out: Optional[StrictInt] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)
    @field_validator("punch_in", "punch_out")
    @classmethod
    def valid_ms(cls, v):
        if v is not None and not EPOCH_MIN <= v <= EPOCH_MAX: raise ValueError("invalid epoch milliseconds")
        return v

def page_response(cursor, total, page, page_size, cleaner):
    return {"items": [cleaner(x) for x in cursor], "total": total, "page": page, "page_size": page_size}

@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(503, "MongoDB unavailable")

@app.post("/employees", status_code=201)
def create_employee(body: EmployeeCreate):
    doc = body.model_dump()
    doc["joined_on"] = doc["joined_on"].isoformat()
    if doc["shift_start"] == doc["shift_end"]:
        raise HTTPException(422, "shift_start must differ from shift_end")
    doc["created_at"] = now_utc()
    try:
        employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return clean_employee(doc)

@app.get("/employees")
def list_employees(department: Optional[str] = None, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    filt = {"department": department} if department is not None else {}
    total = employees.count_documents(filt)
    cur = employees.find(filt, {"_id": 0}).sort("emp_code", ASCENDING).skip((page-1)*page_size).limit(page_size)
    return page_response(cur, total, page, page_size, lambda x: clean_employee(x))

@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchIn):
    emp = employees.find_one({"emp_code": body.emp_code})
    if not emp: raise HTTPException(404, "employee not found")
    pin = trunc_dt(dt_from_ms(body.punched_at) if body.punched_at is not None else now_utc())
    att_date, _, _ = shift_datetimes(emp, pin)
    deriv = derive(emp, body.status, pin, None)
    doc = {"emp_code": body.emp_code, "date": att_date, "status": body.status, "punch_in": pin,
           "punch_out": None, "work_hours": None, "late_minutes": deriv["late_minutes"],
           "overtime_minutes": 0, "half_day": False, "history": []}
    try:
        logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "attendance already exists for this employee and date")
    return clean_record(doc)

@app.post("/attendance/punch-out")
def punch_out(body: PunchOut):
    emp = employees.find_one({"emp_code": body.emp_code})
    if not emp: raise HTTPException(404, "employee not found")
    pout = trunc_dt(dt_from_ms(body.punched_at) if body.punched_at is not None else now_utc())
    target = logs.find_one({"emp_code": body.emp_code, "punch_in": {"$ne": None, "$lte": pout}}, sort=[("punch_in", DESCENDING)])
    if not target: raise HTTPException(404, "no punch-in found")
    if target.get("punch_out") is not None: raise HTTPException(409, "record already punched out")
    if (pout-target["punch_in"]).total_seconds() <= 0 or (pout-target["punch_in"]).total_seconds() > 86400:
        raise HTTPException(422, "punched_at must be after punch_in and within 24 hours")
    deriv = derive(emp, target["status"], target["punch_in"], pout)
    update = logs.update_one({"_id": target["_id"], "punch_out": None}, {"$set": {"punch_out": pout, **deriv}})
    if update.modified_count != 1:
        raise HTTPException(409, "record already punched out")
    target.update({"punch_out": pout, **deriv})
    return clean_record(target)

@app.get("/attendance")
def list_attendance(emp_code: Optional[str] = None, date_from: Optional[date] = None, date_to: Optional[date] = None,
                    status: Optional[str] = None, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    if status is not None and status not in ALL_STATUS: raise HTTPException(422, "invalid status")
    if date_from and date_to and date_from > date_to: raise HTTPException(422, "date_from must be <= date_to")
    filt = {}
    if emp_code: filt["emp_code"] = emp_code
    if date_from or date_to:
        filt["date"] = {}
        if date_from: filt["date"]["$gte"] = date_from.isoformat()
        if date_to: filt["date"]["$lte"] = date_to.isoformat()
    if status: filt["status"] = status
    total = logs.count_documents(filt)
    cur = logs.find(filt).sort([("date", DESCENDING), ("emp_code", ASCENDING)]).skip((page-1)*page_size).limit(page_size)
    return page_response(cur, total, page, page_size, clean_record)

@app.patch("/attendance/{emp_code}/{att_date}")
def regularize(emp_code: str, att_date: date, body: Regularize):
    emp = employees.find_one({"emp_code": emp_code})
    if not emp: raise HTTPException(404, "employee not found")
    original = logs.find_one({"emp_code": emp_code, "date": att_date.isoformat()})
    if not original: raise HTTPException(404, "attendance record not found")
    status = body.status if body.status is not None else original["status"]
    if status in {"ABSENT", "LEAVE"}:
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(422, "ABSENT/LEAVE cannot have punch times")
        pin = pout = None
    else:
        pin = dt_from_ms(body.punch_in) if body.punch_in is not None else original.get("punch_in")
        pout = dt_from_ms(body.punch_out) if body.punch_out is not None else original.get("punch_out")
        if pin is None: raise HTTPException(422, "presence status requires punch_in")
        pin = trunc_dt(pin)
        if shift_datetimes(emp, pin)[0] != att_date.isoformat():
            raise HTTPException(422, "punch_in must belong to the record attendance date")
        if pout is not None:
            pout = trunc_dt(pout)
            if (pout-pin).total_seconds() <= 0 or (pout-pin).total_seconds() > 86400:
                raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")
    derived = derive(emp, status, pin, pout)
    final = {"status": status, "punch_in": pin, "punch_out": pout, **derived}
    changed = {}
    for key, new in final.items():
        old = original.get(key)
        if key == "late_minutes" and old is None: old = 0
        if key == "overtime_minutes" and old is None: old = 0
        if key == "half_day" and old is None: old = False
        if key == "work_hours" and old is None: old = None
        if isinstance(old, datetime): old = trunc_dt(old)
        if old != new:
            changed[key] = {"from": old, "to": new}
    if not changed: raise HTTPException(422, "request does not change the record")
    entry = {"at": now_utc(), "by": body.regularized_by, "reason": body.reason, "changes": changed}
    # Compare-and-swap on existing values prevents concurrent corrections losing history.
    query = {"_id": original["_id"], "history": original.get("history", [])}
    if "history" not in original: query["history"] = {"$exists": False}
    result = logs.update_one(query, {"$set": final, "$push": {"history": entry}})
    if result.modified_count != 1: raise HTTPException(409, "record changed concurrently; retry")
    updated = logs.find_one({"_id": original["_id"]})
    return clean_record(updated)

@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$")):
    emp = employees.find_one({"emp_code": emp_code})
    if not emp: raise HTTPException(404, "employee not found")
    start, end, first, last = month_bounds(month)
    pipeline = [
        {"$match": {"emp_code": emp_code, "date": {"$gte": start, "$lte": end}}},
        {"$group": {"_id": None,
            "present_days": {"$sum": {"$cond": [{"$and": [{"$in": ["$status", list(PRESENCE)]},
                {"$lte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 6]},
                {"$gte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 2]}]},
                {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 0.5, 1]}, 0]}},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}}}}
    ]
    agg = list(logs.aggregate(pipeline))
    vals = agg[0] if agg else {}
    join = max(first, date_parse(emp["joined_on"]))
    wd = working_days(join, last)
    present = rounded(vals.get("present_days", 0), 2)
    pct = rounded(Decimal(str(present))*100/Decimal(wd), 2) if wd else None
    return {"emp_code": emp_code, "month": month, "working_days": wd, "present_days": present,
            "leave_days": vals.get("leave_days", 0), "late_count": vals.get("late_count", 0),
            "total_late_minutes": vals.get("total_late_minutes", 0),
            "total_overtime_minutes": vals.get("total_overtime_minutes", 0), "attendance_pct": pct}

@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"), department: Optional[str] = None):
    start, end, _, month_last = month_bounds(month)
    match = {"joined_on": {"$lte": end}}
    if department is not None: match["department"] = department
    pipeline = [
        {"$match": match},
        {"$lookup": {
            "from": "attendance_logs", "let": {"ec": "$emp_code"},
            "pipeline": [{"$match": {"$expr": {"$and": [
                {"$eq": ["$emp_code", "$$ec"]},
                {"$gte": ["$date", start]}, {"$lte": ["$date", end]}
            ]}}}],
            "as": "month_logs"
        }},
        {"$project": {
            "department": 1, "month_logs": 1,
            "present_days": {
                "$sum": {
                    "$map": {
                        "input": "$month_logs",
                        "as": "l",
                        "in": {
                            "$cond": [
                                {"$and": [
                                    {"$in": ["$$l.status", list(PRESENCE)]},
                                    {"$gte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$$l.date"}}}, 2]},
                                    {"$lte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$$l.date"}}}, 6]}
                                ]},
                                {"$cond": [{"$eq": [{"$ifNull": ["$$l.half_day", False]}, True]}, 0.5, 1]},
                                0
                            ]
                        }
                    }
                }
            },
            "late_count": {"$size": {"$filter": {"input": "$month_logs", "as": "l",
                "cond": {"$gt": [{"$ifNull": ["$$l.late_minutes", 0]}, 0]}}}},
            "total_late_minutes": {"$sum": {"$map": {"input": "$month_logs", "as": "l",
                "in": {"$ifNull": ["$$l.late_minutes", 0]}}}},
            "leave_count": {"$size": {"$filter": {"input": "$month_logs", "as": "l",
                "cond": {"$eq": ["$$l.status", "LEAVE"]}}}},
            "on_duty_count": {"$size": {"$filter": {"input": "$month_logs", "as": "l",
                "cond": {"$eq": ["$$l.status", "ON_DUTY"]}}}},
            "hours": {"$map": {
                "input": {"$filter": {"input": "$month_logs", "as": "l", "cond": {"$and": [
                    {"$in": ["$$l.status", list(PRESENCE)]}, {"$ne": ["$$l.work_hours", None]}
                ]}},
                "as": "l", "in": "$$l.work_hours"
            }}
        }}},
        {"$group": {
            "_id": "$department", "headcount": {"$sum": 1},
            "present_days": {"$sum": "$present_days"}, "late_count": {"$sum": "$late_count"},
            "total_late_minutes": {"$sum": "$total_late_minutes"},
            "leave_count": {"$sum": "$leave_count"}, "on_duty_count": {"$sum": "$on_duty_count"},
            "all_hours": {"$push": "$hours"}
        }},
        {"$project": {
            "department": "$_id", "_id": 0, "headcount": 1, "present_days": 1,
            "late_count": 1, "total_late_minutes": 1, "leave_count": 1, "on_duty_count": 1,
            "flat_hours": {"$reduce": {"input": "$all_hours", "initialValue": [],
                "in": {"$concatArrays": ["$$value", "$$this"]}}}
        }},
        {"$project": {
            "department": 1, "headcount": 1, "present_days": 1, "late_count": 1,
            "total_late_minutes": 1, "leave_count": 1, "on_duty_count": 1,
            "avg_work_hours": {"$cond": [
                {"$gt": [{"$size": "$flat_hours"}, 0]},
                {"$divide": [{"$floor": {"$add": [{"$multiply": [{"$avg": "$flat_hours"}, 100]}, 0.5]}}, 100]}, None
            ]}
        }},
        {"$match": {"headcount": {"$gt": 0}}},
        {"$sort": {"department": 1}}
    ]
    items = list(employees.aggregate(pipeline))
    for x in items: x["present_days"] = rounded(x.get("present_days", 0), 2)
    return {"month": month, "items": items}

@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
                     limit: int = Query(10, ge=1, le=50), department: Optional[str] = None):
    start, end, _, _ = month_bounds(month)
    pipeline = [
        {"$match": {"date": {"$gte": start, "$lte": end}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                    "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}}}},
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "employee"}},
        {"$unwind": "$employee"},
    ]
    if department is not None: pipeline.append({"$match": {"employee.department": department}})
    pipeline += [
        {"$set": {"emp_code": "$_id", "name": "$employee.name", "department": "$employee.department"}},
        {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": 1, "name": 1, "department": 1, "total_late_minutes": 1, "late_count": 1}},
        {"$sort": {"total_late_minutes": -1, "emp_code": 1}}
    ]
    return {"month": month, "items": list(logs.aggregate(pipeline))}

@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_date: date = Query(..., alias="from"), to_date: date = Query(..., alias="to")):
    if to_date < from_date: raise HTTPException(422, "'to' must be on or after 'from'")
    if not employees.find_one({"department": department}): raise HTTPException(404, "department not found")
    # Generate one row per employee per eligible day inside MongoDB; densify fills days before the first join.
    start_s, end_s = from_date.isoformat(), to_date.isoformat()
    start_dt = datetime.combine(from_date, time.min, UTC)
    end_dt = datetime.combine(to_date, time.min, UTC)
    pipeline = [
        {"$match": {"department": department, "joined_on": {"$lte": end_s}}},
        {"$project": {"emp_code": 1, "joined_on": 1,
          "first_day": {"$cond": [{"$gt": ["$joined_on", start_s]},
            {"$dateFromString": {"dateString": "$joined_on"}}, {"$literal": start_dt}]}}},
        {"$set": {"first_day": {"$cond": [{"$lt": ["$first_day", start_dt]}, start_dt, "$first_day"]}}},
        {"$set": {"days": {"$range": [0, {"$add": [{"$dateDiff": {"startDate": "$first_day", "endDate": end_dt, "unit": "day"}}, 1]}]}}},
        {"$unwind": "$days"},
        {"$set": {"day": {"$dateAdd": {"startDate": "$first_day", "unit": "day", "amount": "$days"}}}},
        {"$set": {"day_string": {"$dateToString": {"date": "$day", "format": "%Y-%m-%d"}}}},
        {"$lookup": {"from": "attendance_logs", "let": {"ec": "$emp_code", "d": "$day_string"}, "pipeline": [
            {"$match": {"$expr": {"$and": [{"$eq": ["$emp_code", "$$ec"]}, {"$eq": ["$date", "$$d"]}]}}}
        ], "as": "day_logs"}},
        {"$unwind": {"path": "$day_logs", "preserveNullAndEmptyArrays": True}},
        {"$group": {
            "_id": "$day",
            "headcount": {"$sum": 1},
            "present_count": {"$sum": {"$cond": [
                {"$in": ["$day_logs.status", list(PRESENCE)]},
                {"$cond": [{"$eq": [{"$ifNull": ["$day_logs.half_day", False]}, True]}, 0.5, 1]},
                0
            ]}},
            "late_count": {"$sum": {"$cond": [
                {"$gt": [{"$ifNull": ["$day_logs.late_minutes", 0]}, 0]},
                1, 0
            ]}}
        }},
        {"$densify": {"field": "_id", "range": {"step": 1, "unit": "day", "bounds": [start_dt, end_dt + timedelta(days=1)]}}},
        {"$set": {"date": "$_id", "headcount": {"$ifNull": ["$headcount", 0]},
          "present_count": {"$ifNull": ["$present_count", 0]}, "late_count": {"$ifNull": ["$late_count", 0]}}},
        {"$set": {"date_string": {"$dateToString": {"date": "$date", "format": "%Y-%m-%d"}},
          "is_working_day": {"$and": [{"$gte": [{"$dayOfWeek": "$date"}, 2]}, {"$lte": [{"$dayOfWeek": "$date"}, 6]}]}}},
        {"$set": {"attendance_rate": {"$cond": [{"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
          {"$divide": [{"$floor": {"$add": [{"$multiply": [{"$divide": ["$present_count", "$headcount"]}, 10000]}, 0.5]}}, 10000]}, None]}}},
        {"$setWindowFields": {"sortBy": {"date": 1}, "output": {"moving_avg_7d": {"$avg": "$attendance_rate",
          "window": {"documents": [-6, 0]}}}}},
        {"$project": {"_id": 0, "date": "$date_string", "is_working_day": 1, "headcount": 1,
          "present_count": {"$round": ["$present_count", 2]}, "late_count": 1,
          "attendance_rate": 1, "moving_avg_7d": {"$cond": [{"$ne": ["$moving_avg_7d", None]}, {"$divide": [{"$floor": {"$add": [{"$multiply": ["$moving_avg_7d", 10000]}, 0.5]}}, 10000]}, None]}}},
        {"$sort": {"date": 1}}
    ]
    return {"department": department, "items": list(employees.aggregate(pipeline))}

def explain_pipeline(endpoint, params):
    if endpoint == "attendance_list":
        filt = {}
        if params.get("emp_code"): filt["emp_code"] = params["emp_code"]
        if params.get("status"): filt["status"] = params["status"]
        if params.get("date_from") or params.get("date_to"):
            filt["date"] = {}
            if params.get("date_from"): filt["date"]["$gte"] = params["date_from"]
            if params.get("date_to"): filt["date"]["$lte"] = params["date_to"]
        return "find", logs, filt, [("date", -1), ("emp_code", 1)], params.get("page", 1), params.get("page_size", 20)
    if endpoint == "employee_monthly":
        if not params.get("emp_code") or not params.get("month"): raise HTTPException(422, "emp_code and month required")
        s,e,_,_=month_bounds(params["month"])
        return "aggregate", logs, [{"$match":{"emp_code":params["emp_code"],"date":{"$gte":s,"$lte":e}}},{"$group":{"_id":"$emp_code","count":{"$sum":1}}}], None, None, None
    if endpoint == "department_summary":
        if not params.get("month"): raise HTTPException(422, "month required")
        s,e,_,_=month_bounds(params["month"])
        pipe=[{"$match":{"joined_on":{"$lte":e}, **({"department":params["department"]} if params.get("department") else {})}},
              {"$lookup":{"from":"attendance_logs","let":{"ec":"$emp_code"},"pipeline":[{"$match":{"$expr":{"$and":[{"$eq":["$emp_code","$$ec"]},{"$gte":["$date",s]},{"$lte":["$date",e]}]}}}],"as":"month_logs"}}]
        return "aggregate", employees, pipe, None, None, None
    if endpoint == "late_leaderboard":
        if not params.get("month"): raise HTTPException(422, "month required")
        s,e,_,_=month_bounds(params["month"])
        pipe=[{"$match":{"date":{"$gte":s,"$lte":e}}},{"$group":{"_id":"$emp_code","total_late_minutes":{"$sum":{"$ifNull":["$late_minutes",0]}}}},{"$match":{"total_late_minutes":{"$gt":0}}}]
        return "aggregate", logs, pipe, None, None, None
    if endpoint == "department_trend":
        if not params.get("department") or not params.get("from") or not params.get("to"): raise HTTPException(422, "department, from and to required")
        # Same department/date filters as trend, suitable for checking the indexed employee and log lookups.
        return "aggregate", employees, [{"$match":{"department":params["department"],"joined_on":{"$lte":params["to"]}}}], None, None, None
    raise HTTPException(422, "unknown explain endpoint")

@app.get("/admin/explain/{endpoint}")
def admin_explain(endpoint: Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"],
                  emp_code: Optional[str] = None, month: Optional[str] = None, department: Optional[str] = None,
                  limit: int = Query(10, ge=1, le=50), date_from: Optional[date] = None, date_to: Optional[date] = None,
                  status: Optional[str] = None, from_date: Optional[date] = Query(None, alias="from"),
                  to_date: Optional[date] = Query(None, alias="to"), page: int = Query(1, ge=1),
                  page_size: int = Query(20, ge=1, le=100)):
    params = {"emp_code": emp_code, "month": month, "department": department, "limit": limit,
              "date_from": date_from.isoformat() if date_from else None, "date_to": date_to.isoformat() if date_to else None,
              "status": status, "from": from_date.isoformat() if from_date else None, "to": to_date.isoformat() if to_date else None,
              "page": page, "page_size": page_size}
    mode, coll, query, sort, pg, pgs = explain_pipeline(endpoint, params)
    try:
        if mode == "find":
            cmd = {"find": coll.name, "filter": query, "sort": dict(sort), "skip": (pg-1)*pgs, "limit": pgs}
        else:
            cmd = {"aggregate": coll.name, "pipeline": query, "cursor": {}}
        return db.command("explain", cmd, verbosity="executionStats")
    except PyMongoError as e:
        raise HTTPException(500, f"explain failed: {str(e)}")
