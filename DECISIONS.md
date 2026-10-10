# Engineering decisions

1. **Indexes.** Employees have unique `(emp_code)`, `(department, joined_on, emp_code)` for department headcounts, and `(joined_on, department)` for unfiltered monthly eligibility. Attendance has unique `(emp_code, date descending)` for daily identity and employee/month lookups, `(date descending, emp_code)` for date scans, `(status, date descending, emp_code)` for status lists, and `(emp_code, punch_in descending)` for punch-out selection. A standalone status index would not support the list order, so I rejected it.

2. **Punch-in race.** Both requests calculate the attendance date and attempt insertion. The unique index admits one; the other receives a duplicate-key error, translated to 409. No application lock or preliminary existence check decides the winner.

3. **Ties.** Rank uses total lateness alone. Filtering by rank happens afterward, so cutoff ties all survive. Employee code orders tied rows without changing their rank.

4. **Headcount.** The summary starts with employees who joined by month end. Each employee contributes one to headcount even when the attendance lookup returns nothing. Work-hour sums and counts preserve record-weighted averages.

5. **At 100x.** I would first measure and address the trend's repeated joins, considering daily department rollups updated when attendance is corrected. Explain and endpoint execution currently share the same query builders.
