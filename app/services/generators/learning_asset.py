"""
Learning asset generator.
Uses Claude Opus via Batch API to generate a learning asset from parsed course materials.
Supports YAML output format with lint validation and per-segment splitting.
"""

import json
import logging
import re
import yaml
from app.services.r2 import download_from_r2, upload_text_to_r2
from app.services.prompt_lookup import get_prompt_for_feature
from app.services.modifier_assembly import gather_modifiers

logger = logging.getLogger(__name__)

MODEL = "claude-opus-4-6"
MAX_TOKENS = 16384


async def build_learning_asset_prompt(topic_id: str, supabase_client, framework_type: str = None, student_id: str = None, course_id: str = None) -> str:
    """
    Build the assembled prompt for learning asset generation.
    Returns the full prompt string ready for the Batch API.
    """
    # Get topic info
    topic_result = supabase_client.table("topics").select(
        "id, parsed_text_url, course_id"
    ).eq("id", topic_id).execute()
    topic = topic_result.data[0]

    # Download parsed text from R2
    parsed_text = download_from_r2(topic["parsed_text_url"]).decode("utf-8")
    logger.info(f"Learning asset [{topic_id}] — loaded parsed text ({len(parsed_text)} chars)")

    # Load base prompt (framework-aware lookup)
    base_prompt = get_prompt_for_feature("learning_asset_generator", framework_type)
    logger.info(f"Learning asset [{topic_id}] — loaded base prompt ({len(base_prompt)} chars)")

    # Assemble modifiers
    modifier_text = gather_modifiers(
        feature="learning_asset_generator",
        student_id=student_id,
        course_id=course_id,
        topic_id=topic_id,
    )

    if modifier_text:
        return f"{base_prompt}\n\n---\n\nMODIFIERS:\n\n{modifier_text}\n\n---\n\nSOURCE MATERIAL:\n\n{parsed_text}"
    else:
        return f"{base_prompt}\n\n---\n\nSOURCE MATERIAL:\n\n{parsed_text}"


def lint_learning_asset_yaml(text: str) -> dict:
    """
    Validate learning asset YAML structure.
    Returns {"valid": True/False, "errors": [...], "parsed": yaml_dict_or_None}
    """
    errors = []

    # Strip markdown code fences if present
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r'^```\w*\n', '', text)
        text = re.sub(r'\n```$', '', text)

    # Parse YAML
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        return {"valid": False, "errors": [f"Invalid YAML: {e}"], "parsed": None}

    if not isinstance(data, dict):
        return {"valid": False, "errors": ["Root must be a mapping"], "parsed": None}

    # Check required top-level fields
    for field in ["topic", "organizing_question", "segments"]:
        if field not in data:
            errors.append(f"Missing required field: {field}")

    if "segments" not in data or not isinstance(data.get("segments"), list):
        errors.append("segments must be a list")
        return {"valid": len(errors) == 0, "errors": errors, "parsed": data}

    # Check each segment
    for i, seg in enumerate(data["segments"]):
        prefix = f"segment[{i}]"
        for field in ["name", "hook", "by_end", "subclusters", "notes_capture"]:
            if field not in seg:
                errors.append(f"{prefix}: missing '{field}'")

        if "subclusters" in seg and isinstance(seg["subclusters"], list):
            for j, sc in enumerate(seg["subclusters"]):
                sc_prefix = f"{prefix}.subclusters[{j}]"
                for field in ["capability", "content", "success_markers"]:
                    if field not in sc:
                        errors.append(f"{sc_prefix}: missing '{field}'")

                # Check content length
                content = sc.get("content", "")
                word_count = len(content.split())
                if word_count < 100:
                    errors.append(f"{sc_prefix}: content too short ({word_count} words, min 100)")
                if word_count > 1500:
                    errors.append(f"{sc_prefix}: content too long ({word_count} words, max 1500)")

    return {"valid": len(errors) == 0, "errors": errors, "parsed": data}


def split_segments(topic_id: str, parsed_yaml: dict) -> list:
    """
    Split parsed YAML into per-segment chunks.
    Returns list of (segment_number, segment_yaml_string) tuples.
    """
    segments = []
    for i, seg in enumerate(parsed_yaml.get("segments", [])):
        seg_yaml = yaml.dump(seg, default_flow_style=False, allow_unicode=True)
        segments.append((i + 1, seg_yaml))
    return segments


async def store_learning_asset_result(topic_id: str, supabase_client, text: str) -> str:
    """Store learning asset (YAML or markdown) + per-segment files + manifest on R2."""

    # Try YAML lint
    lint_result = lint_learning_asset_yaml(text)
    if lint_result["valid"]:
        logger.info(f"Learning asset [{topic_id}] — valid YAML with {len(lint_result['parsed'].get('segments', []))} segments")
    elif lint_result["parsed"]:
        logger.warning(f"Learning asset [{topic_id}] — YAML lint warnings: {lint_result['errors']}")
    else:
        logger.info(f"Learning asset [{topic_id}] — not YAML (probably markdown), storing as-is")

    # Store full file
    r2_key = f"{topic_id}/learning_asset.yaml" if lint_result["parsed"] else f"{topic_id}/learning_asset.md"
    upload_text_to_r2(r2_key, text)
    logger.info(f"Learning asset [{topic_id}] — stored on R2 at {r2_key} ({len(text)} chars)")

    # Store per-segment files + manifest if parsed successfully
    notechart_key = None
    if lint_result["parsed"]:
        segments = split_segments(topic_id, lint_result["parsed"])
        manifest = {
            "topic": lint_result["parsed"].get("topic", ""),
            "organizing_question": lint_result["parsed"].get("organizing_question", ""),
            "segment_count": len(segments),
            "segments": []
        }

        for seg_num, seg_yaml in segments:
            seg_key = f"{topic_id}/segments/segment_{seg_num}.yaml"
            upload_text_to_r2(seg_key, seg_yaml)

            seg_data = yaml.safe_load(seg_yaml)
            manifest["segments"].append({
                "number": seg_num,
                "name": seg_data.get("name", f"Segment {seg_num}"),
                "hook": seg_data.get("hook", ""),
                "by_end": seg_data.get("by_end", ""),
                "depends_on": seg_data.get("depends_on"),
            })

        manifest_key = f"{topic_id}/segments/manifest.json"
        upload_text_to_r2(manifest_key, json.dumps(manifest, indent=2))

        # Extract notes_capture questions and store as notechart
        notes_questions = []
        for seg_num, seg_yaml in segments:
            seg_data = yaml.safe_load(seg_yaml)
            for q in seg_data.get("notes_capture", []):
                notes_questions.append({"segment": seg_num, "question": q})

        if notes_questions:
            notechart_key = f"{topic_id}/notechart.json"
            upload_text_to_r2(notechart_key, json.dumps(notes_questions, indent=2))
            logger.info(f"Learning asset [{topic_id}] — extracted {len(notes_questions)} notechart questions")

    # Update topic record
    update_data = {"learning_asset_url": r2_key}
    if notechart_key:
        update_data["notechart_url"] = notechart_key

    supabase_client.table("topics").update(update_data).eq("id", topic_id).execute()

    return r2_key
