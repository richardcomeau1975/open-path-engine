"""
MigrateEzy v2 pipeline — clean build, no dependency on the legacy settlement code.

POST /api/migrateezy/ground    — one-shot retrieval. Identifies the situation and
                                 searches official sources. Not streamed.
POST /api/migrateezy/converse  — one conversation turn. Streams the spoken
                                 response and on-screen anchor card together.

This file does not import the old conversation pipeline (settlement.py,
settlement_generator.py, etc.). Shared infrastructure that is explicitly
permitted: tts.tts_chunk, prompt_lookup.get_prompt_for_feature, Deepgram STT,
clerk_auth.get_current_student.
"""

import base64
import json
import logging
import re

import anthropic
import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from starlette.responses import StreamingResponse

from app.config import settings
from app.middleware.clerk_auth import get_current_student
from app.services.prompt_lookup import get_prompt_for_feature
from app.services.tts import tts_chunk

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/migrateezy", tags=["migrateezy"])

MODEL = "claude-sonnet-4-6"
WEB_SEARCH_TOOL = {"type": "web_search_20250305", "name": "web_search"}

# ────────────────────────────────────────────────────────────────────────────
# Grounding prompt — constant in this file by design.
# ────────────────────────────────────────────────────────────────────────────

GROUNDING_PROMPT = """A person in Canada has come for help with a situation. Your job is to find the real rules that govern it, so the rest of the system can rely on them.

Read the situation and work out exactly what it is: the domain, for example tax, housing, immigration, or employment; the jurisdiction, meaning the province or territory and whether the matter is federal; and the specific question or process at the centre of it.

Then find the authoritative rules. Search the official sources: the responsible government agency's own current published material, the relevant tribunal or board, the governing statute or regulation. Do not rely on anything the person's own document claims about the rules, because a document can be wrong even when it is genuine. Do not state a rule from memory. Search for it, and confirm it against the official source.

Cover the rules that actually bear on this situation, not a general overview of the topic. State each rule plainly. If something cannot be confirmed from an official source, say so rather than guessing.

End your response with a single JSON object and nothing after it:
{
  "domain": "...",
  "jurisdiction": "...",
  "verified_rules": [
    { "rule": "the rule, stated plainly", "source_name": "the official body", "source_url": "the page it came from" }
  ],
  "notes": "anything that could not be confirmed, or that the conversation should be careful about"
}"""


# ────────────────────────────────────────────────────────────────────────────
# Anchor parser — fresh implementation, private to this file.
# ────────────────────────────────────────────────────────────────────────────

class _AnchorParser:
    """Splits streamed text into spoken text and on-screen anchor blocks.

    The model wraps anchor cards between <<<ANCHOR>>> and <<<END>>> markers.
    feed(chunk) returns a list of (kind, content) tuples where kind is
    "speak" or "anchor". The parser is robust to chunk boundaries that
    fall inside a marker.
    """

    _OPEN = "<<<ANCHOR>>>"
    _CLOSE = "<<<END>>>"

    def __init__(self):
        self.in_anchor = False
        self.anchor_buf = ""
        self.speak_buf = ""

    def feed(self, chunk: str):
        results = []
        self.speak_buf += chunk
        while True:
            if not self.in_anchor:
                idx = self.speak_buf.find(self._OPEN)
                if idx == -1:
                    break
                before = self.speak_buf[:idx]
                after = self.speak_buf[idx + len(self._OPEN):]
                if before:
                    results.append(("speak", before))
                self.speak_buf = ""
                self.anchor_buf = after
                self.in_anchor = True
            else:
                self.anchor_buf += self.speak_buf
                self.speak_buf = ""
                idx = self.anchor_buf.find(self._CLOSE)
                if idx == -1:
                    break
                content = self.anchor_buf[:idx]
                after = self.anchor_buf[idx + len(self._CLOSE):]
                results.append(("anchor", content.strip()))
                self.anchor_buf = ""
                self.speak_buf = after
                self.in_anchor = False
        return results

    def flush(self):
        results = []
        if self.in_anchor and self.anchor_buf.strip():
            results.append(("anchor", self.anchor_buf.strip()))
        if self.speak_buf.strip():
            results.append(("speak", self.speak_buf.strip()))
        self.anchor_buf = ""
        self.speak_buf = ""
        return results


# ────────────────────────────────────────────────────────────────────────────
# Sentence chunking for streaming TTS.
# ────────────────────────────────────────────────────────────────────────────

_SENTENCE_RE = re.compile(r'([.!?])\s')


def _next_sentence(buf: str):
    """If `buf` contains a sentence boundary, return (sentence, remainder).
    Otherwise return (None, buf)."""
    m = _SENTENCE_RE.search(buf)
    if not m:
        return None, buf
    end = m.end()
    return buf[:end].strip(), buf[end:]


# ────────────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────────────

def _parse_trailing_json(text: str) -> dict:
    """Find the last balanced JSON object in `text` and return it as a dict.
    Returns {} if no parseable JSON object is found."""
    if not text:
        return {}
    # Walk backwards looking for a "{" whose tail parses as JSON.
    last_brace = text.rfind("{")
    while last_brace != -1:
        candidate = text[last_brace:].strip()
        # Strip a trailing code fence if any
        if candidate.endswith("```"):
            candidate = candidate[:-3].rstrip()
        try:
            obj = json.loads(candidate)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass
        last_brace = text.rfind("{", 0, last_brace)
    return {}


async def _transcribe(audio_b64: str, language: str) -> str:
    audio_bytes = base64.b64decode(audio_b64)
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "https://api.deepgram.com/v1/listen",
            params={"model": "nova-3", "smart_format": "true", "language": language},
            headers={
                "Authorization": f"Token {settings.DEEPGRAM_API_KEY}",
                "Content-Type": "audio/webm",
            },
            content=audio_bytes,
            timeout=30.0,
        )
    try:
        return resp.json()["results"]["channels"][0]["alternatives"][0]["transcript"]
    except (KeyError, IndexError):
        raise HTTPException(status_code=502, detail="Transcription failed")


def _build_verified_rules_block(grounding: dict) -> str:
    """Render the grounding's verified_rules as a Markdown section body.
    Returns empty string if there is nothing to show."""
    if not grounding or not isinstance(grounding, dict):
        return ""
    rules = grounding.get("verified_rules") or []
    if not isinstance(rules, list) or not rules:
        return ""
    lines = []
    for r in rules:
        if not isinstance(r, dict):
            continue
        rule = (r.get("rule") or "").strip()
        if not rule:
            continue
        src_name = (r.get("source_name") or "").strip()
        src_url = (r.get("source_url") or "").strip()
        bits = [f"- {rule}"]
        if src_name or src_url:
            tail_parts = []
            if src_name:
                tail_parts.append(src_name)
            if src_url:
                tail_parts.append(src_url)
            bits.append(f"  (source: {' — '.join(tail_parts)})")
        lines.append("\n".join(bits))
    return "\n".join(lines)


# ────────────────────────────────────────────────────────────────────────────
# Endpoint 1 — POST /api/migrateezy/ground
# ────────────────────────────────────────────────────────────────────────────

@router.post("/ground")
async def migrateezy_ground(request: Request, student: dict = Depends(get_current_student)):
    body = await request.json()
    situation_text = (body.get("situation_text") or "").strip()
    # `language` is accepted for forward compatibility; not used in the ground call.
    _language = body.get("language", "en")

    if not situation_text:
        raise HTTPException(status_code=400, detail="situation_text is required")

    client = anthropic.Anthropic()
    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=4096,
            system=GROUNDING_PROMPT,
            tools=[WEB_SEARCH_TOOL],
            messages=[{"role": "user", "content": situation_text}],
        )
    except Exception as e:
        logger.error(f"Grounding call failed: {e}")
        raise HTTPException(status_code=502, detail="Grounding call failed")

    # Pull text and search queries out of the model's content blocks.
    text_pieces = []
    searches = []
    for block in response.content:
        btype = getattr(block, "type", None)
        if btype == "text":
            text_pieces.append(getattr(block, "text", "") or "")
        elif btype == "server_tool_use" and getattr(block, "name", None) == "web_search":
            inp = getattr(block, "input", None) or {}
            query = inp.get("query", "") if isinstance(inp, dict) else ""
            searches.append({"query": query})

    full_text = "\n".join(text_pieces).strip()
    parsed = _parse_trailing_json(full_text)

    return {
        "domain": parsed.get("domain", "") or "",
        "jurisdiction": parsed.get("jurisdiction", "") or "",
        "verified_rules": parsed.get("verified_rules", []) or [],
        "searches": searches,
        "notes": parsed.get("notes", "") or "",
    }


# ────────────────────────────────────────────────────────────────────────────
# Endpoint 2 — POST /api/migrateezy/converse
# ────────────────────────────────────────────────────────────────────────────

def _load_conversation_prompt() -> str:
    """Pull the active migrateezy_conversation prompt from base_prompts.
    Raises if it cannot be loaded — keeping this strict on purpose so a missing
    DB row surfaces as a 502 rather than silently falling back to nothing."""
    try:
        return get_prompt_for_feature("migrateezy_conversation")
    except Exception as e:
        logger.error(f"Could not load migrateezy_conversation prompt: {e}")
        raise HTTPException(status_code=502, detail="Conversation prompt unavailable")


@router.post("/converse")
async def migrateezy_converse(request: Request, student: dict = Depends(get_current_student)):
    body = await request.json()
    situation_text = (body.get("situation_text") or "").strip()
    message_in = body.get("message")
    audio_b64 = body.get("audio")
    history = body.get("history") or []
    grounding = body.get("grounding")
    language = body.get("language") or "en"

    if not situation_text:
        raise HTTPException(status_code=400, detail="situation_text is required")

    # Resolve the user message: text wins; if audio and no text, transcribe.
    question = (message_in or "").strip()
    if audio_b64 and not question:
        question = (await _transcribe(audio_b64, language) or "").strip()

    # Build the system prompt.
    conv_prompt = _load_conversation_prompt()
    parts = [conv_prompt, "## The situation", situation_text]
    rules_body = _build_verified_rules_block(grounding) if grounding else ""
    if rules_body:
        parts += ["## The verified rules", rules_body]
    system_text = "\n\n".join(parts)

    # Sanitize history to a valid Anthropic message sequence.
    api_messages = []
    for msg in history:
        if isinstance(msg, dict) and msg.get("role") in ("user", "assistant") and msg.get("content"):
            api_messages.append({"role": msg["role"], "content": msg["content"]})
    while api_messages and api_messages[0]["role"] != "user":
        api_messages.pop(0)

    # Append the current user turn. If we have nothing, synthesize an opener.
    if question:
        api_messages.append({"role": "user", "content": question})
    elif not api_messages:
        api_messages.append({"role": "user", "content": "Start the conversation."})
    else:
        api_messages.append({"role": "user", "content": "Please continue."})

    async def generate_stream():
        if question:
            yield f"data: {json.dumps({'type': 'transcript', 'text': question})}\n\n"

        client = anthropic.AsyncAnthropic()
        tts_client = httpx.AsyncClient(timeout=30.0)
        parser = _AnchorParser()
        spoken_buffer = ""
        chunk_index = 0
        full_response = ""
        # Per-block accumulators for web_search tool_use input deltas.
        search_input_buf = {}  # block_index -> str

        try:
            async with client.messages.stream(
                model=MODEL,
                max_tokens=2048,
                tools=[WEB_SEARCH_TOOL],
                system=[{
                    "type": "text",
                    "text": system_text,
                    "cache_control": {"type": "ephemeral"},
                }],
                messages=api_messages,
            ) as stream:
                async for event in stream:
                    etype = getattr(event, "type", None)

                    if etype == "content_block_start":
                        block = getattr(event, "content_block", None)
                        idx = getattr(event, "index", None)
                        if (
                            block is not None
                            and getattr(block, "type", None) == "server_tool_use"
                            and getattr(block, "name", None) == "web_search"
                        ):
                            search_input_buf[idx] = ""

                    elif etype == "content_block_delta":
                        delta = getattr(event, "delta", None)
                        idx = getattr(event, "index", None)
                        dtype = getattr(delta, "type", None) if delta is not None else None

                        if dtype == "text_delta":
                            text = getattr(delta, "text", "") or ""
                            if not text:
                                continue
                            full_response += text

                            for kind, content in parser.feed(text):
                                if kind == "anchor":
                                    if spoken_buffer.strip():
                                        yield f"data: {json.dumps({'type': 'text_chunk', 'index': chunk_index, 'text': spoken_buffer.strip()})}\n\n"
                                        yield await tts_chunk(tts_client, spoken_buffer, chunk_index, language=language)
                                        chunk_index += 1
                                        spoken_buffer = ""
                                    yield f"data: {json.dumps({'type': 'anchor', 'text': content})}\n\n"
                                elif kind == "speak":
                                    spoken_buffer += content

                            # Drain whole sentences for TTS.
                            while True:
                                sentence, remainder = _next_sentence(spoken_buffer)
                                if sentence is None:
                                    break
                                spoken_buffer = remainder
                                if not sentence:
                                    continue
                                yield f"data: {json.dumps({'type': 'text_chunk', 'index': chunk_index, 'text': sentence})}\n\n"
                                yield await tts_chunk(tts_client, sentence, chunk_index, language=language)
                                chunk_index += 1

                        elif dtype == "input_json_delta" and idx in search_input_buf:
                            search_input_buf[idx] += getattr(delta, "partial_json", "") or ""

                    elif etype == "content_block_stop":
                        idx = getattr(event, "index", None)
                        if idx in search_input_buf:
                            raw = search_input_buf.pop(idx, "")
                            query = ""
                            try:
                                obj = json.loads(raw) if raw else {}
                                if isinstance(obj, dict):
                                    query = obj.get("query", "") or ""
                            except Exception:
                                pass
                            yield f"data: {json.dumps({'type': 'search', 'query': query})}\n\n"

            # After the stream ends: flush parser, send any trailing spoken text.
            for kind, content in parser.flush():
                if kind == "anchor":
                    yield f"data: {json.dumps({'type': 'anchor', 'text': content})}\n\n"
                elif kind == "speak":
                    spoken_buffer += content
            if spoken_buffer.strip():
                yield f"data: {json.dumps({'type': 'text_chunk', 'index': chunk_index, 'text': spoken_buffer.strip()})}\n\n"
                yield await tts_chunk(tts_client, spoken_buffer, chunk_index, language=language)
                chunk_index += 1

        except Exception as e:
            logger.error(f"MigrateEzy converse stream failed: {e}")
            yield f"data: {json.dumps({'type': 'error', 'error': str(e)})}\n\n"
        finally:
            await tts_client.aclose()

        # Build the final spoken-only answer text (anchors stripped).
        spoken_only_parts = []
        cleanup = _AnchorParser()
        for kind, content in cleanup.feed(full_response):
            if kind == "speak":
                spoken_only_parts.append(content)
        for kind, content in cleanup.flush():
            if kind == "speak":
                spoken_only_parts.append(content)
        spoken_only = "".join(spoken_only_parts).strip()

        yield f"data: {json.dumps({'type': 'answer', 'text': spoken_only})}\n\n"
        yield f"data: {json.dumps({'type': 'done'})}\n\n"

    return StreamingResponse(
        generate_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )
