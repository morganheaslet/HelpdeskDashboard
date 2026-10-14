/*
  th2-metrics-db — profitability, phone and CSAT reporting views.

  Profitability model (monthly; annual views roll the monthly ones up):
    invoiced_revenue    what was actually billed: invoice subtotal (ex tax)
                        by invoice month, credit memos negative. Lumpy for
                        annual/prepaid agreements.
    normalized_revenue  agreement MRR (base fee + billable additions, spread
                        per month by billing cycle) + non-agreement invoices
                        (T&M, products, projects). Smooth month to month.
    labor_cost          time entry hours x hourly cost (entry's, else member's)
    addition_cost       recurring cost of agreement additions, per month
    product_cost        one-off procurement product cost, by invoice month
    margin              normalized_revenue - all costs  (and an invoiced variant)
*/
GO

/* Agreement MRR per month: one row per agreement per month it was active. */
CREATE OR ALTER VIEW rpt.v_agreement_mrr_monthly AS
WITH months AS (
    SELECT DISTINCT DATEFROMPARTS(YEAR(cal_date), MONTH(cal_date), 1) AS month_start
    FROM meta.calendar WHERE cal_date >= '2025-01-01' AND cal_date <= CAST(SYSUTCDATETIME() AS DATE)
),
active AS (
    SELECT a.*, m.month_start, EOMONTH(m.month_start) AS month_end
    FROM cw.agreements a
    JOIN months m
      ON m.month_start <= ISNULL(a.end_date, '9999-12-31')
     AND EOMONTH(m.month_start) >= ISNULL(a.start_date, '2025-01-01')
     AND (a.date_cancelled IS NULL OR m.month_start < CAST(a.date_cancelled AS DATE))
    WHERE ISNULL(a.one_time_flag, 0) = 0 AND a.months_per_cycle IS NOT NULL
),
adds AS (
    SELECT ac.agreement_id, ac.month_start,
           SUM(CASE WHEN x.bill_customer = 'Billable'
                    THEN ISNULL(x.ext_price, (ISNULL(x.quantity, 0) - ISNULL(x.less_included, 0)) * ISNULL(x.unit_price, 0))
                    ELSE 0 END) AS addition_price,
           SUM(ISNULL(x.ext_cost, ISNULL(x.quantity, 0) * ISNULL(x.unit_cost, 0))) AS addition_cost
    FROM active ac
    JOIN cw.agreement_additions x
      ON x.agreement_id = ac.agreement_id AND x.deleted = 0
     AND ISNULL(x.effective_date, '1900-01-01') <= ac.month_end
     AND (x.cancelled_date IS NULL OR x.cancelled_date > ac.month_start)
    GROUP BY ac.agreement_id, ac.month_start
)
SELECT ac.agreement_id, ac.name AS agreement_name, ac.type_name, ac.company_id, ac.company_name,
       ac.month_start,
       ISNULL(ac.bill_amount, 0) / ac.months_per_cycle                         AS base_mrr,
       ISNULL(ad.addition_price, 0) / ac.months_per_cycle                      AS additions_mrr,
       (ISNULL(ac.bill_amount, 0) + ISNULL(ad.addition_price, 0)) / ac.months_per_cycle AS mrr,
       ISNULL(ad.addition_cost, 0) / ac.months_per_cycle                       AS addition_cost
FROM active ac
LEFT JOIN adds ad ON ad.agreement_id = ac.agreement_id AND ad.month_start = ac.month_start;
GO

CREATE OR ALTER VIEW rpt.v_invoices AS
SELECT i.*,
       DATEFROMPARTS(YEAR(i.invoice_date), MONTH(i.invoice_date), 1) AS month_start,
       -- Sign-normalize: credit memos reduce revenue whether CW stores them
       -- as positive or negative amounts.
       CASE WHEN i.invoice_type = 'CreditMemo' THEN -ABS(ISNULL(i.subtotal, 0))
            ELSE ISNULL(i.subtotal, 0) END AS revenue
FROM cw.invoices i;
GO

/* Contract (agreement) profitability per month. */
CREATE OR ALTER VIEW rpt.v_agreement_profitability_monthly AS
WITH labor AS (
    SELECT agreement_id, DATEFROMPARTS(YEAR(local_date), MONTH(local_date), 1) AS month_start,
           SUM(actual_hours) AS hours, SUM(labor_cost) AS labor_cost,
           SUM(CASE WHEN labor_cost IS NULL THEN actual_hours ELSE 0 END) AS uncosted_hours
    FROM rpt.v_time_entries
    WHERE agreement_id IS NOT NULL
    GROUP BY agreement_id, DATEFROMPARTS(YEAR(local_date), MONTH(local_date), 1)
)
SELECT r.agreement_id, r.agreement_name, r.type_name, r.company_id, r.company_name, r.month_start,
       r.mrr AS revenue,
       ISNULL(l.hours, 0)          AS hours,
       ISNULL(l.labor_cost, 0)     AS labor_cost,
       ISNULL(l.uncosted_hours, 0) AS uncosted_hours,   -- hours with no visible cost rate
       r.addition_cost,
       r.mrr - ISNULL(l.labor_cost, 0) - r.addition_cost AS margin,
       CASE WHEN r.mrr <> 0 THEN (r.mrr - ISNULL(l.labor_cost, 0) - r.addition_cost) / r.mrr END AS margin_pct,
       CASE WHEN ISNULL(l.hours, 0) > 0 THEN r.mrr / l.hours END AS effective_hourly_rate
FROM rpt.v_agreement_mrr_monthly r
LEFT JOIN labor l ON l.agreement_id = r.agreement_id AND l.month_start = r.month_start;
GO

/* Client profitability per month. */
CREATE OR ALTER VIEW rpt.v_client_profitability_monthly AS
WITH keys AS (
    SELECT company_id, month_start FROM rpt.v_agreement_mrr_monthly
    UNION SELECT company_id, month_start FROM rpt.v_invoices WHERE company_id IS NOT NULL
    UNION SELECT company_id, DATEFROMPARTS(YEAR(local_date), MONTH(local_date), 1)
          FROM rpt.v_time_entries WHERE company_id IS NOT NULL
),
mrr AS (
    SELECT company_id, month_start, SUM(mrr) AS agreement_mrr, SUM(addition_cost) AS addition_cost
    FROM rpt.v_agreement_mrr_monthly GROUP BY company_id, month_start
),
inv AS (
    SELECT company_id, month_start,
           SUM(revenue) AS invoiced_revenue,
           SUM(CASE WHEN invoice_type <> 'Agreement' THEN revenue ELSE 0 END) AS non_agreement_revenue
    FROM rpt.v_invoices GROUP BY company_id, month_start
),
labor AS (
    SELECT company_id, DATEFROMPARTS(YEAR(local_date), MONTH(local_date), 1) AS month_start,
           SUM(actual_hours) AS hours, SUM(labor_cost) AS labor_cost,
           SUM(CASE WHEN labor_cost IS NULL THEN actual_hours ELSE 0 END) AS uncosted_hours
    FROM rpt.v_time_entries GROUP BY company_id, DATEFROMPARTS(YEAR(local_date), MONTH(local_date), 1)
),
prod AS (
    SELECT i.company_id, i.month_start, SUM(ISNULL(p.cost, 0) * ISNULL(p.quantity, 0)) AS product_cost
    FROM cw.products p JOIN rpt.v_invoices i ON i.invoice_id = p.invoice_id
    WHERE ISNULL(p.cancelled_flag, 0) = 0
    GROUP BY i.company_id, i.month_start
)
SELECT k.company_id, c.name AS company_name, c.company_types, c.territory, k.month_start,
       ISNULL(inv.invoiced_revenue, 0)                                     AS invoiced_revenue,
       ISNULL(mrr.agreement_mrr, 0) + ISNULL(inv.non_agreement_revenue, 0) AS normalized_revenue,
       ISNULL(mrr.agreement_mrr, 0)                                        AS agreement_mrr,
       ISNULL(labor.hours, 0)          AS hours,
       ISNULL(labor.labor_cost, 0)     AS labor_cost,
       ISNULL(labor.uncosted_hours, 0) AS uncosted_hours,
       ISNULL(mrr.addition_cost, 0)    AS addition_cost,
       ISNULL(prod.product_cost, 0)    AS product_cost,
       ISNULL(mrr.agreement_mrr, 0) + ISNULL(inv.non_agreement_revenue, 0)
         - ISNULL(labor.labor_cost, 0) - ISNULL(mrr.addition_cost, 0) - ISNULL(prod.product_cost, 0) AS margin,
       ISNULL(inv.invoiced_revenue, 0)
         - ISNULL(labor.labor_cost, 0) - ISNULL(mrr.addition_cost, 0) - ISNULL(prod.product_cost, 0) AS invoiced_margin
FROM keys k
LEFT JOIN cw.companies c ON c.company_id = k.company_id
LEFT JOIN mrr   ON mrr.company_id = k.company_id AND mrr.month_start = k.month_start
LEFT JOIN inv   ON inv.company_id = k.company_id AND inv.month_start = k.month_start
LEFT JOIN labor ON labor.company_id = k.company_id AND labor.month_start = k.month_start
LEFT JOIN prod  ON prod.company_id = k.company_id AND prod.month_start = k.month_start;
GO

CREATE OR ALTER VIEW rpt.v_client_profitability_annual AS
SELECT company_id, company_name, company_types, territory, YEAR(month_start) AS year,
       SUM(invoiced_revenue) AS invoiced_revenue, SUM(normalized_revenue) AS normalized_revenue,
       SUM(agreement_mrr) AS agreement_revenue, SUM(hours) AS hours, SUM(labor_cost) AS labor_cost,
       SUM(uncosted_hours) AS uncosted_hours, SUM(addition_cost) AS addition_cost,
       SUM(product_cost) AS product_cost, SUM(margin) AS margin, SUM(invoiced_margin) AS invoiced_margin,
       CASE WHEN SUM(normalized_revenue) <> 0 THEN SUM(margin) / SUM(normalized_revenue) END AS margin_pct
FROM rpt.v_client_profitability_monthly
GROUP BY company_id, company_name, company_types, territory, YEAR(month_start);
GO

CREATE OR ALTER VIEW rpt.v_agreement_profitability_annual AS
SELECT agreement_id, agreement_name, type_name, company_id, company_name, YEAR(month_start) AS year,
       SUM(revenue) AS revenue, SUM(hours) AS hours, SUM(labor_cost) AS labor_cost,
       SUM(uncosted_hours) AS uncosted_hours, SUM(addition_cost) AS addition_cost, SUM(margin) AS margin,
       CASE WHEN SUM(revenue) <> 0 THEN SUM(margin) / SUM(revenue) END AS margin_pct
FROM rpt.v_agreement_profitability_monthly
GROUP BY agreement_id, agreement_name, type_name, company_id, company_name, YEAR(month_start);
GO

/* Phones per agent per day, linked to a CW member where possible. */
CREATE OR ALTER VIEW rpt.v_phone_agent_daily AS
SELECT d.*,
       COALESCE(mp.member_identifier, m.identifier) AS member_identifier,
       CASE WHEN d.inbound_calls > 0 THEN CAST(d.answered AS FLOAT) / d.inbound_calls END AS answer_rate,
       CASE WHEN d.answered > 0 THEN d.talk_seconds / d.answered END AS avg_talk_seconds
FROM dp.agent_daily d
LEFT JOIN dp.agent_member_map mp ON mp.agent_name = d.agent_name
LEFT JOIN cw.members m ON m.display_name = d.agent_name AND mp.agent_name IS NULL;
GO

/* CSAT reviews joined to the ticket they rate. */
CREATE OR ALTER VIEW rpt.v_csat AS
SELECT r.*,
       CAST((r.rated_on AT TIME ZONE 'UTC') AT TIME ZONE s.report_timezone AS DATE) AS rated_local_date,
       t.board_name AS cw_board_name, t.company_id, t.owner_identifier, t.closer_identifier,
       t.owner_name, t.priority_name, t.type_name
FROM sb.reviews r
CROSS JOIN meta.settings s
LEFT JOIN rpt.v_tickets t ON t.ticket_id = r.ticket_id;
GO
