"""notifier: consumes domain events from SQS and raises alerts for the important ones.

Flow in AWS:  deploy/incident service -> SNS topic -> SQS queue -> this worker -> SNS alert topic (email)
If handling a message raises, we do NOT delete it. SQS redelivers it, and after
maxReceiveCount attempts it lands in the dead-letter queue (DLQ).
"""
import json
import logging
import os
import signal
import sys

import boto3

logging.basicConfig(stream=sys.stdout, level=logging.INFO, format="%(message)s")
log = logging.getLogger("notifier")

QUEUE_URL = os.environ["QUEUE_URL"]
ALERT_TOPIC_ARN = os.getenv("ALERT_TOPIC_ARN")  # optional: no email alerts if unset

sqs = boto3.client("sqs")
sns = boto3.client("sns")

running = True


def stop(signum, frame):
    # ECS sends SIGTERM on deploys/scale-in: finish the current batch, then exit cleanly.
    global running
    log.info(json.dumps({"event": "shutdown_requested"}))
    running = False


signal.signal(signal.SIGTERM, stop)
signal.signal(signal.SIGINT, stop)


def should_alert(event):
    etype, data = event.get("type"), event.get("data", {})
    if etype == "incident.opened" and data.get("severity") in ("sev1", "sev2"):
        return True
    if etype == "deployment.recorded" and data.get("status") in ("failed", "rolled_back"):
        return True
    return False


def handle(event):
    log.info(json.dumps({"event": "event_received", "type": event.get("type"), "source": event.get("source")}))
    if not should_alert(event):
        return
    subject = f"[OpsBoard] {event['type']}"
    body = json.dumps(event["data"], indent=2, default=str)
    if ALERT_TOPIC_ARN:
        sns.publish(TopicArn=ALERT_TOPIC_ARN, Subject=subject[:100], Message=body)
    log.info(json.dumps({"event": "alert_sent", "subject": subject, "email": bool(ALERT_TOPIC_ARN)}))


def main():
    log.info(json.dumps({"event": "worker_started", "queue": QUEUE_URL}))
    while running:
        # Long polling (20s) is cheaper and faster than short polling.
        resp = sqs.receive_message(QueueUrl=QUEUE_URL, MaxNumberOfMessages=10, WaitTimeSeconds=20)
        for msg in resp.get("Messages", []):
            try:
                body = json.loads(msg["Body"])
                # SNS wraps the payload in an envelope unless raw message delivery is enabled.
                event = json.loads(body["Message"]) if "Message" in body else body
                handle(event)
                sqs.delete_message(QueueUrl=QUEUE_URL, ReceiptHandle=msg["ReceiptHandle"])
            except Exception:
                log.exception("message_failed")  # left in the queue -> retried -> DLQ
    log.info(json.dumps({"event": "worker_stopped"}))


if __name__ == "__main__":
    main()
