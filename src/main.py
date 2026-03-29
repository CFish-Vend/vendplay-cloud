from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
import stripe
import os
import psycopg
from datetime import datetime, timezone

app = FastAPI()

stripe.api_key = os.getenv("STRIPE_SECRET_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")


def get_conn():
    return psycopg.connect(DATABASE_URL)


def init_db():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS audits (
                    id SERIAL PRIMARY KEY,
                    timestamp TIMESTAMPTZ NOT NULL,
                    source TEXT NOT NULL,
                    table_name TEXT NOT NULL,
                    status TEXT NOT NULL,
                    amount_cents INTEGER NOT NULL DEFAULT 0
                )
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS vend_queue (
                    id SERIAL PRIMARY KEY,
                    timestamp TIMESTAMPTZ NOT NULL,
                    table_name TEXT NOT NULL,
                    status TEXT NOT NULL
                )
            """)
        conn.commit()


def add_audit(source: str, table: str, status: str = "completed", amount_cents: int = 0):
    ts = datetime.now(timezone.utc)

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO audits (timestamp, source, table_name, status, amount_cents)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING id, timestamp, source, table_name, status, amount_cents
                """,
                (ts, source, table, status, amount_cents),
            )
            row = cur.fetchone()
        conn.commit()

    return {
        "id": row[0],
        "timestamp": row[1].isoformat(),
        "source": row[2],
        "table": row[3],
        "status": row[4],
        "amount_cents": row[5],
    }


def queue_vend(table_id: str):
    ts = datetime.now(timezone.utc)

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO vend_queue (timestamp, table_name, status)
                VALUES (%s, %s, %s)
                RETURNING id, timestamp, table_name, status
                """,
                (ts, table_id, "pending"),
            )
            row = cur.fetchone()
        conn.commit()

    return {
        "id": row[0],
        "timestamp": row[1].isoformat(),
        "table": row[2],
        "status": row[3],
    }


def get_next_vend(table: str):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, timestamp, table_name, status
                FROM vend_queue
                WHERE table_name = %s AND status = 'pending'
                ORDER BY timestamp ASC
                LIMIT 1
                """,
                (table,),
            )
            row = cur.fetchone()

            if not row:
                return None

            cur.execute(
                """
                UPDATE vend_queue
                SET status = 'completed'
                WHERE id = %s
                """,
                (row[0],),
            )
        conn.commit()

    return {
        "id": row[0],
        "timestamp": row[1].isoformat(),
        "table": row[2],
        "status": "pending",
    }


def get_all_audits():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, timestamp, source, table_name, status, amount_cents
                FROM audits
                ORDER BY timestamp DESC
            """)
            rows = cur.fetchall()

    return [
        {
            "id": r[0],
            "timestamp": r[1].isoformat(),
            "source": r[2],
            "table": r[3],
            "status": r[4],
            "amount_cents": r[5],
        }
        for r in rows
    ]


@app.on_event("startup")
def startup():
    init_db()


@app.get("/")
async def root():
    return {"status": "vendplay cloud running"}


@app.get("/buy/{table_name}")
async def buy(table_name: str):
    session = stripe.checkout.Session.create(
        payment_method_types=["card"],
        line_items=[{
            "price_data": {
                "currency": "cad",
                "product_data": {"name": f"{table_name} Vend"},
                "unit_amount": 200,
            },
            "quantity": 1,
        }],
        mode="payment",
        success_url=os.getenv("BASE_URL") + f"/success?table={table_name}",
        cancel_url=os.getenv("BASE_URL") + f"/cancel?table={table_name}",
        metadata={"table_id": table_name},
    )
    return RedirectResponse(session.url, status_code=303)


@app.get("/success")
async def success(table: str | None = None):
    return {"status": "payment success", "table": table}


@app.get("/cancel")
async def cancel(table: str | None = None):
    return {"status": "payment cancelled", "table": table}


@app.post("/stripe/webhook")
async def stripe_webhook(request: Request):
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature")
    endpoint_secret = os.getenv("STRIPE_WEBHOOK_SECRET")

    event = stripe.Webhook.construct_event(
        payload, sig_header, endpoint_secret
    )

    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]

        try:
            table_name = session.metadata.get["table_id"]
        except Exception:
            table_name = "tbl_001"

        queue_vend(table_name)
        add_audit(
            source="online_payment",
            table=table_name,
            status="completed",
            amount_cents=200,
        )

    return {"received": True}

@app.get("/next-vend/{table_name}")
async def next_vend(table_name: str):
    record = get_next_vend(table_name)
    if record:
        return {"status": "pending", "table": table_name}
    return {"status": "none", "table": table_name}


@app.post("/log-manual-vend/{table_name}")
async def log_manual_vend(table_name: str):
    record = add_audit(
        source="manual_switch",
        table=table_name,
        status="completed",
        amount_cents=200,
    )
    return {"logged": True, "record": record}


@app.get("/audits")
async def audits():
    return get_all_audits()


@app.get("/audits/summary")
async def audits_summary():
    records = get_all_audits()

    total_count = len(records)
    total_amount_cents = sum(r.get("amount_cents", 0) for r in records)

    by_source = {}
    by_table = {}

    for r in records:
        source = r.get("source", "unknown")
        table = r.get("table", "unknown")
        amount = r.get("amount_cents", 0)

        if source not in by_source:
            by_source[source] = {"count": 0, "amount_cents": 0}
        by_source[source]["count"] += 1
        by_source[source]["amount_cents"] += amount

        if table not in by_table:
            by_table[table] = {"count": 0, "amount_cents": 0}
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
    records = [r for r in get_all_audits() if r.get("table") == table_name]
    total_amount_cents = sum(r.get("amount_cents", 0) for r in records)

    return {
        "table": table_name,
        "transaction_count": len(records),
        "total_amount_cents": total_amount_cents,
        "total_amount_dollars": round(total_amount_cents / 100, 2),
        "transactions": records,
    }
