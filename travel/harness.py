"""Guardrail harness: the layer between your app and the travel model.

The model decides what to say and which tools to call. The harness enforces the rules a small model cannot be
trusted with (behaviour spec section 19):

1. Emergencies get a fixed "call 112" reply before the model is consulted, plus an urgent handoff.
2. Card, CVV, OTP, Aadhaar, PAN and passport numbers are removed before the model or the logs see them.
3. The model can never book. A create_booking call becomes a confirmation summary built from supplier data;
   the booking runs only when the app calls confirm_booking() from an explicit Confirm action.
4. Booking outcomes (confirmed / pending / failed) are written by templates from the tool result.
"""
import dataclasses
import json
import re
import urllib.request

from travel import tools as travel_tools
from travel.india import inr

EMERGENCY = re.compile(
    r"not breathing|can'?t breathe|unconscious|collapsed|not respond|unresponsive|bleeding|heart attack|chest pain|drown|"
    r"\bfire\b|\bsmoke\b|(had|in) an accident|(road|car|bike) accident|hit by a|fell and|has fallen and|fell off|"
    r"child (is )?missing|missing child|lost (my )?(child|son|daughter|kid)|attacked|assault|"
    r"behosh|saans nahi|jawab nahi de|khoon", re.I)
EMERGENCY_TEXT = ("This sounds like an emergency. Please call 112 (India's emergency number) right now and follow the "
                  "dispatcher's instructions. Stay with the person, and don't move anyone who is injured unless they are in "
                  "danger where they are.{handoff} When everyone is safe, I'm here to help with your travel plans.")
PII = [
    (re.compile(r"\b(?:cvv|cvc)\s*:?\s*\d{3,4}\b", re.I), "[CVV removed]"),
    (re.compile(r"\botp\s*:?\s*(?:is\s*)?\d{4,8}\b", re.I), "[OTP removed]"),
    (re.compile(r"\b(?:\d[ -]?){12,18}\d\b"), "[card number removed]"),               # 13-19 digits
    (re.compile(r"\b\d{4}[ -]?\d{4}[ -]?\d{4}\b"), "[ID number removed]"),            # Aadhaar
    (re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b"), "[PAN removed]"),
    (re.compile(r"\b[A-Z]\d{7}\b"), "[passport number removed]"),
]
PII_NOTICE = ("For your safety I removed the card or ID details from your message. Please don't share them in chat; "
              "payment happens only in the secure booking step.\n\n")
FALLBACK = "Sorry, I couldn't complete that just now. Could you say that again in a different way?"


@dataclasses.dataclass
class Reply:
    kind: str                      # "message" | "confirm_booking" | "booking_result" | "emergency"
    text: str                      # show this to the customer
    pending_booking: dict | None = None   # with kind "confirm_booking": data for the Confirm screen


def redact(text):
    found = False
    for pattern, label in PII:
        text, n = pattern.subn(label, text)
        found = found or n > 0
    return text, found


def _rule(offer):
    rule = offer.get("rate_rule") or offer.get("fare_rule") or "Cancellation terms not provided by the supplier"
    return "Non-refundable (no refund if you cancel)" if rule.upper().replace("-", "_") == "NON_REFUNDABLE" else rule


def payable(offer):
    return int(offer.get("total_inr", 0)) + int((offer.get("taxes_estimate_inr") or 0) if offer.get("taxes_included") is False else 0)


def summary(offer, travellers):
    if "hotel" in offer:
        room = offer.get("room")
        product = f"{offer['hotel']}, {room}" if room else offer["hotel"]
        when = (f"{offer['check_in']} to {offer['check_out']} ({offer.get('nights')} nights)" if offer.get("check_in")
                else f"{offer.get('nights')} nights")
    else:
        product = f"{offer.get('airline', 'Flight')} {offer.get('from', '')} to {offer.get('to', '')}"
        when = f"{offer.get('date', '')} departing {offer.get('departs', '')}".strip()
    if offer.get("taxes_included") is True:
        taxes = "taxes included"
    elif offer.get("taxes_included") is False:
        if offer.get("taxes_estimate_inr"):
            taxes = f"includes about {inr(offer['taxes_estimate_inr'])} estimated taxes"
        else:
            taxes = "taxes are extra; the amount is not confirmed"
    else:
        taxes = "tax treatment is not confirmed by the provider"
    return (f"Please confirm this booking:\n- {product}\n- {when}\n- Travellers: {travellers}\n"
            f"- Total: {inr(payable(offer))} ({taxes})\n- Cancellation: {_rule(offer)}\n\n"
            "Press Confirm to book, or tell me what you'd like to change.")


def status_text(result, offer):
    status = str(result.get("status", "")).upper()
    if status == "CONFIRMED":
        return (f"Booked and confirmed. Reference {result.get('booking_ref')}. Charged "
                f"{inr(result.get('charged_inr', payable(offer)))}. Cancellation: {_rule(offer)}.")
    if status.startswith("PENDING"):
        return (f"Your booking request is submitted but not confirmed yet: the supplier still has to accept it "
                f"(request {result.get('request_id')}). Payment is authorised but not taken. Please don't treat it as booked "
                "until I confirm it.")
    reason = str(result.get("reason", "")).upper()
    if reason == "PAYMENT_DECLINED":
        return "The payment was declined, so you are not booked and nothing was charged. You can try another payment method or pick another option."
    if reason in ("NO_LONGER_AVAILABLE", "SOLD_OUT"):
        return "That option is no longer available, so you are not booked and nothing was charged. Shall I look for alternatives?"
    if reason == "PRICE_MISMATCH":
        return (f"The price changed to {inr(result.get('current_total_inr', 0))}, so nothing was booked. "
                "Would you like to see the updated offer?")
    return "The booking did not go through, so you are not booked and nothing was charged."


class OpenAIClient:
    """Calls an OpenAI-compatible server (llama-server, LM Studio / Bionic) with thinking off."""

    def __init__(self, base_url, temperature=0.7, timeout=300):
        self.url = base_url.rstrip("/") + "/v1/chat/completions"
        self.temperature, self.timeout = temperature, timeout

    def __call__(self, messages, tools):
        body = {"model": "local", "messages": messages, "tools": tools, "max_tokens": 600, "temperature": self.temperature,
                "top_p": 0.8, "top_k": 20, "chat_template_kwargs": {"enable_thinking": False}}
        req = urllib.request.Request(self.url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            msg = json.load(r)["choices"][0]["message"]
        return {"content": msg.get("content") or "", "tool_calls": msg.get("tool_calls") or []}


class Harness:
    def __init__(self, llm, backend, system_prompt, today, tools=None, max_tool_rounds=4):
        """llm(messages, tools) -> {"content", "tool_calls"}; backend.call(name, args) -> dict, backend.get_offer(id)."""
        self.llm, self.backend, self.today = llm, backend, today
        self.tools = tools if tools is not None else travel_tools.ALL
        self.system = system_prompt
        self.max_tool_rounds = max_tool_rounds
        self.messages = []          # conversation without the system prompt
        self.pending = None         # booking waiting for the customer's Confirm

    def _tool_names(self):
        return {t["function"]["name"] for t in self.tools}

    def send(self, text):
        if EMERGENCY.search(text):
            return self._emergency(text)
        clean, had_pii = redact(text)
        self.messages.append({"role": "user", "content": clean})
        prefix = PII_NOTICE if had_pii else ""
        for _ in range(self.max_tool_rounds + 1):
            out = self.llm([{"role": "system", "content": self.system}] + self.messages, self.tools)
            calls = out.get("tool_calls") or []
            if not calls:
                content = (out.get("content") or "").strip() or FALLBACK
                self.messages.append({"role": "assistant", "content": content})
                return Reply("message", prefix + content)
            self.messages.append({"role": "assistant", "content": out.get("content") or "", "tool_calls": calls})
            for c in calls:
                name, args = c["function"]["name"], c["function"]["arguments"]
                args = json.loads(args) if isinstance(args, str) else dict(args)
                if name == "create_booking":
                    reply = self._hold_booking(c, args)
                    if reply:
                        return Reply(reply.kind, prefix + reply.text, reply.pending_booking)
                    continue
                result = self.backend.call(name, args) if name in self._tool_names() else {"error": "UNKNOWN_TOOL"}
                self.messages.append({"role": "tool", "tool_call_id": c.get("id", ""), "content": json.dumps(result, ensure_ascii=False)})
        self.messages.append({"role": "assistant", "content": FALLBACK})
        return Reply("message", prefix + FALLBACK)

    def _hold_booking(self, call, args):
        offer = self.backend.get_offer(args.get("offer_id"))
        if offer is None:
            self.messages.append({"role": "tool", "tool_call_id": call.get("id", ""),
                                  "content": json.dumps({"status": "FAILED", "reason": "UNKNOWN_OFFER"})})
            return None
        travellers = args.get("travelers") or offer.get("guests") or offer.get("passengers")
        self.pending = {"offer_id": offer["offer_id"], "travelers": travellers, "total_price_inr": payable(offer), "offer": offer}
        text = summary(offer, travellers)
        self.messages.append({"role": "tool", "tool_call_id": call.get("id", ""),
                              "content": json.dumps({"status": "AWAITING_CUSTOMER_CONFIRMATION"})})
        self.messages.append({"role": "assistant", "content": text})
        return Reply("confirm_booking", text, {k: v for k, v in self.pending.items() if k != "offer"})

    def confirm_booking(self):
        """Call only from an explicit customer action (a Confirm button), never from model output."""
        if not self.pending:
            return Reply("message", "There is no booking waiting for confirmation.")
        p, self.pending = self.pending, None
        args = {"offer_id": p["offer_id"], "travelers": p["travelers"], "total_price_inr": p["total_price_inr"],
                "customer_confirmed_terms": True}
        result = self.backend.call("create_booking", args)
        text = status_text(result, p["offer"])
        self.messages += [{"role": "user", "content": "(The customer pressed Confirm.)"},
                          {"role": "assistant", "content": "", "tool_calls": [{"id": "confirm", "type": "function",
                           "function": {"name": "create_booking", "arguments": json.dumps(args)}}]},
                          {"role": "tool", "tool_call_id": "confirm", "content": json.dumps(result, ensure_ascii=False)},
                          {"role": "assistant", "content": text}]
        return Reply("booking_result", text)

    def cancel_booking(self):
        self.pending = None
        text = "No problem, I haven't booked anything. What would you like to change?"
        self.messages += [{"role": "user", "content": "(The customer did not confirm.)"}, {"role": "assistant", "content": text}]
        return Reply("message", text)

    def _emergency(self, text):
        clean, _ = redact(text)
        handoff = ""
        if "handoff_to_human" in self._tool_names():
            result = self.backend.call("handoff_to_human", {"summary": f"EMERGENCY reported by customer: {clean[:300]}", "urgency": "urgent"})
            if str(result.get("status", "")).upper() == "TRANSFERRED":
                handoff = " I've alerted our support team as urgent."
        reply = EMERGENCY_TEXT.format(handoff=handoff)
        self.messages += [{"role": "user", "content": clean}, {"role": "assistant", "content": reply}]
        return Reply("emergency", reply)
