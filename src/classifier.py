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

Guidelines:
- Be warm, friendly, and genuinely helpful
- Be concise — guests don't want essays
- If you reference property details, use the Knowledge Hub info provided
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
) -> str:
    """Draft a response to a guest message using Claude Sonnet.

    Returns the draft response text.
    """
    context = f"""Property: {property_name}
Guest: {guest_name}
Check-in: {checkin_date}
Check-out: {checkout_date}
Issue type: {classification['category']} ({classification['urgency']} urgency)
Issue summary: {classification['summary']}

Property description:
{property_description[:2000]}

Knowledge Hub context:
{knowledge_hub_context[:3000]}"""

    if conversation_history:
        context += f"\n\nRecent conversation:\n{conversation_history[:2000]}"

    context += f"\n\nNew guest message to respond to:\n{message_text}"

    response = _get_client().messages.create(
        model=DRAFT_MODEL,
        max_tokens=500,
        messages=[{"role": "user", "content": context}],
        system=DRAFT_RESPONSE_PROMPT,
    )

    return response.content[0].text.strip()
