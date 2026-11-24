"""deploy-service: records deployments (what version of which service went where, and how it went)."""
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

SERVICE = "deploy-service"
VALID_ENVS = {"dev", "staging", "prod"}
VALID_STATUSES = {"success", "failed", "rolled_back"}

# Log JSON to stdout: ECS ships it to CloudWatch Logs, where Logs Insights can query the fields.
logging.basicConfig(stream=sys.stdout, level=logging.INFO, format="%(message)s")
log = logging.getLogger(SERVICE)

app = Flask(__name__)


def get_conn():
    # In AWS, ECS injects these. DB_PASSWORD comes from Secrets Manager.
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
                        CREATE TABLE IF NOT EXISTS deployments (
                            id SERIAL PRIMARY KEY,
                            service TEXT NOT NULL,
                            version TEXT NOT NULL,
                            environment TEXT NOT NULL,
                            status TEXT NOT NULL,
                            deployed_by TEXT NOT NULL DEFAULT 'unknown',
                            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                        )
                        """
                    )
            log.info(json.dumps({"event": "db_ready"}))
            return
        except psycopg2.OperationalError as exc:
            log.warning(json.dumps({"event": "db_wait", "attempt": attempt, "error": str(exc).strip()}))
            time.sleep(delay)
    raise RuntimeError("Database not reachable; exiting so ECS restarts the task")


# --- Events ------------------------------------------------------------------
_sns = None


def publish_event(event_type, data):
    """Publish a domain event to SNS. Failure must never fail the user's request (loose coupling)."""
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


# --- Health ------------------------------------------------------------------
# /health : liveness, used by the ALB target group. Does NOT touch the DB, otherwise a
#           DB blip makes the ALB mark every healthy task unhealthy and restart them all.
# /ready  : readiness, checks the DB. Use it for debugging and dashboards.
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


# --- API ---------------------------------------------------------------------
@app.get("/api/deployments")
def list_deployments():
    env = request.args.get("environment")
    query = "SELECT * FROM deployments"
    params = ()
    if env:
        query += " WHERE environment = %s"
        params = (env,)
    query += " ORDER BY id DESC LIMIT 50"
    with closing(get_conn()) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(query, params)
        return jsonify(cur.fetchall())


@app.post("/api/deployments")
def create_deployment():
    body = request.get_json(silent=True) or {}
    service = str(body.get("service", "")).strip()
    version = str(body.get("version", "")).strip()
    environment = body.get("environment", "dev")
    status = body.get("status", "success")
    deployed_by = str(body.get("deployed_by", "unknown")).strip() or "unknown"

    if not service or not version:
        return jsonify(error="service and version are required"), 400
    if environment not in VALID_ENVS:
        return jsonify(error=f"environment must be one of {sorted(VALID_ENVS)}"), 400
    if status not in VALID_STATUSES:
        return jsonify(error=f"status must be one of {sorted(VALID_STATUSES)}"), 400

    with closing(get_conn()) as conn:
        with conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """INSERT INTO deployments (service, version, environment, status, deployed_by)
                   VALUES (%s, %s, %s, %s, %s) RETURNING *""",
                (service, version, environment, status, deployed_by),
            )
            row = cur.fetchone()

    publish_event("deployment.recorded", row)
    return jsonify(row), 201


init_db()
