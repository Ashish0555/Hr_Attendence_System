# Starter review

Locations below refer to the original 162-line `app/main.py`. Regression cases are in `tests/test_api.py`.

| # | Where | Defect | Concrete symptom or check | Fix |
|---|---|---|---|---|
| 1 | MongoClient, line 22 | Default reads produce naive UTC datetimes; helpers mix them with aware IST values. | Subtracting a seeded naive datetime from an aware shift end raises `TypeError`. | Read timezone-aware UTC datetimes and use explicit IST shift boundaries. |
| 2 | Startup | No unique employee index. | Eight simultaneous identical creates can insert duplicates. | Create a unique employee-code index at startup; translate duplicate-key errors to 409. |
| 3 | Startup | No unique daily-attendance index. | Parallel punch-ins can create multiple records for one day. | Unique employee/date index and direct insertion with conflict handling. |
| 4 | Startup | No indexes for listing, joins, or analytics. | Explain can report collection scans on the large dataset. | Add indexes for the actual query patterns; share query builders with explain. |
| 5 | `compute_late_minutes`, lines 35–37 | Floors minutes before testing the grace boundary. | 09:40:01 with a 09:30 shift returns 0 instead of 10. | Compare elapsed seconds against 600, then floor minutes. |
| 6 | `compute_late_minutes`, line 35 | Anchors shift start to the punch's calendar date. | A 00:05 overnight punch belongs to the previous day's 22:00 shift. | Pass the resolved attendance date explicitly. |
| 7 | `compute_work_hours`, line 41 | Binary float rounding does not implement decimal half-up. | 16,074 seconds is 4.465 hours and must report 4.47. | Divide integer seconds using Decimal and quantize half-up. |
| 8 | `compute_overtime`, line 46 | Shift end always falls on the attendance date. | 22:00–06:00 with next-day 06:40 punch-out yields 1,480 instead of 40 minutes. | Advance the end date for overnight shifts. |
| 9 | `compute_overtime`, line 47 | Missing the 30-minute minimum. | Five minutes after shift end incorrectly counts as overtime. | Count only floored durations of at least 30 minutes. |
| 10 | `EmployeeIn`, lines 53–60 | Missing code, length, email, clock, calendar-date, and unequal-shift validation. | Bad email, February 30, 24:00, or identical shift times are accepted. | Add field constraints and a cross-field shift check. |
| 11 | `PunchInIn`, lines 65–66 | Timestamp coercion/ranges and presence statuses are unchecked. | Seconds, numeric strings, integral floats, null, or ABSENT can be accepted. | Strict bounded integers; omitted timestamp defaults to now; presence-only status. |
| 12 | `health`, line 74 | Always reports readiness. | Returns 200 during a database outage. | Ping MongoDB; database failures return 503. |
| 13 | `create_employee`, lines 79–83 | Check-then-insert is not an atomic uniqueness guarantee. | Two requests both pass the initial lookup. | Let the unique index arbitrate insertion. |
| 14 | `create_employee`, line 82 | Host-local naive creation time. | Non-UTC hosts can store local wall time as UTC. | Generate an aware UTC instant truncated to seconds. |
| 15 | Employee and attendance responses | Datetimes are returned without epoch-millisecond conversion. | Timestamps become ISO strings, including nested history values. | Recursively convert datetime values at the API boundary. |
| 16 | `list_employees`, line 93 | Off-by-one page offset. | Page 1 skips the first 20 employees. | Use `(page - 1) * page_size`. |
| 17 | `list_employees`, line 94 | Total ignores the department filter. | Filtered page reports company-wide headcount. | Count with the same filter. |
| 18 | `list_employees`, line 95 | No explicit employee-code ordering. | Pages depend on insertion/query order. | Sort by employee code before pagination. |
| 19 | Both list endpoints | Missing page bounds and size limit. | Page 0 or page size 101 is accepted. | Validate page >= 1 and size 1–100. |
| 20 | `punch_in`, lines 101, 113 | Missing employee is dereferenced. | Unknown code raises a server error. | Return 404 before calculating attendance. |
| 21 | `punch_in`, lines 102–103 | Host-local timestamp conversion and calendar date. | Results depend on server timezone and fail overnight attribution. | Decode milliseconds into UTC and derive the attendance day in IST. |
| 22 | `punch_in`, line 102 | Truthiness substitutes now for zero; fractional seconds survive. | Invalid zero silently becomes now; subsecond values are stored. | Validate timestamp bounds and truncate valid inputs before any calculation. |
| 23 | `punch_in`, lines 104–118 | Daily existence check races with insertion. | Simultaneous requests can both succeed. | Atomic unique-index enforcement gives exactly one 201. |
| 24 | `punch_in` / `list_attendance`, lines 119, 149 | Exposes MongoDB identity as `id`. | Responses violate the natural-key contract. | Return employee/date identity and omit internal IDs. |
| 25 | `list_attendance`, lines 137–143 | Dates, status, and reversed ranges are not validated. | Invalid calendar dates or `date_from > date_to` return plausible empty results. | Validate before querying; return the required 422 body. |
| 26 | `list_attendance`, lines 144–147 | Loads and sorts all matches before slicing. | Memory and response cost grow with all matching documents. | Perform indexed sorting and pagination in MongoDB; count separately. |
| 27 | `list_attendance`, line 145 | Missing employee-code tie-breaker. | Same-day rows have unstable ordering. | Sort by date descending, then employee code ascending. |
| 28 | `list_attendance`, line 150 | Legacy defaults are omitted. | The supplied legacy record lacks required `history` and `half_day` response fields. | Fill missing fields at the response boundary without rewriting seeded data. |

## Punch-out interpretation

Punch-out selects the latest eligible record and returns 409 if it is already closed. A timestamp before all recorded punches is treated as a reversed punch (422), while an employee with no punch-in receives 404.

## Examined and retained

- Synchronous routes with synchronous PyMongo are appropriate here; converting only the routes to async would block the event loop during database calls.
- `load_dotenv()` already preserves existing environment variables by default.
- Calendar dates and shift clocks stay strings, as required by the stored-data contract.
- Missing attendance records are not converted into ABSENT records.
- Pydantic's default handling of extra fields intentionally ignores client-supplied derived values.
- Absence of authentication is within assignment scope; correction authors are free text.
