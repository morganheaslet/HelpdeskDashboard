/*
  th2-metrics-db — Power BI star schema (schema pbi). One view per Power BI
  table, with report-friendly column names. Business rules stay in rpt.*;
  these views only shape them for the model in powerbi/.

  Keys
  - TechKey = UPPER(member identifier). Power BI relationships compare text
    case-insensitively, and ConnectWise stamps closedBy/resolvedBy in
    whatever case the member typed (e.g. 'tepps' vs 'TEpps'), so the key is
    normalized here to keep Tech's key column unique.
  - '(UNASSIGNED)' stands in for tickets with no owner.
  - Dates are LOCAL (report timezone) calendar dates.

  Grant: reporting_reader gets SELECT on this schema at the bottom.
*/
IF SCHEMA_ID('pbi') IS NULL EXEC('CREATE SCHEMA pbi');
GO

/* ------------------------------------------------------------ dimensions */

CREATE OR ALTER VIEW pbi.Calendar AS
SELECT
    c.cal_date                                                   AS [Date],
    YEAR(c.cal_date)                                             AS [Year],
    DATEFROMPARTS(YEAR(c.cal_date), MONTH(c.cal_date), 1)        AS [Month Start],
    FORMAT(c.cal_date, 'MMM yyyy')                               AS [Month],
    YEAR(c.cal_date) * 100 + MONTH(c.cal_date)                   AS [Month Sort],
    DATEADD(DAY, -(DATEDIFF(DAY, '19000101', c.cal_date) % 7), c.cal_date) AS [Week Start],   -- Monday
    'Wk of ' + FORMAT(DATEADD(DAY, -(DATEDIFF(DAY, '19000101', c.cal_date) % 7), c.cal_date), 'M/d/yy') AS [Week],
    FORMAT(c.cal_date, 'ddd')                                    AS [Weekday],
    DATEDIFF(DAY, '19000101', c.cal_date) % 7 + 1                AS [Weekday Sort],           -- 1 = Monday
    CAST(c.is_weekday AS BIT)                                    AS [Is Weekday],
    CAST(c.is_holiday AS BIT)                                    AS [Is Holiday],
    CAST(CASE WHEN c.is_weekday = 1 AND c.is_holiday = 0 THEN 1 ELSE 0 END AS BIT) AS [Is Working Day],
    -- Synced history is complete from 2025-01-01. Earlier dates exist only
    -- for older tickets that were touched since then — filter reports to
    -- [Is Full History] = True so pre-2025 totals don't look like real volume.
    CAST(CASE WHEN c.cal_date >= '2025-01-01' THEN 1 ELSE 0 END AS BIT) AS [Is Full History]
FROM meta.calendar c
CROSS JOIN meta.settings s
-- Through the end of the current year: Power BI date tables must be
-- contiguous, and future dates past that only clutter slicers.
WHERE c.cal_date <= DATEFROMPARTS(YEAR(CAST((SYSUTCDATETIME() AT TIME ZONE 'UTC') AT TIME ZONE s.report_timezone AS DATE)), 12, 31);
GO

CREATE OR ALTER VIEW pbi.Tech AS
WITH refs AS (   -- identifiers used on tickets/time that aren't (or are no longer) members
    SELECT UPPER(owner_identifier) AS k FROM cw.tickets WHERE owner_identifier IS NOT NULL
    UNION SELECT UPPER(closed_by)   FROM cw.tickets WHERE closed_by IS NOT NULL
    UNION SELECT UPPER(resolved_by) FROM cw.tickets WHERE resolved_by IS NOT NULL
    UNION SELECT UPPER(responded_by) FROM cw.tickets WHERE responded_by IS NOT NULL
    UNION SELECT UPPER(member_identifier) FROM cw.time_entries WHERE member_identifier IS NOT NULL
),
members AS (
    SELECT UPPER(identifier) AS k, m.*,
           ROW_NUMBER() OVER (PARTITION BY UPPER(identifier) ORDER BY inactive, member_id DESC) AS rn
    FROM cw.members m
)
SELECT
    m.k                                                           AS [TechKey],
    COALESCE(NULLIF(m.display_name, ''), m.identifier)            AS [Tech],
    m.identifier                                                  AS [Identifier],
    m.title                                                       AS [Title],
    m.default_department                                          AS [Department],
    CAST(CASE WHEN m.inactive = 1 THEN 0 ELSE 1 END AS BIT)       AS [Is Active],
    CAST(ISNULL(m.include_in_utilization, 0) AS BIT)              AS [In CW Utilization],
    -- API/integration members (e.g. NinjaOneAPI) close tickets automatically.
    CAST(CASE WHEN m.license_class = 'A' OR m.identifier LIKE '%API%' THEN 1 ELSE 0 END AS BIT) AS [Is Automation],
    ISNULL(m.daily_capacity, 8)                                   AS [Daily Capacity Hours],
    m.hourly_cost                                                 AS [Hourly Cost]
FROM members m WHERE m.rn = 1
UNION ALL
SELECT r.k, r.k + ' (not a member)', r.k, NULL, NULL, CAST(0 AS BIT), CAST(0 AS BIT),
       CAST(CASE WHEN r.k LIKE '%API%' OR r.k LIKE '%WORKFLOW%' THEN 1 ELSE 0 END AS BIT), 8, NULL
FROM refs r WHERE NOT EXISTS (SELECT 1 FROM cw.members m WHERE UPPER(m.identifier) = r.k)
UNION ALL
SELECT '(UNASSIGNED)', '(Unassigned)', NULL, NULL, NULL, CAST(1 AS BIT), CAST(0 AS BIT), CAST(0 AS BIT), 0, NULL;
GO

CREATE OR ALTER VIEW pbi.Board AS
SELECT
    b.board_id                                                    AS [BoardId],
    b.name                                                        AS [Board],
    CAST(CASE WHEN b.board_id IN (24, 38, 20, 54) THEN 1 ELSE 0 END AS BIT) AS [Is Throughput Board],
    CAST(CASE WHEN b.name = 'Alerts' THEN 1 ELSE 0 END AS BIT)    AS [Is Alerts],
    CAST(CASE WHEN b.inactive = 1 OR b.name LIKE 'z(%' THEN 0 ELSE 1 END AS BIT) AS [Is Active]
FROM cw.boards b
UNION ALL
SELECT -1, '(No board)', CAST(0 AS BIT), CAST(0 AS BIT), CAST(0 AS BIT);
GO

CREATE OR ALTER VIEW pbi.Company AS
WITH recent AS (SELECT DISTINCT company_id FROM cw.tickets WHERE date_entered >= '2025-01-01' AND deleted = 0)
SELECT c.company_id AS [CompanyId], c.name AS [Company], c.company_types AS [Company Types],
       c.territory AS [Territory], c.status_name AS [Company Status],
       -- ~190 of ~1,600 companies have had a ticket since 2025; client
       -- pickers filter on this instead of listing every company.
       CAST(CASE WHEN r.company_id IS NOT NULL THEN 1 ELSE 0 END AS BIT) AS [Has Recent Tickets]
FROM cw.companies c
LEFT JOIN recent r ON r.company_id = c.company_id
UNION ALL
SELECT -1, '(No company)', NULL, NULL, NULL, CAST(0 AS BIT);
GO

CREATE OR ALTER VIEW pbi.Agreement AS
SELECT agreement_id AS [AgreementId], name AS [Agreement], type_name AS [Agreement Type],
       ISNULL(company_id, -1) AS [CompanyId], agreement_status AS [Agreement Status],
       billing_cycle_name AS [Billing Cycle], bill_amount AS [Bill Amount],
       start_date AS [Start Date], end_date AS [End Date]
FROM cw.agreements;
GO

/* --------------------------------------------------------------- tickets */

CREATE OR ALTER VIEW pbi.Tickets AS
SELECT
    t.ticket_id                                         AS [TicketId],
    ISNULL(t.board_id, -1)                              AS [BoardId],
    ISNULL(t.company_id, -1)                            AS [CompanyId],
    ISNULL(UPPER(t.owner_identifier), '(UNASSIGNED)')   AS [OwnerKey],
    UPPER(t.closer_identifier)                          AS [CloserKey],
    UPPER(COALESCE(t.resolved_by, t.closed_by))         AS [ResolverKey],
    UPPER(t.responded_by)                               AS [ResponderKey],
    t.entered_local_date                                AS [Entered Date],
    CASE WHEN t.closed_flag = 1 THEN t.closed_local_date END AS [Closed Date],
    t.resolved_local_date                               AS [Resolved Date],     -- local dates stored by the sync (sql/007)
    t.responded_local_date                              AS [Responded Date],
    t.summary                                           AS [Summary],
    t.status_name                                       AS [Status],
    t.priority_name                                     AS [Priority],
    t.type_name                                         AS [Type],
    t.subtype_name                                      AS [Subtype],
    t.item_name                                         AS [Item],
    t.source_name                                       AS [Source],
    t.contact_name                                      AS [Contact],
    -- Entered outside business hours (local): before 7am, from 6pm, or on a
    -- weekend. Weekday from a known Monday (1900-01-01) so DATEFIRST doesn't matter.
    CAST(CASE WHEN DATEDIFF(DAY, '19000101', t.entered_local_date) % 7 >= 5
               OR DATEPART(HOUR, t.entered_local) < 7
               OR DATEPART(HOUR, t.entered_local) >= 18 THEN 1 ELSE 0 END AS BIT) AS [Entered After Hours],
    t.sla_name                                          AS [SLA],
    CAST(t.is_open AS BIT)                              AS [Is Open],
    CAST(t.is_waiting AS BIT)                           AS [Is Waiting],
    CAST(t.closed_flag AS BIT)                          AS [Is Closed],
    CAST(t.is_in_sla AS BIT)                            AS [Is In SLA Now],
    -- SLA-clock minutes from ConnectWise (business hours per the SLA
    -- calendar); the calendar-hour durations are alongside for comparison.
    t.respond_minutes / 60.0                            AS [Response Hours (SLA clock)],
    t.resolve_minutes / 60.0                            AS [Resolution Hours (SLA clock)],
    t.hours_to_respond                                  AS [Response Hours (calendar)],
    t.hours_to_resolve                                  AS [Resolution Hours (calendar)],
    t.sla_respond_target_hours                          AS [Response Target Hours],
    t.sla_resolve_target_hours                          AS [Resolution Target Hours],
    t.met_respond_sla                                   AS [Met Response SLA],     -- 1/0/NULL (no target or not yet responded)
    t.met_resolve_sla                                   AS [Met Resolution SLA],
    CASE WHEN t.is_open = 1 THEN DATEDIFF(MINUTE, t.date_entered, SYSUTCDATETIME()) / 1440.0 END AS [Open Age Days],
    t.actual_hours                                      AS [Ticket Hours]
FROM rpt.v_tickets t;
GO

/*
  Assignment events: one row each time a ticket landed on a tech.
  - Owner changes the sync has observed (cw.ticket_events, from go-live
    2026-10-14 onward) are exact to within a sync interval.
  - Before that, ConnectWise's API keeps no assignment history, so the
    ticket's first known owner counts as assigned on the day it was entered
    (for tickets routed through Dispatch, that's the day it was created).
*/
CREATE OR ALTER VIEW pbi.TicketAssignments AS
WITH first_owner_event AS (
    -- earliest observed owner change per ticket: its old value is the
    -- original owner (one pass over the small events table, not a
    -- per-ticket subquery)
    SELECT ticket_id, old_value
    FROM (SELECT ticket_id, old_value,
                 ROW_NUMBER() OVER (PARTITION BY ticket_id ORDER BY event_at, ticket_event_id) AS rn
          FROM cw.ticket_events WHERE field = 'owner') x
    WHERE rn = 1
)
SELECT
    t.ticket_id AS [TicketId],
    UPPER(CASE WHEN f.ticket_id IS NOT NULL THEN f.old_value ELSE t.owner_identifier END) AS [TechKey],
    t.entered_local_date AS [Assigned Date],
    'Initial' AS [Assignment Kind],
    ISNULL(t.board_id, -1) AS [BoardId], ISNULL(t.company_id, -1) AS [CompanyId], t.priority_name AS [Priority]
FROM cw.tickets t
CROSS JOIN meta.settings s
LEFT JOIN first_owner_event f ON f.ticket_id = t.ticket_id
WHERE t.deleted = 0
  AND (CASE WHEN f.ticket_id IS NOT NULL THEN f.old_value ELSE t.owner_identifier END) IS NOT NULL
UNION ALL
SELECT
    e.ticket_id, UPPER(e.new_value),
    CAST((e.event_at AT TIME ZONE 'UTC') AT TIME ZONE s.report_timezone AS DATE),
    'Reassigned',
    ISNULL(t.board_id, -1), ISNULL(t.company_id, -1), t.priority_name
FROM cw.ticket_events e
JOIN cw.tickets t ON t.ticket_id = e.ticket_id AND t.deleted = 0
CROSS JOIN meta.settings s
WHERE e.field = 'owner' AND e.new_value IS NOT NULL;
GO

-- Start-of-day open backlog per tech (from cw.backlog_daily — exists from the
-- first daily snapshot after the backfill completes, forward).
CREATE OR ALTER VIEW pbi.BacklogDaily AS
SELECT snapshot_date AS [Date], board_id AS [BoardId],
       CASE WHEN owner_identifier = '' THEN '(UNASSIGNED)' ELSE UPPER(owner_identifier) END AS [TechKey],
       status_name AS [Status], priority_name AS [Priority], open_count AS [Open Tickets]
FROM cw.backlog_daily;
GO

/* ------------------------------------------------------- time / capacity */

CREATE OR ALTER VIEW pbi.TimeEntries AS
SELECT
    e.time_entry_id                                   AS [TimeEntryId],
    UPPER(e.member_identifier)                        AS [TechKey],
    e.local_date                                      AS [Date],
    ISNULL(e.company_id, -1)                          AS [CompanyId],
    e.agreement_id                                    AS [AgreementId],
    e.ticket_id                                       AS [TicketId],
    ISNULL(e.charge_to_type, '(none)')                AS [Charge Type],
    e.work_type_name                                  AS [Work Type],
    e.work_role_name                                  AS [Work Role],
    e.billable_option                                 AS [Billable Option],
    e.actual_hours                                    AS [Hours],
    CASE WHEN e.counts_toward_utilization = 1 AND e.is_pto = 0 THEN e.actual_hours ELSE 0 END AS [Utilized Hours],
    CASE WHEN e.is_billable = 1 THEN e.actual_hours ELSE 0 END AS [Billable Hours],
    CASE WHEN e.is_pto = 1 THEN e.actual_hours ELSE 0 END      AS [PTO Hours],
    e.labor_cost                                      AS [Labor Cost],
    CASE WHEN e.labor_cost IS NULL THEN e.actual_hours ELSE 0 END AS [Uncosted Hours]
FROM rpt.v_time_entries e
WHERE e.local_date IS NOT NULL;
GO

/*
  Daily capacity per tech: [Daily Capacity Hours] on each working day they
  were employed. Employment runs from the later of hire date / 2025-01-01 /
  first time entry, to inactive date — or, for members marked inactive with
  no inactive date, their last time entry. Automation members are excluded.
  Utilization % = utilized hours / (capacity - PTO), at any date grain.
*/
CREATE OR ALTER VIEW pbi.Capacity AS
WITH span AS (
    SELECT m.identifier, UPPER(m.identifier) AS k, ISNULL(m.daily_capacity, 8) AS cap, m.inactive,
           CAST(m.hire_date AS DATE) AS hire_date, CAST(m.inactive_date AS DATE) AS inactive_date,
           MIN(e.local_date) AS first_entry, MAX(e.local_date) AS last_entry
    FROM cw.members m
    JOIN rpt.v_time_entries e ON e.member_identifier = m.identifier
    WHERE ISNULL(m.license_class, '') <> 'A' AND m.identifier NOT LIKE '%API%'
    GROUP BY m.identifier, m.daily_capacity, m.inactive, m.hire_date, m.inactive_date
)
SELECT s.k AS [TechKey], c.cal_date AS [Date], CAST(s.cap AS DECIMAL(6,2)) AS [Capacity Hours]
FROM span s
CROSS JOIN meta.settings st
JOIN meta.calendar c
  ON c.is_weekday = 1 AND c.is_holiday = 0
 AND c.cal_date >= (SELECT MAX(d) FROM (VALUES (s.hire_date), (CAST('2025-01-01' AS DATE)), (s.first_entry)) v(d))
 AND c.cal_date <= COALESCE(s.inactive_date, CASE WHEN s.inactive = 1 THEN s.last_entry END,
                            CAST((SYSUTCDATETIME() AT TIME ZONE 'UTC') AT TIME ZONE st.report_timezone AS DATE));
GO

/* --------------------------------------------------------- profitability */

CREATE OR ALTER VIEW pbi.ClientMonth AS
SELECT ISNULL(company_id, -1) AS [CompanyId], month_start AS [Month Start],
       invoiced_revenue AS [Invoiced Revenue], normalized_revenue AS [Normalized Revenue],
       agreement_mrr AS [Agreement Revenue], hours AS [Hours], labor_cost AS [Labor Cost],
       uncosted_hours AS [Uncosted Hours], addition_cost AS [Addition Cost], product_cost AS [Product Cost],
       margin AS [Margin], invoiced_margin AS [Invoiced Margin]
FROM rpt.v_client_profitability_monthly
WHERE month_start IS NOT NULL;
GO

CREATE OR ALTER VIEW pbi.AgreementMonth AS
SELECT agreement_id AS [AgreementId], ISNULL(company_id, -1) AS [CompanyId], month_start AS [Month Start],
       revenue AS [Revenue], hours AS [Hours], labor_cost AS [Labor Cost], uncosted_hours AS [Uncosted Hours],
       addition_cost AS [Addition Cost], margin AS [Margin]
FROM rpt.v_agreement_profitability_monthly;
GO

/* ---------------------------------------------------------------- phones */

CREATE OR ALTER VIEW pbi.PhoneDaily AS
SELECT stat_date AS [Date], agent_name AS [Agent],
       UPPER(member_identifier) AS [TechKey],   -- NULL until mapped in dp.agent_member_map
       inbound_calls AS [Inbound Calls], answered AS [Answered], missed AS [Missed],
       abandoned AS [Abandoned], talk_seconds / 3600.0 AS [Talk Hours]
FROM rpt.v_phone_agent_daily;
GO

GRANT SELECT ON SCHEMA::pbi TO reporting_reader;
GO
