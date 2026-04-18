"""
Content serving endpoints.
Generates presigned URLs for R2-stored content so the browser can load it directly.
"""

import json
import re
import anthropic
from fastapi import APIRouter, Depends, HTTPException, Request
from app.config import settings
from app.middleware.clerk_auth import get_current_student
from app.services.supabase import get_supabase
from app.services.file_parser import parse_file
from app.services.r2 import download_from_r2, upload_text_to_r2, upload_bytes_to_r2, generate_presigned_url
from app.services.prompt_lookup import get_prompt_for_feature
from app.services.modifier_assembly import gather_modifiers

router = APIRouter()


@router.get("/api/topics/{topic_id}/content")
async def get_topic_content(topic_id: str, request: Request, student: dict = Depends(get_current_student)):
    """
    Return presigned URLs for all generated content for a topic.
    The frontend uses these URLs to load images, audio, and text directly from R2.
    """
    supabase = get_supabase()

    result = supabase.table("topics").select(
        "id, name, week_number, generation_status, "
        "learning_asset_url, podcast_script_url, podcast_audio_url, "
        "notechart_url, visual_overview_script_url, visual_overview_images, "
        "visual_overview_audio_urls, courses(student_id)"
    ).eq("id", topic_id).execute()

    if not result.data:
        raise HTTPException(status_code=404, detail="Topic not found")

    topic = result.data[0]
    if topic.get("courses", {}).get("student_id") != student["id"]:
        raise HTTPException(status_code=403, detail="Not your topic")

    # Build presigned URLs for all content
    content = {
        "topic_id": topic["id"],
        "name": topic["name"],
        "week_number": topic.get("week_number"),
        "generation_status": topic["generation_status"],
    }

    # Single files
    if topic.get("learning_asset_url"):
        content["learning_asset"] = generate_presigned_url(topic["learning_asset_url"])

    if topic.get("podcast_script_url"):
        content["podcast_script"] = generate_presigned_url(topic["podcast_script_url"])

    if topic.get("podcast_audio_url"):
        content["podcast_audio"] = generate_presigned_url(topic["podcast_audio_url"])

    if topic.get("notechart_url"):
        content["notechart"] = generate_presigned_url(topic["notechart_url"])

    if topic.get("visual_overview_script_url"):
        content["visual_overview_script"] = generate_presigned_url(topic["visual_overview_script_url"])
        # Parse script to extract slide metadata (anchor_text etc.)
        try:
            script_raw = download_from_r2(topic["visual_overview_script_url"]).decode("utf-8")
            script_clean = script_raw.strip()
            if script_clean.startswith("```"):
                script_clean = script_clean[script_clean.index("\n") + 1:]
            if script_clean.endswith("```"):
                script_clean = script_clean[:-3]
            import json as _json
            slides = _json.loads(script_clean.strip())
            content["visual_overview_slides"] = [
                {"slide_number": s.get("slide_number", i + 1), "anchor_text": s.get("anchor_text", "")}
                for i, s in enumerate(slides)
            ]
        except Exception:
            pass  # Graceful fallback — old scripts may not parse

    # Image arrays
    images = topic.get("visual_overview_images") or []
    if images:
        content["visual_overview_images"] = [
            {"key": key, "url": generate_presigned_url(key)} for key in images
        ]

    # Audio arrays
    audio_urls = topic.get("visual_overview_audio_urls") or []
    if audio_urls:
        content["visual_overview_audio"] = [
            {"key": key, "url": generate_presigned_url(key)} for key in audio_urls
        ]

    # Lecture timestamps (if generated via Inworld TTS)
    try:
        ts_bytes = download_from_r2(f"{topic_id}/lecture_timestamps.json")
        content["lecture_timestamps"] = json.loads(ts_bytes.decode("utf-8"))
    except Exception:
        pass

    # Load lecture segments manifest if it exists
    try:
        manifest_bytes = download_from_r2(f"{topic_id}/lecture/manifest.json")
        manifest = json.loads(manifest_bytes.decode("utf-8"))

        # Generate presigned URLs for each segment's assets
        for seg in manifest["segments"]:
            if seg.get("audio_url"):
                seg["audio"] = generate_presigned_url(seg["audio_url"])
            if seg.get("image_url"):
                seg["image"] = generate_presigned_url(seg["image_url"])
            if seg.get("timestamps_url"):
                seg["timestamps"] = json.loads(
                    download_from_r2(seg["timestamps_url"]).decode("utf-8")
                )

        content["lecture_segments"] = manifest["segments"]
    except Exception:
        content["lecture_segments"] = None

    return content


@router.get("/api/content/presign")
async def presign_single(key: str, request: Request, student: dict = Depends(get_current_student)):
    """
    Generate a presigned URL for a single R2 key.
    Useful for on-demand content loading. Requires authentication.
    """
    if not key:
        raise HTTPException(status_code=400, detail="key parameter required")

    try:
        url = generate_presigned_url(key)
        return {"key": key, "url": url}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to generate presigned URL: {str(e)}")


@router.get("/api/topics/{topic_id}/notechart/questions")
async def get_notechart_questions(
    topic_id: str,
    request: Request,
    student: dict = Depends(get_current_student),
):
    """
    Return the note chart questions and any saved answers for the current student.
    Questions come from the generated notechart JSON on R2.
    Answers come from Supabase.
    """
    supabase = get_supabase()

    # Get topic info
    topic_result = supabase.table("topics").select("id, notechart_url").eq("id", topic_id).execute()
    if not topic_result.data:
        raise HTTPException(status_code=404, detail="Topic not found")

    topic = topic_result.data[0]
    if not topic.get("notechart_url"):
        raise HTTPException(status_code=404, detail="No note chart generated yet")

    # Download and parse questions
    raw = download_from_r2(topic["notechart_url"]).decode("utf-8")

    # Strip markdown code fences if present
    clean = raw.strip()
    if clean.startswith("```"):
        first_nl = clean.index("\n")
        clean = clean[first_nl + 1:]
    if clean.endswith("```"):
        clean = clean[:-3]
    clean = clean.strip()

    try:
        questions = json.loads(clean)
    except json.JSONDecodeError:
        questions = [{"section": "Questions", "question": clean}]

    # Get saved answers for this student
    student_id = student["id"]
    saved_answers = {}
    answers_result = supabase.table("note_chart_answers").select(
        "question, answer"
    ).eq("topic_id", topic_id).eq("student_id", student_id).execute()
    saved_answers = {a["question"]: a["answer"] for a in answers_result.data}

    # Merge answers into questions
    for q in questions:
        q["answer"] = saved_answers.get(q.get("question", ""), "")

    return {"questions": questions}


@router.post("/api/topics/{topic_id}/notechart/save")
async def save_notechart_answers(
    topic_id: str,
    request: Request,
    student: dict = Depends(get_current_student),
):
    """
    Save note chart answers for the current student.
    Body: { "answers": [{"section": "...", "question": "...", "answer": "..."}] }
    Uses upsert to create or update.
    """
    supabase = get_supabase()

    body = await request.json()
    answers = body.get("answers", [])

    if not answers:
        return {"saved": 0}

    student_id = student["id"]

    # Upsert each answer
    saved_count = 0
    for item in answers:
        question = item.get("question", "").strip()
        answer = item.get("answer", "").strip()
        section = item.get("section", "")

        if not question:
            continue

        supabase.table("note_chart_answers").upsert({
            "topic_id": topic_id,
            "student_id": student_id,
            "section": section,
            "question": question,
            "answer": answer,
        }, on_conflict="topic_id,student_id,question").execute()
        saved_count += 1

    return {"saved": saved_count}


@router.get("/api/topics/{topic_id}/quiz")
async def get_quiz(topic_id: str, request: Request, student: dict = Depends(get_current_student)):
    """
    Return quiz questions for a topic.
    Generates on first request, caches on R2 for subsequent requests.
    """
    from app.services.generators.quiz import generate_quiz

    supabase = get_supabase()

    # Verify topic exists, has a learning asset, and belongs to this student
    topic_result = supabase.table("topics").select(
        "id, learning_asset_url, courses(student_id)"
    ).eq("id", topic_id).execute()

    if not topic_result.data:
        raise HTTPException(status_code=404, detail="Topic not found")

    if topic_result.data[0].get("courses", {}).get("student_id") != student["id"]:
        raise HTTPException(status_code=403, detail="Not your topic")

    if not topic_result.data[0].get("learning_asset_url"):
        raise HTTPException(status_code=404, detail="No learning asset generated yet")

    # Look up course info for framework lookup + modifier assembly
    topic_for_fw = supabase.table("topics").select("course_id").eq("id", topic_id).execute()
    framework_type = None
    student_id = None
    course_id = None
    if topic_for_fw.data and topic_for_fw.data[0].get("course_id"):
        course_id = topic_for_fw.data[0]["course_id"]
        course_res = supabase.table("courses").select("framework_type, student_id").eq("id", course_id).execute()
        if course_res.data:
            framework_type = course_res.data[0].get("framework_type")
            student_id = course_res.data[0].get("student_id")

    try:
        questions = await generate_quiz(topic_id, supabase, framework_type=framework_type, student_id=student_id, course_id=course_id)
        return {"questions": questions}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Quiz generation failed: {str(e)}")


@router.post("/api/topics/{topic_id}/exam/upload")
async def upload_exam(
    topic_id: str,
    request: Request,
    student: dict = Depends(get_current_student),
):
    """
    Upload a sample exam, analyze it with Sonnet, store the analysis.
    Accepts file upload (PDF, DOCX, PNG, JPG, TXT).
    """
    supabase = get_supabase()

    # Verify topic exists
    topic_result = supabase.table("topics").select("id, course_id").eq("id", topic_id).execute()
    if not topic_result.data:
        raise HTTPException(status_code=404, detail="Topic not found")

    # Get the uploaded file
    form = await request.form()
    file = form.get("file")
    if not file:
        raise HTTPException(status_code=400, detail="No file uploaded")

    filename = file.filename
    file_bytes = await file.read()

    # Store the original exam on R2
    exam_key = f"{topic_id}/exam/{filename}"
    upload_bytes_to_r2(exam_key, file_bytes, content_type=file.content_type or "application/octet-stream")

    # Parse text from the exam
    try:
        exam_text = parse_file(filename, file_bytes)
    except ValueError:
        exam_text = f"[Uploaded image file: {filename} — {len(file_bytes)} bytes. Unable to extract text from image.]"

    # Look up framework_type for this topic's course
    framework_type = None
    course_id = topic_result.data[0].get("course_id")
    if course_id:
        course_res = supabase.table("courses").select("framework_type").eq("id", course_id).execute()
        framework_type = course_res.data[0]["framework_type"] if course_res.data else None

    # Load exam analysis prompt (framework-aware lookup)
    base_prompt = get_prompt_for_feature("exam_analyzer", framework_type)

    # Assemble modifiers
    student_id_for_mod = student["id"]
    modifier_text = gather_modifiers(
        feature="exam_analyzer",
        student_id=student_id_for_mod,
        course_id=course_id,
        topic_id=topic_id,
    )

    if modifier_text:
        exam_prompt = f"{base_prompt}\n\n---\n\nMODIFIERS:\n\n{modifier_text}\n\n---\n\nSAMPLE EXAM CONTENT:\n\n{exam_text}"
    else:
        exam_prompt = f"{base_prompt}\n\n---\n\nSAMPLE EXAM CONTENT:\n\n{exam_text}"

    # Call Sonnet with streaming
    client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)
    analysis_text = ""
    with client.messages.stream(
        model="claude-sonnet-4-6",
        max_tokens=8000,
        messages=[{
            "role": "user",
            "content": exam_prompt
        }]
    ) as stream:
        for text in stream.text_stream:
            analysis_text += text

    # Store analysis on R2
    analysis_key = f"{topic_id}/exam_analysis.md"
    upload_text_to_r2(analysis_key, analysis_text)

    # Extract a short format description
    format_prompt = (
        "Read this exam analysis and write a single sentence describing the test format. "
        "Include: question types (multiple choice, short answer, essay, etc), "
        "cognitive level (recall, application, analysis, etc), "
        "and any notable patterns. "
        "Example: 'Multiple choice and short answer, testing recognition and application of concepts.' "
        "Write ONLY the description, nothing else."
        f"\n\n{analysis_text}"
    )
    format_result = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=200,
        messages=[{"role": "user", "content": format_prompt}]
    )
    format_description = format_result.content[0].text.strip()

    # Store format description on R2
    format_key = f"{topic_id}/exam_format.txt"
    upload_text_to_r2(format_key, format_description)

    # Upsert modifier
    student_id = student["id"]
    course_id = topic_result.data[0].get("course_id")

    if course_id:
        existing = supabase.table("modifiers").select("id").eq(
            "student_id", student_id
        ).eq("course_id", course_id).eq("modifier_type", "course_info").limit(1).execute()

        if existing.data:
            supabase.table("modifiers").update({
                "content": analysis_text,
            }).eq("id", existing.data[0]["id"]).execute()
        else:
            supabase.table("modifiers").insert({
                "student_id": student_id,
                "course_id": course_id,
                "topic_id": topic_id,
                "modifier_type": "course_info",
                "content": analysis_text,
            }).execute()

    return {
        "analysis": analysis_text,
        "format_description": format_description,
        "exam_file": exam_key,
        "analysis_file": analysis_key,
    }


@router.get("/api/topics/{topic_id}/exam/analysis")
async def get_exam_analysis(topic_id: str, request: Request, student: dict = Depends(get_current_student)):
    """Return stored exam analysis if it exists. Falls back to sibling topic in same course."""
    supabase = get_supabase()

    # Verify ownership
    topic_check = supabase.table("topics").select("id, courses(student_id)").eq("id", topic_id).execute()
    if not topic_check.data:
        raise HTTPException(status_code=404, detail="Topic not found")
    if topic_check.data[0].get("courses", {}).get("student_id") != student["id"]:
        raise HTTPException(status_code=403, detail="Not your topic")

    analysis_key = f"{topic_id}/exam_analysis.md"

    try:
        analysis_text = download_from_r2(analysis_key).decode("utf-8")

        # Try to read format description
        format_description = None
        try:
            format_description = download_from_r2(f"{topic_id}/exam_format.txt").decode("utf-8")
        except Exception:
            pass

        return {
            "analysis": analysis_text,
            "format_description": format_description,
            "exists": True,
        }
    except Exception:
        pass

    # No analysis for this topic — check siblings in the same course
    topic_result = supabase.table("topics").select("course_id").eq("id", topic_id).execute()
    if topic_result.data:
        course_id = topic_result.data[0]["course_id"]
        siblings = supabase.table("topics") \
            .select("id") \
            .eq("course_id", course_id) \
            .neq("id", topic_id) \
            .execute()

        for sibling in siblings.data:
            sib_id = sibling["id"]
            try:
                analysis_text = download_from_r2(f"{sib_id}/exam_analysis.md").decode("utf-8")

                format_description = None
                try:
                    format_description = download_from_r2(f"{sib_id}/exam_format.txt").decode("utf-8")
                except Exception:
                    pass

                return {
                    "analysis": analysis_text,
                    "format_description": format_description,
                    "exists": True,
                    "inherited": True,
                    "inherited_from_topic_id": sib_id,
                }
            except Exception:
                continue

    return {"analysis": None, "format_description": None, "exists": False}


@router.post("/api/topics/{topic_id}/notechart/evaluate")
async def evaluate_notechart(topic_id: str, request: Request, student: dict = Depends(get_current_student)):
    supabase = get_supabase()

    # Get the student's answers
    answers_result = supabase.table("note_chart_answers") \
        .select("section, question, answer") \
        .eq("topic_id", topic_id) \
        .eq("student_id", student["id"]) \
        .execute()

    if not answers_result.data:
        raise HTTPException(400, "No answers to evaluate")

    # Get the learning asset
    topic = supabase.table("topics") \
        .select("learning_asset_url, course_id") \
        .eq("id", topic_id) \
        .execute()

    if not topic.data or not topic.data[0].get("learning_asset_url"):
        raise HTTPException(400, "No learning asset available")

    asset_bytes = download_from_r2(topic.data[0]["learning_asset_url"])
    learning_asset = asset_bytes.decode("utf-8")

    # Format the answers for the prompt
    answers_text = ""
    for a in answers_result.data:
        answers_text += f"Section: {a.get('section', 'General')}\n"
        answers_text += f"Question: {a['question']}\n"
        answers_text += f"Student's answer: {a.get('answer', '(blank)')}\n\n"

    # Build the evaluation prompt
    eval_prompt = (
        "You are evaluating a student's recall answers against a learning asset. "
        "For each question, determine if the student's answer is SOLID (demonstrates understanding "
        "of the key concepts) or FUZZY (missing key concepts, vague, or incorrect).\n\n"
        "For SOLID answers, briefly note what they got right.\n"
        "For FUZZY answers, briefly note what's missing — what the student got (green) and what's missing (amber).\n\n"
        "Respond in ONLY this JSON format, no other text:\n"
        "```json\n"
        '[\n'
        '  {\n'
        '    "question": "the exact question text",\n'
        '    "status": "solid" or "fuzzy",\n'
        '    "got": "what the student demonstrated (brief)",\n'
        '    "missing": "what is missing (brief, null if solid)"\n'
        '  }\n'
        ']\n'
        "```\n\n"
        f"LEARNING ASSET:\n\n{learning_asset}\n\n"
        f"STUDENT'S ANSWERS:\n\n{answers_text}"
    )

    # Call Haiku
    client = anthropic.Anthropic()

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=4096,
        messages=[{"role": "user", "content": eval_prompt}],
    )

    response_text = response.content[0].text

    # Extract JSON from potential markdown code block
    json_match = re.search(r'```json\s*(.*?)\s*```', response_text, re.DOTALL)
    if json_match:
        json_text = json_match.group(1)
    else:
        json_text = response_text.strip()

    try:
        evaluation = json.loads(json_text)
    except json.JSONDecodeError:
        raise HTTPException(500, "Failed to parse evaluation response")

    # Store results
    for item in evaluation:
        supabase.table("verifier_results").upsert({
            "topic_id": topic_id,
            "student_id": student["id"],
            "question": item["question"],
            "status": item["status"],
            "got": item.get("got", ""),
            "missing": item.get("missing"),
        }, on_conflict="topic_id,student_id,question").execute()

    return {"evaluation": evaluation}


@router.get("/api/topics/{topic_id}/notechart/evaluation")
async def get_evaluation(topic_id: str, student: dict = Depends(get_current_student)):
    supabase = get_supabase()

    result = supabase.table("verifier_results") \
        .select("question, status, got, missing") \
        .eq("topic_id", topic_id) \
        .eq("student_id", student["id"]) \
        .execute()

    if not result.data:
        return {"evaluation": None, "exists": False}

    solid = [r for r in result.data if r["status"] == "solid"]
    fuzzy = [r for r in result.data if r["status"] == "fuzzy"]

    return {
        "evaluation": result.data,
        "solid": solid,
        "fuzzy": fuzzy,
        "exists": True,
    }


@router.get("/api/topics/{topic_id}/learning-asset")
async def get_learning_asset(topic_id: str, request: Request, student: dict = Depends(get_current_student)):
    """Return the learning asset text."""
    supabase = get_supabase()
    topic_result = supabase.table("topics").select("id, learning_asset_url, courses(student_id)").eq("id", topic_id).execute()
    if not topic_result.data:
        raise HTTPException(status_code=404, detail="Topic not found")

    if topic_result.data[0].get("courses", {}).get("student_id") != student["id"]:
        raise HTTPException(status_code=403, detail="Not your topic")

    topic = topic_result.data[0]
    if not topic.get("learning_asset_url"):
        raise HTTPException(status_code=404, detail="No learning asset generated yet")

    text = download_from_r2(topic["learning_asset_url"]).decode("utf-8")
    return {"text": text}
