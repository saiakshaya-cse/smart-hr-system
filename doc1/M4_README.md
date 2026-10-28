# Attendance & Leave Management (M4): Weeks 1 and 2

Attendance and leave modules of the Smart HRMS.

**Apps:** `apps/attendance/`, `apps/leave_management/`
**Scope so far:** models, migrations and the backend service layer (business rules, validation, permissions, transactions) with unit tests. Employee-facing pages, forms and endpoints belong to M6. Events for notifications are planned for Week 3.

---

## Status

| Item | Status |
|---|---|
| Models and migrations | Done |
| Attendance: check-in, check-out, HR correction with audit, monthly summary | Done |
| Leave: apply, approve, reject, cancel (Pending only), status history | Done |
| Leave balance: checked at submission and approval, deducted at approval | Done |
| Working-day calculation (weekends, public holidays, calendar-year split) | Done |
| Unit tests | Done (see Tests) |
| Django admin registration | Done |
| Rules document (`docs/M4_ATTENDANCE_LEAVE_RULES.md`) | Maintained by the team lead; open items listed there |
| Pages, forms and endpoints that call these services | Not started (M6; to be coordinated) |
| Quota and carry-forward settings | Not started |
| Events for email notifications (M5) | Done |
| Dashboard aggregates | Not started (Week 4) |

---

## Service functions

The business rules live in `services.py` in each app. Other code must call these functions and must not change statuses or balances directly. Every function raises `django.core.exceptions.ValidationError` when a rule is broken.

### `apps/attendance/services.py`

| Function | What it does |
|---|---|
| `check_in(employee, actor)` | Creates today's attendance record with status `PRESENT`. Only the employee's own user can call it, only for an active employee, and only once per day. Runs in a transaction. |
| `check_out(employee, actor)` | Sets today's check-out time. Own user only. Requires a check-in the same day, rejects a second check-out, and requires the check-out to be later than the check-in. |
| `correct_attendance(attendance_id, actor, *, status, check_in, check_out, reason)` | HR only (Admin and employees are rejected). Requires a reason and a valid status, and checks the time order. Stores who corrected it, when and why. The audit event, with old and new values, is recorded by `AuditedModelMixin` on save. |
| `get_monthly_attendance_summary(employee, year, month)` | Counts attendance statuses for one employee in a calendar month, from stored records. Days with no record are not counted. |

### `apps/leave_management/services.py`

| Function | What it does |
|---|---|
| `create_leave_request(employee, leave_type, start_date, end_date, reason)` | Rejects a terminated employee, an inactive leave type, an end date before the start date, and overlap with the employee's Pending or Approved requests. Checks the balance for every year of the request (a missing balance row is rejected) without deducting it. Creates a `PENDING` request. |
| `change_leave_status(leave_request_id, new_status, changed_by, remarks)` | Applies a status change under the rules below, writes a history row and an audit event. Runs in a transaction. |
| `calculate_working_days(start_date, end_date)` | Counts Monday to Friday days, excluding public holidays. |
| `calculate_working_days_by_year(start_date, end_date)` | Same count, split by calendar year. |
| `deduct_leave_balance(leave_request)` | Used by the approval step. Deducts the working days from each year's balance, and deducts nothing unless every year has enough. |

### Rules applied by `change_leave_status`

- `Pending` can move to `Approved`, `Rejected` or `Cancelled`. `Approved`, `Rejected` and `Cancelled` are terminal, so any other change raises a validation error.
- Approve and reject: HR or Admin only, remarks required, and nobody can approve or reject their own request.
- Cancel: only a Pending request, by the employee who owns it or by HR/Admin. Another employee is rejected.
- On approval the balance is checked again under a row lock and then deducted. If it is not enough, the request becomes `Rejected` with a note and no balance changes.
- Approver, approver remarks and decision time are stored only on approval or rejection.
- Every successful change writes a `LeaveStatusHistory` row and an audit event (`APPROVE`, `REJECT`, or `UPDATE` for a cancellation). A failed change writes neither.

Balance restoration is not applicable, because an Approved request cannot be cancelled. The helpers `get_unused_leave_days` and `restore_leave_balance`, and the `restoration_*` fields, are kept unused for a possible policy change.

---

## Models

### `attendance.Attendance`
One record per employee per day: employee, date, check-in, check-out, status (`PRESENT`, `ABSENT`, `LATE`, `HALF_DAY`, `ON_LEAVE`), correction details (`corrected_by`, `corrected_at`, `correction_note`), timestamps. Inherits `AuditedModelMixin`.

### `leave_management`

| Model | Purpose |
|---|---|
| `LeaveType` | Names of leave kinds, entered as data (`name`, `is_active`). |
| `LeaveBalance` | Remaining days per employee, leave type and year. |
| `LeaveRequest` | One application: dates, reason, status, approver, approver remarks, decision time. The `restoration_*` fields are not used at present. |
| `LeaveStatusHistory` | One row for every status change. A signal writes the first row when a request is created. |
| `PublicHoliday` | One row per excluded date (`date`, `name`). |

### Relationships

```
accounts.User <-- employees.Employee (user, optional one-to-one)
                       |
                       +-- Attendance
                       +-- LeaveBalance
                       +-- LeaveRequest <-- LeaveStatusHistory

LeaveType <-- LeaveBalance, LeaveRequest
```

Records belong to `employees.Employee`. Fields that record who performed an action point to the user model through `settings.AUTH_USER_MODEL`.

### Delete behaviour
Employee, leave type and leave request links use `PROTECT`. User links (`corrected_by`, `approver`, `changed_by`) use `SET_NULL`, so the record stays.

---

## Database rules

| Rule | Where |
|---|---|
| One attendance record per employee per date | `Attendance` unique constraint |
| Check-out later than check-in | `Attendance` check constraint |
| Leave end date not before start date | `LeaveRequest` check constraint |
| One balance per employee, leave type and year | `LeaveBalance` unique constraint |
| One public holiday per date | `PublicHoliday` unique field |

---

## Migrations

| App | Migration | Creates |
|---|---|---|
| `attendance` | `0001_initial.py` | `Attendance` |
| `leave_management` | `0001_initial.py` | `LeaveType`, `LeaveBalance`, `LeaveRequest`, `LeaveStatusHistory` |
| `leave_management` | `0002_publicholiday.py` | `PublicHoliday` |
| `leave_management` | `0003_leaverequest_restoration_completed_at_and_more.py` | Restoration fields on `LeaveRequest` (currently unused) |

All were generated with `makemigrations`, and `makemigrations --check` reports no changes. Once merged, an existing migration should not be edited.

---

## Tests

Run the attendance and leave-management tests:

```bash
python manage.py test apps.attendance apps.leave_management
```

Run the full project test suite:

```bash
python manage.py test --verbosity=2
```

Run the PostgreSQL concurrency integration test separately:

```bash
python manage.py test apps.leave_management.tests.LeaveRequestConcurrencyTests --verbosity=2
```

The concurrency integration test requires PostgreSQL. It verifies that concurrent overlapping leave applications for the same employee are serialized using an employee-row lock, and that the second overlapping request is rejected after the first transaction commits.

| Area              |                    Tests | Covers                                                                                                                                                                                                                    |
| ----------------- | -----------------------: | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Attendance        |                       23 | Check-in and check-out success and failure, duplicate prevention, inactive employees, own-user-only rules, HR-only correction, audit events with old and new values, monthly summary and its boundaries.                  |
| Leave management          | 43 discovered | Status transitions and terminal states, role and ownership checks, history, application validation and overlap, balance checks at submission and approval, cross-year balances, working-day calculation and audit events. |
| PostgreSQL concurrency |   Included in the 43 leave tests | PostgreSQL row-lock behavior when two overlapping leave requests are submitted concurrently for the same employee.                                                                                                        |

**Latest verification:**

* Full project test suite: 210 tests discovered, 209 passed and 1 skipped when run using the non-PostgreSQL configuration.
* PostgreSQL concurrency integration test: 1 passed, 0 skipped.
* `python manage.py makemigrations --check`: no changes detected.
* Django system checks: no issues reported.


---

## Known limits

-  **Database concurrency:** The PostgreSQL integration test verifies that concurrent overlapping leave requests for the same employee are serialized using `select_for_update()` inside a transaction. SQLite does not provide equivalent row-lock behavior, so the PostgreSQL integration test must be run against PostgreSQL to verify this scenario.
- Changes made in Django admin skip the service rules. Use admin for viewing and test data only.
- The monthly summary counts stored records only.

---

## Not included yet

- Pages, forms, endpoints and the permission classes on views. Employee dashboard placeholders are not functional integration.
- Quota settings per leave type, carry-forward, and a workflow for balance adjustments (balances can be edited in Django admin).
- `On-leave` attendance records are not created when leave is approved.
- Automatic assignment of `Late` and `Half-day`.
- Events for email notifications (Week 3) and dashboard aggregates (Week 4).

Decisions still open are listed in the rules document.

---

## Notes for other members

- **M6 (frontend):** call the service functions and pass `request.user` as the actor. Do not set statuses or balances directly. Status values are stored in upper case (for example `PENDING`).
- **M5 (notifications):** leave decisions and attendance corrections are stored on `LeaveRequest` and `Attendance`. The event format will be agreed in Week 3.
- **M2 (audit):** attendance corrections are audited through `AuditedModelMixin`. Leave decisions call `log_audit`.
- **Please tell M4 before** renaming `Employee` or changing its primary key, since the foreign keys depend on it.
