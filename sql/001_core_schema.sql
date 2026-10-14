/*
  th2-metrics-db — core schema: sync bookkeeping + ConnectWise service/time data.

  Conventions
  - Every timestamp column is UTC (DATETIME2, no offset). "Local" (Pacific)
    reporting boundaries are applied in views / queries via AT TIME ZONE —
    never stored converted.
  - Natural ConnectWise ids are the primary keys, so the sync can upsert
    (MERGE) by id and re-running any sync window is always safe.
  - Reference names (board_name, status_name, ...) are denormalized onto
    tickets / time entries as they were at sync time, so reports don't need a
    join for the common case and a renamed status doesn't rewrite history.
  - Idempotent: safe to re-run this whole script.
*/

IF SCHEMA_ID('meta') IS NULL EXEC('CREATE SCHEMA meta');
IF SCHEMA_ID('cw')   IS NULL EXEC('CREATE SCHEMA cw');
GO

/* ---------------------------------------------------------------- meta */

IF OBJECT_ID('meta.sync_state') IS NULL
CREATE TABLE meta.sync_state (
    entity              NVARCHAR(50)  NOT NULL PRIMARY KEY,
    cursor_utc          DATETIME2(0)  NULL,     -- incremental high-water mark (lastUpdated)
    cursor_id           INT           NULL,     -- rotation position for per-parent pulls (agreement additions)
    backfill_complete   BIT           NOT NULL DEFAULT 0,
    last_success_at     DATETIME2(0)  NULL,
    last_error_at       DATETIME2(0)  NULL,
    last_error          NVARCHAR(2000) NULL,
    last_daily_job_date DATE          NULL      -- local date a once-per-day task last ran
);

IF OBJECT_ID('meta.sync_log') IS NULL
CREATE TABLE meta.sync_log (
    sync_log_id   BIGINT IDENTITY PRIMARY KEY,
    entity        NVARCHAR(50)  NOT NULL,
    started_at    DATETIME2(0)  NOT NULL,
    finished_at   DATETIME2(0)  NULL,
    rows_upserted INT           NULL,
    status        NVARCHAR(20)  NOT NULL,       -- ok | error | partial
    message       NVARCHAR(2000) NULL
);
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_sync_log_entity_started')
CREATE INDEX IX_sync_log_entity_started ON meta.sync_log (entity, started_at DESC);
GO

/* ---------------------------------------------------------- reference */

IF OBJECT_ID('cw.members') IS NULL
CREATE TABLE cw.members (
    member_id               INT           NOT NULL PRIMARY KEY,
    identifier              NVARCHAR(50)  NOT NULL,
    first_name              NVARCHAR(100) NULL,
    last_name               NVARCHAR(100) NULL,
    display_name            AS (LTRIM(RTRIM(CONCAT(first_name, ' ', last_name)))),
    title                   NVARCHAR(100) NULL,
    member_type             NVARCHAR(100) NULL,
    license_class           NVARCHAR(5)   NULL,    -- A/C/F/X; API members show up here too
    inactive                BIT           NOT NULL DEFAULT 0,
    inactive_date           DATETIME2(0)  NULL,
    hire_date               DATETIME2(0)  NULL,
    daily_capacity          DECIMAL(6,2)  NULL,    -- hours/day, drives utilization
    include_in_utilization  BIT           NULL,
    hourly_cost             DECIMAL(10,2) NULL,    -- may be hidden by the API member's security role
    hourly_rate             DECIMAL(10,2) NULL,
    work_role_name          NVARCHAR(100) NULL,
    default_department      NVARCHAR(100) NULL,
    default_location        NVARCHAR(100) NULL,
    reports_to              NVARCHAR(50)  NULL,
    synced_at               DATETIME2(0)  NOT NULL
);
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'UX_members_identifier')
CREATE UNIQUE INDEX UX_members_identifier ON cw.members (identifier);

IF OBJECT_ID('cw.work_types') IS NULL
CREATE TABLE cw.work_types (
    work_type_id      INT           NOT NULL PRIMARY KEY,
    name              NVARCHAR(100) NOT NULL,
    bill_time         NVARCHAR(30)  NULL,
    utilization_flag  BIT           NULL,      -- counts toward utilization
    accrual_type      NVARCHAR(30)  NULL,      -- Holiday/PTO/Sick/Vacation -> excluded from capacity
    inactive          BIT           NOT NULL DEFAULT 0,
    synced_at         DATETIME2(0)  NOT NULL
);

IF OBJECT_ID('cw.work_roles') IS NULL
CREATE TABLE cw.work_roles (
    work_role_id  INT           NOT NULL PRIMARY KEY,
    name          NVARCHAR(100) NOT NULL,
    hourly_rate   DECIMAL(10,2) NULL,
    inactive      BIT           NOT NULL DEFAULT 0,
    synced_at     DATETIME2(0)  NOT NULL
);

IF OBJECT_ID('cw.boards') IS NULL
CREATE TABLE cw.boards (
    board_id      INT           NOT NULL PRIMARY KEY,
    name          NVARCHAR(100) NOT NULL,
    project_flag  BIT           NULL,
    inactive      BIT           NOT NULL DEFAULT 0,
    synced_at     DATETIME2(0)  NOT NULL
);

IF OBJECT_ID('cw.board_statuses') IS NULL
CREATE TABLE cw.board_statuses (
    status_id               INT           NOT NULL PRIMARY KEY,
    board_id                INT           NOT NULL,
    name                    NVARCHAR(100) NOT NULL,
    closed_status           BIT           NULL,
    escalation_status       NVARCHAR(30)  NULL,  -- NotResponded/Responded/ResolutionPlan/Resolved/NoEscalation — drives SLA stamps
    time_entry_not_allowed  BIT           NULL,
    inactive                BIT           NOT NULL DEFAULT 0,
    sort_order              INT           NULL,
    synced_at               DATETIME2(0)  NOT NULL
);

IF OBJECT_ID('cw.teams') IS NULL
CREATE TABLE cw.teams (
    team_id      INT           NOT NULL PRIMARY KEY,
    board_id     INT           NOT NULL,
    name         NVARCHAR(100) NOT NULL,
    team_leader  NVARCHAR(50)  NULL,
    synced_at    DATETIME2(0)  NOT NULL
);

IF OBJECT_ID('cw.priorities') IS NULL
CREATE TABLE cw.priorities (
    priority_id  INT           NOT NULL PRIMARY KEY,
    name         NVARCHAR(100) NOT NULL,
    sort_order   INT           NULL,
    synced_at    DATETIME2(0)  NOT NULL
);

IF OBJECT_ID('cw.slas') IS NULL
CREATE TABLE cw.slas (
    sla_id            INT           NOT NULL PRIMARY KEY,
    name              NVARCHAR(100) NOT NULL,
    based_on          NVARCHAR(30)  NULL,   -- AllHours/Customer/MyCalendar/Custom
    respond_hours     DECIMAL(9,2)  NULL,
    plan_within       DECIMAL(9,2)  NULL,
    resolution_hours  DECIMAL(9,2)  NULL,
    synced_at         DATETIME2(0)  NOT NULL
);

IF OBJECT_ID('cw.sla_priority_targets') IS NULL
CREATE TABLE cw.sla_priority_targets (
    sla_id            INT           NOT NULL,
    priority_id       INT           NOT NULL,
    respond_hours     DECIMAL(9,2)  NULL,
    plan_within       DECIMAL(9,2)  NULL,
    resolution_hours  DECIMAL(9,2)  NULL,
    synced_at         DATETIME2(0)  NOT NULL,
    PRIMARY KEY (sla_id, priority_id)
);

IF OBJECT_ID('cw.companies') IS NULL
CREATE TABLE cw.companies (
    company_id         INT           NOT NULL PRIMARY KEY,
    identifier         NVARCHAR(50)  NULL,
    name               NVARCHAR(200) NOT NULL,
    status_name        NVARCHAR(100) NULL,
    company_types      NVARCHAR(400) NULL,   -- comma-joined type names
    territory          NVARCHAR(100) NULL,
    market             NVARCHAR(100) NULL,
    parent_company_id  INT           NULL,
    deleted            BIT           NOT NULL DEFAULT 0,
    date_acquired      DATETIME2(0)  NULL,
    employee_count     INT           NULL,
    last_updated       DATETIME2(0)  NULL,
    synced_at          DATETIME2(0)  NOT NULL
);
GO

/* ------------------------------------------------------------ tickets */

IF OBJECT_ID('cw.tickets') IS NULL
CREATE TABLE cw.tickets (
    ticket_id         INT           NOT NULL PRIMARY KEY,
    record_type       NVARCHAR(30)  NULL,    -- only ServiceTicket is synced (project tickets excluded)
    board_id          INT           NULL,
    board_name        NVARCHAR(100) NULL,
    status_id         INT           NULL,
    status_name       NVARCHAR(100) NULL,
    priority_id       INT           NULL,
    priority_name     NVARCHAR(100) NULL,
    type_name         NVARCHAR(100) NULL,
    subtype_name      NVARCHAR(100) NULL,
    item_name         NVARCHAR(100) NULL,
    team_id           INT           NULL,
    team_name         NVARCHAR(100) NULL,
    source_name       NVARCHAR(100) NULL,
    severity          NVARCHAR(20)  NULL,
    impact            NVARCHAR(20)  NULL,
    company_id        INT           NULL,
    company_name      NVARCHAR(200) NULL,
    contact_name      NVARCHAR(200) NULL,
    owner_identifier  NVARCHAR(50)  NULL,    -- NULL = unassigned
    summary           NVARCHAR(500) NULL,
    parent_ticket_id  INT           NULL,
    agreement_id      INT           NULL,
    agreement_name    NVARCHAR(200) NULL,
    sla_id            INT           NULL,
    sla_name          NVARCHAR(100) NULL,
    sla_status        NVARCHAR(100) NULL,
    is_in_sla         BIT           NULL,
    date_entered      DATETIME2(0)  NULL,
    entered_by        NVARCHAR(50)  NULL,
    last_updated      DATETIME2(0)  NULL,
    closed_flag       BIT           NOT NULL DEFAULT 0,
    closed_date       DATETIME2(0)  NULL,
    closed_by         NVARCHAR(50)  NULL,
    date_responded    DATETIME2(0)  NULL,
    responded_by      NVARCHAR(50)  NULL,
    date_resplan      DATETIME2(0)  NULL,
    resplan_by        NVARCHAR(50)  NULL,
    date_resolved     DATETIME2(0)  NULL,
    resolved_by       NVARCHAR(50)  NULL,
    respond_minutes   INT           NULL,
    resplan_minutes   INT           NULL,
    resolve_minutes   INT           NULL,
    minutes_waiting   INT           NULL,
    actual_hours      DECIMAL(9,2)  NULL,
    budget_hours      DECIMAL(9,2)  NULL,
    customer_updated  BIT           NULL,
    deleted           BIT           NOT NULL DEFAULT 0,   -- set by reconciliation when CW no longer returns it
    synced_at         DATETIME2(0)  NOT NULL
);
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_tickets_board_open')
CREATE INDEX IX_tickets_board_open ON cw.tickets (board_id, closed_flag) INCLUDE (status_name, owner_identifier, priority_name);
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_tickets_date_entered')
CREATE INDEX IX_tickets_date_entered ON cw.tickets (date_entered) INCLUDE (board_id, owner_identifier, company_id);
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_tickets_closed_date')
CREATE INDEX IX_tickets_closed_date ON cw.tickets (closed_date) INCLUDE (board_id, closed_by, owner_identifier, company_id);
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_tickets_company')
CREATE INDEX IX_tickets_company ON cw.tickets (company_id);

-- Status / owner transitions as observed by the sync, from go-live forward.
-- ConnectWise has no bulk history API (audit trail is one call per ticket),
-- so this is built by diffing each synced ticket against its stored row.
-- event_at is the ticket's lastUpdated at the time the change was seen.
IF OBJECT_ID('cw.ticket_events') IS NULL
CREATE TABLE cw.ticket_events (
    ticket_event_id  BIGINT IDENTITY PRIMARY KEY,
    ticket_id        INT           NOT NULL,
    event_at         DATETIME2(0)  NOT NULL,
    field            NVARCHAR(20)  NOT NULL,   -- status | owner | board
    old_value        NVARCHAR(100) NULL,
    new_value        NVARCHAR(100) NULL
);
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_ticket_events_ticket')
CREATE INDEX IX_ticket_events_ticket ON cw.ticket_events (ticket_id, event_at);

-- Start-of-day open backlog, taken by the first sync after local midnight.
-- The only way to answer "what did the backlog look like on day X" later.
IF OBJECT_ID('cw.backlog_daily') IS NULL
CREATE TABLE cw.backlog_daily (
    snapshot_date     DATE          NOT NULL,   -- local (report timezone) date
    board_id          INT           NOT NULL,
    status_name       NVARCHAR(100) NOT NULL,
    priority_name     NVARCHAR(100) NOT NULL,
    owner_identifier  NVARCHAR(50)  NOT NULL,   -- '' = unassigned
    open_count        INT           NOT NULL,
    oldest_age_days   DECIMAL(9,1)  NULL,
    PRIMARY KEY (snapshot_date, board_id, status_name, priority_name, owner_identifier)
);
GO

/* --------------------------------------------------------------- time */

IF OBJECT_ID('cw.time_entries') IS NULL
CREATE TABLE cw.time_entries (
    time_entry_id     INT           NOT NULL PRIMARY KEY,
    member_identifier NVARCHAR(50)  NULL,
    charge_to_type    NVARCHAR(30)  NULL,    -- ServiceTicket/ProjectTicket/ChargeCode/Activity/Company
    charge_to_id      INT           NULL,
    ticket_id         INT           NULL,
    project_id        INT           NULL,
    company_id        INT           NULL,
    company_name      NVARCHAR(200) NULL,
    agreement_id      INT           NULL,
    agreement_name    NVARCHAR(200) NULL,
    work_type_id      INT           NULL,
    work_type_name    NVARCHAR(100) NULL,
    work_role_id      INT           NULL,
    work_role_name    NVARCHAR(100) NULL,
    billable_option   NVARCHAR(20)  NULL,    -- Billable/DoNotBill/NoCharge/NoDefault
    time_start        DATETIME2(0)  NULL,
    time_end          DATETIME2(0)  NULL,
    actual_hours      DECIMAL(9,2)  NULL,
    hours_deduct      DECIMAL(9,2)  NULL,
    hours_billed      DECIMAL(9,2)  NULL,
    invoice_hours     DECIMAL(9,2)  NULL,
    agreement_hours   DECIMAL(9,2)  NULL,
    agreement_amount  DECIMAL(12,2) NULL,
    hourly_rate       DECIMAL(10,2) NULL,
    hourly_cost       DECIMAL(10,2) NULL,
    status            NVARCHAR(30)  NULL,    -- Open/PendingApproval/Approved.../Billed
    time_sheet_id     INT           NULL,
    invoice_id        INT           NULL,
    ticket_board      NVARCHAR(100) NULL,
    date_entered      DATETIME2(0)  NULL,
    last_updated      DATETIME2(0)  NULL,
    deleted           BIT           NOT NULL DEFAULT 0,
    synced_at         DATETIME2(0)  NOT NULL
);
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_time_entries_member_start')
CREATE INDEX IX_time_entries_member_start ON cw.time_entries (member_identifier, time_start) INCLUDE (actual_hours, work_type_id, billable_option, charge_to_type);
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_time_entries_company_start')
CREATE INDEX IX_time_entries_company_start ON cw.time_entries (company_id, time_start) INCLUDE (actual_hours, agreement_id, hourly_cost);
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_time_entries_ticket')
CREATE INDEX IX_time_entries_ticket ON cw.time_entries (ticket_id);

IF OBJECT_ID('cw.time_sheets') IS NULL
CREATE TABLE cw.time_sheets (
    time_sheet_id      INT           NOT NULL PRIMARY KEY,
    member_identifier  NVARCHAR(50)  NULL,
    year               INT           NULL,
    period             INT           NULL,
    date_start         DATE          NULL,
    date_end           DATE          NULL,
    status             NVARCHAR(30)  NULL,
    hours              DECIMAL(9,2)  NULL,
    deadline           DATETIME2(0)  NULL,
    last_updated       DATETIME2(0)  NULL,
    synced_at          DATETIME2(0)  NOT NULL
);
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_time_sheets_member_start')
CREATE INDEX IX_time_sheets_member_start ON cw.time_sheets (member_identifier, date_start);
GO
