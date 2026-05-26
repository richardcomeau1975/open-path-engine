"""
One-off seed: insert the MigrateEzy conversation prompt into base_prompts.

Idempotent. If an active global row for the feature already exists, it does
nothing. Run once from the engine repo root, in an environment where the
engine's dependencies are installed and SUPABASE_URL and SUPABASE_SERVICE_KEY
are set to the same values used on Render:

    python scripts/seed_prompts.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.supabase import get_supabase

FEATURE = "migrateezy_conversation"
PROMPT_PATH = (
    Path(__file__).resolve().parent.parent
    / "app" / "prompts" / "migrateezy_conversation.md"
)


def main() -> None:
    content = PROMPT_PATH.read_text(encoding="utf-8").strip()
    if not content:
        raise SystemExit(f"Prompt file is empty: {PROMPT_PATH}")

    sb = get_supabase()
    existing = (
        sb.table("base_prompts")
        .select("id, version")
        .eq("feature", FEATURE)
        .eq("is_active", True)
        .is_("framework_type", "null")
        .order("version", desc=True)
        .limit(1)
        .execute()
    )
    if existing.data:
        row = existing.data[0]
        print(
            f"Active row for '{FEATURE}' already exists "
            f"(version {row['version']}). No action taken."
        )
        return

    sb.table("base_prompts").insert({
        "feature": FEATURE,
        "framework_type": None,
        "content": content,
        "version": 1,
        "is_active": True,
        "created_by": "seed_script",
    }).execute()
    print(f"Seeded '{FEATURE}' version 1 ({len(content)} chars).")


if __name__ == "__main__":
    main()
