from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
import stripe
import os

app = FastAPI()

# SET YOUR STRIPE KEY HERE
stripe.api_key = os.getenv("STRIPE_SECRET_KEY")

# Temporary in-memory store
pending_vends = []

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

    return {"received": True}

@app.get("/next-vend")
async def next_vend():
    if pending_vends:
        return pending_vends.pop(0)
    return {"status": "none"}
