/*
  th2-metrics-db — finance (ConnectWise agreements/invoices/products),
  phones (Dialpad) and CSAT (SmileBack). Same conventions as 001: UTC
  timestamps, natural keys, idempotent.
*/
IF SCHEMA_ID('dp') IS NULL EXEC('CREATE SCHEMA dp');
IF SCHEMA_ID('sb') IS NULL EXEC('CREATE SCHEMA sb');
GO

/* ------------------------------------------------------------ finance */

IF OBJECT_ID('cw.agreements') IS NULL
CREATE TABLE cw.agreements (
    agreement_id          INT           NOT NULL PRIMARY KEY,
    name                  NVARCHAR(200) NULL,
    type_name             NVARCHAR(100) NULL,
    company_id            INT           NULL,
    company_name          NVARCHAR(200) NULL,
    parent_agreement_id   INT           NULL,
    agreement_status      NVARCHAR(30)  NULL,   -- Active/Cancelled/Expired/Inactive
    cancelled_flag        BIT           NULL,
    date_cancelled        DATETIME2(0)  NULL,
    start_date            DATE          NULL,
    end_date              DATE          NULL,
    no_ending_date        BIT           NULL,
    bill_amount           DECIMAL(12,2) NULL,   -- base fee per billing cycle
    billing_cycle_name    NVARCHAR(50)  NULL,
    months_per_cycle      TINYINT       NULL,   -- derived from billing_cycle_name at sync; NULL = unknown
    application_units     NVARCHAR(20)  NULL,   -- Amount/Hours/Incidents
    application_limit     DECIMAL(12,2) NULL,
    application_unlimited BIT           NULL,
    application_cycle     NVARCHAR(30)  NULL,
    one_time_flag         BIT           NULL,
    sla_id                INT           NULL,
    last_updated          DATETIME2(0)  NULL,
    synced_at             DATETIME2(0)  NOT NULL
);
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_agreements_company')
CREATE INDEX IX_agreements_company ON cw.agreements (company_id);

-- Per-seat / per-device recurring lines on an agreement. Refreshed per
-- agreement on a rotation (one API call each), so `deleted` marks additions
-- that vanished on the agreement's latest refresh.
IF OBJECT_ID('cw.agreement_additions') IS NULL
CREATE TABLE cw.agreement_additions (
    addition_id         INT           NOT NULL PRIMARY KEY,
    agreement_id        INT           NOT NULL,
    product_identifier  NVARCHAR(100) NULL,
    description         NVARCHAR(300) NULL,
    quantity            DECIMAL(12,2) NULL,
    less_included       DECIMAL(12,2) NULL,
    billed_quantity     DECIMAL(12,2) NULL,
    unit_price          DECIMAL(12,4) NULL,   -- per agreement billing cycle
    unit_cost           DECIMAL(12,4) NULL,
    ext_price           DECIMAL(12,2) NULL,
    ext_cost            DECIMAL(12,2) NULL,
    bill_customer       NVARCHAR(20)  NULL,   -- Billable/DoNotBill/NoCharge
    effective_date      DATE          NULL,
    cancelled_date      DATE          NULL,
    agreement_status    NVARCHAR(30)  NULL,
    last_updated        DATETIME2(0)  NULL,
    deleted             BIT           NOT NULL DEFAULT 0,
    synced_at           DATETIME2(0)  NOT NULL
);
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_additions_agreement')
CREATE INDEX IX_additions_agreement ON cw.agreement_additions (agreement_id);

IF OBJECT_ID('cw.invoices') IS NULL
CREATE TABLE cw.invoices (
    invoice_id          INT           NOT NULL PRIMARY KEY,
    invoice_number      NVARCHAR(50)  NULL,
    invoice_type        NVARCHAR(30)  NULL,   -- Agreement/CreditMemo/DownPayment/Miscellaneous/Progress/Standard
    invoice_date        DATE          NULL,
    company_id          INT           NULL,
    company_name        NVARCHAR(200) NULL,
    agreement_id        INT           NULL,
    apply_to_type       NVARCHAR(30)  NULL,
    apply_to_id         INT           NULL,
    ticket_id           INT           NULL,
    project_id          INT           NULL,
    status_name         NVARCHAR(100) NULL,
    agreement_amount    DECIMAL(12,2) NULL,
    service_total       DECIMAL(12,2) NULL,
    product_total       DECIMAL(12,2) NULL,
    expense_total       DECIMAL(12,2) NULL,
    service_adjustment  DECIMAL(12,2) NULL,
    subtotal            DECIMAL(12,2) NULL,
    sales_tax           DECIMAL(12,2) NULL,
    total               DECIMAL(12,2) NULL,
    credits             DECIMAL(12,2) NULL,
    payments            DECIMAL(12,2) NULL,
    balance             DECIMAL(12,2) NULL,
    last_updated        DATETIME2(0)  NULL,
    synced_at           DATETIME2(0)  NOT NULL
);
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_invoices_company_date')
CREATE INDEX IX_invoices_company_date ON cw.invoices (company_id, invoice_date);

-- One-off product sales (procurement), for product cost of goods.
IF OBJECT_ID('cw.products') IS NULL
CREATE TABLE cw.products (
    product_item_id     INT           NOT NULL PRIMARY KEY,
    catalog_identifier  NVARCHAR(100) NULL,
    description         NVARCHAR(300) NULL,
    product_class       NVARCHAR(30)  NULL,
    company_id          INT           NULL,
    agreement_id        INT           NULL,
    ticket_id           INT           NULL,
    invoice_id          INT           NULL,
    quantity            DECIMAL(12,2) NULL,
    price               DECIMAL(12,4) NULL,
    cost                DECIMAL(12,4) NULL,
    billable_option     NVARCHAR(20)  NULL,
    cancelled_flag      BIT           NULL,
    date_entered        DATETIME2(0)  NULL,
    last_updated        DATETIME2(0)  NULL,
    synced_at           DATETIME2(0)  NOT NULL
);
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_products_invoice')
CREATE INDEX IX_products_invoice ON cw.products (invoice_id);
GO

/* ------------------------------------------------------------- phones */

-- One row per agent per local day (the export's timezone is the report
-- timezone, so stat_date is already a local date).
IF OBJECT_ID('dp.agent_daily') IS NULL
CREATE TABLE dp.agent_daily (
    stat_date         DATE          NOT NULL,
    agent_name        NVARCHAR(200) NOT NULL,
    inbound_calls     INT           NOT NULL DEFAULT 0,
    answered          INT           NOT NULL DEFAULT 0,
    missed            INT           NOT NULL DEFAULT 0,
    abandoned         INT           NOT NULL DEFAULT 0,
    talk_seconds      DECIMAL(12,1) NOT NULL DEFAULT 0,
    ring_seconds      DECIMAL(12,1) NOT NULL DEFAULT 0,
    synced_at         DATETIME2(0)  NOT NULL,
    PRIMARY KEY (stat_date, agent_name)
);

-- Optional manual mapping from Dialpad agent names to ConnectWise members,
-- for per-tech views that combine phone and ticket stats. Rows whose name
-- already matches cw.members.display_name don't need an entry.
IF OBJECT_ID('dp.agent_member_map') IS NULL
CREATE TABLE dp.agent_member_map (
    agent_name         NVARCHAR(200) NOT NULL PRIMARY KEY,
    member_identifier  NVARCHAR(50)  NOT NULL
);
GO

/* --------------------------------------------------------------- CSAT */

IF OBJECT_ID('sb.reviews') IS NULL
CREATE TABLE sb.reviews (
    review_id       BIGINT        NOT NULL PRIMARY KEY,
    rating_raw      NVARCHAR(30)  NULL,
    rating_score    SMALLINT      NULL,      -- +1 positive / 0 neutral / -1 negative; NULL = unrated
    comment         NVARCHAR(2000) NULL,
    status          NVARCHAR(50)  NULL,
    tags            NVARCHAR(500) NULL,
    rated_on        DATETIME2(0)  NULL,
    viewed_on       DATETIME2(0)  NULL,
    last_modified   DATETIME2(0)  NULL,
    ticket_id       INT           NULL,      -- ConnectWise ticket id (verify on first sync — see sync_smileback.py)
    ticket_title    NVARCHAR(500) NULL,
    board_name      NVARCHAR(100) NULL,      -- SmileBack "segment"
    ticket_closed_on DATETIME2(0) NULL,
    agents          NVARCHAR(500) NULL,      -- comma-joined technician names
    contact_name    NVARCHAR(200) NULL,
    contact_email   NVARCHAR(200) NULL,
    company_name    NVARCHAR(200) NULL,
    permalink       NVARCHAR(400) NULL,
    synced_at       DATETIME2(0)  NOT NULL
);
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_reviews_ticket')
CREATE INDEX IX_reviews_ticket ON sb.reviews (ticket_id);
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_reviews_rated_on')
CREATE INDEX IX_reviews_rated_on ON sb.reviews (rated_on);
GO
