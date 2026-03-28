from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
import stripe
import os
from datetime import datetime

app = FastAPI()

stripe.api_key = os.getenv("STRIPE_SECRET_KEY")

pending_vends = []
audit_log = []


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
        audit_log.append({
            "timestamp": datetime.utcnow().isoformat(),
            "source": "online_payment",
            "table": "Table 1",
            "status": "completed"
        })

    return {"received": True}


@app.get("/next-vend")
async def next_vend():
    if pending_vends:
        return pending_vends.pop(0)
    return {"status": "none"}


@app.post("/log-manual-vend")
async def log_manual_vend():
    record = {
        "timestamp": datetime.utcnow().isoformat(),
        "source": "manual_switch",
        "table": "Table 1",
        "status": "completed"
    }
    audit_log.append(record)
    return {"logged": True, "record": record}


@app.get("/audits")
async def audits():
    return audit_log
