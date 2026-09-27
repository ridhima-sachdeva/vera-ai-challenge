import os
import re
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

app = FastAPI(title="Vera Challenge Bot", version="0.1.0")
START = time.time()

# The judge explicitly allows in-memory persistence as long as the bot does not restart.
contexts: dict[tuple[str, str], dict[str, Any]] = {}
conversations: dict[str, dict[str, Any]] = {}
sent_suppression_keys: set[str] = set()
merchant_opt_out: set[str] = set()
merchant_auto_reply_counts: dict[str, int] = {}

VALID_SCOPES = {"category", "merchant", "customer", "trigger"}

AUTO_REPLY_PATTERNS = [
    r"thank you for contacting",
    r"thanks for contacting",
    r"we(?:'|’)ll get back to you",
    r"we will get back to you",
    r"our team will respond",
    r"away message",
    r"business hours",
    r"currently unavailable",
]

OPT_OUT_PATTERNS = [
    r"\bstop\b",
    r"\bnot interested\b",
    r"\bdon'?t message\b",
    r"\bdo not message\b",
    r"\bunsubscribe\b",
    r"\bleave me alone\b",
    r"\bthis is useless\b",
]

COMMIT_PATTERNS = [
    r"\blet'?s do it\b",
    r"\blets do it\b",
    r"\bgo ahead\b",
    r"\bdo it\b",
    r"\byes please\b",
    r"\bproceed\b",
    r"\bstart it\b",
    r"\bsounds good\b",
    r"\bok(?:ay)?[,\s]+do it\b",
]

OFF_TOPIC_PATTERNS = [
    r"\bgst\b",
    r"\btax filing\b",
    r"\bincome tax\b",
    r"\bitr\b",
    r"\bloan\b",
]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def get_payload(scope: str, context_id: Optional[str]) -> Optional[dict]:
    if not context_id:
        return None
    item = contexts.get((scope, context_id))
    return item["payload"] if item else None


def first_name_from_merchant(merchant: dict) -> str:
    ident = merchant.get("identity") or {}
    if ident.get("owner_first_name"):
        return str(ident["owner_first_name"])
    name = str(ident.get("name") or "there").strip()
    # Keep business names intact when an owner name is not available.
    return name


def merchant_display(merchant: dict) -> str:
    ident = merchant.get("identity") or {}
    return str(ident.get("name") or "your business")


def customer_display(customer: Optional[dict]) -> str:
    if not customer:
        return "there"
    ident = customer.get("identity") or {}
    return str(ident.get("name") or "there")


def active_offer(merchant: dict, category: dict) -> Optional[str]:
    offers = merchant.get("offers") or []
    for offer in offers:
        if str(offer.get("status", "")).lower() == "active" and offer.get("title"):
            return str(offer["title"])
    for offer in category.get("offer_catalog") or []:
        if offer.get("title"):
            return str(offer["title"])
    return None


def category_digest_item(category: dict, trigger: dict) -> Optional[dict]:
    digest = category.get("digest") or []
    payload = trigger.get("payload") or {}
    wanted = (
        payload.get("top_item_id")
        or payload.get("digest_item_id")
        or payload.get("item_id")
        or payload.get("research_id")
    )
    if wanted:
        for item in digest:
            if item.get("id") == wanted:
                return item
    return digest[0] if digest else None


def trim(text: str, limit: int = 650) -> str:
    text = re.sub(r"\s+", " ", str(text)).strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def make_suppression_key(trigger: dict, trigger_id: str) -> str:
    return str(trigger.get("suppression_key") or f"trigger:{trigger_id}")


def rationale_for_context(category: dict, merchant: dict, trigger: dict, customer: Optional[dict]) -> str:
    kind = trigger.get("kind", "unknown")
    scope = trigger.get("scope", "merchant")
    bits = [f"Used trigger={kind}", f"scope={scope}", "merchant-specific context"]
    if customer:
        bits.append("customer preferences/relationship")
    if category.get("voice"):
        bits.append("category voice")
    return "; ".join(bits)


def compose_research(category: dict, merchant: dict, trigger: dict) -> tuple[str, str]:
    item = category_digest_item(category, trigger)
    name = first_name_from_merchant(merchant)
    signals = merchant.get("signals") or []
    signal_text = ""
    if signals:
        # Human-readable one useful signal without inventing anything.
        s = str(signals[0]).replace("_", " ").replace(":", " ")
        signal_text = f" This may be relevant given your {s}."

    if item:
        title = item.get("title") or item.get("summary") or "new category update"
        source = item.get("source")
        summary = item.get("summary")
        specific = summary or title
        source_suffix = f" — {source}" if source else ""
        body = (
            f"{name}, quick one: {specific}.{signal_text} "
            f"Want me to turn this into a short customer-ready WhatsApp you can use?{source_suffix}"
        )
    else:
        body = (
            f"{name}, I found a fresh update for your category that may be useful for your business. "
            "Want a 2-line summary plus a customer-ready WhatsApp draft?"
        )
    return trim(body), "open_ended"


def compose_customer_message(category: dict, merchant: dict, trigger: dict, customer: dict) -> tuple[str, str]:
    kind = str(trigger.get("kind") or "")
    name = customer_display(customer)
    business = merchant_display(merchant)
    payload = trigger.get("payload") or {}
    relationship = customer.get("relationship") or {}
    prefs = customer.get("preferences") or {}
    offer = active_offer(merchant, category)

    if kind == "recall_due":
        last_visit = relationship.get("last_visit")
        detail = f" Your last visit was {last_visit}." if last_visit else ""
        offer_text = f" {offer}." if offer else ""
        slot_text = ""
        for key in ("slots", "available_slots", "suggested_slots"):
            slots = payload.get(key)
            if isinstance(slots, list) and slots:
                slot_text = " Available options: " + " / ".join(map(str, slots[:2])) + "."
                break
        body = (
            f"Hi {name}, {business} here.{detail} Your recall is due.{offer_text}{slot_text} "
            "Would you like us to help you book a suitable time?"
        )
        return trim(body), "binary_yes_no"

    if kind in {"customer_lapsed_hard", "winback_due"}:
        services = relationship.get("services_received") or []
        continuity = f" after your {services[-1]}" if services else ""
        offer_text = f" We currently have {offer}." if offer else ""
        body = (
            f"Hi {name}, {business} here. It's been a while since we saw you{continuity}.{offer_text} "
            "Want us to help you pick a convenient time to come back?"
        )
        return trim(body), "binary_yes_no"

    if kind == "chronic_refill_due":
        meds = payload.get("medicines") or payload.get("items") or payload.get("prescriptions")
        med_text = ""
        if isinstance(meds, list) and meds:
            med_text = " for " + ", ".join(map(str, meds[:3]))
        due = payload.get("run_out_date") or payload.get("due_date")
        due_text = f" around {due}" if due else ""
        body = (
            f"Hi {name}, {business} here. Your regular refill{med_text} looks due{due_text}. "
            "Should we keep it ready for pickup/delivery?"
        )
        return trim(body), "binary_yes_no"

    if kind == "bridal_followup":
        wedding = payload.get("wedding_date") or (customer.get("preferences") or {}).get("wedding_date")
        wedding_text = f" ahead of your {wedding} wedding" if wedding else " for your bridal prep"
        offer_text = f" {offer}." if offer else ""
        body = (
            f"Hi {name} 💍 {business} here. Quick follow-up{wedding_text}.{offer_text} "
            "Want us to block your preferred slot for the next session?"
        )
        return trim(body), "binary_yes_no"

    offer_text = f" We currently have {offer}." if offer else ""
    pref = prefs.get("preferred_slots")
    pref_text = f" We can work around your {pref} preference." if pref else ""
    return trim(
        f"Hi {name}, {business} here. Quick update for you.{offer_text}{pref_text} "
        "Would you like the details?"
    ), "binary_yes_no"


def compose_merchant_message(category: dict, merchant: dict, trigger: dict) -> tuple[str, str]:
    kind = str(trigger.get("kind") or "")
    payload = trigger.get("payload") or {}
    name = first_name_from_merchant(merchant)
    perf = merchant.get("performance") or {}
    peer = category.get("peer_stats") or {}
    offer = active_offer(merchant, category)

    if kind in {"research_digest", "compliance_alert", "cde_webinar"}:
        return compose_research(category, merchant, trigger)

    if kind == "curious_ask_due":
        trend = (category.get("trend_signals") or [{}])[0]
        query = trend.get("query")
        delta = trend.get("delta_yoy")
        trend_text = ""
        if query:
            if isinstance(delta, (int, float)):
                trend_text = f" '{query}' is showing {delta * 100:.0f}% YoY movement."
            else:
                trend_text = f" I'm seeing movement around '{query}'."
        offer_text = f" One concrete angle: {offer}." if offer else ""
        body = (
            f"{name}, quick market check.{trend_text}{offer_text} "
            "Want me to turn this into one WhatsApp/GBP idea for this week?"
        )
        return trim(body), "open_ended"

    if kind in {"seasonal_perf_dip", "performance_dip"}:
        views = perf.get("views")
        calls = perf.get("calls")
        delta = (perf.get("delta_7d") or {}).get("views_pct")
        numbers = []
        if views is not None:
            numbers.append(f"{views} views")
        if calls is not None:
            numbers.append(f"{calls} calls")
        if isinstance(delta, (int, float)):
            numbers.append(f"{delta * 100:+.0f}% views vs prior period")
        stats = ", ".join(numbers)
        peer_ctr = peer.get("avg_ctr")
        ctr = perf.get("ctr")
        ctr_text = ""
        if isinstance(ctr, (int, float)) and isinstance(peer_ctr, (int, float)):
            ctr_text = f" Your CTR is {ctr*100:.1f}% vs peer {peer_ctr*100:.1f}%."
        body = (
            f"{name}, your latest pattern{f' is {stats}' if stats else ''}.{ctr_text} "
            "This looks worth interpreting before changing spend. Want the 2-step action I would take?"
        )
        return trim(body), "binary_yes_no"

    if kind == "ipl_match_today":
        event = payload.get("match") or payload.get("event") or payload.get("title") or "today's match"
        offer_text = f" {offer} could be the lead offer." if offer else ""
        body = (
            f"{name}, {event} changes today's demand pattern.{offer_text} "
            "Want a short match-day message tailored to your likely peak window?"
        )
        return trim(body), "binary_yes_no"

    if kind == "active_planning_intent":
        topic = payload.get("topic") or payload.get("intent") or payload.get("request") or "the plan we discussed"
        offer_text = f" We can anchor it around {offer}." if offer else ""
        body = (
            f"{name}, picking up {topic}.{offer_text} "
            "I can turn this into a concrete customer-facing draft now. Shall I draft it?"
        )
        return trim(body), "binary_yes_no"

    if kind in {"supply_alert", "recall_alert"}:
        title = payload.get("title") or payload.get("alert") or payload.get("product") or "a new supply update"
        batches = payload.get("batches")
        batch_text = ""
        if isinstance(batches, list) and batches:
            batch_text = " Affected batches: " + ", ".join(map(str, batches[:5])) + "."
        body = (
            f"{name}, important: {title}.{batch_text} "
            "Please check your stock before dispensing. Want a quick checklist for the team?"
        )
        return trim(body), "binary_yes_no"

    # Generic but grounded fallback.
    trigger_label = kind.replace("_", " ") if kind else "new update"
    offer_text = f" A usable angle is {offer}." if offer else ""
    body = (
        f"{name}, quick update on {trigger_label}.{offer_text} "
        "Want me to turn the current context into one practical next step?"
    )
    return trim(body), "binary_yes_no"


def is_auto_reply(message: str) -> bool:
    msg = message.lower()
    return any(re.search(p, msg) for p in AUTO_REPLY_PATTERNS)


def is_opt_out(message: str) -> bool:
    msg = message.lower()
    return any(re.search(p, msg) for p in OPT_OUT_PATTERNS)


def is_commit(message: str) -> bool:
    msg = message.lower()
    return any(re.search(p, msg) for p in COMMIT_PATTERNS)


def is_off_topic(message: str) -> bool:
    msg = message.lower()
    return any(re.search(p, msg) for p in OFF_TOPIC_PATTERNS)


class ContextBody(BaseModel):
    scope: str
    context_id: str
    version: int
    payload: dict[str, Any]
    delivered_at: Optional[str] = None


class TickBody(BaseModel):
    now: str
    available_triggers: list[str] = Field(default_factory=list)


class ReplyBody(BaseModel):
    conversation_id: str
    merchant_id: Optional[str] = None
    customer_id: Optional[str] = None
    from_role: str
    message: str
    received_at: str
    turn_number: int


@app.get("/")
async def root():
    return {"service": "vera-challenge-bot", "status": "ok"}


@app.get("/v1/healthz")
async def healthz():
    counts = {"category": 0, "merchant": 0, "customer": 0, "trigger": 0}
    for scope, _ in contexts.keys():
        if scope in counts:
            counts[scope] += 1
    return {
        "status": "ok",
        "uptime_seconds": int(time.time() - START),
        "contexts_loaded": counts,
    }


@app.get("/v1/metadata")
async def metadata():
    return {
        "team_name": os.getenv("TEAM_NAME", "Vera Challenge Team"),
        "team_members": [x.strip() for x in os.getenv("TEAM_MEMBERS", "Ridhima").split(",") if x.strip()],
        "model": "deterministic-context-composer-v1",
        "approach": "trigger router + 4-context grounded composer + replay safety rules",
        "contact_email": os.getenv("CONTACT_EMAIL", "replace-me@example.com"),
        "version": "0.1.0",
        "submitted_at": os.getenv("SUBMITTED_AT", utc_now()),
    }


@app.post("/v1/context")
async def push_context(body: ContextBody):
    if body.scope not in VALID_SCOPES:
        return JSONResponse(
            status_code=400,
            content={"accepted": False, "reason": "invalid_scope", "details": f"scope must be one of {sorted(VALID_SCOPES)}"},
        )

    key = (body.scope, body.context_id)
    cur = contexts.get(key)
    if cur:
        if body.version < cur["version"]:
            return JSONResponse(
                status_code=409,
                content={"accepted": False, "reason": "stale_version", "current_version": cur["version"]},
            )
        if body.version == cur["version"]:
            # Idempotent re-post: acknowledge without replacing.
            return {
                "accepted": True,
                "ack_id": f"ack_{body.context_id}_v{body.version}",
                "stored_at": cur.get("stored_at", utc_now()),
            }

    stored_at = utc_now()
    contexts[key] = {"version": body.version, "payload": body.payload, "stored_at": stored_at}
    return {
        "accepted": True,
        "ack_id": f"ack_{body.context_id}_v{body.version}",
        "stored_at": stored_at,
    }


@app.post("/v1/tick")
async def tick(body: TickBody):
    actions = []

    for trigger_id in body.available_triggers[:20]:
        trigger = get_payload("trigger", trigger_id)
        if not trigger:
            continue

        merchant_id = trigger.get("merchant_id")
        customer_id = trigger.get("customer_id")

        if not merchant_id or merchant_id in merchant_opt_out:
            continue

        merchant = get_payload("merchant", merchant_id)
        if not merchant:
            continue

        category_slug = merchant.get("category_slug") or (trigger.get("payload") or {}).get("category")
        category = get_payload("category", category_slug)
        if not category:
            continue

        customer = get_payload("customer", customer_id) if customer_id else None
        if customer_id and not customer:
            continue

        suppression_key = make_suppression_key(trigger, trigger_id)
        if suppression_key in sent_suppression_keys:
            continue

        if customer:
            body_text, cta = compose_customer_message(category, merchant, trigger, customer)
            send_as = "merchant_on_behalf"
        else:
            body_text, cta = compose_merchant_message(category, merchant, trigger)
            send_as = "vera"

        if not body_text:
            continue

        conv_id = f"conv_{uuid.uuid4().hex[:16]}"
        action = {
            "conversation_id": conv_id,
            "merchant_id": merchant_id,
            "customer_id": customer_id,
            "send_as": send_as,
            "trigger_id": trigger_id,
            "template_name": f"vera_{str(trigger.get('kind') or 'generic')}_v1",
            "template_params": [],
            "body": body_text,
            "cta": cta,
            "suppression_key": suppression_key,
            "rationale": rationale_for_context(category, merchant, trigger, customer),
        }

        conversations[conv_id] = {
            "merchant_id": merchant_id,
            "customer_id": customer_id,
            "trigger_id": trigger_id,
            "trigger_kind": trigger.get("kind"),
            "send_as": send_as,
            "last_bot_body": body_text,
            "turns": [{"from": "bot", "msg": body_text}],
            "auto_reply_count": 0,
            "ended": False,
        }
        sent_suppression_keys.add(suppression_key)
        actions.append(action)

    return {"actions": actions}


@app.post("/v1/reply")
async def reply(body: ReplyBody):
    state = conversations.setdefault(
        body.conversation_id,
        {
            "merchant_id": body.merchant_id,
            "customer_id": body.customer_id,
            "trigger_id": None,
            "trigger_kind": None,
            "turns": [],
            "auto_reply_count": 0,
            "ended": False,
        },
    )

    if state.get("ended"):
        return {"action": "end", "rationale": "Conversation already closed."}

    msg = body.message.strip()
    state["turns"].append({"from": body.from_role, "msg": msg})

    # Hard opt-out / frustration: end immediately and suppress the merchant.
    if is_opt_out(msg):
        if body.merchant_id:
            merchant_opt_out.add(body.merchant_id)
        state["ended"] = True
        return {
            "action": "end",
            "rationale": "Explicit opt-out/frustration detected; closing gracefully and suppressing further sends for this merchant.",
        }

    # Auto-reply handling becomes stricter when repeated.
    if is_auto_reply(msg):
      merchant_id = body.merchant_id or state.get("merchant_id") or "unknown"

      merchant_auto_reply_counts[merchant_id] = (
        merchant_auto_reply_counts.get(merchant_id, 0) + 1
      )

      n = merchant_auto_reply_counts[merchant_id]

      if n >= 3:
        state["ended"] = True
        return {
            "action": "end",
            "rationale": "Repeated WhatsApp Business auto-reply detected across conversations; stopping further follow-up.",
        }

      return {
        "action": "wait",
        "wait_seconds": 14400,
        "rationale": "Detected likely WhatsApp Business canned auto-reply; backing off before trying again.",
      }

    if body.merchant_id:
      merchant_auto_reply_counts[body.merchant_id] = 0

    # Explicit commitment must transition to action, not another qualifier.
    if is_commit(msg):
        merchant = get_payload("merchant", body.merchant_id)
        category = get_payload("category", merchant.get("category_slug")) if merchant else None
        offer = active_offer(merchant or {}, category or {}) if merchant and category else None
        offer_text = f" using {offer}" if offer else ""
        response = (
            f"Great — moving to action now{offer_text}. "
            "I’ll turn the current plan into a ready-to-use customer message. Reply CONFIRM to proceed with this draft."
        )
        response = trim(response)
        if response == state.get("last_bot_body"):
            response += " Ready when you are."
        state["last_bot_body"] = response
        state["turns"].append({"from": "bot", "msg": response})
        return {
            "action": "send",
            "body": response,
            "cta": "binary_confirm_cancel",
            "rationale": "Explicit commitment detected; switched immediately from qualification to execution with one concrete next step.",
        }

    # Off-topic request: politely redirect to the original mission.
    if is_off_topic(msg):
        response = (
            "That’s outside what I can handle directly, so I’d leave it to the appropriate professional. "
            "Coming back to the business-growth task we were discussing — want me to prepare the practical next step?"
        )
        response = trim(response)
        state["last_bot_body"] = response
        state["turns"].append({"from": "bot", "msg": response})
        return {
            "action": "send",
            "body": response,
            "cta": "binary_yes_no",
            "rationale": "Declined an out-of-scope request politely and returned to the original Vera task.",
        }

    # General engaged reply: acknowledge and advance without hallucinating.
    if "?" in msg:
        response = (
            "Yes — I can help within the merchant-growth context already shared with me. "
            "I’ll keep it grounded in your current business data and category context. Want the shortest practical recommendation first?"
        )
        cta = "binary_yes_no"
        rationale = "Answered conservatively from available context and offered a low-friction next step."
    else:
        response = (
            "Got it. I’ll keep this practical and use only the business/category context already available. "
            "Want me to turn it into a ready-to-use customer message now?"
        )
        cta = "binary_yes_no"
        rationale = "Acknowledged the reply and advanced toward an executable next step without inventing facts."

    response = trim(response)
    if response == state.get("last_bot_body"):
        response = trim(response + " I can keep it to one concise draft.")
    state["last_bot_body"] = response
    state["turns"].append({"from": "bot", "msg": response})

    return {"action": "send", "body": response, "cta": cta, "rationale": rationale}


@app.post("/v1/teardown")
async def teardown():
    contexts.clear()
    conversations.clear()
    sent_suppression_keys.clear()
    merchant_opt_out.clear()
    merchant_auto_reply_counts.clear()
    return {"status": "cleared"}
