# Course Schedule Plotting API

FastAPI service for student accounts and schedule plotting, and authenticated
administrator management of subjects, classes, and schedule submissions.

## Run locally

1. Install the packages in `requirements.txt`.
2. Copy `.env.example` to `.env` and set the database connection values.
3. Start the service with `uvicorn main:app --reload`.

Interactive API documentation is available at `/docs`; liveness is available
at `/health`.

## Authentication

`POST /students/register`, `POST /students/login`, and `POST /admin/login` are
public so users can create accounts or sign in. Login sets an HttpOnly session
cookie that expires after eight hours; the browser sends it automatically.
Every other student or admin endpoint requires the matching session role.
Students can access only their own records.

Sessions are invalidated if malformed, expired, or associated with a deleted
student or inactive/deleted admin. Missing or invalid sessions return `401`.
`POST /students/logout` and `POST /admin/logout` clear the session cookie.
`SESSION_SECRET` signs the cookie contents; it is a private server setting,
not a user-facing access token. Existing `AUTH_TOKEN_SECRET` and
`ADMIN_TOKEN_SECRET` settings are accepted as migration fallbacks.

### Create the initial admin account

Run this once in the PostgreSQL SQL editor. It stores a bcrypt hash, not the
plaintext password. `pgcrypto` provides PostgreSQL's `crypt` and `gen_salt`
functions; if the extension is already enabled, the first statement is a no-op.


Then sign in with username `Admin1` and password `AdminTest123` at
`POST /admin/login`. Change this sample password after the first login.

## Use-case examples

Examples use `http://localhost:8000` as the base URL. Passwords and identifiers
are sample values only.

### Student registration and sign-in

```http
POST /students/register
Content-Type: application/json

{
  "student_id": "202400123",
  "email": "student@phinmaed.com",
  "password": "sample-password-123",
  "first_name": "Alex",
  "last_name": "Santos",
  "year_level": 2
}
```

**201 Created**

```json
{
  "student_id": "202400123",
  "email": "student@phinmaed.com",
  "first_name": "Alex",
  "last_name": "Santos",
  "year_level": 2
}
```

Sign in with `POST /students/login` and `{"email":"student@phinmaed.com","password":"sample-password-123"}`.
Successful sign-in returns `200 OK` with `success`, `message`, and a `user`
object. Login also sets the session cookie. Browsers keep and send it
automatically on subsequent `/students` requests.

```json
{
  "success": true,
  "message": "Login successful.",
  "user": {
    "student_id": "202400123",
    "first_name": "Alex",
    "last_name": "Santos",
    "email": "student@phinmaed.com",
    "year_level": 2
  }
}
```

Invalid credentials return `401`. Requests without a valid session return
`401`; attempting to use one student's session to access another student's ID
returns `403`. Call `POST /students/logout` to clear the cookie.

### Find subjects, plot a class, and submit the schedule

The browser sends the login session cookie with each request:

`GET /students/subjects?year_level=2&semester=1` and
`GET /students/schedules` return available subject/class data (`200 OK`).
Plot a class with:

```http
POST /students/202400123/plots
Content-Type: application/json

{"offering_id": 42}
```

**201 Created**

```json
{
  "plot_id": 17,
  "student_id": "202400123",
  "offering_id": 42,
  "status": "Draft"
}
```

Read the student's plotted classes with `GET /students/202400123/plots`
(`200 OK`). Submit all draft plots to the administrator with
`POST /students/202400123/submit` (no request body).

**200 OK**

```json
{
  "message": "Schedule submitted to the admin.",
  "plots": [
    {"plot_id": 17, "offering_id": 42, "status": "Submitted"}
  ]
}
```

Duplicate or closed/full class plots return `409 Conflict`; submitting when
there are no drafts also returns `409`.

### Export the plotted schedule

```http
GET /students/202400123/plots/pdf
```

**200 OK**, `Content-Type: application/pdf`, with a downloadable
`schedule-202400123.pdf` containing the student's plotted classes and statuses.
If the student ID is not associated with an account, the response is
`404 Not Found`. A student with no plots receives a PDF that says no classes
have been plotted.

### UC-07: Administrator sign-in and protected actions

```http
POST /admin/login
Content-Type: application/json

{"username":"Admin1","password":"AdminTest123"}
```

**200 OK**

```json
{
  "success": true,
  "admin": {
    "admin_id": 1,
    "username": "Admin1",
    "display_name": "Admin1",
    "is_active": true
  }
}
```

The login response sets an HttpOnly session cookie. For example,
`GET /admin/sections` returns `200 OK` and the section list in the same browser
session. Call `POST /admin/logout` to clear the cookie.
`PUT /admin/password` also requires the session and a request body such as
`{"username":"Admin1","current_password":"AdminTest123","new_password":"new-sample-password-123"}`.

### Administrator catalog, rooms, instructors, and sections

After signing in, the browser includes the session cookie for these operations:

| Operation | Request | Success |
|---|---|---:|
| List subjects | `GET /admin/subjects` | 200 |
| Create a subject | `POST /admin/subjects` with `{"subject_code":"CS101","subject_name":"Intro to Computing","year_level":1,"semester":1}` | 201 |
| Edit a subject | `PUT /admin/subjects/{subject_id}` with the same subject fields | 200 |
| Delete a subject | `DELETE /admin/subjects/{subject_id}` | 200 |
| Set semester | `PUT /admin/semester` with `{"semester":2}` | 200 |
| List instructors | `GET /admin/instructors` | 200 |
| Create an instructor | `POST /admin/instructors` with `{"instructor_name":"Jordan Lee"}` | 201 |
| Edit an instructor | `PUT /admin/instructors/{instructor_id}` with `{"instructor_name":"Jordan Lee"}` | 200 |
| Delete an instructor | `DELETE /admin/instructors/{instructor_id}` | 200 |
| List rooms | `GET /admin/rooms` | 200 |
| Create a room | `POST /admin/rooms` with `{"building":"North Hall","room_num":"204"}` | 201 |
| Edit a room | `PUT /admin/rooms/{room_id}` with `{"building":"North Hall","room_num":"205"}` | 200 |
| Delete a room | `DELETE /admin/rooms/{room_id}` | 200 |
| List students | `GET /admin/students` | 200 |
| List sections | `GET /admin/sections` | 200 |
| Create a section | `POST /admin/sections` with `{"subject_id":1,"section_name":"A","max_capacity":30}` | 201 |
| Edit a section | `PUT /admin/sections/{section_id}` with the section fields | 200 |
| Open/close a section | `PUT /admin/sections/{section_id}/availability` with `{"is_open":true}` | 200 |
| Publish a section | `PUT /admin/sections/{section_id}/publish` | 200 |
| Delete a section | `DELETE /admin/sections/{section_id}` | 200 |

Deleting an instructor or room assigned to a class returns `409 Conflict`.
Unknown resource IDs return `404 Not Found`. Validation failures return `422`.
For example, `POST /admin/rooms` with
`{"building":"North Hall","room_num":"204"}` returns **201 Created**:

```json
{"room_id": 2, "building": "North Hall", "room_num": "204"}
```

### UC-08/UC-09: Manage classes and review submitted plots

Create a class in a section with
`POST /admin/sections/{section_id}/classes`:

```json
{
  "instructor_id": 3,
  "room_id": 2,
  "day_of_the_week": "Monday",
  "start_time": "09:00:00",
  "end_time": "10:30:00",
  "class_type": "Lecture",
  "max_capacity": 30
}
```

**201 Created**

```json
{
  "schedule": {
    "schedule_id": 12,
    "day_of_the_week": "Monday",
    "start_time": "09:00:00",
    "end_time": "10:30:00",
    "section_id": 1
  },
  "class": {
    "offering_id": 42,
    "subject_id": 1,
    "room_id": 2,
    "instructor_id": 3,
    "max_capacity": 30,
    "schedule_id": 12,
    "section_id": 1,
    "class_type": "Lecture"
  }
}
```

Edit a class
using `PUT /admin/sections/{section_id}/classes/{offering_id}` and the same
request fields; delete it using
`DELETE /admin/sections/{section_id}/classes/{offering_id}`. Both successful
operations return `200 OK`. A class already used in a plotted schedule cannot
be deleted (`409 Conflict`).

`GET /admin/submissions` lists submitted plots (`200 OK`). Review one with
`PATCH /admin/submissions/{plot_id}` and
`{"status":"Approved"}` or `{"status":"Rejected"}`. Success returns `200 OK`
with the updated plot, for example:

```json
{"plot_id":17,"student_id":"202400123","offering_id":42,"status":"Approved"}
```

A plot that is not awaiting review returns `404`.

## Endpoint status summary

| Endpoint | Success | Common error responses |
|---|---:|---|
| `GET /` and `GET /health` | 200 | — |
| `POST /students/register` | 201 | 409 duplicate student/email; 422 invalid input |
| `POST /students/login` | 200 | 401 invalid credentials |
| `POST /students/logout`, `POST /admin/logout` | 200 | — |
| `PUT /students/password` | 200 | 400 incorrect current password |
| `PUT /students/year-level` | 200 | 404 student not found |
| `GET /students/subjects`, `/students/schedules` | 200 | 422 invalid query |
| `GET /students/{student_id}/profile` | 200 | 404 student not found; 422 invalid ID |
| `GET /students/{student_id}/plots` | 200 | 422 invalid ID |
| `GET /students/{student_id}/plots/pdf` | 200 PDF | 404 student not found; 422 invalid ID |
| `POST /students/{student_id}/plots` | 201 | 404 class not found; 409 closed/full/duplicate; 422 invalid input |
| `POST /students/{student_id}/submit` | 200 | 409 no draft classes |
| `POST /admin/login` | 200 | 401 invalid credentials; 503 session secret not configured |
| Protected `/admin/*` routes | varies by operation | 401 missing/invalid session; 404 missing resource; 422 invalid input |

All admin subject, instructor, room, student, section, class, semester, and
submission endpoints require an admin session cookie. The login and logout
endpoints are public.
Student subjects, schedules, profile, plots, PDF export, password, year-level,
and submit endpoints require a student session cookie.
