"""
Modifier assembly — gathers all applicable modifiers and concatenates them
onto the base prompt before an AI call.

Assembly order (within each type, global before specific):
1. Global system modifiers (no student, no course)
2. Course-scoped system modifiers
3. Global course_info modifiers
4. Course-scoped course_info modifiers
5. Topic-scoped course_info modifiers
6. Global personalization modifiers
7. Course-scoped personalization modifiers
"""

import logging
from app.services.supabase import get_supabase

logger = logging.getLogger(__name__)


def gather_modifiers(
    feature: str,
    student_id: str = None,
    course_id: str = None,
    topic_id: str = None,
) -> str:
    """
    Gather all applicable modifiers for this feature at all scopes.
    Returns a single string of concatenated modifier content, or empty string if none.
    """
    supabase = get_supabase()

    all_modifiers = []

    for modifier_type in ["system_modifier", "course_info", "personalization"]:
        # Global modifiers (no student, no course)
        global_result = supabase.table("modifiers") \
            .select("content, feature") \
            .eq("modifier_type", modifier_type) \
            .is_("student_id", "null") \
            .is_("course_id", "null") \
            .execute()

        for row in global_result.data:
            if row.get("feature") is None or row.get("feature") == feature:
                all_modifiers.append(row["content"])

        # Course-scoped modifiers
        if course_id:
            course_result = supabase.table("modifiers") \
                .select("content, feature") \
                .eq("modifier_type", modifier_type) \
                .eq("course_id", course_id) \
                .is_("topic_id", "null") \
                .execute()

            for row in course_result.data:
                if row.get("feature") is None or row.get("feature") == feature:
                    all_modifiers.append(row["content"])

        # Topic-scoped modifiers (mainly for course_info / testing profile)
        if topic_id:
            topic_result = supabase.table("modifiers") \
                .select("content, feature") \
                .eq("modifier_type", modifier_type) \
                .eq("topic_id", topic_id) \
                .execute()

            for row in topic_result.data:
                if row.get("feature") is None or row.get("feature") == feature:
                    all_modifiers.append(row["content"])

    if not all_modifiers:
        logger.info(f"Modifiers [{feature}] — no modifiers found")
        return ""

    logger.info(f"Modifiers [{feature}] — assembled {len(all_modifiers)} modifier(s)")
    return "\n\n---\n\n".join(all_modifiers)
