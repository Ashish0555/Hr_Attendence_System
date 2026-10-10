# Employee Attendance and Analytics API

Python 3.11+, FastAPI, PyMongo, and MongoDB 6.0+.
All application code is in `app/main.py`; the HTTP contract is `openapi.yaml`.

## Run

With MongoDB running locally, from the repository root:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export MONGO_URI='mongodb://localhost:27017'
export MONGO_DB='attendance_db'
uvicorn app.main:app --port 8000
```

An optional `.env` is supported; existing environment variables take priority.
Indexes are created at startup. `GET /health` pings MongoDB and returns 503 if it is unavailable.

`python sample_seed.py` is optional: it **replaces both collections** with six employees and nine attendance logs. Use it only in a disposable database.

## Verify

```bash
pip install -r requirements-dev.txt
TEST_MONGO_URI='mongodb://localhost:27017' python -m pytest -q
TEST_MONGO_URI='mongodb://localhost:27017' HRONE_SCALE_TEST=1 python -m pytest -q -s
```

Tests require real MongoDB. They create a randomly named `hrone_test_...` database and remove only that database afterward. The scale check inserts 100,000 records and verifies startup indexing and execution plans, including lookup scans.

See `REVIEW.md` for the starter review and `DECISIONS.md` for indexing, concurrency, and aggregation decisions.
