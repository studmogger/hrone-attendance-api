# Employee Attendance & Analytics API

FastAPI backend backed by MongoDB implementing strict attendance tracking, business rules (R1 - R10), and aggregation pipelines per `openapi.yaml`.

## Requirements
- Python 3.11+
- MongoDB 6.0+ (or MongoDB Atlas)

## How to Run
```bash
python -m venv .venv
# Linux / macOS:
source .venv/bin/activate
# Windows:
.venv\Scripts\activate

pip install -r requirements.txt

# Configure your MONGO_URI and MONGO_DB in .env or environment variables
# Example:
# MONGO_URI=mongodb+srv://<user>:<password>@cluster.mongodb.net/?retryWrites=true&w=majority
# MONGO_DB=attendance_db

# Seed sample documents (optional)
python sample_seed.py

# Start application
uvicorn app.main:app --port 8000
```

## Running Automated Tests
```bash
python -m pytest -v
```

## Key Implementations
- **Startup**: Database indexes are automatically created at startup idempotently.
- **Rules Engine**: Strict IST timezone calculations, overnight shift date alignment (R1), late minutes floor calculation (R2), overtime threshold (R3), half-up rounding (R4, R8), and half-day detection (R5).
- **Concurrency**: Unique compound database indexes on `(emp_code, date)` and optimistic history versioning for race-safe punch-ins, punch-outs, and regularizations.
- **Analytics**: High-performance MongoDB aggregation pipelines (`/analytics/employees/{emp_code}/monthly`, `/analytics/departments/summary`, `/analytics/leaderboard/late`, `/analytics/departments/{department}/trend`) executed 100% inside database engine without Python loops.
- **Explain Probe**: `/admin/explain/{endpoint}` reports execution stats verifying index scans (`IXSCAN`).
- **Audit & Review**: See `REVIEW.md` for starter bug review and `DECISIONS.md` for architectural decisions.
