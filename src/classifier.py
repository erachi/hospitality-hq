"""Issue classification and response drafting using Claude API."""

import anthropic
from functools import lru_cache
from config import get_anthropic_key, CLASSIFY_MODEL, DRAFT_MODEL


@lru_cache(maxsize=1)
def _get_client():
    """Cached Anthropic client — created once per Lambda cold start."""
    return anthropic.Anthropic(api_key=get_anthropic_key())


CLASSIFICATION_PROMPT = """You are a guest issue classifier for a short-term rental property management system.

Classify the following guest message into exactly ONE category and ONE urgency level, and decide whether the host needs to send a response.

Categories:
- URGENT_MAINTENANCE: Lockouts, broken AC/heating, plumbing issues, electrical problems, safety hazards, no hot water, WiFi completely down
- COMPLAINT: Cleanliness issues, noise complaints, missing amenities, property not matching description, neighbor issues
- PRE_ARRIVAL: Check-in questions, directions, early check-in requests, luggage drop-off, parking questions
- GENERAL: Pool heating requests, vendor approvals, general questions, late checkout requests, extra supplies
- POSITIVE: Compliments, thank-yous, positive feedback, expressing enjoyment

Urgency levels:
- HIGH: Needs response within 1 hour (safety, lockouts, no AC in summer, plumbing emergencies)
- MEDIUM: Needs response within 4 hours (complaints, pre-arrival day-of questions)
- LOW: Can wait 12+ hours (general inquiries, positive feedback, future-dated questions)

Response needed:
- YES if the message contains a question, a request, a problem to resolve, or anything that would feel rude to leave unanswered.
- NO only for pure thanks/compliments/FYI with no embedded ask. A "thank you, and by the way can you…" still needs a response.

Descriptor:
- 2-4 words naming WHAT the guest wants or reported, in title case.
- Action-oriented noun phrase, not a category label.
- Good: "Early check-in request", "AC not working", "Late checkout request", "Noise complaint", "Thank you note".
- Bad: "Positive feedback", "General", "Urgent maintenance" (these are categories, not descriptors).

Respond in this exact format (no other text):
CATEGORY: <category>
URGENCY: <urgency>
RESPONSE_NEEDED: <YES|NO>
DESCRIPTOR: <2-4 word noun phrase>
SUMMARY: <one-line summary of the issue>"""


DRAFT_RESPONSE_PROMPT = """You are a warm, professional hospitality assistant for a luxury Scottsdale vacation rental.

Your job is to draft a response to a guest message. The host will review and approve before sending.

Sources of truth (use in this priority order):
1. AUTHORITATIVE FACTS from our curated knowledge base — these are ground truth. Cite them exactly; do not contradict or paraphrase into something different.
2. INTERNAL NOTES / ISSUES / RESOLUTIONS — decisions the host team has already made about THIS reservation in prior Slack threads. Honor them: if an issue is marked resolved, treat it as resolved. If there's a note about the guest, incorporate it. Never expose the literal note text to the guest.
3. PAST GUEST Q&A — examples of how similar questions were answered before. Use these as both a style and content guide.
4. PRECEDENTS — what we've done in similar situations in the past.
5. Property description and Hospitable Knowledge Hub — supplemental context only, use when the above don't cover the question.

Guidelines:
- Be warm, friendly, and genuinely helpful
- Be concise — guests don't want essays
- When a fact exists in AUTHORITATIVE FACTS, use it verbatim. Do NOT invent check-in times, wifi passwords, instructions, or policies.
- If the guest's question isn't covered by any source, say so honestly or mark it with [HOST: verify this detail]
- For maintenance issues, acknowledge the problem and let them know you're on it
- For questions, give a clear direct answer
- Never make promises about refunds or compensation without host approval — instead say something like "let me check with my team"
- Sign off naturally (no need for a formal signature)
- Match the guest's communication style (casual if they're casual, formal if they're formal)

CRITICAL: This is a DRAFT. The host will review before sending. If unsure about anything, note it in brackets like [HOST: verify this detail]."""


def classify_message(message_text: str, property_name: str) -> dict:
    """Classify a guest message using Claude Haiku.

    Returns dict with keys: category, urgency, summary
    """
    response = _get_client().messages.create(
        model=CLASSIFY_MODEL,
        max_tokens=200,
        messages=[
            {
                "role": "user",
                "content": f"Property: {property_name}\n\nGuest message:\n{message_text}",
            }
        ],
        system=CLASSIFICATION_PROMPT,
    )

    result_text = response.content[0].text.strip()

    # Parse the structured response. Default response_needed to True — fail-safe
    # towards surfacing the draft rather than silently suppressing it.
    result = {
        "category": "GENERAL",
        "urgency": "MEDIUM",
        "summary": "Guest message",
        "response_needed": True,
        "descriptor": "Guest Message",
    }

    for line in result_text.split("\n"):
        line = line.strip()
        if line.startswith("CATEGORY:"):
            result["category"] = line.split(":", 1)[1].strip()
        elif line.startswith("URGENCY:"):
            result["urgency"] = line.split(":", 1)[1].strip()
        elif line.startswith("RESPONSE_NEEDED:"):
            value = line.split(":", 1)[1].strip().upper()
            result["response_needed"] = value == "YES"
        elif line.startswith("DESCRIPTOR:"):
            result["descriptor"] = line.split(":", 1)[1].strip()
        elif line.startswith("SUMMARY:"):
            result["summary"] = line.split(":", 1)[1].strip()

    return result


CONVERSATION_SUMMARY_PROMPT = """You are summarizing a guest-host conversation thread for a short-term rental host who needs to triage a new guest message at a glance.

Produce EXACTLY two Slack-mrkdwn bullet points, each starting with "• ":
• *Agreed so far:* what commitments, bookings, or confirmations have been made by either side — short clause. If none, say "nothing yet".
• *Still open:* what the guest is waiting on, what the host still needs to verify or do, or what's ambiguous — short clause. If nothing's open, say "nothing open".

Keep each bullet to one line (max ~150 chars). No preamble, no trailing prose. If the thread is too thin to summarize, output:
• *Agreed so far:* nothing yet
• *Still open:* nothing open"""


def summarize_conversation(messages: list[dict], property_name: str) -> str:
    """Build a 2-bullet summary of a reservation conversation thread.

    Returns an empty string if there's not enough history to bother summarizing
    (fewer than 2 messages). Uses Haiku — ~$0.001 and ~500ms per call.
    """
    if not messages or len(messages) < 2:
        return ""

    # Build a compact transcript. Keep the full thread if it fits; otherwise
    # trim to the most recent 30 messages, which covers most guest threads.
    window = messages[-30:]
    lines = []
    for msg in window:
        sender = msg.get("sender_type", "")
        body = (msg.get("body") or "").strip()
        if not body:
            continue
        label = "GUEST" if sender == "guest" else "HOST"
        if len(body) > 400:
            body = body[:400] + "..."
        lines.append(f"[{label}] {body}")

    if not lines:
        return ""

    transcript = "\n".join(lines)
    user_content = f"Property: {property_name}\n\nConversation:\n{transcript}"

    response = _get_client().messages.create(
        model=CLASSIFY_MODEL,
        max_tokens=300,
        messages=[{"role": "user", "content": user_content}],
        system=CONVERSATION_SUMMARY_PROMPT,
    )

    return response.content[0].text.strip()


def draft_response(
    message_text: str,
    property_name: str,
    property_description: str,
    knowledge_hub_context: str,
    guest_name: str,
    checkin_date: str,
    checkout_date: str,
    classification: dict,
    conversation_history: str = "",
    local_kb_context: str = "",
    thread_logs_context: str = "",
) -> str:
    """Draft a response to a guest message using Claude Sonnet.

    Returns the draft response text.

    The local KB context (already formatted by knowledge_base_loader.format_for_claude)
    is the highest-priority source and is placed prominently in the prompt.

    thread_logs_context (if provided) contains prior internal notes, issues, or
    resolutions logged by the host team in Slack threads on THIS reservation.
    It gives the draft memory of what's already been discussed or fixed.
    """
    header = f"""Property: {property_name}
Guest: {guest_name}
Check-in: {checkin_date}
Check-out: {checkout_date}
Issue type: {classification['category']} ({classification['urgency']} urgency)
Issue summary: {classification['summary']}"""

    sections = [header]

    # Local KB — highest priority. Included first so Claude anchors on it.
    if local_kb_context.strip():
        sections.append(local_kb_context[:6000])

    # Prior internal notes/issues/resolutions for this reservation —
    # high priority because they reflect decisions already made.
    if thread_logs_context.strip():
        sections.append(thread_logs_context[:3000])

    # Hospitable's structured Knowledge Hub — supplemental
    if knowledge_hub_context.strip():
        sections.append(
            "═══ Supplemental — Hospitable Knowledge Hub ═══\n"
            + knowledge_hub_context[:3000]
        )

    # Property description — lowest priority context
    if property_description.strip():
        sections.append(
            "═══ Supplemental — Property description ═══\n"
            + property_description[:2000]
        )

    if conversation_history:
        sections.append(f"Recent conversation:\n{conversation_history[:2000]}")

    sections.append(f"New guest message to respond to:\n{message_text}")

    context = "\n\n".join(sections)

    response = _get_client().messages.create(
        model=DRAFT_MODEL,
        max_tokens=500,
        messages=[{"role": "user", "content": context}],
        system=DRAFT_RESPONSE_PROMPT,
    )

    return response.content[0].text.strip()
