# Employee Attendance & Analytics API

FastAPI + MongoDB implementation for the HROne Software Engineer Trainee assignment.

## Run locally

Requires Python 3.11+ and MongoDB 6.0+.

```bash
python -m venv .venv
# Windows PowerShell:
.venv\Scripts\Activate.ps1
# macOS/Linux:
# source .venv/bin/activate

pip install -r requirements.txt
$env:MONGO_URI = "mongodb://localhost:27017"
$env:MONGO_DB = "attendance_db"
uvicorn app.main:app --port 8000
```

On macOS/Linux, export `MONGO_URI` and `MONGO_DB` instead of using PowerShell syntax. The application also reads a local `.env` file if you create one; do not commit it. Indexes are created at startup. `GET /health` checks MongoDB readiness and the interactive API docs are at `/docs`.

## Before submission

- Run the API against MongoDB 6.0+ and test the contract's boundary cases, concurrency cases, and analytics.
- Verify each endpoint's response fields and status codes against `openapi.yaml`.
- `REVIEW.md` is **not final**: the supplied upload did not include the original `app/main.py` starter, so starter-specific defects could not be verified. Obtain that file and complete the review table before submitting.
- Check the explain plans against a large dataset. Explain output can vary by MongoDB version and data distribution.
- Do not commit `.env`, credentials, a Dockerfile, virtual environments, or the confidential assignment statement.

## Current scope / caveat

This project is an implementation draft based on the provided contract and data model. It has been syntax-checked, but a live MongoDB integration test and the assignment's hidden-data cases have not been run in this environment. Review and test it locally, and understand the code before submitting.
