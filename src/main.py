from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse, RedirectResponse
import stripe
import os
import psycopg2
from contextlib import contextmanager

# force deploy

app = FastAPI()

# ======================
# CONFIG
# ======================

WEBHOOK_PATH = "/stripe/webhook"
NEXT_VEND_PATH = "/next-vend"

STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET")
BASE_URL = os.getenv("BASE_URL")

stripe.api_key = STRIPE_SECRET_KEY

DATABASE_URL = os.getenv("DATABASE_URL")

# ======================
# DB
# ======================

@contextmanager
def get_conn():
    conn = psycopg2.connect(DATABASE_URL)
    try:
        yield conn
    finally:
        conn.close()

def init_db():
    with get_conn() as conn:
        with conn.cursor() as cur:

            cur.execute("""
            CREATE TABLE IF NOT EXISTS vend_queue (
                id SERIAL PRIMARY KEY,
                table_id TEXT,
                status TEXT DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT NOW()
            );
            """)

            cur.execute("""
            CREATE TABLE IF NOT EXISTS audits (
                id SERIAL PRIMARY KEY,
                table_id TEXT,
                source TEXT,
                amount_cents INTEGER,
                created_at TIMESTAMP DEFAULT NOW()
            );
            """)

            cur.execute("""
            CREATE TABLE IF NOT EXISTS table_config (
                table_id TEXT PRIMARY KEY,
                display_name TEXT NOT NULL,
                price_cents INTEGER NOT NULL,
                active BOOLEAN DEFAULT TRUE,
                free_play BOOLEAN DEFAULT FALSE
            );
            """)

            cur.execute("""
            CREATE TABLE IF NOT EXISTS device_status (
                table_id TEXT PRIMARY KEY,
                last_seen TIMESTAMP DEFAULT NOW()
            );
            """)

            # Seed tables
            cur.execute("""
            INSERT INTO table_config (table_id, display_name, price_cents)
            VALUES
            ('tbl_001', 'Table 1', 200),
            ('tbl_002', 'Table 2', 200)
            ON CONFLICT (table_id) DO NOTHING;
            """)

            cur.execute("""
            CREATE TABLE IF NOT EXISTS processed_events (
                event_id TEXT PRIMARY KEY
            );
            """)

            conn.commit()

init_db()

# ======================
# QUEUE
# ======================

def queue_vend(table_id):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO vend_queue (table_id) VALUES (%s)",
                (table_id,)
            )
            conn.commit()

def get_next_vend(table_id):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id FROM vend_queue
                WHERE table_id = %s AND status = 'pending'
                ORDER BY created_at ASC
                LIMIT 1
            """, (table_id,))
            row = cur.fetchone()

            if not row:
                return None

            vend_id = row[0]

            cur.execute("""
                UPDATE vend_queue
                SET status = 'sent'
                WHERE id = %s
            """, (vend_id,))

            conn.commit()

            return vend_id

# ======================
# BUY
# ======================

@app.get("/buy/{table_id}")
def buy(table_id: str):

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT price_cents, free_play, active
                FROM table_config
                WHERE table_id = %s
            """, (table_id,))
            row = cur.fetchone()

    if not row:
        raise HTTPException(status_code=404, detail="Invalid table")

    price_cents, free_play, active = row

    if not active:
        return {"status": "table_disabled"}

    if free_play:
        return {"status": "free_play_enabled"}

    session = stripe.checkout.Session.create(
        payment_method_types=["card"],
        line_items=[{
            "price_data": {
                "currency": "usd",
                "product_data": {"name": f"Vend {table_id}"},
                "unit_amount": price_cents,
            },
            "quantity": 1,
        }],
        mode="payment",
        success_url=f"{BASE_URL}/success?table_id={table_id}",
        cancel_url=f"{BASE_URL}/cancel",
        metadata={"table_id": table_id}
    )

    return RedirectResponse(session.url, status_code=303)

# ======================
# WEBHOOK
# ======================

@app.post(WEBHOOK_PATH)
async def stripe_webhook(request: Request):

    payload = await request.body()
    sig_header = request.headers.get("stripe-signature")

    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, STRIPE_WEBHOOK_SECRET
        )
            event_id = event["id"]
    except Exception as e:
        return JSONResponse(status_code=400, content={"error": str(e)})

    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        table_id = session["metadata"]["table_id"]

        print("WEBHOOK RECEIVED:", table_id, event_id)

        try:
            with get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        SELECT event_id FROM processed_events
                        WHERE event_id = %s
                        LIMIT 1
                    """, (event_id,))
                    existing = cur.fetchone()

                    if existing:
                        print("Duplicate Stripe event ignored")
                    else:
                        cur.execute("""
                            INSERT INTO processed_events (event_id)
                            VALUES (%s)
                        """, (event_id,))
                        conn.commit()

                        queue_vend(table_id)
        except Exception as e:
            print("QUEUE ERROR:", e)

    return {"status": "ok"}

# ======================
# HUB POLL
# ======================

@app.get(f"{NEXT_VEND_PATH}/{{table_id}}")
def next_vend(table_id: str):

    vend_id = get_next_vend(table_id)

    if not vend_id:
        return {"status": "none"}

    return {
        "status": "pending",
        "table": table_id,
        "vend_id": vend_id
    }

# ======================
# MANUAL VEND
# ======================

@app.post("/manual-vend/{table_id}")
def manual_vend(table_id: str):

    with get_conn() as conn:
        with conn.cursor() as cur:

            cur.execute("""
                SELECT price_cents
                FROM table_config
                WHERE table_id = %s
            """, (table_id,))
            row = cur.fetchone()

            if not row:
                raise HTTPException(status_code=404, detail="Invalid table")

            price_cents = row[0]

            cur.execute("""
                INSERT INTO audits (table_id, source, amount_cents)
                VALUES (%s, %s, %s)
            """, (
                table_id,
                "manual_switch",
                price_cents
            ))

            conn.commit()

    return {"status": "logged"}

# ======================
# HEARTBEAT
# ======================

@app.post("/heartbeat/{table_id}")
async def heartbeat(table_id: str):

    with get_conn() as conn:
        with conn.cursor() as cur:

            cur.execute("""
                INSERT INTO device_status (table_id, last_seen)
                VALUES (%s, NOW())
                ON CONFLICT (table_id)
                DO UPDATE SET last_seen = NOW();
            """, (table_id,))

            conn.commit()

    return {"status": "ok"}

@app.get("/success")
def success(table_id: str = None):
    if table_id:
        print("SUCCESS PAGE VEND:", table_id)
        queue_vend(table_id)

    return {"status": "payment success"}

@app.get("/cancel")
def cancel():
    return {"status": "payment cancelled"}
