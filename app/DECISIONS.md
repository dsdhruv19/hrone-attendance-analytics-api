# DECISIONS.md

1. **Indexes.** I created a unique index on `employees.emp_code` because that business identifier must be unique. The unique `(emp_code, date)` attendance index makes duplicate punch-ins race-safe. The employee department/join-date index supports department headcount and trend queries. Attendance indexes on `(date, emp_code)` and `(emp_code, date, status)` support sorted lists and monthly employee queries; the date/status/late index helps monthly late ranking. The single-column `_id` index was not enough for these access patterns.

2. **Punch-in race.** Both requests may check the employee, but neither relies on a prior attendance lookup. They attempt an insert for the same natural key. MongoDB's unique index allows one insert; the duplicate-key error becomes HTTP 409 for the other request.

3. **Ties.** The leaderboard uses MongoDB `$rank`, not row position. If employees tie at the cutoff rank, every employee with rank less than or equal to `limit` is returned, ordered by late minutes and then employee code.

4. **Headcount.** The department summary starts from eligible employees and looks up their logs. Employees with no logs still contribute one to headcount.

5. **100x data.** I would measure query plans and latency, consider a sharded/partitioned strategy only if measurements justified it, and keep common analytics indexed or pre-aggregated if real-time recomputation became too expensive.
