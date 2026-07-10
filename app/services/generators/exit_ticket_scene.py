"""
Exit ticket scene generator.
Per cluster: dramatize the cluster's test as a two-voice dialogue (Sonnet),
render it with the existing Gemini multi-speaker TTS, store JSON + WAV on R2.

R2 layout per topic:
  {topic_id}/exit_ticket/segment_{N}_scene.json   (lines, questions with dot tags, answer_key)
  {topic_id}/exit_ticket/segment_{N}_scene.wav
Cluster N == lecture segment N (enforced by the segment-structure modifier).
"""

import asyncio
import json
import logging
import re

import anthropic

from app.config import settings
from app.services.r2 import download_from_r2, upload_text_to_r2, upload_bytes_to_r2
from app.services.prompt_lookup import get_prompt_for_feature
from app.services.modifier_assembly import gather_modifiers

logger = logging.getLogger(__name__)

MODEL = "claude-sonnet-4-6"
MAX_TOKENS = 4000

CLUSTER_RE = re.compile(r"^###?\s*Cluster\s+(\d+)\b.*$", re.MULTILINE)


def extract_clusters(asset_markdown: str) -> list:
    """
    Split the learning asset into clusters by '## Cluster N' / '### Cluster N' headings.
    Returns list of {number, title, content} in document order.
    Content runs from the heading to the next cluster heading or the Chain section or EOF.
    """
    matches = list(CLUSTER_RE.finditer(asset_markdown))
    clusters = []
    for i, m in enumerate(matches):
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(asset_markdown)
        block = asset_markdown[start:end]
        chain_m = re.search(r"^###?\s*Chain\b", block, re.MULTILINE)
        if chain_m:
            block = block[: chain_m.start()]
        clusters.append({
            "number": int(m.group(1)),
            "title": m.group(0).lstrip("# ").strip(),
            "content": block.strip(),
        })
    return clusters


def _parse_scene_json(raw: str) -> dict:
    clean = raw.strip()
    if clean.startswith("```"):
        clean = clean[clean.index("\n") + 1:]
    if clean.endswith("```"):
        clean = clean[:-3]
    scene = json.loads(clean.strip())
    if not scene.get("lines") or not scene.get("questions"):
        raise ValueError("Scene JSON missing lines or questions")
    return scene


async def _generate_scene_json(
    cluster: dict,
    framework_type: str,
    student_id: str,
    course_id: str,
    topic_id: str,
    target_dots: list = None,
) -> dict:
    base_prompt = get_prompt_for_feature("exit_ticket_scene", framework_type)

    modifier_text = gather_modifiers(
        feature="exit_ticket_scene",
        student_id=student_id,
        course_id=course_id,
        topic_id=topic_id,
    )

    parts = [base_prompt]
    if modifier_text:
        parts.append(f"---\n\nMODIFIERS:\n\n{modifier_text}")
    if target_dots:
        parts.append(f"---\n\ntarget_dots: {json.dumps(target_dots)}")
    parts.append(f"---\n\nCLUSTER:\n\n{cluster['content']}")
    prompt = "\n\n".join(parts)

    def _stream_sync() -> str:
        client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)
        out = ""
        with client.messages.stream(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            messages=[{"role": "user", "content": prompt}],
        ) as stream:
            for text in stream.text_stream:
                out += text
        return out

    raw = await asyncio.to_thread(_stream_sync)
    logger.info(f"Exit ticket scene — Sonnet returned {len(raw)} chars for cluster {cluster['number']}")
    return _parse_scene_json(raw)


def _scene_to_speaker_script(scene: dict) -> str:
    """Format scene lines as the EXPERT:/HOST: labeled script the TTS path expects."""
    label_map = {"SPEAKER_A": "EXPERT", "SPEAKER_B": "HOST"}
    lines = []
    for line in scene["lines"]:
        speaker = label_map.get(line.get("speaker", "SPEAKER_A"), "EXPERT")
        text = (line.get("text") or "").strip()
        if text:
            lines.append(f"{speaker}: {text}")
    return "\n\n".join(lines)


async def generate_exit_ticket_scenes(topic_id: str, supabase_client, framework_type: str = None, student_id: str = None, course_id: str = None) -> dict:
    """
    Generate scene JSON + audio for every cluster in the topic's learning asset.
    Returns {"scenes": count, "clusters": total}.
    """
    # Import here to avoid any import-cycle risk with the audio module
    from app.services.generators.podcast_audio import _tts_chunks, _clean_script_for_gemini, _pcm_to_wav

    topic_result = supabase_client.table("topics").select("id, learning_asset_url").eq("id", topic_id).execute()
    if not topic_result.data or not topic_result.data[0].get("learning_asset_url"):
        raise ValueError(f"No learning asset for topic {topic_id}")

    asset = download_from_r2(topic_result.data[0]["learning_asset_url"]).decode("utf-8")
    clusters = extract_clusters(asset)
    if not clusters:
        raise ValueError("No '## Cluster N' headings found in learning asset — cannot generate scenes")

    generated = 0
    for cluster in clusters:
        n = cluster["number"]
        try:
            scene = await _generate_scene_json(
                cluster, framework_type, student_id, course_id, topic_id
            )

            json_key = f"{topic_id}/exit_ticket/segment_{n}_scene.json"
            upload_text_to_r2(json_key, json.dumps(scene, indent=2))

            script = _clean_script_for_gemini(_scene_to_speaker_script(scene))
            pcm, duration = await _tts_chunks(topic_id, f"scene{n}", [script])
            wav_bytes = _pcm_to_wav(pcm)
            audio_key = f"{topic_id}/exit_ticket/segment_{n}_scene.wav"
            upload_bytes_to_r2(audio_key, wav_bytes, content_type="audio/wav")

            logger.info(f"Exit ticket scene [{topic_id}] — segment {n}: JSON + audio stored ({duration:.0f}s)")
            generated += 1
        except Exception as e:
            logger.error(f"Exit ticket scene [{topic_id}] — segment {n} failed: {e}")

    if generated == 0:
        raise ValueError("No scenes were generated")
    return {"scenes": generated, "clusters": len(clusters)}
