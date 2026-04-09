"""
Shared prompt lookup with framework-aware two-step resolution.
"""

from app.services.supabase import get_supabase


def get_prompt_for_feature(feature: str, framework_type: str = None) -> str:
    """
    Two-step prompt lookup:
    1. If framework_type is provided, look for a framework-specific prompt
    2. If none found (or no framework_type), fall back to global (framework_type IS NULL)

    Returns the prompt content string, or raises an exception if no prompt found.
    """
    supabase = get_supabase()

    # Step 1: Try framework-specific
    if framework_type:
        result = supabase.table("base_prompts") \
            .select("content") \
            .eq("feature", feature) \
            .eq("framework_type", framework_type) \
            .eq("is_active", True) \
            .order("version", desc=True) \
            .limit(1) \
            .execute()
        if result.data:
            return result.data[0]["content"]

    # Step 2: Fall back to global (framework_type is NULL)
    result = supabase.table("base_prompts") \
        .select("content") \
        .eq("feature", feature) \
        .is_("framework_type", "null") \
        .eq("is_active", True) \
        .order("version", desc=True) \
        .limit(1) \
        .execute()
    if result.data:
        return result.data[0]["content"]

    raise Exception(f"No active prompt found for feature '{feature}' (framework: {framework_type})")
