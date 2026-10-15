# DECISIONS.md

1. **Indexes.**
We created unique `{emp_code: 1}` on `employees` for identity, `{department: 1, emp_code: 1}` for filtered listing, and `{joined_on: 1}` for headcount period filtering. On `attendance_logs`: unique `{emp_code: 1, date: 1}` to strictly enforce one record per employee/day; `{date: -1, emp_code: 1}` for list paging and sort; `{emp_code: 1, punch_in: -1}` for instant punch-out lookups; and `{date: 1, late_minutes: 1}` for leaderboard scans. We rejected a single-field index on `date`, as compound indexes cover prefix ranges and sorts together.

2. **Punch-in race.**
Both concurrent requests calculate the IST date and execute `insert_one`. The database engine enforces the unique index on `(emp_code, date)`. Exactly one insert commits (returning 201), while the concurrent insert raises `DuplicateKeyError`, which our handler intercepts to return HTTP 409 Conflict.

3. **Ties.**
The pipeline uses `$setWindowFields` with `$rank` for standard competition ranking (1, 2, 2, 4). Filtering `rank <= limit` occurs after ranking, so all tied employees at the cutoff are included. Tied rows are ordered by `emp_code` ascending.

4. **Headcount.**
The aggregation starts on `employees`, matching `joined_on <= month_end`, grouping by department, and summing headcount before joining attendance logs. Employees without attendance logs are fully counted.

5. **100x data.**
I would maintain pre-aggregated daily department rollups and shard `attendance_logs` on `(department, date)` to distribute write throughput and avoid scanning millions of documents on analytics requests.
