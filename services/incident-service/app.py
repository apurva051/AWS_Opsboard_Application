"""incident-service: tracks production incidents (open -> resolved)."""
import json
import logging
import os
import sys
import time
from contextlib import closing

import boto3
import psycopg2
from botocore.config import Config
from flask import Flask, g, jsonify, request
from psycopg2.extras import RealDictCursor

SERVICE = "incident-service"
VALID_SEVERITIES = {"sev1", "sev2", "sev3"}

logging.basicConfig(stream=sys.stdout, level=logging.INFO, format="%(message)s")
log = logging.getLogger(SERVICE)

app = Flask(__name__)


def get_conn():
    return psycopg2.connect(
        host=os.environ["DB_HOST"],
        port=os.getenv("DB_PORT", "5432"),
        dbname=os.environ["DB_NAME"],
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"],
        connect_timeout=5,
    )


def init_db(retries=10, delay=3):
    for attempt in range(1, retries + 1):
        try:
            with closing(get_conn()) as conn:
                with conn, conn.cursor() as cur:
                    cur.execute(
                        """
                        CREATE TABLE IF NOT EXISTS incidents (
                            id SERIAL PRIMARY KEY,
                            title TEXT NOT NULL,
                            service TEXT NOT NULL,
                            severity TEXT NOT NULL,
                            status TEXT NOT NULL DEFAULT 'open',
                            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                            resolved_at TIMESTAMPTZ
                        )
                        """
                    )
            log.info(json.dumps({"event": "db_ready"}))
            return
        except psycopg2.OperationalError as exc:
            log.warning(json.dumps({"event": "db_wait", "attempt": attempt, "error": str(exc).strip()}))
            time.sleep(delay)
    raise RuntimeError("Database not reachable; exiting so ECS restarts the task")


_sns = None


def publish_event(event_type, data):
    """Publish a domain event to SNS. Failure must never fail the user's request."""
    global _sns
    message = {"type": event_type, "source": SERVICE, "data": data}
    topic = os.getenv("EVENT_TOPIC_ARN")
    if not topic:
        log.info(json.dumps({"event": "event_local_only", "message": message}, default=str))
        return
    try:
        if _sns is None:
            _sns = boto3.client("sns", config=Config(connect_timeout=2, read_timeout=3, retries={"max_attempts": 1}))
        _sns.publish(
            TopicArn=topic,
            Message=json.dumps(message, default=str),
            MessageAttributes={"type": {"DataType": "String", "StringValue": event_type}},
        )
    except Exception:
        log.exception("event_publish_failed")


@app.before_request
def start_timer():
    g.start = time.time()


@app.after_request
def log_request(resp):
    log.info(
        json.dumps(
            {
                "event": "request",
                "service": SERVICE,
                "method": request.method,
                "path": request.path,
                "status": resp.status_code,
                "ms": round((time.time() - g.start) * 1000, 1),
            }
        )
    )
    return resp


@app.get("/health")
def health():
    return jsonify(status="ok", service=SERVICE)


@app.get("/ready")
def ready():
    try:
        with closing(get_conn()) as conn, conn.cursor() as cur:
            cur.execute("SELECT 1")
        return jsonify(status="ready", service=SERVICE)
    except Exception as exc:
        return jsonify(status="db_unreachable", error=str(exc)), 503


@app.get("/api/incidents")
def list_incidents():
    status = request.args.get("status")
    query = "SELECT * FROM incidents"
    params = ()
    if status:
        query += " WHERE status = %s"
        params = (status,)
    query += " ORDER BY id DESC LIMIT 50"
    with closing(get_conn()) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(query, params)
        return jsonify(cur.fetchall())


@app.post("/api/incidents")
def open_incident():
    body = request.get_json(silent=True) or {}
    title = str(body.get("title", "")).strip()
    service = str(body.get("service", "")).strip()
    severity = body.get("severity", "sev3")

    if not title or not service:
        return jsonify(error="title and service are required"), 400
    if severity not in VALID_SEVERITIES:
        return jsonify(error=f"severity must be one of {sorted(VALID_SEVERITIES)}"), 400

    with closing(get_conn()) as conn:
        with conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "INSERT INTO incidents (title, service, severity) VALUES (%s, %s, %s) RETURNING *",
                (title, service, severity),
            )
            row = cur.fetchone()

    publish_event("incident.opened", row)
    return jsonify(row), 201


@app.post("/api/incidents/<int:incident_id>/resolve")
def resolve_incident(incident_id):
    with closing(get_conn()) as conn:
        with conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """UPDATE incidents SET status = 'resolved', resolved_at = NOW()
                   WHERE id = %s AND status = 'open' RETURNING *""",
                (incident_id,),
            )
            row = cur.fetchone()
    if not row:
        return jsonify(error="incident not found or already resolved"), 404

    publish_event("incident.resolved", row)
    return jsonify(row)


init_db()
