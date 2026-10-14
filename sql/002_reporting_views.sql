/*
  th2-metrics-db — reporting layer (schema rpt): the business rules live here,
  once, instead of being re-implemented in every API endpoint.

  - Local dates use meta.settings.report_timezone (a SQL Server / Windows tz
    name; 'Pacific Standard Time' covers PST and PDT).
  - "Open" = closed_flag 0 AND the status isn't a closed-type status (some
    legacy tickets have closedFlag=false with a "Closed"/"Resolved*" status).
  - Unassigned tickets never count toward a tech.
  - Project tickets aren't in cw.tickets at all (excluded at sync).
  Idempotent: safe to re-run.
*/
IF SCHEMA_ID('rpt') IS NULL EXEC('CREATE SCHEMA rpt');
GO

IF OBJECT_ID('meta.settings') IS NULL
BEGIN
    CREATE TABLE meta.settings (
        id               TINYINT       NOT NULL PRIMARY KEY DEFAULT 1 CHECK (id = 1),
        report_timezone  NVARCHAR(64)  NOT NULL
    );
    INSERT INTO meta.settings (id, report_timezone) VALUES (1, 'Pacific Standard Time');
END;

-- Closed-type status names (lower-case), matching aggregate.CLOSED_STATUSES.
IF OBJECT_ID('meta.closed_status_names') IS NULL
BEGIN
    CREATE TABLE meta.closed_status_names (name NVARCHAR(100) NOT NULL PRIMARY KEY);
    INSERT INTO meta.closed_status_names (name) VALUES
        ('closed'), ('resolved*'), ('resolved'), ('canceled'), ('cancelled'),
        ('no communication'), ('qc hold'), ('qc/no communication');
END;

-- "Waiting on first touch" statuses, matching aggregate.WAITING_STATUSES.
IF OBJECT_ID('meta.waiting_status_names') IS NULL
BEGIN
    CREATE TABLE meta.waiting_status_names (name NVARCHAR(100) NOT NULL PRIMARY KEY);
    INSERT INTO meta.waiting_status_names (name) VALUES
        ('new'), ('assigned*'), ('assigned'), ('client responded');
END;

-- Calendar for capacity math. Holidays aren't in ConnectWise's API in a form
-- we've confirmed, so is_holiday is maintained by hand (UPDATE meta.calendar
-- SET is_holiday = 1 WHERE cal_date IN (...)).
IF OBJECT_ID('meta.calendar') IS NULL
BEGIN
    CREATE TABLE meta.calendar (
        cal_date    DATE NOT NULL PRIMARY KEY,
        is_weekday  BIT  NOT NULL,
        is_holiday  BIT  NOT NULL DEFAULT 0
    );
    WITH d AS (
        SELECT CAST('2025-01-01' AS DATE) AS cal_date
        UNION ALL SELECT DATEADD(DAY, 1, cal_date) FROM d WHERE cal_date < '2030-12-31'
    )
    INSERT INTO meta.calendar (cal_date, is_weekday)
    -- DATEDIFF from a known Monday (1900-01-01) avoids depending on DATEFIRST.
    SELECT cal_date, CASE WHEN DATEDIFF(DAY, '19000101', cal_date) % 7 < 5 THEN 1 ELSE 0 END
    FROM d OPTION (MAXRECURSION 0);
END;

-- Extend back to 2010: the sync pulls every ticket UPDATED since 2025, which
-- includes tickets entered as far back as 2010 (and old still-open ones), so
-- their dates need calendar rows. Monthly rollups still start at 2025-01-01
-- (the synced-history start), see v_utilization_monthly / v_agreement_mrr_monthly.
IF NOT EXISTS (SELECT 1 FROM meta.calendar WHERE cal_date = '2010-01-01')
BEGIN
    WITH d AS (
        SELECT CAST('2010-01-01' AS DATE) AS cal_date
        UNION ALL SELECT DATEADD(DAY, 1, cal_date) FROM d WHERE cal_date < '2024-12-31'
    )
    INSERT INTO meta.calendar (cal_date, is_weekday)
    SELECT cal_date, CASE WHEN DATEDIFF(DAY, '19000101', cal_date) % 7 < 5 THEN 1 ELSE 0 END
    FROM d WHERE NOT EXISTS (SELECT 1 FROM meta.calendar c WHERE c.cal_date = d.cal_date)
    OPTION (MAXRECURSION 0);
END;
GO

/* --------------------------------------------------------------- tickets */
CREATE OR ALTER VIEW rpt.v_tickets AS
SELECT
    t.*,
    CAST((t.date_entered AT TIME ZONE 'UTC') AT TIME ZONE s.report_timezone AS DATE) AS entered_local_date,
    CAST((t.closed_date  AT TIME ZONE 'UTC') AT TIME ZONE s.report_timezone AS DATE) AS closed_local_date,
    CASE WHEN t.closed_flag = 0
              AND ISNULL(bs.closed_status, 0) = 0
              AND cs.name IS NULL THEN 1 ELSE 0 END                         AS is_open,
    CASE WHEN ws.name IS NOT NULL THEN 1 ELSE 0 END                          AS is_waiting,
    COALESCE(NULLIF(mo.display_name, ''), t.owner_identifier)                AS owner_name,
    -- Who actually closed it. Owner is only set on ~25% of tickets and
    -- differs from closedBy on about half of closed ones, so closedBy is the
    -- per-tech "closed" credit; owner/resolvedBy are fallbacks.
    COALESCE(t.closed_by, t.resolved_by, t.owner_identifier)                 AS closer_identifier,
    -- Calendar-hour durations. respond/resolve_minutes from ConnectWise are
    -- the SLA-clock (business-hours) equivalents — use those for SLA.
    DATEDIFF(MINUTE, t.date_entered, t.date_responded) / 60.0                AS hours_to_respond,
    DATEDIFF(MINUTE, t.date_entered, COALESCE(t.date_resolved, t.closed_date)) / 60.0 AS hours_to_resolve,
    tgt.respond_hours      AS sla_respond_target_hours,
    tgt.resolution_hours   AS sla_resolve_target_hours,
    CASE WHEN t.respond_minutes IS NULL OR tgt.respond_hours IS NULL THEN NULL
         WHEN t.respond_minutes <= tgt.respond_hours * 60 THEN 1 ELSE 0 END  AS met_respond_sla,
    CASE WHEN t.resolve_minutes IS NULL OR tgt.resolution_hours IS NULL THEN NULL
         WHEN t.resolve_minutes <= tgt.resolution_hours * 60 THEN 1 ELSE 0 END AS met_resolve_sla
FROM cw.tickets t
CROSS JOIN meta.settings s
LEFT JOIN cw.board_statuses bs ON bs.status_id = t.status_id
LEFT JOIN meta.closed_status_names cs ON cs.name = LOWER(t.status_name)
LEFT JOIN meta.waiting_status_names ws ON ws.name = LOWER(t.status_name)
LEFT JOIN cw.members mo ON mo.identifier = t.owner_identifier
LEFT JOIN cw.sla_priority_targets tgt ON tgt.sla_id = t.sla_id AND tgt.priority_id = t.priority_id
WHERE t.deleted = 0;
GO

/* ------------------------------------------------------- time / labor */
CREATE OR ALTER VIEW rpt.v_time_entries AS
SELECT
    e.*,
    CAST((e.time_start AT TIME ZONE 'UTC') AT TIME ZONE s.report_timezone AS DATE) AS local_date,
    COALESCE(NULLIF(m.display_name, ''), e.member_identifier)   AS member_name,
    ISNULL(wt.utilization_flag, 0)                               AS counts_toward_utilization,
    wt.accrual_type,
    -- TH2's "PTO/Holiday" work type has no accrual type set in ConnectWise,
    -- so PTO is also recognized by work type name.
    CASE WHEN wt.accrual_type IS NOT NULL
           OR e.work_type_name LIKE '%PTO%' OR e.work_type_name LIKE '%Holiday%'
           OR e.work_type_name LIKE '%Vacation%' OR e.work_type_name LIKE '%Sick%'
         THEN 1 ELSE 0 END                                       AS is_pto,
    CASE WHEN e.billable_option = 'Billable' THEN 1 ELSE 0 END   AS is_billable,
    -- Labor cost: the entry's own cost if CW recorded one, else the member's.
    -- NULL when neither is visible to the API member's security role.
    e.actual_hours * COALESCE(e.hourly_cost, m.hourly_cost)      AS labor_cost
FROM cw.time_entries e
CROSS JOIN meta.settings s
LEFT JOIN cw.members m ON m.identifier = e.member_identifier
LEFT JOIN cw.work_types wt ON wt.work_type_id = e.work_type_id
WHERE e.deleted = 0;
GO

/*
  Monthly utilization per tech, using ConnectWise's own configuration:
    capacity      = working days in the month x member.daily_capacity - PTO hours
    utilized      = hours on work types flagged "counts toward utilization"
    utilization % = utilized / capacity
  Only members flagged include_in_utilization. Working days exclude weekends
  and meta.calendar holidays, and are clipped to hire/inactive dates.
*/
CREATE OR ALTER VIEW rpt.v_utilization_monthly AS
WITH months AS (
    SELECT DATEFROMPARTS(YEAR(cal_date), MONTH(cal_date), 1) AS month_start, cal_date, is_weekday, is_holiday
    FROM meta.calendar
    WHERE cal_date >= '2025-01-01' AND cal_date <= CAST(SYSUTCDATETIME() AS DATE)
),
capacity AS (
    SELECT m.identifier, mo.month_start,
           SUM(CASE WHEN mo.is_weekday = 1 AND mo.is_holiday = 0 THEN 1 ELSE 0 END) AS working_days,
           MAX(m.daily_capacity) AS daily_capacity
    FROM cw.members m
    JOIN months mo
      ON mo.cal_date >= ISNULL(CAST(m.hire_date AS DATE), '2025-01-01')
     AND (m.inactive_date IS NULL OR mo.cal_date < CAST(m.inactive_date AS DATE))
    WHERE m.include_in_utilization = 1
    GROUP BY m.identifier, mo.month_start
),
hours AS (
    SELECT member_identifier, DATEFROMPARTS(YEAR(local_date), MONTH(local_date), 1) AS month_start,
           SUM(actual_hours) AS total_hours,
           SUM(CASE WHEN counts_toward_utilization = 1 AND is_pto = 0 THEN actual_hours ELSE 0 END) AS utilized_hours,
           SUM(CASE WHEN is_billable = 1 THEN actual_hours ELSE 0 END) AS billable_hours,
           SUM(CASE WHEN is_pto = 1 THEN actual_hours ELSE 0 END) AS pto_hours,
           SUM(CASE WHEN charge_to_type = 'ServiceTicket' THEN actual_hours ELSE 0 END) AS ticket_hours,
           SUM(CASE WHEN charge_to_type = 'ProjectTicket' THEN actual_hours ELSE 0 END) AS project_hours
    FROM rpt.v_time_entries
    GROUP BY member_identifier, DATEFROMPARTS(YEAR(local_date), MONTH(local_date), 1)
)
SELECT
    c.identifier AS member_identifier,
    COALESCE(NULLIF(m.display_name, ''), c.identifier) AS member_name,
    c.month_start,
    c.working_days,
    c.daily_capacity,
    c.working_days * c.daily_capacity - ISNULL(h.pto_hours, 0) AS capacity_hours,
    ISNULL(h.total_hours, 0)    AS total_hours,
    ISNULL(h.utilized_hours, 0) AS utilized_hours,
    ISNULL(h.billable_hours, 0) AS billable_hours,
    ISNULL(h.pto_hours, 0)      AS pto_hours,
    ISNULL(h.ticket_hours, 0)   AS ticket_hours,
    ISNULL(h.project_hours, 0)  AS project_hours,
    CASE WHEN c.working_days * c.daily_capacity - ISNULL(h.pto_hours, 0) > 0
         THEN ISNULL(h.utilized_hours, 0) / (c.working_days * c.daily_capacity - ISNULL(h.pto_hours, 0))
    END AS utilization_pct,
    CASE WHEN c.working_days * c.daily_capacity - ISNULL(h.pto_hours, 0) > 0
         THEN ISNULL(h.billable_hours, 0) / (c.working_days * c.daily_capacity - ISNULL(h.pto_hours, 0))
    END AS billable_pct
FROM capacity c
JOIN cw.members m ON m.identifier = c.identifier
LEFT JOIN hours h ON h.member_identifier = c.identifier AND h.month_start = c.month_start
WHERE c.working_days > 0;
GO

/* Timesheet compliance: periods not yet submitted/approved past deadline. */
CREATE OR ALTER VIEW rpt.v_timesheet_status AS
SELECT ts.*, COALESCE(NULLIF(m.display_name, ''), ts.member_identifier) AS member_name,
       CASE WHEN ts.status IN ('Open', 'Rejected') AND ts.deadline < SYSUTCDATETIME() THEN 1 ELSE 0 END AS is_overdue
FROM cw.time_sheets ts
LEFT JOIN cw.members m ON m.identifier = ts.member_identifier;
GO

/* Sync health — what the dashboard / an alert rule checks. */
CREATE OR ALTER VIEW rpt.v_sync_health AS
SELECT s.entity, s.cursor_utc, s.backfill_complete, s.last_success_at, s.last_error_at, s.last_error,
       DATEDIFF(MINUTE, s.last_success_at, SYSUTCDATETIME()) AS minutes_since_success
FROM meta.sync_state s;
GO
