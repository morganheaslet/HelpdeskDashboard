/*
  th2-metrics-db — stored local (report-timezone) dates.

  Measured 2026-10-14 on S0: reading 190k tickets took 0.6s raw but 66s
  with the views' four AT TIME ZONE conversions per row (~85% of the
  Tickets view's cost; time-entry rollups had the same problem). The sync
  now computes local timestamps/dates once, in Python, when it writes a
  row (shared/sync_cw.py, REPORT_TIMEZONE), and the views read the stored
  columns.

  Order to apply: (1) this script's ALTERs, (2) deploy the sync code that
  writes these columns, (3) EXEC meta.usp_backfill_local_dates — fills any
  row still NULL (all pre-existing rows, plus anything the old code wrote
  in between), (4) re-run 002 and 006 so the views use the columns.

  If REPORT_TIMEZONE / meta.settings.report_timezone ever changes, NULL
  these columns and re-run the backfill; both settings must name the same
  zone (America/Los_Angeles == 'Pacific Standard Time').
*/

IF COL_LENGTH('cw.tickets', 'entered_local') IS NULL
    ALTER TABLE cw.tickets ADD
        entered_local        DATETIME2(0) NULL,   -- local wall-clock time entered (after-hours checks)
        entered_local_date   DATE NULL,
        closed_local_date    DATE NULL,
        resolved_local_date  DATE NULL,
        responded_local_date DATE NULL;

IF COL_LENGTH('cw.time_entries', 'local_date') IS NULL
    ALTER TABLE cw.time_entries ADD local_date DATE NULL;
GO

CREATE OR ALTER PROCEDURE meta.usp_backfill_local_dates
AS
BEGIN
    SET NOCOUNT ON;
    DECLARE @tz NVARCHAR(64) = (SELECT report_timezone FROM meta.settings);
    DECLARE @n INT = 1;

    -- Batched so each statement stays short on a small tier.
    WHILE @n > 0
    BEGIN
        UPDATE TOP (20000) t SET
            entered_local        = CAST((t.date_entered   AT TIME ZONE 'UTC') AT TIME ZONE @tz AS DATETIME2(0)),
            entered_local_date   = CAST((t.date_entered   AT TIME ZONE 'UTC') AT TIME ZONE @tz AS DATE),
            closed_local_date    = CAST((t.closed_date    AT TIME ZONE 'UTC') AT TIME ZONE @tz AS DATE),
            resolved_local_date  = CAST((t.date_resolved  AT TIME ZONE 'UTC') AT TIME ZONE @tz AS DATE),
            responded_local_date = CAST((t.date_responded AT TIME ZONE 'UTC') AT TIME ZONE @tz AS DATE)
        FROM cw.tickets t
        WHERE (t.date_entered   IS NOT NULL AND t.entered_local_date   IS NULL)
           OR (t.closed_date    IS NOT NULL AND t.closed_local_date    IS NULL)
           OR (t.date_resolved  IS NOT NULL AND t.resolved_local_date  IS NULL)
           OR (t.date_responded IS NOT NULL AND t.responded_local_date IS NULL);
        SET @n = @@ROWCOUNT;
    END;

    SET @n = 1;
    WHILE @n > 0
    BEGIN
        UPDATE TOP (20000) e SET
            local_date = CAST((e.time_start AT TIME ZONE 'UTC') AT TIME ZONE @tz AS DATE)
        FROM cw.time_entries e
        WHERE e.time_start IS NOT NULL AND e.local_date IS NULL;
        SET @n = @@ROWCOUNT;
    END;
END;
GO
