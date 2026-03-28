from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
import stripe
import os
from datetime import datetime

app = FastAPI()

stripe.api_key = os.getenv("STRIPE_SECRET_KEY")

pending_vends = []
audit_log = []


def add_audit(source: str, table: str = "Table 1", status: str = "completed", amount_cents: int = 0):
    record = {
        "timestamp": datetime.utcnow().isoformat(),
        "source": source,
        "table": table,
        "status": status,
        "amount_cents": amount_cents,
    }
    audit_log.append(record)
    return record


@app.get("/")
async def root():
    return {"status": "vendplay cloud running"}


@app.get("/buy")
async def buy():
    session = stripe.checkout.Session.create(
        payment_method_types=["card"],
        line_items=[{
            "price_data": {
                "currency": "cad",
                "product_data": {"name": "Table Vend"},
                "unit_amount": 200,
            },
            "quantity": 1,
        }],
        mode="payment",
        success_url=os.getenv("BASE_URL") + "/success",
        cancel_url=os.getenv("BASE_URL") + "/cancel",
    )
    return RedirectResponse(session.url, status_code=303)


@app.get("/success")
async def success():
    return {"status": "payment success"}


@app.get("/cancel")
async def cancel():
    return {"status": "payment cancelled"}


@app.post("/stripe/webhook")
async def stripe_webhook(request: Request):
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature")
    endpoint_secret = os.getenv("STRIPE_WEBHOOK_SECRET")

    event = stripe.Webhook.construct_event(
        payload, sig_header, endpoint_secret
    )

    if event["type"] == "checkout.session.completed":
        pending_vends.append({"status": "pending"})
        add_audit(
            source="online_payment",
            table="Table 1",
            status="completed",
            amount_cents=200,
        )

    return {"received": True}


@app.get("/next-vend")
async def next_vend():
    if pending_vends:
        return pending_vends.pop(0)
    return {"status": "none"}


@app.post("/log-manual-vend")
async def log_manual_vend():
    record = add_audit(
        source="manual_switch",
        table="Table 1",
        status="completed",
        amount_cents=200,
    )
    return {"logged": True, "record": record}


@app.get("/audits")
async def audits():
    return audit_log


@app.get("/audits/summary")
async def audits_summary():
    total_count = len(audit_log)
    total_amount_cents = sum(r.get("amount_cents", 0) for r in audit_log)

    by_source = {}
    by_table = {}

    for r in audit_log:
        source = r.get("source", "unknown")
        table = r.get("table", "unknown")
        amount = r.get("amount_cents", 0)

        if source not in by_source:
            by_source[source] = {
                "count": 0,
                "amount_cents": 0,
            }
        by_source[source]["count"] += 1
        by_source[source]["amount_cents"] += amount

        if table not in by_table:
            by_table[table] = {
                "count": 0,
                "amount_cents": 0,
            }
        by_table[table]["count"] += 1
        by_table[table]["amount_cents"] += amount

    return {
        "total_transactions": total_count,
        "total_amount_cents": total_amount_cents,
        "total_amount_dollars": round(total_amount_cents / 100, 2),
        "by_source": by_source,
        "by_table": by_table,
    }


@app.get("/audits/table/{table_name}")
async def audits_by_table(table_name: str):
    records = [r for r in audit_log if r.get("table") == table_name]

    total_amount_cents = sum(r.get("amount_cents", 0) for r in records)

    return {
        "table": table_name,
        "transaction_count": len(records),
        "total_amount_cents": total_amount_cents,
        "total_amount_dollars": round(total_amount_cents / 100, 2),
        "transactions": records,
    }
