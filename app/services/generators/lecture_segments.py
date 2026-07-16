"""
Lecture script segmentation.
Splits a monolithic lecture script into segments at [IMAGE_PROMPT] boundaries.
Stores per-segment scripts and a manifest on R2.
"""

import re
import json
import logging
from app.services.r2 import download_from_r2, upload_text_to_r2
from app.services.generators.exit_ticket_scene import extract_clusters

CLUSTER_TITLE_PREFIX_RE = re.compile(r'^Cluster\s+\d+\s*[:.\-—–]\s*', re.IGNORECASE)
OUTCOME_BOLD_LEAD_RE = re.compile(r'^\s*[-*]\s+\*\*(.+?)\*\*', re.MULTILINE)


def _cluster_display_meta(cluster: dict) -> tuple[str, list[str]]:
    """Title = cluster heading minus the 'Cluster N:' prefix.
    Outcomes = the bold-lead strings of the cluster's dot bullets, trailing periods stripped."""
    title = CLUSTER_TITLE_PREFIX_RE.sub("", cluster["title"]).strip()
    outcomes = [m.group(1).strip().rstrip(".") for m in OUTCOME_BOLD_LEAD_RE.finditer(cluster["content"])]
    return title, outcomes

logger = logging.getLogger(__name__)


def parse_lecture_segments(script_text: str) -> list[dict]:
    """
    Parse a lecture script into segments.
    Each segment starts with an [IMAGE_PROMPT: "..."] marker.

    Returns list of dicts:
    [
        {
            "number": 1,
            "image_prompt": "a student staring at dense text...",
            "script": "TEACHER: Here's something that shouldn't be true...",
            "anchors": ["The professor's understanding never makes it into the PDF"]
        },
        ...
    ]
    """
    # Split at IMAGE_PROMPT markers
    # Pattern: [IMAGE_PROMPT: "text"]
    image_prompt_pattern = r'\[IMAGE_PROMPT:\s*"([^"]+)"\]'

    # Find all IMAGE_PROMPT positions
    markers = list(re.finditer(image_prompt_pattern, script_text))

    if not markers:
        # No markers — treat entire script as one segment
        anchors = [m.group(1) for m in re.finditer(r'\[ANCHOR:\s*"([^"]+)"\]', script_text)]
        return [{
            "number": 1,
            "image_prompt": "",
            "script": script_text.strip(),
            "anchors": anchors,
        }]

    segments = []
    for i, marker in enumerate(markers):
        start = marker.start()
        end = markers[i + 1].start() if i + 1 < len(markers) else len(script_text)

        segment_text = script_text[start:end].strip()
        image_prompt = marker.group(1)

        # Extract anchors from this segment
        anchors = [m.group(1) for m in re.finditer(r'\[ANCHOR:\s*"([^"]+)"\]', segment_text)]

        segments.append({
            "number": i + 1,
            "image_prompt": image_prompt,
            "script": segment_text,
            "anchors": anchors,
        })

    return segments


async def split_and_store_segments(topic_id: str, supabase_client) -> dict:
    """
    Load the lecture script from R2, split into segments, store each segment
    and a manifest on R2.

    Returns the manifest dict.
    """
    # Load the lecture script
    topic = supabase_client.table("topics").select(
        "podcast_script_url, learning_asset_url"
    ).eq("id", topic_id).execute()

    if not topic.data or not topic.data[0].get("podcast_script_url"):
        raise ValueError(f"No lecture script found for topic {topic_id}")

    script_bytes = download_from_r2(topic.data[0]["podcast_script_url"])
    script_text = script_bytes.decode("utf-8")
    logger.info(f"Lecture segments [{topic_id}] — loaded script ({len(script_text)} chars)")

    # Parse into segments
    segments = parse_lecture_segments(script_text)
    logger.info(f"Lecture segments [{topic_id}] — found {len(segments)} segments")

    # Load learning asset clusters for titles/outcomes (non-fatal if unavailable)
    clusters_by_number = {}
    learning_asset_url = topic.data[0].get("learning_asset_url")
    if learning_asset_url:
        try:
            asset_text = download_from_r2(learning_asset_url).decode("utf-8")
            clusters = extract_clusters(asset_text)
            clusters_by_number = {c["number"]: c for c in clusters}
            if len(clusters) != len(segments):
                logger.warning(
                    f"Lecture segments [{topic_id}] — segment count ({len(segments)}) != "
                    f"cluster count ({len(clusters)}); titles/outcomes filled where they map"
                )
        except Exception as e:
            logger.warning(f"Lecture segments [{topic_id}] — could not load clusters for titles/outcomes: {e}")

    # Store each segment's script
    manifest = {
        "topic_id": topic_id,
        "segment_count": len(segments),
        "segments": [],
    }

    for seg in segments:
        seg_num = seg["number"]

        # Store segment script
        script_key = f"{topic_id}/lecture/segment_{seg_num}.md"
        upload_text_to_r2(script_key, seg["script"])

        entry = {
            "number": seg_num,
            "image_prompt": seg["image_prompt"],
            "anchors": seg["anchors"],
            "script_url": script_key,
            "audio_url": None,  # filled by audio generator
            "image_url": None,  # filled by image generator
            "timestamps_url": None,  # filled by audio generator
        }
        cluster = clusters_by_number.get(seg_num)
        if cluster:
            title, outcomes = _cluster_display_meta(cluster)
            entry["title"] = title
            entry["outcomes"] = outcomes
        manifest["segments"].append(entry)

        logger.info(f"Lecture segments [{topic_id}] — stored segment {seg_num} script ({len(seg['script'])} chars)")

    # Store manifest
    manifest_key = f"{topic_id}/lecture/manifest.json"
    upload_text_to_r2(manifest_key, json.dumps(manifest, indent=2))
    logger.info(f"Lecture segments [{topic_id}] — stored manifest at {manifest_key}")

    return manifest
