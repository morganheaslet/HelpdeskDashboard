"""
ConnectWise finance -> th2-metrics-db, for client and contract profitability.

- Invoices and procurement products: incremental by lastUpdated, using the
  same windowed backfill as tickets (sync_cw._windowed_sync).
- Agreements: a few hundred rows — fully re-pulled once a day.
- Agreement additions (per-seat/per-device lines): ConnectWise only exposes
  them per agreement (one call each), so each run refreshes the next batch of
  agreements in id order within its time budget and wraps around — every
  agreement is refreshed every few hours without one run doing hundreds of
  calls. Additions missing from an agreement's latest pull are marked deleted.
"""
import logging
from datetime import datetime, timezone

from shared import db
from shared.sync_cw import _id, _name, _s, _num, _date, _info, _windowed_sync

INVOICE_FIELDS = [
    "id", "invoiceNumber", "type", "date", "company", "agreement", "applyToType", "applyToId",
    "ticket", "project", "status", "agreementAmount", "serviceTotal", "productTotal",
    "expenseTotal", "serviceAdjustmentAmount", "subtotal", "salesTax", "total", "credits",
    "payments", "balance", "_info/lastUpdated",
]

PRODUCT_FIELDS = [
    "id", "catalogItem", "description", "productClass", "company", "agreement", "ticket",
    "invoice", "quantity", "price", "cost", "billableOption", "cancelledFlag",
    "_info/dateEntered", "_info/lastUpdated",
]


def months_per_cycle(cycle_name):
    """Billing cycle name -> months it covers. ConnectWise cycle names are
    configurable per instance, so this matches on keywords; anything it
    can't place is stored NULL and logged (the MRR view then excludes it
    rather than guessing)."""
    n = (cycle_name or "").lower().replace("-", "").replace(" ", "")
    if not n:
        return None
    if "bimonth" in n:
        return 2
    if "quarter" in n:
        return 3
    if "semi" in n or "biannual" in n or "halfyear" in n or "6month" in n:
        return 6
    if "biennial" in n or "2year" in n:
        return 24
    if "year" in n or "annual" in n:
        return 12
    if "month" in n:
        return 1
    return None


def _invoice_rows(records, now):
    return [{
        "invoice_id": i["id"],
        "invoice_number": _s(i.get("invoiceNumber"), 50),
        "invoice_type": _s(i.get("type"), 30),
        "invoice_date": _date(i.get("date")),
        "company_id": _id(i.get("company")), "company_name": _name(i.get("company"), 200),
        "agreement_id": _id(i.get("agreement")),
        "apply_to_type": _s(i.get("applyToType"), 30), "apply_to_id": i.get("applyToId"),
        "ticket_id": _id(i.get("ticket")), "project_id": _id(i.get("project")),
        "status_name": _name(i.get("status")),
        "agreement_amount": i.get("agreementAmount"), "service_total": i.get("serviceTotal"),
        "product_total": i.get("productTotal"), "expense_total": i.get("expenseTotal"),
        "service_adjustment": i.get("serviceAdjustmentAmount"),
        "subtotal": i.get("subtotal"), "sales_tax": i.get("salesTax"), "total": i.get("total"),
        "credits": i.get("credits"), "payments": i.get("payments"), "balance": i.get("balance"),
        "last_updated": db.parse_ts(_info(i, "lastUpdated")),
        "synced_at": now,
    } for i in records]


def _product_rows(records, now):
    return [{
        "product_item_id": p["id"],
        "catalog_identifier": _s((p.get("catalogItem") or {}).get("identifier"), 100),
        "description": _s(p.get("description"), 300),
        "product_class": _s(p.get("productClass"), 30),
        "company_id": _id(p.get("company")), "agreement_id": _id(p.get("agreement")),
        "ticket_id": _id(p.get("ticket")), "invoice_id": _id(p.get("invoice")),
        "quantity": p.get("quantity"), "price": p.get("price"), "cost": p.get("cost"),
        "billable_option": _s(p.get("billableOption"), 20),
        "cancelled_flag": p.get("cancelledFlag"),
        "date_entered": db.parse_ts(_info(p, "dateEntered")),
        "last_updated": db.parse_ts(_info(p, "lastUpdated")),
        "synced_at": now,
    } for p in records]


def sync_invoices(cw, conn, deadline):
    return _windowed_sync(cw, conn, "cw_invoices", "/finance/invoices", INVOICE_FIELDS, _invoice_rows, deadline)


def sync_products(cw, conn, deadline):
    return _windowed_sync(cw, conn, "cw_products", "/procurement/products", PRODUCT_FIELDS, _product_rows, deadline)


def sync_agreements(cw, conn):
    now = db.utcnow()
    rows, unknown_cycles = [], set()
    for a in cw.get_all("/finance/agreements"):
        cycle = _name(a.get("billingCycle"), 50)
        months = months_per_cycle(cycle)
        if cycle and months is None:
            unknown_cycles.add(cycle)
        rows.append({
            "agreement_id": a["id"], "name": _s(a.get("name"), 200),
            "type_name": _name(a.get("type")),
            "company_id": _id(a.get("company")), "company_name": _name(a.get("company"), 200),
            "parent_agreement_id": _id(a.get("parentAgreement")),
            "agreement_status": _s(a.get("agreementStatus"), 30),
            "cancelled_flag": a.get("cancelledFlag"),
            "date_cancelled": db.parse_ts(a.get("dateCancelled")),
            "start_date": _date(a.get("startDate")), "end_date": _date(a.get("endDate")),
            "no_ending_date": a.get("noEndingDateFlag"),
            "bill_amount": a.get("billAmount"),
            "billing_cycle_name": cycle, "months_per_cycle": months,
            "application_units": _s(a.get("applicationUnits"), 20),
            "application_limit": a.get("applicationLimit"),
            "application_unlimited": a.get("applicationUnlimitedFlag"),
            "application_cycle": _s(a.get("applicationCycle"), 30),
            "one_time_flag": a.get("oneTimeFlag"),
            "sla_id": _id(a.get("sla")),
            "last_updated": db.parse_ts(_info(a, "lastUpdated")),
            "synced_at": now,
        })
    if unknown_cycles:
        logging.warning(
            "Unrecognized agreement billing cycles %s — those agreements are left out of MRR "
            "until sync_finance.months_per_cycle() learns them", sorted(unknown_cycles),
        )
    return db.upsert(conn, "cw.agreements", ["agreement_id"], rows)


def sync_additions(cw, conn, deadline):
    state = db.get_state(conn, "cw_additions")
    after_id = state["cursor_id"] or 0
    cur = conn.cursor()
    cur.execute(
        "SELECT agreement_id FROM cw.agreements WHERE agreement_id > ? ORDER BY agreement_id", after_id,
    )
    queue = [r[0] for r in cur.fetchall()]
    if not queue:  # wrapped around — start the next lap
        cur.execute("SELECT agreement_id FROM cw.agreements ORDER BY agreement_id")
        queue = [r[0] for r in cur.fetchall()]
    total = 0
    for agreement_id in queue:
        if datetime.now(timezone.utc) >= deadline:
            break
        now = db.utcnow()
        additions = cw.get_all(f"/finance/agreements/{agreement_id}/additions")
        rows = [{
            "addition_id": x["id"], "agreement_id": agreement_id,
            "product_identifier": _s((x.get("product") or {}).get("identifier"), 100),
            "description": _s(x.get("description") or (x.get("product") or {}).get("description"), 300),
            "quantity": x.get("quantity"), "less_included": x.get("lessIncluded"),
            "billed_quantity": x.get("billedQuantity"),
            "unit_price": x.get("unitPrice"), "unit_cost": x.get("unitCost"),
            "ext_price": x.get("extPrice"), "ext_cost": x.get("extCost"),
            "bill_customer": _s(x.get("billCustomer"), 20),
            "effective_date": _date(x.get("effectiveDate")),
            "cancelled_date": _date(x.get("cancelledDate")),
            "agreement_status": _s(x.get("agreementStatus"), 30),
            "last_updated": db.parse_ts(_info(x, "lastUpdated")),
            "deleted": False, "synced_at": now,
        } for x in additions]
        total += db.upsert(conn, "cw.agreement_additions", ["addition_id"], rows)
        live = [r["addition_id"] for r in rows]
        if live:
            cur.execute(
                f"UPDATE cw.agreement_additions SET deleted = 1, synced_at = ? "
                f"WHERE agreement_id = ? AND deleted = 0 AND addition_id NOT IN ({','.join('?' * len(live))})",
                now, agreement_id, *live,
            )
        else:
            cur.execute(
                "UPDATE cw.agreement_additions SET deleted = 1, synced_at = ? WHERE agreement_id = ? AND deleted = 0",
                now, agreement_id,
            )
        db.set_state(conn, "cw_additions", cursor_id=agreement_id)
        conn.commit()
    return total
