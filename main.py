"""WhatsApp Cloud API echo bot (FastAPI). Same env vars and behavior as the Node version."""
import hashlib
import hmac
import json
import logging
import os
from collections import OrderedDict

import httpx
from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, Request
from fastapi.responses import PlainTextResponse, Response

load_dotenv()  # reads .env locally; on Render the dashboard env vars are used

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("wa-bot")


def env(key: str, default: str = "") -> str:
    # strip() kills stray spaces/newlines pasted into env vars
    return (os.getenv(key) or default).strip()


VERIFY_TOKEN = env("VERIFY_TOKEN")
PHONE_NUMBER_ID = env("PHONE_NUMBER_ID")
ACCESS_TOKEN = env("ACCESS_TOKEN")
APP_SECRET = env("APP_SECRET")  # Meta app -> Settings -> Basic -> App secret
GRAPH_VERSION = env("GRAPH_VERSION", "v26.0")
LOG_BODY = env("LOG_BODY", "1") != "0"  # set LOG_BODY=0 to silence body logs

for k, v in {"VERIFY_TOKEN": VERIFY_TOKEN, "PHONE_NUMBER_ID": PHONE_NUMBER_ID, "ACCESS_TOKEN": ACCESS_TOKEN}.items():
    if not v:
        log.warning("env var %s is not set", k)
if not APP_SECRET:
    log.warning("APP_SECRET not set, webhook signatures are NOT verified")
log.info("Token length: %d, phone id: %s, graph: %s", len(ACCESS_TOKEN), PHONE_NUMBER_ID, GRAPH_VERSION)

app = FastAPI()

# dedupe: Meta retries deliveries, don't reply twice to the same message
_seen: "OrderedDict[str, None]" = OrderedDict()


def is_duplicate(msg_id: str | None) -> bool:
    if not msg_id:
        return False
    if msg_id in _seen:
        return True
    _seen[msg_id] = None
    if len(_seen) > 1000:
        _seen.popitem(last=False)
    return False


def valid_signature(raw: bytes, header: str) -> bool:
    if not APP_SECRET:
        return True
    expected = "sha256=" + hmac.new(APP_SECRET.encode(), raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(header or "", expected)


@app.middleware("http")
async def request_logger(request: Request, call_next):
    log.info("%s %s", request.method, request.url.path + (f"?{request.url.query}" if request.url.query else ""))
    return await call_next(request)


# health check (Render + uptime pinger)
@app.get("/")
@app.head("/")
async def health():
    return PlainTextResponse("ok")


# webhook verification (Meta calls this when you save the webhook URL)
@app.get("/webhook")
async def verify(request: Request):
    q = request.query_params
    if q.get("hub.mode") == "subscribe" and q.get("hub.verify_token") == VERIFY_TOKEN:
        log.info("Webhook verified")
        return PlainTextResponse(q.get("hub.challenge", ""))
    log.warning("Webhook verification FAILED (mode/token mismatch)")
    return Response(status_code=403)


# incoming messages
@app.post("/webhook")
async def webhook(request: Request, background: BackgroundTasks):
    raw = await request.body()  # raw bytes, needed for the signature check

    if not valid_signature(raw, request.headers.get("x-hub-signature-256", "")):
        log.warning("Invalid signature -> 401. Check APP_SECRET on Render (no spaces, correct app).")
        return Response(status_code=401)

    try:
        body = json.loads(raw or b"{}")
    except json.JSONDecodeError:
        log.warning("Bad JSON body")
        return Response(status_code=400)

    log.info("POST /webhook received")
    if LOG_BODY:
        log.info("WEBHOOK BODY: %s", json.dumps(body))

    try:
        for entry in body.get("entry") or []:
            for change in entry.get("changes") or []:
                value = change.get("value") or {}

                # delivery/read/failed status updates
                for s in value.get("statuses") or []:
                    log.info("Status: %s -> %s %s", s.get("status"), s.get("recipient_id"),
                             json.dumps(s["errors"]) if s.get("errors") else "")

                for message in value.get("messages") or []:
                    if is_duplicate(message.get("id")):
                        log.info("Duplicate message, skipping: %s", message.get("id"))
                        continue
                    sender = message.get("from")
                    mtype = message.get("type")
                    text = (message.get("text") or {}).get("body") if mtype == "text" else None
                    log.info("Message from %s: %s", sender, text if text is not None else f"[{mtype}]")

                    reply = f"You said: {text}" if text else f"Got a {mtype} message, I only read text for now."
                    # runs after the 200 is sent, so Meta gets its ack fast
                    background.add_task(send_message, sender, reply)
    except Exception:
        log.exception("handler error")

    return Response(status_code=200)


async def send_message(to: str, body: str) -> None:
    url = f"https://graph.facebook.com/{GRAPH_VERSION}/{PHONE_NUMBER_ID}/messages"
    payload = {"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": body}}
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(url, json=payload, headers={"Authorization": f"Bearer {ACCESS_TOKEN}"})
        log.info("Graph reply status: %s %s", r.status_code, r.text)
        if r.status_code >= 400:
            log.error("send failed: Graph API %s: %s", r.status_code, r.text)
    except Exception as e:
        log.error("send failed: %s", e)