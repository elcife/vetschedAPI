import base64
import binascii
import hashlib
import hmac
import io
import json
import os
import re
from contextlib import asynccontextmanager
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Literal

import asyncpg
import bcrypt
from dotenv import load_dotenv
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from pydantic import BaseModel, Field, field_validator, model_validator
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
from xml.sax.saxutils import escape

load_dotenv(Path(__file__).with_name(".env"))

# Load settings from .env.


# Check password length before hashing.
def validate_bcrypt_password(password: str) -> str:
    if len(password.encode("utf-8")) > 72:
        raise ValueError("Password must be no more than 72 UTF-8 bytes.")
    return password


# Open and close the database connection pool.
@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = {
        "user": os.getenv("user"),
        "password": os.getenv("password"),
        "host": os.getenv("host"),
        "port": os.getenv("port"),
        "database": os.getenv("database"),
    }
    if not all(settings.values()):
        raise RuntimeError(
            "Set user, password, host, port, and database in .env."
        )

    settings["port"] = int(settings["port"])
    app.state.pool = await asyncpg.create_pool(ssl="require", statement_cache_size=0, **settings)
    try:
        yield
    finally:
        await app.state.pool.close()


# Configure the FastAPI app and its routes.
app = FastAPI(
    title="Course Schedule Plotting",
    version="1.0.0",
    lifespan=lifespan,
    openapi_tags=[
        {
            "name": "Students",
            "description": "Student accounts, available subjects, and schedule plotting.",
        },
        {
            "name": "Admin",
            "description": "Administrator sign-in and schedule management.",
        },
        {"name": "System", "description": "API status and service information."},
    ],
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
# API information and health links.
@app.get("/", tags=["System"])
async def home():
    return {
        "message": "Course Schedule Plotting API",
        "docs": "/docs",
        "health": "/health",
    }


# Request data validation models.
class StudentAccountCreate(BaseModel):
    student_id: str = Field(min_length=1, max_length=50)
    email: str = Field(min_length=3, max_length=255)
    password: str = Field(min_length=10, max_length=72)
    first_name: str = Field(min_length=1, max_length=80)
    last_name: str = Field(min_length=1, max_length=80)
    year_level: int | None = Field(default=None, ge=1, le=5)

    @field_validator("student_id", mode="before")
    @classmethod
    def remove_student_id_dashes(cls, student_id: str) -> str:
        return student_id.replace("-", "") if isinstance(student_id, str) else student_id

    @field_validator("student_id")
    @classmethod
    def validate_student_id(cls, student_id: str) -> str:
        if not student_id.isdigit() or not 9 <= len(student_id) <= 12:
            raise ValueError("Student ID must contain 9 to 12 digits; hyphens are ignored.")
        return student_id

    @field_validator("email", mode="before")
    @classmethod
    def normalize_student_email(cls, email: str) -> str:
        return email.strip().lower() if isinstance(email, str) else email

    @field_validator("email")
    @classmethod
    def validate_student_email(cls, email: str) -> str:
        local, separator, domain = email.rpartition("@")
        if (
            not local
            or any(character.isspace() for character in email)
            or not separator
            or domain != "phinmaed.com"
            or re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+", local) is None
        ):
            raise ValueError("Only @phinmaed.com email addresses are allowed.")
        return email

    @field_validator("password")
    @classmethod
    def validate_password_size(cls, password: str) -> str:
        return validate_bcrypt_password(password)


class PlotCreate(BaseModel):
    offering_id: int = Field(gt=0)


class SubjectCreate(BaseModel):
    subject_code: str = Field(min_length=1, max_length=50)
    subject_name: str = Field(min_length=1, max_length=255)
    year_level: int | None = Field(default=None, ge=1, le=5)
    semester: Literal[1, 2] = 1


class StudentLogin(BaseModel):
    email: str = Field(min_length=3, max_length=255)
    password: str = Field(min_length=1, max_length=72)

    @field_validator("password")
    @classmethod
    def validate_password_size(cls, password: str) -> str:
        return validate_bcrypt_password(password)


class StudentPasswordChange(BaseModel):
    email: str = Field(min_length=3, max_length=255)
    old_password: str = Field(min_length=1, max_length=72)
    new_password: str = Field(min_length=10, max_length=72)

    @field_validator("old_password", "new_password")
    @classmethod
    def validate_password_size(cls, password: str) -> str:
        return validate_bcrypt_password(password)


class StudentYearLevelChange(BaseModel):
    email: str = Field(min_length=3, max_length=255)
    year_level: int = Field(ge=1, le=5)


class AdminLogin(BaseModel):
    username: str = Field(min_length=1, max_length=50)
    password: str = Field(min_length=1, max_length=72)

    @field_validator("password")
    @classmethod
    def validate_password_size(cls, password: str) -> str:
        return validate_bcrypt_password(password)


class AdminPasswordChange(BaseModel):
    username: str = Field(min_length=1, max_length=50)
    current_password: str = Field(min_length=1, max_length=72)
    new_password: str = Field(min_length=10, max_length=72)

    @field_validator("current_password", "new_password")
    @classmethod
    def validate_password_size(cls, password: str) -> str:
        return validate_bcrypt_password(password)


class SemesterChange(BaseModel):
    semester: Literal[1, 2]


class SectionUpdate(BaseModel):
    subject_id: int = Field(gt=0)
    section_name: str = Field(min_length=1, max_length=50)
    max_capacity: int = Field(gt=0)


class SectionAvailability(BaseModel):
    is_open: bool


class InstructorCreate(BaseModel):
    instructor_name: str = Field(min_length=1, max_length=255)


class RoomCreate(BaseModel):
    building: str = Field(min_length=1, max_length=100)
    room_num: str = Field(min_length=1, max_length=50)


class SectionCreate(BaseModel):
    subject_id: int = Field(gt=0)
    section_name: str = Field(min_length=1, max_length=50)
    max_capacity: int = Field(gt=0)


class ClassCreate(BaseModel):
    instructor_id: int = Field(gt=0)
    room_id: int = Field(gt=0)
    day_of_the_week: str = Field(min_length=1, max_length=20)
    start_time: time
    end_time: time
    class_type: Literal["Lecture", "Lab"] = "Lecture"
    max_capacity: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def validate_time_range(self):
        if self.end_time <= self.start_time:
            raise ValueError("end_time must be later than start_time.")
        return self


class SubmissionDecision(BaseModel):
    status: Literal["Approved", "Rejected"]

# Get the database connection pool.
def get_pool(request: Request) -> asyncpg.Pool:
    pool = getattr(request.app.state, "pool", None)
    if pool is None:
        raise HTTPException(status_code=503, detail="Database is not available.")
    return pool


def get_auth_token_secret() -> bytes:
    secret = (
        os.getenv("SESSION_SECRET")
        or os.getenv("AUTH_TOKEN_SECRET")
        or os.getenv("ADMIN_TOKEN_SECRET")
    )
    if secret is None or len(secret.encode("utf-8")) < 32:
        raise HTTPException(
            status_code=503,
            detail="Configure SESSION_SECRET with at least 32 UTF-8 bytes.",
        )
    return secret.encode("utf-8")


def encode_token_segment(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def create_token(claims: dict) -> str:
    header = encode_token_segment(b'{"alg":"HS256","typ":"JWT"}')
    expires_at = int((datetime.now(timezone.utc) + timedelta(hours=8)).timestamp())
    payload_data = {**claims, "exp": expires_at}
    payload = encode_token_segment(json.dumps(payload_data, separators=(",", ":")).encode("utf-8"))
    signing_input = f"{header}.{payload}"
    signature = hmac.new(
        get_auth_token_secret(), signing_input.encode("ascii"), hashlib.sha256
    ).digest()
    return f"{signing_input}.{encode_token_segment(signature)}"


def create_admin_token(admin_id: int, username: str) -> str:
    return create_token({"sub": username, "admin_id": admin_id, "role": "admin"})


def create_student_token(student_id: str) -> str:
    return create_token({"sub": student_id, "role": "student"})


def decode_token(token: str) -> dict:
    try:
        header_segment, payload_segment, signature_segment = token.split(".")
        header = json.loads(
            base64.urlsafe_b64decode(
                header_segment + "=" * (-len(header_segment) % 4)
            )
        )
        payload = json.loads(
            base64.urlsafe_b64decode(
                payload_segment + "=" * (-len(payload_segment) % 4)
            )
        )
        signature = base64.urlsafe_b64decode(
            signature_segment + "=" * (-len(signature_segment) % 4)
        )
        signing_input = f"{header_segment}.{payload_segment}"
        expected_signature = hmac.new(
            get_auth_token_secret(), signing_input.encode("ascii"), hashlib.sha256
        ).digest()
        expires_at = payload["exp"]
        subject = payload["sub"]
        role = payload["role"]
        if (
            header != {"alg": "HS256", "typ": "JWT"}
            or not hmac.compare_digest(signature, expected_signature)
            or not isinstance(expires_at, int)
            or expires_at <= int(datetime.now(timezone.utc).timestamp())
            or not isinstance(subject, str)
            or role not in ("admin", "student")
            or (role == "admin" and not isinstance(payload.get("admin_id"), int))
        ):
            raise ValueError("Invalid token claims.")
        return payload
    except (
        ValueError,
        KeyError,
        TypeError,
        json.JSONDecodeError,
        UnicodeDecodeError,
        binascii.Error,
    ):
        raise HTTPException(
            status_code=401,
            detail="Invalid or expired session. Please sign in again.",
        ) from None


async def require_admin(
    request: Request,
    pool: asyncpg.Pool = Depends(get_pool),
) -> dict:
    session = request.cookies.get("admin_session")
    if session is None:
        raise HTTPException(
            status_code=401,
            detail="Admin login required.",
        )
    token_admin = decode_token(session)
    if token_admin["role"] != "admin":
        raise HTTPException(status_code=403, detail="Admin login required.")
    row = await pool.fetchrow(
        """SELECT admin_id, username FROM admin_account
           WHERE admin_id = $1 AND LOWER(username) = LOWER($2) AND is_active = TRUE""",
        token_admin["admin_id"],
        token_admin["sub"],
    )
    if row is None:
        raise HTTPException(
            status_code=401,
            detail="Admin account is inactive or no longer exists.",
        )
    return {"admin_id": row["admin_id"], "username": row["username"]}


async def require_student(
    request: Request,
    pool: asyncpg.Pool = Depends(get_pool),
) -> dict:
    session = request.cookies.get("student_session")
    if session is None:
        raise HTTPException(status_code=401, detail="Student login required.")
    token_student = decode_token(session)
    student_id = token_student["sub"]
    if token_student["role"] != "student":
        raise HTTPException(status_code=403, detail="Student login required.")
    row = await pool.fetchrow(
        """SELECT student_id, email FROM account
           WHERE student_id = $1""",
        student_id,
    )
    if row is None:
        raise HTTPException(
            status_code=401,
            detail="Student account no longer exists.",
        )
    return {"student_id": row["student_id"], "email": row["email"]}


def set_session_cookie(
    response: Response, name: str, session: str, request: Request
) -> None:
    response.set_cookie(
        key=name,
        value=session,
        max_age=8 * 60 * 60,
        httponly=True,
        secure=request.url.scheme == "https",
        samesite="lax",
        path="/",
    )


def clear_session_cookie(response: Response, name: str, request: Request) -> None:
    response.delete_cookie(
        key=name,
        httponly=True,
        secure=request.url.scheme == "https",
        samesite="lax",
        path="/",
    )


student_auth_router = APIRouter(prefix="/students", tags=["Students"])
student_router = APIRouter(
    prefix="/students", tags=["Students"], dependencies=[Depends(require_student)]
)
admin_auth_router = APIRouter(prefix="/admin", tags=["Admin"])
admin_router = APIRouter(
    prefix="/admin", tags=["Admin"], dependencies=[Depends(require_admin)]
)


# Clean and validate a student ID.
def normalize_student_id(student_id: str) -> str:
    normalized = student_id.replace("-", "")
    if not normalized.isdigit() or not 9 <= len(normalized) <= 12:
        raise HTTPException(status_code=422, detail="Student ID must contain 9 to 12 digits.")
    return normalized


def ensure_student_owns_account(student_id: str, current_student: dict) -> None:
    if student_id != current_student["student_id"]:
        raise HTTPException(
            status_code=403,
            detail="You can only access your own student account.",
        )


# Student account routes.
@student_auth_router.post("/register", status_code=status.HTTP_201_CREATED)
async def create_student_account(data: StudentAccountCreate, pool: asyncpg.Pool = Depends(get_pool)):
    password_hash = bcrypt.hashpw(data.password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
    try:
        row = await pool.fetchrow(
            """INSERT INTO account
               (student_id, email, password_hash, first_name, last_name, year_level)
               VALUES ($1, $2, $3, $4, $5, $6)
               RETURNING student_id, email, first_name, last_name, year_level""",
            data.student_id,
            data.email,
            password_hash,
            data.first_name,
            data.last_name,
            data.year_level,
        )
    except asyncpg.UniqueViolationError:
        raise HTTPException(status_code=409, detail="Student ID or email is already in use.") from None
    return dict(row)


# Sign in a student.
@student_auth_router.post("/login")
async def student_login(
    data: StudentLogin,
    request: Request,
    response: Response,
    pool: asyncpg.Pool = Depends(get_pool),
):
    row = await pool.fetchrow(
        """SELECT student_id, email, first_name, last_name, year_level, password_hash
           FROM account WHERE LOWER(email) = LOWER($1)""",
        data.email.strip(),
    )
    if row is None or not bcrypt.checkpw(data.password.encode("utf-8"), row["password_hash"].encode("utf-8")):
        raise HTTPException(status_code=401, detail="Invalid email or password.")
    set_session_cookie(
        response, "student_session", create_student_token(row["student_id"]), request
    )
    return {
        "success": True,
        "message": "Login successful.",
        "user": {key: row[key] for key in ("student_id", "first_name", "last_name", "email", "year_level")},
    }


@student_auth_router.post("/logout")
async def student_logout(request: Request, response: Response):
    clear_session_cookie(response, "student_session", request)
    return {"success": True, "message": "Logged out."}


# Change a student's password.
@student_router.put("/password")
async def change_student_password(
    data: StudentPasswordChange,
    current_student: dict = Depends(require_student),
    pool: asyncpg.Pool = Depends(get_pool),
):
    email = data.email.strip().lower()
    if email != current_student["email"].lower():
        raise HTTPException(status_code=403, detail="You can only change your own password.")
    async with pool.acquire() as connection:
        async with connection.transaction():
            row = await connection.fetchrow(
                """SELECT student_id, password_hash FROM account
                   WHERE student_id = $1 AND LOWER(email) = $2 FOR UPDATE""",
                current_student["student_id"],
                email,
            )
            if row is None or not bcrypt.checkpw(
                data.old_password.encode("utf-8"), row["password_hash"].encode("utf-8")
            ):
                raise HTTPException(status_code=400, detail="Current password is incorrect.")
            password_hash = bcrypt.hashpw(
                data.new_password.encode("utf-8"), bcrypt.gensalt()
            ).decode("utf-8")
            await connection.execute(
                "UPDATE account SET password_hash = $1 WHERE student_id = $2",
                password_hash,
                row["student_id"],
            )
    return {"success": True, "message": "Password updated successfully."}


# Update year level and clear the schedule if it changes.
@student_router.put("/year-level")
async def change_student_year_level(
    data: StudentYearLevelChange,
    current_student: dict = Depends(require_student),
    pool: asyncpg.Pool = Depends(get_pool),
):
    if data.email.strip().lower() != current_student["email"].lower():
        raise HTTPException(status_code=403, detail="You can only update your own account.")
    async with pool.acquire() as connection:
        async with connection.transaction():
            row = await connection.fetchrow(
                """UPDATE account SET year_level = $1
                   WHERE student_id = $2 AND LOWER(email) = LOWER($3)
                   RETURNING student_id""",
                data.year_level,
                current_student["student_id"],
                data.email.strip(),
            )
            if row is None:
                raise HTTPException(status_code=404, detail="Student account not found.")
            await connection.execute(
                "DELETE FROM plotting WHERE student_id = $1", row["student_id"]
            )
            await connection.execute(
                """INSERT INTO schedule_submission (student_id, is_submitted, submitted_at)
                   VALUES ($1, FALSE, NULL)
                   ON CONFLICT (student_id) DO UPDATE
                   SET is_submitted = FALSE, submitted_at = NULL""",
                row["student_id"],
            )
    return {"success": True, "message": "Year level updated and schedule cleared."}


# List available offerings for the selected year and semester.
@student_router.get("/subjects")
async def student_subjects(
    year_level: int | None = None,
    semester: Literal[1, 2] | None = None,
    pool: asyncpg.Pool = Depends(get_pool),
):
    subjects = await pool.fetch(
        """SELECT subject_id AS id, TRIM(subject_code) AS code, subject_name AS name,
                  year_level AS "yearLevel", semester
           FROM subject
           WHERE ($1::integer IS NULL OR year_level = $1)
             AND ($2::smallint IS NULL OR semester = $2)
           ORDER BY year_level, semester, subject_id""",
        year_level,
        semester,
    )
    if not subjects:
        return []

    subject_ids = [subject["id"] for subject in subjects]
    sections = await pool.fetch(
        """SELECT sec.section_id AS id, sec.section_name AS type, o.subject_id AS "subjectId",
                  MIN(sch.start_time) AS start, MAX(sch.end_time) AS "end",
                  ARRAY_AGG(DISTINCT sch.day_of_the_week ORDER BY sch.day_of_the_week)
                      FILTER (WHERE sch.day_of_the_week IS NOT NULL) AS days,
                  MAX(i.instructor_name) AS instructor,
                  MAX(CONCAT_WS(', ', NULLIF(TRIM(r.building), ''),
                                      NULLIF(TRIM(r.room_num), ''))) AS room,
                  sec.max_capacity AS "maxCapacity", MAX(o.class_type) AS "classType",
                  ARRAY_AGG(DISTINCT o.class_type) FILTER (WHERE o.class_type IS NOT NULL)
                      AS "classTypes"
           FROM section sec
           JOIN schedule sch ON sch.section_id = sec.section_id
           JOIN offering o ON o.schedule_id = sch.schedule_id AND o.subject_id = ANY($1::integer[])
           LEFT JOIN instructor i ON i.instructor_id = o.instructor_id
           LEFT JOIN room r ON r.room_id = o.room_id
           WHERE sec.is_open = TRUE
           GROUP BY sec.section_id, sec.section_name, o.subject_id, sec.max_capacity
           ORDER BY sec.section_id""",
        subject_ids,
    )
    sections_by_subject: dict[int, list[dict]] = {}
    for section in sections:
        section_data = dict(section)
        section_data["days"] = section_data["days"] or []
        section_data["classTypes"] = section_data["classTypes"] or []
        section_data["start"] = str(section_data["start"] or "")
        section_data["end"] = str(section_data["end"] or "")
        section_data["subjectId"] = section_data.pop("subjectId")
        sections_by_subject.setdefault(section["subjectId"], []).append(section_data)

    return [
        {**dict(subject), "sections": sections_by_subject.get(subject["id"], [])}
        for subject in subjects
    ]


# Get a student's account details by ID.
@student_router.get("/{student_id}/profile")
async def student_profile(
    student_id: str,
    current_student: dict = Depends(require_student),
    pool: asyncpg.Pool = Depends(get_pool),
):
    student_id = normalize_student_id(student_id)
    ensure_student_owns_account(student_id, current_student)
    row = await pool.fetchrow(
        """SELECT student_id, email, first_name, last_name, year_level
           FROM account WHERE student_id = $1""",
        student_id,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Student account not found.")
    return dict(row)


# List open class schedules.
@student_router.get("/schedules")
async def available_schedules(pool: asyncpg.Pool = Depends(get_pool)):
    rows = await pool.fetch(
        """SELECT o.offering_id, s.subject_code, s.subject_name, sec.section_name,
                  o.class_type, o.max_capacity, i.instructor_name,
                  r.building, r.room_num, sch.day_of_the_week,
                  sch.start_time, sch.end_time
           FROM offering o
           JOIN subject s ON s.subject_id = o.subject_id
           JOIN schedule sch ON sch.schedule_id = o.schedule_id
           JOIN section sec ON sec.section_id = COALESCE(o.section_id, sch.section_id)
           LEFT JOIN instructor i ON i.instructor_id = o.instructor_id
           LEFT JOIN room r ON r.room_id = o.room_id
           WHERE sec.is_open = TRUE
           ORDER BY s.subject_code, sec.section_name, sch.day_of_the_week, sch.start_time"""
    )
    return [dict(row) for row in rows]


# Show a student's draft schedule and status.
@student_router.get("/{student_id}/plots")
async def student_plots(
    student_id: str,
    current_student: dict = Depends(require_student),
    pool: asyncpg.Pool = Depends(get_pool),
):
    student_id = normalize_student_id(student_id)
    ensure_student_owns_account(student_id, current_student)
    rows = await pool.fetch(
        """SELECT p.plot_id, p.status, o.offering_id, o.class_type,
                  s.subject_code, s.subject_name, sec.section_name,
                  sch.day_of_the_week, sch.start_time, sch.end_time
           FROM plotting p
           JOIN offering o ON o.offering_id = p.offering_id
           JOIN subject s ON s.subject_id = o.subject_id
           JOIN schedule sch ON sch.schedule_id = o.schedule_id
           JOIN section sec ON sec.section_id = COALESCE(o.section_id, sch.section_id)
           WHERE p.student_id = $1 ORDER BY p.plot_id""",
        student_id,
    )
    return [dict(row) for row in rows]


@student_router.get("/{student_id}/plots/pdf")
async def export_student_plots_pdf(
    student_id: str,
    current_student: dict = Depends(require_student),
    pool: asyncpg.Pool = Depends(get_pool),
):
    student_id = normalize_student_id(student_id)
    ensure_student_owns_account(student_id, current_student)
    student = await pool.fetchrow(
        "SELECT first_name, last_name FROM account WHERE student_id = $1",
        student_id,
    )
    if student is None:
        raise HTTPException(status_code=404, detail="Student account not found.")
    plots = await pool.fetch(
        """SELECT s.subject_code, s.subject_name, sec.section_name,
                  o.class_type, sch.day_of_the_week, sch.start_time, sch.end_time,
                  p.status
           FROM plotting p
           JOIN offering o ON o.offering_id = p.offering_id
           JOIN subject s ON s.subject_id = o.subject_id
           JOIN schedule sch ON sch.schedule_id = o.schedule_id
           JOIN section sec ON sec.section_id = COALESCE(o.section_id, sch.section_id)
           WHERE p.student_id = $1 ORDER BY sch.day_of_the_week, sch.start_time, p.plot_id""",
        student_id,
    )

    styles = getSampleStyleSheet()
    output = io.BytesIO()
    document = SimpleDocTemplate(
        output, pagesize=letter, title=f"Schedule for {student_id}"
    )
    story = [
        Paragraph("Plotted Schedule", styles["Title"]),
        Paragraph(
            f"Student: {escape(student['first_name'])} {escape(student['last_name'])} "
            f"({student_id})",
            styles["Normal"],
        ),
        Spacer(1, 16),
    ]
    if plots:
        rows = [["Subject", "Section", "Type", "Day", "Time", "Status"]]
        for plot in plots:
            rows.append(
                [
                    Paragraph(
                        escape(f"{plot['subject_code']} - {plot['subject_name']}"),
                        styles["BodyText"],
                    ),
                    Paragraph(escape(str(plot["section_name"])), styles["BodyText"]),
                    Paragraph(escape(str(plot["class_type"])), styles["BodyText"]),
                    Paragraph(escape(str(plot["day_of_the_week"])), styles["BodyText"]),
                    Paragraph(
                        escape(f"{plot['start_time']} - {plot['end_time']}"),
                        styles["BodyText"],
                    ),
                    Paragraph(escape(str(plot["status"])), styles["BodyText"]),
                ]
            )
        rows[0] = [
            Paragraph(f"<b>{heading}</b>", styles["BodyText"])
            for heading in rows[0]
        ]
        table = Table(
            rows,
            colWidths=[155, 50, 50, 60, 80, 65],
            repeatRows=1,
        )
        table.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#24476b")),
                    ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                    ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#b7c3cf")),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#edf2f7")]),
                    ("FONTSIZE", (0, 0), (-1, -1), 8),
                    ("LEFTPADDING", (0, 0), (-1, -1), 4),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                ]
            )
        )
        story.append(table)
    else:
        story.append(Paragraph("No classes have been plotted.", styles["Normal"]))

    document.build(story)
    pdf = output.getvalue()
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="schedule-{student_id}.pdf"'},
    )


# Add an available class to the student's draft schedule.
@student_router.post("/{student_id}/plots", status_code=status.HTTP_201_CREATED)
async def plot_schedule(
    student_id: str,
    data: PlotCreate,
    current_student: dict = Depends(require_student),
    pool: asyncpg.Pool = Depends(get_pool),
):
    student_id = normalize_student_id(student_id)
    ensure_student_owns_account(student_id, current_student)
    async with pool.acquire() as connection:
        async with connection.transaction():
            offering = await connection.fetchrow(
                """SELECT o.offering_id, o.max_capacity,
                          COALESCE(o.section_id, sch.section_id) AS section_id
                   FROM offering o
                   LEFT JOIN schedule sch ON sch.schedule_id = o.schedule_id
                   WHERE o.offering_id = $1 FOR UPDATE OF o""",
                data.offering_id,
            )
            if offering is None:
                raise HTTPException(status_code=404, detail="Class schedule not found.")
            section_is_open = await connection.fetchval(
                "SELECT is_open FROM section WHERE section_id = $1",
                offering["section_id"],
            )
            if not section_is_open:
                raise HTTPException(status_code=409, detail="This section is not open for plotting.")
            duplicate = await connection.fetchval(
                """SELECT EXISTS(
                       SELECT 1 FROM plotting
                       WHERE student_id = $1 AND offering_id = $2
                         AND status IN ('Draft', 'Submitted', 'Approved')
                   )""",
                student_id,
                data.offering_id,
            )
            if duplicate:
                raise HTTPException(status_code=409, detail="This class is already in your plotted schedule.")
            count = await connection.fetchval(
                """SELECT COUNT(*) FROM plotting
                   WHERE offering_id = $1 AND status IN ('Draft', 'Submitted', 'Approved')""",
                data.offering_id,
            )
            if count >= offering["max_capacity"]:
                raise HTTPException(status_code=409, detail="This class has reached capacity.")
            row = await connection.fetchrow(
                """INSERT INTO plotting (student_id, offering_id, status)
                   VALUES ($1, $2, 'Draft') RETURNING plot_id, student_id, offering_id, status""",
                student_id,
                data.offering_id,
            )
    return dict(row)


# Submit the student's draft schedule for approval.
@student_router.post("/{student_id}/submit")
async def submit_plotted_schedule(
    student_id: str,
    current_student: dict = Depends(require_student),
    pool: asyncpg.Pool = Depends(get_pool),
):
    student_id = normalize_student_id(student_id)
    ensure_student_owns_account(student_id, current_student)
    async with pool.acquire() as connection:
        async with connection.transaction():
            rows = await connection.fetch(
                """UPDATE plotting SET status = 'Submitted'
                   WHERE student_id = $1 AND status = 'Draft'
                   RETURNING plot_id, offering_id, status""",
                student_id,
            )
            if not rows:
                raise HTTPException(status_code=409, detail="There are no draft classes to submit.")
            await connection.execute(
                """INSERT INTO schedule_submission (student_id, is_submitted, submitted_at)
                   VALUES ($1, TRUE, CURRENT_TIMESTAMP)
                   ON CONFLICT (student_id) DO UPDATE
                   SET is_submitted = TRUE, submitted_at = CURRENT_TIMESTAMP""",
                student_id,
            )
    return {"message": "Schedule submitted to the admin.", "plots": [dict(row) for row in rows]}


# Admin sign-in and password routes.
@admin_auth_router.post("/login")
async def admin_login(
    data: AdminLogin,
    request: Request,
    response: Response,
    pool: asyncpg.Pool = Depends(get_pool),
):
    row = await pool.fetchrow(
        """SELECT admin_id, username, display_name, is_active,
                  password_hash = crypt($2, password_hash) AS password_matches
           FROM admin_account WHERE LOWER(username) = LOWER($1)""",
        data.username.strip(),
        data.password,
    )
    if (
        row is None
        or not row["is_active"]
        or not row["password_matches"]
    ):
        raise HTTPException(status_code=401, detail="Invalid admin username or password.")
    set_session_cookie(
        response,
        "admin_session",
        create_admin_token(row["admin_id"], row["username"]),
        request,
    )
    return {
        "success": True,
        "admin": {
            key: row[key]
            for key in ("admin_id", "username", "display_name", "is_active")
        },
    }


@admin_auth_router.post("/logout")
async def admin_logout(request: Request, response: Response):
    clear_session_cookie(response, "admin_session", request)
    return {"success": True, "message": "Logged out."}


# Change an admin's password after verifying the current one.
@admin_router.put("/password")
async def change_admin_password(
    data: AdminPasswordChange,
    admin: dict = Depends(require_admin),
    pool: asyncpg.Pool = Depends(get_pool),
):
    if data.username.strip().lower() != admin["username"].lower():
        raise HTTPException(status_code=403, detail="You can only change your own password.")
    async with pool.acquire() as connection:
        async with connection.transaction():
            row = await connection.fetchrow(
                """SELECT admin_id, password_hash FROM admin_account
                   WHERE admin_id = $1 AND is_active = TRUE FOR UPDATE""",
                admin["admin_id"],
            )
            if row is None:
                raise HTTPException(status_code=401, detail="Admin account not found or inactive.")
            if not bcrypt.checkpw(
                data.current_password.encode("utf-8"), row["password_hash"].encode("utf-8")
            ):
                raise HTTPException(status_code=401, detail="Current password is incorrect.")
            password_hash = bcrypt.hashpw(
                data.new_password.encode("utf-8"), bcrypt.gensalt()
            ).decode("utf-8")
            await connection.execute(
                "UPDATE admin_account SET password_hash = $1 WHERE admin_id = $2",
                password_hash,
                row["admin_id"],
            )
    return {"success": True, "message": "Password updated successfully."}


# Subject management routes.
@admin_router.post("/subjects", status_code=status.HTTP_201_CREATED)
async def create_subject(data: SubjectCreate, pool: asyncpg.Pool = Depends(get_pool)):
    row = await pool.fetchrow(
        """INSERT INTO subject (subject_code, subject_name, year_level, semester)
           VALUES ($1, $2, $3, $4) RETURNING *""",
        data.subject_code,
        data.subject_name,
        data.year_level,
        data.semester,
    )
    return dict(row)


# Update a subject.
@admin_router.put("/subjects/{subject_id}")
async def update_subject(
    subject_id: int, data: SubjectCreate, pool: asyncpg.Pool = Depends(get_pool)
):
    row = await pool.fetchrow(
        """UPDATE subject
           SET subject_code = $1, subject_name = $2, year_level = $3, semester = $4
           WHERE subject_id = $5 RETURNING *""",
        data.subject_code,
        data.subject_name,
        data.year_level,
        data.semester,
        subject_id,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Subject not found.")
    return dict(row)


# Remove a subject from the catalog.
@admin_router.delete("/subjects/{subject_id}")
async def delete_subject(subject_id: int, pool: asyncpg.Pool = Depends(get_pool)):
    deleted = await pool.fetchval(
        "DELETE FROM subject WHERE subject_id = $1 RETURNING subject_id", subject_id
    )
    if deleted is None:
        raise HTTPException(status_code=404, detail="Subject not found.")
    return {"success": True}


# List all subjects.
@admin_router.get("/subjects")
async def list_subjects(pool: asyncpg.Pool = Depends(get_pool)):
    rows = await pool.fetch(
        """SELECT subject_id AS id, TRIM(subject_code) AS code, subject_name AS name,
                  year_level AS "yearLevel", semester
           FROM subject ORDER BY year_level, semester, subject_id"""
    )
    return [dict(row) for row in rows]


# Change the active semester and reset scheduling.
@admin_router.put("/semester")
async def change_semester(data: SemesterChange, pool: asyncpg.Pool = Depends(get_pool)):
    async with pool.acquire() as connection:
        async with connection.transaction():
            await connection.execute("DELETE FROM plotting")
            updated = await connection.execute(
                "UPDATE account SET semester = $1", data.semester
            )
            await connection.execute(
                "UPDATE schedule_submission SET is_submitted = FALSE, submitted_at = NULL"
            )
    updated_count = int(updated.rsplit(" ", 1)[-1])
    return {"success": True, "semester": data.semester, "updated": updated_count}


# Instructor management routes.
@admin_router.post("/instructors", status_code=status.HTTP_201_CREATED)
async def create_instructor(data: InstructorCreate, pool: asyncpg.Pool = Depends(get_pool)):
    name = data.instructor_name.strip()
    row = await pool.fetchrow(
        """INSERT INTO instructor (instructor_name) VALUES ($1)
           RETURNING instructor_id AS id, instructor_name AS name""",
        name,
    )
    return dict(row)


# List instructors.
@admin_router.get("/instructors")
async def list_instructors(pool: asyncpg.Pool = Depends(get_pool)):
    rows = await pool.fetch(
        "SELECT instructor_id AS id, instructor_name AS name FROM instructor ORDER BY instructor_name"
    )
    return [dict(row) for row in rows]


@admin_router.put("/instructors/{instructor_id}")
async def update_instructor(
    instructor_id: int,
    data: InstructorCreate,
    pool: asyncpg.Pool = Depends(get_pool),
):
    row = await pool.fetchrow(
        """UPDATE instructor SET instructor_name = $1
           WHERE instructor_id = $2
           RETURNING instructor_id AS id, instructor_name AS name""",
        data.instructor_name.strip(),
        instructor_id,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Instructor not found.")
    return dict(row)


@admin_router.delete("/instructors/{instructor_id}")
async def delete_instructor(instructor_id: int, pool: asyncpg.Pool = Depends(get_pool)):
    async with pool.acquire() as connection:
        async with connection.transaction():
            instructor = await connection.fetchrow(
                "SELECT instructor_id FROM instructor WHERE instructor_id = $1 FOR UPDATE",
                instructor_id,
            )
            if instructor is None:
                raise HTTPException(status_code=404, detail="Instructor not found.")
            if await connection.fetchval(
                "SELECT EXISTS(SELECT 1 FROM offering WHERE instructor_id = $1)",
                instructor_id,
            ):
                raise HTTPException(
                    status_code=409,
                    detail="Cannot delete an instructor assigned to a class.",
                )
            await connection.execute(
                "DELETE FROM instructor WHERE instructor_id = $1", instructor_id
            )
    return {"success": True}


# Add a classroom.
@admin_router.post("/rooms", status_code=status.HTTP_201_CREATED)
async def create_room(data: RoomCreate, pool: asyncpg.Pool = Depends(get_pool)):
    row = await pool.fetchrow(
        "INSERT INTO room (building, room_num) VALUES ($1, $2) RETURNING *",
        data.building,
        data.room_num,
    )
    return dict(row)


# List classrooms.
@admin_router.get("/rooms")
async def list_rooms(pool: asyncpg.Pool = Depends(get_pool)):
    rows = await pool.fetch("SELECT * FROM room ORDER BY building, room_num")
    return [dict(row) for row in rows]


@admin_router.put("/rooms/{room_id}")
async def update_room(
    room_id: int, data: RoomCreate, pool: asyncpg.Pool = Depends(get_pool)
):
    row = await pool.fetchrow(
        """UPDATE room SET building = $1, room_num = $2
           WHERE room_id = $3 RETURNING room_id, building, room_num""",
        data.building,
        data.room_num,
        room_id,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Room not found.")
    return dict(row)


@admin_router.delete("/rooms/{room_id}")
async def delete_room(room_id: int, pool: asyncpg.Pool = Depends(get_pool)):
    async with pool.acquire() as connection:
        async with connection.transaction():
            room = await connection.fetchrow(
                "SELECT room_id FROM room WHERE room_id = $1 FOR UPDATE", room_id
            )
            if room is None:
                raise HTTPException(status_code=404, detail="Room not found.")
            if await connection.fetchval(
                "SELECT EXISTS(SELECT 1 FROM offering WHERE room_id = $1)", room_id
            ):
                raise HTTPException(
                    status_code=409,
                    detail="Cannot delete a room assigned to a class.",
                )
            await connection.execute("DELETE FROM room WHERE room_id = $1", room_id)
    return {"success": True}


# List student accounts.
@admin_router.get("/students")
async def list_students(pool: asyncpg.Pool = Depends(get_pool)):
    rows = await pool.fetch(
        """SELECT student_id AS id, student_id AS "studentId",
                  CONCAT_WS(' ', first_name, last_name) AS name, year_level AS "yearLevel",
                  semester, email
           FROM account ORDER BY last_name, first_name, student_id"""
    )
    return [dict(row) for row in rows]


# Create a section for a subject.
@admin_router.post("/sections", status_code=status.HTTP_201_CREATED)
async def create_section(data: SectionCreate, pool: asyncpg.Pool = Depends(get_pool)):
    async with pool.acquire() as connection:
        async with connection.transaction():
            section = await connection.fetchrow(
                """INSERT INTO section (section_name, subject_id, max_capacity, is_open)
                   VALUES ($1, $2, $3, FALSE) RETURNING *""",
                data.section_name,
                data.subject_id,
                data.max_capacity,
            )
    return dict(section)


@admin_router.get("/sections")
async def list_sections(pool: asyncpg.Pool = Depends(get_pool)):
    rows = await pool.fetch(
        """SELECT sec.section_id, sec.section_name, sec.subject_id, s.subject_code,
                  s.subject_name, sec.max_capacity, sec.is_open,
                  sec.is_open AS published,
                  COUNT(DISTINCT p.student_id) AS enrollment_count
           FROM section sec
           JOIN subject s ON s.subject_id = sec.subject_id
           LEFT JOIN schedule sch ON sch.section_id = sec.section_id
           LEFT JOIN offering o ON o.schedule_id = sch.schedule_id
           LEFT JOIN plotting p ON p.offering_id = o.offering_id
           GROUP BY sec.section_id, s.subject_code, s.subject_name
           ORDER BY s.subject_code, sec.section_name"""
    )
    section_ids = [row["section_id"] for row in rows]
    slots = await pool.fetch(
        """SELECT sch.section_id, o.class_type AS "classType",
                  sch.day_of_the_week AS day, sch.start_time AS start, sch.end_time AS "end",
                  i.instructor_name AS instructor,
                  CONCAT_WS(', ', NULLIF(TRIM(r.building), ''),
                                   NULLIF(TRIM(r.room_num), '')) AS room,
                  o.offering_id
           FROM schedule sch
           JOIN offering o ON o.schedule_id = sch.schedule_id
           LEFT JOIN instructor i ON i.instructor_id = o.instructor_id
           LEFT JOIN room r ON r.room_id = o.room_id
           WHERE sch.section_id = ANY($1::integer[])
           ORDER BY sch.section_id, sch.day_of_the_week, sch.start_time""",
        section_ids,
    ) if section_ids else []
    classes_by_section: dict[int, list[dict]] = {}
    for slot in slots:
        class_type = slot["classType"] or "Lecture"
        start = str(slot["start"])
        end = str(slot["end"])
        room = slot["room"] or ""
        key = (class_type, start, end, slot["instructor"] or "", room)
        classes = classes_by_section.setdefault(slot["section_id"], [])
        class_group = next((item for item in classes if item["_key"] == key), None)
        if class_group is None:
            class_group = {
                "_key": key,
                "classType": class_type,
                "start": start,
                "end": end,
                "days": [],
                "instructor": slot["instructor"] or "",
                "room": room,
                "offeringIds": [],
            }
            classes.append(class_group)
        if slot["day"] not in class_group["days"]:
            class_group["days"].append(slot["day"])
        if slot["offering_id"] not in class_group["offeringIds"]:
            class_group["offeringIds"].append(slot["offering_id"])

    result = []
    for row in rows:
        section = dict(row)
        section_classes = classes_by_section.get(section["section_id"], [])
        for class_group in section_classes:
            class_group.pop("_key")
        section["classes"] = section_classes
        section["offeringIds"] = sorted(
            {offering_id for class_group in section_classes for offering_id in class_group["offeringIds"]}
        )
        section["enrollment_count"] = int(section["enrollment_count"])
        section["remainingSeats"] = max(
            section["max_capacity"] - section["enrollment_count"], 0
        )
        result.append(section)
    return result


@admin_router.put("/sections/{section_id}")
async def update_section(
    section_id: int, data: SectionUpdate, pool: asyncpg.Pool = Depends(get_pool)
):
    row = await pool.fetchrow(
        """UPDATE section
           SET section_name = $1, subject_id = $2, max_capacity = $3
           WHERE section_id = $4 RETURNING *""",
        data.section_name,
        data.subject_id,
        data.max_capacity,
        section_id,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Section not found.")
    return dict(row)


@admin_router.put("/sections/{section_id}/availability")
async def set_section_availability(
    section_id: int, data: SectionAvailability, pool: asyncpg.Pool = Depends(get_pool)
):
    if data.is_open and not await pool.fetchval(
        """SELECT EXISTS(
               SELECT 1 FROM offering o
               JOIN schedule sch ON sch.schedule_id = o.schedule_id
               WHERE sch.section_id = $1 OR o.section_id = $1
           )""",
        section_id,
    ):
        raise HTTPException(status_code=409, detail="Cannot open a section without at least one class.")
    row = await pool.fetchrow(
        "UPDATE section SET is_open = $1 WHERE section_id = $2 RETURNING section_id, is_open",
        data.is_open,
        section_id,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Section not found.")
    return {"success": True, "id": row["section_id"], "isOpen": row["is_open"]}


@admin_router.delete("/sections/{section_id}")
async def delete_section(section_id: int, pool: asyncpg.Pool = Depends(get_pool)):
    deleted = await pool.fetchval(
        "DELETE FROM section WHERE section_id = $1 RETURNING section_id", section_id
    )
    if deleted is None:
        raise HTTPException(status_code=404, detail="Section not found.")
    return {"success": True}


@admin_router.post("/sections/{section_id}/classes", status_code=status.HTTP_201_CREATED)
async def add_class_to_section(
    section_id: int,
    data: ClassCreate,
    pool: asyncpg.Pool = Depends(get_pool),
):
    async with pool.acquire() as connection:
        async with connection.transaction():
            section = await connection.fetchrow(
                "SELECT subject_id, max_capacity FROM section WHERE section_id = $1 FOR UPDATE",
                section_id,
            )
            if section is None:
                raise HTTPException(status_code=404, detail="Section not found.")
            schedule = await connection.fetchrow(
                """INSERT INTO schedule (day_of_the_week, start_time, end_time, section_id)
                   VALUES ($1, $2, $3, $4) RETURNING *""",
                data.day_of_the_week,
                data.start_time,
                data.end_time,
                section_id,
            )
            offering = await connection.fetchrow(
                """INSERT INTO offering
                   (subject_id, room_id, instructor_id, max_capacity, schedule_id, section_id, class_type)
                   VALUES ($1, $2, $3, $4, $5, $6, $7) RETURNING *""",
                section["subject_id"],
                data.room_id,
                data.instructor_id,
                data.max_capacity or section["max_capacity"],
                schedule["schedule_id"],
                section_id,
                data.class_type,
            )
    return {"schedule": dict(schedule), "class": dict(offering)}


@admin_router.put("/sections/{section_id}/classes/{offering_id}")
async def update_class_in_section(
    section_id: int,
    offering_id: int,
    data: ClassCreate,
    pool: asyncpg.Pool = Depends(get_pool),
):
    async with pool.acquire() as connection:
        async with connection.transaction():
            current = await connection.fetchrow(
                """SELECT o.schedule_id, sec.max_capacity
                   FROM offering o
                   JOIN schedule sch ON sch.schedule_id = o.schedule_id
                   JOIN section sec ON sec.section_id = COALESCE(o.section_id, sch.section_id)
                   WHERE o.offering_id = $1 AND sec.section_id = $2
                   FOR UPDATE OF o, sch""",
                offering_id,
                section_id,
            )
            if current is None:
                raise HTTPException(status_code=404, detail="Class not found in this section.")
            schedule = await connection.fetchrow(
                """UPDATE schedule
                   SET day_of_the_week = $1, start_time = $2, end_time = $3
                   WHERE schedule_id = $4 RETURNING *""",
                data.day_of_the_week,
                data.start_time,
                data.end_time,
                current["schedule_id"],
            )
            offering = await connection.fetchrow(
                """UPDATE offering
                   SET instructor_id = $1, room_id = $2, max_capacity = $3, class_type = $4
                   WHERE offering_id = $5 RETURNING *""",
                data.instructor_id,
                data.room_id,
                data.max_capacity or current["max_capacity"],
                data.class_type,
                offering_id,
            )
    return {"schedule": dict(schedule), "class": dict(offering)}


@admin_router.delete("/sections/{section_id}/classes/{offering_id}")
async def delete_class_from_section(
    section_id: int,
    offering_id: int,
    pool: asyncpg.Pool = Depends(get_pool),
):
    async with pool.acquire() as connection:
        async with connection.transaction():
            current = await connection.fetchrow(
                """SELECT o.schedule_id
                   FROM offering o
                   JOIN section sec ON sec.section_id = COALESCE(
                       o.section_id,
                       (SELECT sch.section_id FROM schedule sch WHERE sch.schedule_id = o.schedule_id)
                   )
                   WHERE o.offering_id = $1 AND sec.section_id = $2
                   FOR UPDATE OF o""",
                offering_id,
                section_id,
            )
            if current is None:
                raise HTTPException(status_code=404, detail="Class not found in this section.")
            if await connection.fetchval(
                "SELECT EXISTS(SELECT 1 FROM plotting WHERE offering_id = $1)",
                offering_id,
            ):
                raise HTTPException(
                    status_code=409,
                    detail="Cannot delete a class that appears in a plotted schedule.",
                )
            await connection.execute(
                "DELETE FROM offering WHERE offering_id = $1", offering_id
            )
            if current["schedule_id"] is not None:
                await connection.execute(
                    """DELETE FROM schedule
                       WHERE schedule_id = $1
                         AND NOT EXISTS (
                             SELECT 1 FROM offering WHERE schedule_id = $1
                         )""",
                    current["schedule_id"],
                )
    return {"success": True}


@admin_router.put("/sections/{section_id}/publish")
async def publish_section(section_id: int, pool: asyncpg.Pool = Depends(get_pool)):
    section = await pool.fetchrow(
        "UPDATE section SET is_open = TRUE WHERE section_id = $1 RETURNING section_id",
        section_id,
    )
    if section is None:
        raise HTTPException(status_code=404, detail="Section not found.")
    return {"message": "Section published and open for student plotting.", "section_id": section_id}


@admin_router.get("/submissions")
async def list_submissions(pool: asyncpg.Pool = Depends(get_pool)):
    rows = await pool.fetch(
        """SELECT p.plot_id, p.student_id, p.status, a.first_name, a.last_name, a.email,
                  o.offering_id, o.class_type, s.subject_code, s.subject_name,
                  sec.section_name, sch.day_of_the_week, sch.start_time, sch.end_time
           FROM plotting p
           JOIN schedule_submission ss
             ON ss.student_id = p.student_id AND ss.is_submitted = TRUE
           JOIN account a ON a.student_id = p.student_id
           JOIN offering o ON o.offering_id = p.offering_id
           JOIN subject s ON s.subject_id = o.subject_id
           JOIN schedule sch ON sch.schedule_id = o.schedule_id
           JOIN section sec ON sec.section_id = COALESCE(o.section_id, sch.section_id)
           WHERE p.status IN ('Submitted', 'Enrolled')
           ORDER BY p.student_id, s.subject_code"""
    )
    return [dict(row) for row in rows]


@admin_router.patch("/submissions/{plot_id}")
async def review_submission(
    plot_id: int,
    decision: SubmissionDecision,
    pool: asyncpg.Pool = Depends(get_pool),
):
    row = await pool.fetchrow(
        """UPDATE plotting SET status = $2
           WHERE plot_id = $1 AND status IN ('Submitted', 'Enrolled')
             AND EXISTS (
                 SELECT 1 FROM schedule_submission ss
                 WHERE ss.student_id = plotting.student_id AND ss.is_submitted = TRUE
             )
           RETURNING plot_id, student_id, offering_id, status""",
        plot_id,
        decision.status,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Submitted plot not found.")
    return dict(row)


app.include_router(student_auth_router)
app.include_router(student_router)
app.include_router(admin_auth_router)
app.include_router(admin_router)


@app.get("/health", tags=["System"])
async def health_check():
    return {"status": "ok"}
