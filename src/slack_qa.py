"""Claude-powered Q&A for Slack alert threads.

When a host replies in a thread with a question, we assemble all available
context (knowledge base, reservation, message history, prior notes/issues)
and ask Claude Haiku for a concise, honest answer.

Haiku is used for latency — Slack retries if we don't 200 within 3s.
"""

import logging

import anthropic

from config import get_anthropic_key, CLASSIFY_MODEL
from classifier import _get_client  # reuse cached client

logger = logging.getLogger(__name__)


QA_SYSTEM_PROMPT = """You are an internal assistant for a short-term rental host team. \
You're answering questions about a specific reservation that the team asked in an internal Slack thread. \
The guest is NEVER reading your response.

Your job: answer clearly and concisely using only the context you're given below. \
If the answer isn't in the context, say so honestly — do NOT guess or fabricate.

Style:
- Answer like a teammate: brief, direct, friendly.
- Prefer 1-3 sentences. Use a bulleted list only if the answer is naturally a list.
- Quote the canonical source when citing facts (wifi, policies, instructions). No need to cite the context section name.
- If the question is about a past note, issue, or resolution logged in the thread, reference it directly.

When you can't answer:
  Reply: "I don't have that in the knowledge base or reservation data. You may want to check Hospitable directly or ask the guest."
"""


def answer(
    question: str,
    *,
    property_name: str = "",
    local_kb_context: str = "",
    hospitable_kb_context: str = "",
    reservation_summary: str = "",
    conversation_history: str = "",
    thread_logs_context: str = "",
) -> str:
    """Generate a single Slack reply to a host's thread question.

    All context arguments are pre-formatted text blocks (the callers assemble
    them). This keeps this module LLM-focused and easy to unit test.
    """
    if not question or not question.strip():
        return "I didn't catch a question there. Try asking something like 'what's the wifi password?' or 'when do they check out?'"

    sections = [f"Property: {property_name or 'Unknown'}"]

    if local_kb_context.strip():
        sections.append(local_kb_context[:6000])

    if thread_logs_context.strip():
        sections.append(thread_logs_context[:3000])

    if reservation_summary.strip():
        sections.append(f"═══ Reservation details ═══\n{reservation_summary[:2000]}")

    if hospitable_kb_context.strip():
        sections.append(
            "═══ Supplemental — Hospitable Knowledge Hub ═══\n"
            + hospitable_kb_context[:3000]
        )

    if conversation_history.strip():
        sections.append(
            "═══ Recent conversation with guest ═══\n"
            + conversation_history[:2000]
        )

    sections.append(f"Host's question in the internal thread:\n{question.strip()}")

    prompt = "\n\n".join(sections)

    try:
        response = _get_client().messages.create(
            model=CLASSIFY_MODEL,  # Haiku — fast enough for Slack's 3s window
            max_tokens=400,
            system=QA_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.content[0].text.strip()
    except Exception as e:
        logger.error(f"Q&A generation failed: {e}")
        return "_Sorry — I hit an error trying to answer. Try again or check Hospitable directly._"
