"""Issue classification and response drafting using Claude API."""

import anthropic
from functools import lru_cache
from config import get_anthropic_key, CLASSIFY_MODEL, DRAFT_MODEL


@lru_cache(maxsize=1)
def _get_client():
    """Cached Anthropic client — created once per Lambda cold start."""
    return anthropic.Anthropic(api_key=get_anthropic_key())


CLASSIFICATION_PROMPT = """You are a guest issue classifier for a short-term rental property management system.

Classify the following guest message into exactly ONE category and ONE urgency level.

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

Respond in this exact format (no other text):
CATEGORY: <category>
URGENCY: <urgency>
SUMMARY: <one-line summary of the issue>"""


DRAFT_RESPONSE_PROMPT = """You are a warm, professional hospitality assistant for a luxury Scottsdale vacation rental.

Your job is to draft a response to a guest message. The host will review and approve before sending.

Sources of truth (use in this priority order):
1. AUTHORITATIVE FACTS from our curated knowledge base — these are ground truth. Cite them exactly; do not contradict or paraphrase into something different.
2. PAST GUEST Q&A — examples of how similar questions were answered before. Use these as both a style and content guide.
3. PRECEDENTS — what we've done in similar situations in the past.
4. Property description and Hospitable Knowledge Hub — supplemental context only, use when the above don't cover the question.

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

    # Parse the structured response
    result = {"category": "GENERAL", "urgency": "MEDIUM", "summary": "Guest message"}

    for line in result_text.split("\n"):
        line = line.strip()
        if line.startswith("CATEGORY:"):
            result["category"] = line.split(":", 1)[1].strip()
        elif line.startswith("URGENCY:"):
            result["urgency"] = line.split(":", 1)[1].strip()
        elif line.startswith("SUMMARY:"):
            result["summary"] = line.split(":", 1)[1].strip()

    return result


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
) -> str:
    """Draft a response to a guest message using Claude Sonnet.

    Returns the draft response text.

    The local KB context (already formatted by knowledge_base_loader.format_for_claude)
    is the highest-priority source and is placed prominently in the prompt.
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
