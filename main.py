import os
from contextlib import asynccontextmanager
from datetime import time
from typing import Literal

import asyncpg
import bcrypt
from dotenv import load_dotenv
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request, status
from pydantic import BaseModel, Field, field_validator, model_validator

load_dotenv()


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = {
        "user": os.getenv("DB_USER"),
        "password": os.getenv("DB_PASSWORD"),
        "host": os.getenv("DB_HOST"),
        "port": os.getenv("DB_PORT"),
        "database": os.getenv("DB_NAME"),
    }
    if not all(settings.values()):
        raise RuntimeError("Set DB_USER, DB_PASSWORD, DB_HOST, DB_PORT, and DB_NAME in .env.")

    settings["port"] = int(settings["port"])
    app.state.pool = await asyncpg.create_pool(statement_cache_size=0, **settings)
    try:
        yield
    finally:
        await app.state.pool.close()


app = FastAPI(title="Course Schedule Plotting", version="1.0.0", lifespan=lifespan)
student_router = APIRouter(prefix="/students", tags=["Students"])
admin_router = APIRouter(prefix="/admin", tags=["Admin"])


@app.get("/")
async def home():
    return {
        "message": "Course Schedule Plotting API",
        "docs": "/docs",
        "health": "/health",
    }


class StudentAccountCreate(BaseModel):
    student_id: str = Field(min_length=1, max_length=50)
    email: str = Field(min_length=3, max_length=255)
    password: str = Field(min_length=8, max_length=72)
    first_name: str = Field(min_length=1, max_length=100)
    last_name: str = Field(min_length=1, max_length=100)
    year_level: int | None = Field(default=None, ge=1, le=5)

    @field_validator("student_id", mode="before")
    @classmethod
    def remove_student_id_dashes(cls, student_id: str) -> str:
        return student_id.replace("-", "") if isinstance(student_id, str) else student_id

    @field_validator("student_id")
    @classmethod
    def validate_student_id(cls, student_id: str) -> str:
        if not student_id or not all("0" <= character <= "9" for character in student_id):
            raise ValueError("Student ID must contain numbers only; hyphens are ignored.")
        return student_id

    @field_validator("password")
    @classmethod
    def validate_password_size(cls, password: str) -> str:
        if len(password.encode("utf-8")) > 72:
            raise ValueError("Password must be no more than 72 UTF-8 bytes.")
        return password


class PlotCreate(BaseModel):
    offering_id: int = Field(gt=0)


class SubjectCreate(BaseModel):
    subject_code: str = Field(min_length=1, max_length=50)
    subject_name: str = Field(min_length=1, max_length=255)
    year_level: int | None = Field(default=None, ge=1, le=5)


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


def get_pool(request: Request) -> asyncpg.Pool:
    pool = getattr(request.app.state, "pool", None)
    if pool is None:
        raise HTTPException(status_code=503, detail="Database is not available.")
    return pool


def normalize_student_id(student_id: str) -> str:
    return student_id.replace("-", "")


@student_router.post("/register", status_code=status.HTTP_201_CREATED)
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


@student_router.get("/{student_id}/profile")
async def student_profile(student_id: str, pool: asyncpg.Pool = Depends(get_pool)):
    student_id = normalize_student_id(student_id)
    row = await pool.fetchrow(
        """SELECT student_id, email, first_name, last_name, year_level
           FROM account WHERE student_id = $1""",
        student_id,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Student account not found.")
    return dict(row)


@student_router.get("/schedules")
async def available_schedules(pool: asyncpg.Pool = Depends(get_pool)):
    rows = await pool.fetch(
        """SELECT o.offering_id, s.subject_code, s.subject_name, sec.section_name,
                  o.class_type, o.max_capacity, i.instructor_name,
                  r.building, r.room_num, sch.day_of_the_week,
                  sch.start_time, sch.end_time
           FROM offering o
           JOIN subject s ON s.subject_id = o.subject_id
           JOIN section sec ON sec.section_id = o.section_id
           JOIN schedule sch ON sch.schedule_id = o.schedule_id
           JOIN admin_section_draft draft ON draft.section_id = sec.section_id
           LEFT JOIN instructor i ON i.instructor_id = o.instructor_id
           LEFT JOIN room r ON r.room_id = o.room_id
           WHERE sec.is_open = TRUE AND draft.published = TRUE
           ORDER BY s.subject_code, sec.section_name, sch.day_of_the_week, sch.start_time"""
    )
    return [dict(row) for row in rows]


@student_router.get("/{student_id}/plots")
async def student_plots(student_id: str, pool: asyncpg.Pool = Depends(get_pool)):
    student_id = normalize_student_id(student_id)
    rows = await pool.fetch(
        """SELECT p.plot_id, p.status, o.offering_id, o.class_type,
                  s.subject_code, s.subject_name, sec.section_name,
                  sch.day_of_the_week, sch.start_time, sch.end_time
           FROM plotting p
           JOIN offering o ON o.offering_id = p.offering_id
           JOIN subject s ON s.subject_id = o.subject_id
           JOIN section sec ON sec.section_id = o.section_id
           JOIN schedule sch ON sch.schedule_id = o.schedule_id
           WHERE p.student_id = $1 ORDER BY p.plot_id""",
        student_id,
    )
    return [dict(row) for row in rows]


@student_router.post("/{student_id}/plots", status_code=status.HTTP_201_CREATED)
async def plot_schedule(
    student_id: str,
    data: PlotCreate,
    pool: asyncpg.Pool = Depends(get_pool),
):
    student_id = normalize_student_id(student_id)
    async with pool.acquire() as connection:
        async with connection.transaction():
            offering = await connection.fetchrow(
                """SELECT o.offering_id, o.max_capacity, o.section_id
                   FROM offering o WHERE o.offering_id = $1 FOR UPDATE""",
                data.offering_id,
            )
            if offering is None:
                raise HTTPException(status_code=404, detail="Class schedule not found.")
            section_is_open = await connection.fetchval(
                """SELECT sec.is_open AND draft.published
                   FROM section sec JOIN admin_section_draft draft
                     ON draft.section_id = sec.section_id
                   WHERE sec.section_id = $1""",
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


@student_router.post("/{student_id}/submit")
async def submit_plotted_schedule(student_id: str, pool: asyncpg.Pool = Depends(get_pool)):
    student_id = normalize_student_id(student_id)
    rows = await pool.fetch(
        """UPDATE plotting SET status = 'Submitted'
           WHERE student_id = $1 AND status = 'Draft'
           RETURNING plot_id, offering_id, status""",
        student_id,
    )
    if not rows:
        raise HTTPException(status_code=409, detail="There are no draft classes to submit.")
    return {"message": "Schedule submitted to the admin.", "plots": [dict(row) for row in rows]}


@admin_router.post("/subjects", status_code=status.HTTP_201_CREATED)
async def create_subject(data: SubjectCreate, pool: asyncpg.Pool = Depends(get_pool)):
    row = await pool.fetchrow(
        """INSERT INTO subject (subject_code, subject_name, year_level)
           VALUES ($1, $2, $3) RETURNING *""",
        data.subject_code,
        data.subject_name,
        data.year_level,
    )
    return dict(row)


@admin_router.get("/subjects")
async def list_subjects(pool: asyncpg.Pool = Depends(get_pool)):
    rows = await pool.fetch("SELECT * FROM subject ORDER BY subject_code")
    return [dict(row) for row in rows]


@admin_router.post("/instructors", status_code=status.HTTP_201_CREATED)
async def create_instructor(data: InstructorCreate, pool: asyncpg.Pool = Depends(get_pool)):
    row = await pool.fetchrow(
        "INSERT INTO instructor (instructor_name) VALUES ($1) RETURNING *",
        data.instructor_name,
    )
    return dict(row)


@admin_router.get("/instructors")
async def list_instructors(pool: asyncpg.Pool = Depends(get_pool)):
    rows = await pool.fetch("SELECT * FROM instructor ORDER BY instructor_name")
    return [dict(row) for row in rows]


@admin_router.post("/rooms", status_code=status.HTTP_201_CREATED)
async def create_room(data: RoomCreate, pool: asyncpg.Pool = Depends(get_pool)):
    row = await pool.fetchrow(
        "INSERT INTO room (building, room_num) VALUES ($1, $2) RETURNING *",
        data.building,
        data.room_num,
    )
    return dict(row)


@admin_router.get("/rooms")
async def list_rooms(pool: asyncpg.Pool = Depends(get_pool)):
    rows = await pool.fetch("SELECT * FROM room ORDER BY building, room_num")
    return [dict(row) for row in rows]


@admin_router.get("/students")
async def list_students(pool: asyncpg.Pool = Depends(get_pool)):
    rows = await pool.fetch(
        """SELECT student_id, first_name, last_name, email, year_level
           FROM account ORDER BY last_name, first_name, student_id"""
    )
    return [dict(row) for row in rows]


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
            await connection.execute(
                """INSERT INTO admin_section_draft (section_id, subject_id, published)
                   VALUES ($1, $2, FALSE)""",
                section["section_id"],
                data.subject_id,
            )
    return dict(section)


@admin_router.get("/sections")
async def list_sections(pool: asyncpg.Pool = Depends(get_pool)):
    rows = await pool.fetch(
        """SELECT sec.section_id, sec.section_name, sec.subject_id, s.subject_code,
                  s.subject_name, sec.max_capacity, sec.is_open, draft.published
           FROM section sec
           JOIN subject s ON s.subject_id = sec.subject_id
           LEFT JOIN admin_section_draft draft ON draft.section_id = sec.section_id
           ORDER BY s.subject_code, sec.section_name"""
    )
    return [dict(row) for row in rows]


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


@admin_router.put("/sections/{section_id}/publish")
async def publish_section(section_id: int, pool: asyncpg.Pool = Depends(get_pool)):
    async with pool.acquire() as connection:
        async with connection.transaction():
            draft = await connection.fetchrow(
                """UPDATE admin_section_draft SET published = TRUE
                   WHERE section_id = $1 RETURNING section_id""",
                section_id,
            )
            if draft is None:
                raise HTTPException(status_code=404, detail="Section draft not found.")
            await connection.execute(
                "UPDATE section SET is_open = TRUE WHERE section_id = $1", section_id
            )
    return {"message": "Section published and open for student plotting.", "section_id": section_id}


@admin_router.get("/submissions")
async def list_submissions(pool: asyncpg.Pool = Depends(get_pool)):
    rows = await pool.fetch(
        """SELECT p.plot_id, p.student_id, a.first_name, a.last_name, a.email,
                  o.offering_id, o.class_type, s.subject_code, s.subject_name,
                  sec.section_name, sch.day_of_the_week, sch.start_time, sch.end_time
           FROM plotting p
           JOIN account a ON a.student_id = p.student_id
           JOIN offering o ON o.offering_id = p.offering_id
           JOIN subject s ON s.subject_id = o.subject_id
           JOIN section sec ON sec.section_id = o.section_id
           JOIN schedule sch ON sch.schedule_id = o.schedule_id
           WHERE p.status = 'Submitted'
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
           WHERE plot_id = $1 AND status = 'Submitted'
           RETURNING plot_id, student_id, offering_id, status""",
        plot_id,
        decision.status,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Submitted plot not found.")
    return dict(row)


app.include_router(student_router)
app.include_router(admin_router)


@app.get("/health", tags=["System"])
async def health_check():
    return {"status": "ok"}
