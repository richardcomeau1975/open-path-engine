"""
Settlement situation generator.

Takes a situation (a document plus the client's stated need) and produces the
dot-based intermediate representation: clusters of dots, each dot a navigation
capability carrying a fluency dimension, joined by a chain, with regulated-advice
boundaries flagged.

This is the upstream source of truth for the settlement domain. Every downstream
surface reads the structure this function returns.

NOTE ON THE PROMPT: SETTLEMENT_GENERATOR_PROMPT below is a FIRST-DRAFT prompt.
It is refined and tested separately by the operator. The contract the rest of the
system depends on is the FUNCTION: generate_settlement_asset(situation_text,
client_need) returns a dict matching the schema described in the prompt. A refined
prompt must keep producing that schema.
"""

import json
import logging

import anthropic

logger = logging.getLogger(__name__)

MODEL = "claude-opus-4-6"
MAX_TOKENS = 16384


SETTLEMENT_GENERATOR_PROMPT = """You are the situation generator for a settlement navigation platform. You take a real situation a newcomer to Canada is facing, described to you as a document plus a statement of what the person needs, and you produce the structured representation that every downstream surface of the platform reads from.

This representation is built from DOTS. A dot is the unit of the whole system. Get the dots right and everything downstream works. Get them wrong and nothing does.

WHAT A DOT IS

A dot is a testable capability for navigating the situation. It is something the person can DO, never a topic and never a definition. "Identify what the CRA letter is actually asking you to provide" is a dot. "The CRA" is not a dot, it is a topic. "Definition of a notice of assessment" is not a dot, it is a definition.

The entry point of a dot is always the capability. Any terminology lives INSIDE the dot, as the vocabulary the person needs to handle the situation, never as the headline.

THE FLUENCY DIMENSION

Every dot carries a dimension: fluency. A capability is not done-or-not-done. It is held somewhere on a gradient. So every dot specifies two ends of that gradient:
- halting: what this capability looks like when the person can technically do it but not fluently. Hesitant, imprecise, missing the right framing, losing the thread under pressure.
- fluent: what this capability looks like done well. Clear, confident, correctly framed, holding up under pressure.

The fluency dimension is what the practice surface rehearses against and what the dual-language diagnostic measures. A dot without a real, specific fluency gradient is incomplete.

CLUSTERS AND THE CHAIN

Dots group into clusters. A cluster is organized around a takeaway, which is what the person can now navigate that they could not before. A cluster takeaway is not a topic label. "Your rights" is a topic. "You can tell when the landlord is allowed to enter and when they are not" is a takeaway.

The clusters are joined by a chain: the order they must come in, and why each one depends on the one before. The person cannot decide what to do until they understand what the document is asking; they cannot handle the interaction until they know their position. The chain states that logic.

SHAPED BY THE NEED

The same document produces different dots depending on what the person needs. A CRA letter for someone who only needs to understand it yields a different set of dots than the same letter for someone who has to make the phone call. Read the stated need. Let it decide which capabilities matter and at what depth. Do not generate the encyclopedic set of every possible capability. Generate the set this person needs for what they are facing.

THE BOUNDARY, NON-NEGOTIABLE

This platform helps people understand and navigate situations and prepare for interactions. It does NOT give medical, legal, or immigration advice. Immigration advice in Canada is regulated by IRCC and giving it without authorization is an offence.

You must not generate dots that amount to giving regulated advice. A dot may be "explain your situation clearly to the CRA agent", because that is navigation and preparation. A dot may not be a recommendation about whether to formally dispute, appeal, or take legal action, because that crosses into regulated advice. Keep every dot in the lane of understanding, navigating, and preparing.

Wherever the situation genuinely crosses into territory that needs a regulated professional, a lawyer, a doctor, or a regulated immigration consultant, record it in boundary_flags, so the downstream surfaces redirect the person to the right professional instead of overreaching.

OUTPUT

Output ONLY valid JSON. No prose before or after. No markdown code fences. Use exactly this schema:

{
  "situation_summary": "one plain-language sentence naming the situation",
  "domain": "a short lowercase tag for the kind of situation, for example housing, cra, employment",
  "clusters": [
    {
      "id": "c1",
      "title": "the cluster takeaway, stated as what the person can now navigate",
      "hook": "one sentence on why this matters to the person in this situation",
      "dots": [
        {
          "id": "c1d1",
          "capability": "the navigation capability, what the person can DO",
          "causal_chain": "the why and how that lives inside this dot, the reasoning the person needs",
          "fluency_dimension": {
            "halting": "what this capability looks like done haltingly",
            "fluent": "what this capability looks like done fluently"
          }
        }
      ]
    }
  ],
  "chain": "how the clusters connect, the through-line, why each depends on the one before",
  "boundary_flags": [
    "each point where this situation crosses into medical, legal, or immigration advice and needs a regulated professional"
  ]
}

If the situation crosses no regulated boundary, return an empty array for boundary_flags."""


def _strip_code_fences(raw: str) -> str:
    """Remove a surrounding markdown code fence if the model added one."""
    raw = raw.strip()
    if raw.startswith("```"):
        lines = raw.split("\n")
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        raw = "\n".join(lines).strip()
    return raw


def generate_settlement_asset(situation_text: str, client_need: str) -> dict:
    """
    Run the settlement generator. Returns the dot-based representation as a dict.
    Raises json.JSONDecodeError if the model output is not valid JSON.
    """
    user_content = (
        "SITUATION DOCUMENT:\n"
        f"{situation_text}\n\n"
        "WHAT THE PERSON NEEDS:\n"
        f"{client_need}\n\n"
        "Produce the structured representation now. Output only the JSON."
    )

    client = anthropic.Anthropic()
    response = client.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=SETTLEMENT_GENERATOR_PROMPT,
        messages=[{"role": "user", "content": user_content}],
    )

    raw = response.content[0].text
    cleaned = _strip_code_fences(raw)
    return json.loads(cleaned)
