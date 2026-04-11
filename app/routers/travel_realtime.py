"""
Travel advisor — Inworld Realtime API version.
WebSocket proxy: browser ↔ backend ↔ Inworld Realtime API.
YAML cards loaded from R2 and injected into session instructions.
Inworld routes to Anthropic Claude Sonnet for reasoning, handles STT + TTS.
"""

import json
import logging
import asyncio
import websockets
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Depends
from app.config import settings
from app.services.r2 import download_from_r2

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/travel-realtime", tags=["travel-realtime"])

# ── YAML card keys on R2 ──
DESTINATION_CARD_KEYS = [
    "travel/jamaica-destination-card.yaml",
    "travel/antigua-barbuda-destination-card.yaml",
    "travel/trinidad-tobago-destination-card.yaml",
    "travel/barbados-destination-card.yaml",
]

# Cache cards in memory after first load
_cached_cards = None


def _load_destination_cards() -> str:
    global _cached_cards
    if _cached_cards is None:
        cards = []
        for key in DESTINATION_CARD_KEYS:
            try:
                raw = download_from_r2(key).decode("utf-8")
                cards.append(raw)
            except Exception as e:
                logger.warning(f"Could not load {key}: {e}")
        _cached_cards = "\n\n---\n\n".join(cards)
    return _cached_cards


TRAVEL_SYSTEM_PROMPT = """You are Sam, a destination intelligence assistant for travel advisors. Fast, accurate, conversational.

You are talking to a travel advisor, not a client. They need answers they can use on a call.

Your knowledge comes from structured destination intelligence cards. If something isn't covered, say so.

RESPONSE FORMAT — THIS IS NON-NEGOTIABLE:
- MAX 3 sentences per response. Count them. If you wrote more than 3, delete until you have 3.
- Give ONE recommendation. Not two. Not three. ONE. The best fit. If they want alternatives, they'll ask.
- NEVER use bold text, asterisks, bullet points, dashes, lists, or headers. Plain speech only.
- This is SPOKEN AUDIO. The advisor is listening, not reading. Talk like a colleague in the hallway.

EXAMPLES OF GOOD RESPONSES:
When advisor hasn't given enough detail: "Are they after romantic and secluded or more of a social energy? And do they care about loyalty points — Marriott, Hyatt, anything like that?"
When advisor has given enough detail: "Sandals Grande on Dickenson Bay — voted most romantic resort 14 years running, Rondoval suites with plunge pools, and it's right on the best beach in Antigua."

WHAT YOU DO:
- ASK FIRST. When the advisor gives you a scenario, ask the one or two things you need to give a precise recommendation. Don't guess and then ask if you guessed right.
- Example: advisor says "client wants adults-only in Antigua." You say: "Are they after romantic and secluded, or do they want energy and nightlife? And what's the budget range?" Then when they answer, you give the ONE perfect fit.
- Once you have enough to recommend, lead with the answer. Name, location, why it fits. One breath.
- If the advisor already gave you everything you need, skip the questions and recommend.
- If a property is closed, say so and give the alternative in the same sentence.
- Flag safety or advisory issues naturally.
- Be honest about what you don't know.
- Never say "YAML", "destination card", "data source", or anything technical.

DESTINATION INTELLIGENCE:

"""


def _build_session_config() -> dict:
    """Build the Inworld Realtime session configuration."""
    cards = _load_destination_cards()
    instructions = TRAVEL_SYSTEM_PROMPT + cards

    return {
        "type": "session.update",
        "session": {
            "type": "realtime",
            "modelId": "anthropic/claude-sonnet-4-6",
            "instructions": instructions,
            "output_modalities": ["audio", "text"],
            "audio": {
                "input": {
                    "turn_detection": {
                        "type": "semantic_vad",
                        "eagerness": "medium",
                        "create_response": True,
                        "interrupt_response": True,
                    }
                },
                "output": {
                    "model": "inworld-tts-1.5-max",
                    "voice": "Dennis",
                },
            },
        },
    }


INWORLD_REALTIME_URL = "wss://api.inworld.ai/api/v1/realtime/session"


@router.websocket("/realtime")
async def travel_realtime(browser_ws: WebSocket):
    """
    WebSocket proxy: browser ↔ this server ↔ Inworld Realtime API.
    Browser sends mic audio, receives audio chunks back.
    """
    # Basic auth check — verify token in query params
    token = browser_ws.query_params.get("token")
    if not token:
        await browser_ws.close(code=4001, reason="Missing auth token")
        return

    await browser_ws.accept()
    logger.info("Browser connected to /api/travel/realtime")

    # Connect to Inworld Realtime API
    inworld_url = f"{INWORLD_REALTIME_URL}?key=travel-{id(browser_ws)}&protocol=realtime"
    inworld_headers = {
        "Authorization": f"Basic {settings.INWORLD_API_KEY}",
    }

    try:
        async with websockets.connect(
            inworld_url,
            additional_headers=inworld_headers,
        ) as inworld_ws:
            logger.info("Connected to Inworld Realtime API")

            setup_done = asyncio.Event()
            session_config = _build_session_config()

            async def inworld_to_browser():
                """Forward Inworld events to browser."""
                setup_count = 0
                try:
                    async for raw in inworld_ws:
                        msg = json.loads(raw)
                        event_type = msg.get("type", "")

                        # Handle setup handshake
                        if not setup_done.is_set():
                            if event_type == "session.created":
                                # Send session config
                                await inworld_ws.send(json.dumps(session_config))
                                setup_count += 1
                            elif event_type == "session.updated":
                                setup_done.set()
                                logger.info("Inworld session configured — ready for audio")
                                # Tell browser we're ready
                                await browser_ws.send_json({"type": "ready"})
                            continue

                        # Forward relevant events to browser
                        if event_type == "response.output_audio.delta":
                            # Audio chunk — forward to browser for playback
                            await browser_ws.send_json({
                                "type": "audio",
                                "delta": msg.get("delta", ""),
                            })
                        elif event_type == "response.output_text.delta":
                            # Text chunk — forward for optional display
                            await browser_ws.send_json({
                                "type": "text",
                                "delta": msg.get("delta", ""),
                            })
                        elif event_type == "response.output_text.done":
                            await browser_ws.send_json({
                                "type": "text_done",
                                "text": msg.get("text", ""),
                            })
                        elif event_type == "input_audio_buffer.speech_started":
                            await browser_ws.send_json({"type": "speech_started"})
                        elif event_type == "input_audio_buffer.speech_stopped":
                            await browser_ws.send_json({"type": "speech_stopped"})
                        elif event_type == "response.done":
                            await browser_ws.send_json({"type": "response_done"})
                        elif event_type == "error":
                            logger.error(f"Inworld error: {msg}")
                            await browser_ws.send_json({
                                "type": "error",
                                "error": msg.get("error", {}).get("message", "Unknown error"),
                            })

                except websockets.exceptions.ConnectionClosed:
                    logger.info("Inworld connection closed")
                except Exception as e:
                    logger.error(f"Inworld→browser error: {e}")

            async def browser_to_inworld():
                """Forward browser audio to Inworld."""
                # Wait for setup to complete
                await setup_done.wait()

                try:
                    while True:
                        data = await browser_ws.receive_json()
                        msg_type = data.get("type", "")

                        if msg_type == "audio":
                            # Forward mic audio to Inworld
                            await inworld_ws.send(json.dumps({
                                "type": "input_audio_buffer.append",
                                "audio": data.get("audio", ""),
                            }))
                        elif msg_type == "text":
                            # Text input — create a conversation item
                            await inworld_ws.send(json.dumps({
                                "type": "conversation.item.create",
                                "item": {
                                    "type": "message",
                                    "role": "user",
                                    "content": [{
                                        "type": "input_text",
                                        "text": data.get("text", ""),
                                    }],
                                },
                            }))
                            # Trigger response
                            await inworld_ws.send(json.dumps({
                                "type": "response.create",
                            }))

                except WebSocketDisconnect:
                    logger.info("Browser disconnected")
                except Exception as e:
                    logger.error(f"Browser→Inworld error: {e}")

            # Run both directions concurrently
            await asyncio.gather(
                inworld_to_browser(),
                browser_to_inworld(),
                return_exceptions=True,
            )

    except Exception as e:
        logger.error(f"Failed to connect to Inworld: {e}")
        try:
            await browser_ws.send_json({"type": "error", "error": str(e)})
        except:
            pass
    finally:
        try:
            await browser_ws.close()
        except:
            pass
