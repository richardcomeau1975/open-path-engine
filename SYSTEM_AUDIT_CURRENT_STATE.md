# SYSTEM AUDIT — CURRENT STATE
**Generated: 2026-03-20**

---

# SECTION A: Backend (open-path-engine)

## A1. File Structure

```
.
./.env.example
./.gitignore
./.python-version
./=3.0.0                          # stray file, artifact from bad pip install
./README.md
./render.yaml
./requirements.txt
./app/__init__.py
./app/config.py
./app/main.py
./app/middleware/__init__.py
./app/middleware/clerk_auth.py
./app/routers/__init__.py
./app/routers/admin.py
./app/routers/content.py
./app/routers/courses.py
./app/routers/generate.py
./app/routers/students.py
./app/routers/topics.py
./app/routers/webhooks.py
./app/services/__init__.py
./app/services/file_parser.py
./app/services/generators/__init__.py
./app/services/generators/images.py
./app/services/generators/learning_asset.py
./app/services/generators/narration_audio.py
./app/services/generators/notechart.py
./app/services/generators/podcast_audio.py
./app/services/generators/podcast_script.py
./app/services/generators/quiz.py
./app/services/generators/tts.py
./app/services/generators/visual_overview.py
./app/services/pipeline.py
./app/services/r2.py
./app/services/supabase.py
./tests/test_file_parser.py
```

## A2. API Routes

### Direct on app (main.py)
| Method | Path | Auth | Description | Returns |
|--------|------|------|-------------|---------|
| GET | `/api/health` | None | Health check | `{"status": "ok"}` |
| GET | `/api/health/services` | None | Checks Supabase + R2 connectivity | `{"supabase": "ok/error", "r2": "ok/error"}` |

### students.py (prefix /api)
| Method | Path | Auth | Description | Returns |
|--------|------|------|-------------|---------|
| GET | `/api/me` | Clerk JWT | Returns current student record | Student dict |

### courses.py (prefix /api)
| Method | Path | Auth | Description | Returns |
|--------|------|------|-------------|---------|
| GET | `/api/courses` | Clerk JWT | Lists active courses for authenticated student | List of course dicts |

### topics.py (prefix /api)
| Method | Path | Auth | Description | Returns |
|--------|------|------|-------------|---------|
| GET | `/api/courses/{course_id}/topics` | Clerk JWT | Lists topics for a course (verifies ownership) | List of topic dicts |
| GET | `/api/topics/{topic_id}/dashboard` | Clerk JWT | Topic metadata + feature availability/progress map | `{topic, features}` |
| GET | `/api/topics/{topic_id}/status` | Clerk JWT | Generation status + which outputs exist | `{topic, features}` — **BUG: references `topic_data` which is undefined; should be `topic`** |
| POST | `/api/topics` | Clerk JWT | Creates topic with file uploads (multipart). Parses files, stores on R2. | `{topic, uploaded_files}` |

### generate.py (no prefix)
| Method | Path | Auth | Description | Returns |
|--------|------|------|-------------|---------|
| POST | `/api/topics/{topic_id}/generate` | **None** | Kicks off generation pipeline in background | `{"status": "started", ...}` |

### content.py (no prefix)
| Method | Path | Auth | Description | Returns |
|--------|------|------|-------------|---------|
| GET | `/api/topics/{topic_id}/content` | **None** | Presigned URLs for all generated content | Content dict with presigned URLs |
| GET | `/api/content/presign` | **None** | Presigned URL for single R2 key | `{key, url}` |
| GET | `/api/topics/{topic_id}/notechart/questions` | Clerk JWT | Notechart questions merged with student's saved answers | `{questions}` |
| POST | `/api/topics/{topic_id}/notechart/save` | Clerk JWT | Saves/upserts notechart answers | `{saved: count}` |
| GET | `/api/topics/{topic_id}/quiz` | **None** | Quiz questions (generates on first call, caches on R2) | `{questions}` |
| POST | `/api/topics/{topic_id}/exam/upload` | Clerk JWT | Uploads exam, analyzes with Sonnet, stores analysis | `{analysis, exam_file, analysis_file}` |
| GET | `/api/topics/{topic_id}/exam/analysis` | **None** | Returns stored exam analysis | `{analysis, exists}` |
| GET | `/api/topics/{topic_id}/learning-asset` | **None** | Returns learning asset markdown text | `{text}` |

### webhooks.py (prefix /api/webhooks)
| Method | Path | Auth | Description | Returns |
|--------|------|------|-------------|---------|
| POST | `/api/webhooks/clerk` | Svix signature | Handles Clerk `user.created` webhook | `{status}` |

### admin.py (prefix /api/admin)
All routes except `/login` require admin Bearer token.

| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/admin/login` | Password auth, returns session token |
| GET | `/api/admin/students` | List non-archived students |
| POST | `/api/admin/students` | Create student (calls Clerk API + Supabase) |
| POST | `/api/admin/students/{id}/archive` | Soft-archive student |
| GET | `/api/admin/courses` | List active courses with student info |
| POST | `/api/admin/courses` | Create course |
| POST | `/api/admin/courses/{id}/archive` | Soft-archive course |
| GET | `/api/admin/courses/{id}/topics` | List topics for course |
| GET | `/api/admin/modifier-types` | Hardcoded modifier type definitions |
| GET | `/api/admin/modifiers` | List modifiers (filterable) |
| POST | `/api/admin/modifiers` | Create/update modifier (upsert) |
| DELETE | `/api/admin/modifiers/{id}` | Delete modifier |
| GET | `/api/admin/prompt-sockets` | Hardcoded prompt socket definitions |
| GET | `/api/admin/prompts` | List prompts (filterable) |
| POST | `/api/admin/prompts` | Create new prompt version |
| PUT | `/api/admin/prompts/{id}` | Edit prompt (deactivates old, creates new version) |
| GET | `/api/admin/prompts/{id}/history` | Version history |
| POST | `/api/admin/prompts/{id}/rollback` | Rollback to specific version |
| POST | `/api/admin/prompts/global-replace` | Find-and-replace across all active prompts |
| GET | `/api/admin/batch-jobs` | List recent 50 batch jobs |
| POST | `/api/admin/topics/{id}/rerun` | Re-run generation pipeline |
| GET | `/api/admin/activity` | Dashboard stats + recent jobs/topics |

## A3. Database Interaction

### Table: students
| Operation | Endpoint(s) |
|-----------|-------------|
| SELECT by clerk_id | `clerk_auth.get_current_student` |
| SELECT all non-archived | `admin/list_students` |
| SELECT by email | `admin/create_student`, `webhooks/clerk` |
| INSERT | `admin/create_student`, `webhooks/clerk` |
| UPDATE archived_at | `admin/archive_student` |
| UPDATE clerk_id | `webhooks/clerk` (linking) |

### Table: courses
| Operation | Endpoint(s) |
|-----------|-------------|
| SELECT by student_id, active | `courses/list_courses` |
| SELECT all active with students | `admin/list_courses` |
| INSERT | `admin/create_course` |
| UPDATE active=false | `admin/archive_course` |

### Table: topics
| Operation | Endpoint(s) |
|-----------|-------------|
| SELECT by course_id | `topics/list_topics`, `admin/list_course_topics` |
| SELECT by id | Multiple content/generate endpoints |
| INSERT | `topics/create_topic_with_upload` |
| UPDATE various URL columns | All generators, pipeline |
| UPDATE generation_status | Pipeline (generating/completed/failed), admin rerun |

### Table: base_prompts
| Operation | Endpoint(s) |
|-----------|-------------|
| SELECT active by feature | All generators, quiz, exam_analysis |
| SELECT all active | `admin/list_prompts`, `admin/global_replace` |
| INSERT new version | `admin/create_prompt`, `admin/edit_prompt` |
| UPDATE is_active | `admin/edit_prompt`, `admin/rollback_prompt` |

### Table: modifiers
| Operation | Endpoint(s) |
|-----------|-------------|
| SELECT filtered | `admin/list_modifiers` |
| INSERT/UPDATE | `admin/create_or_update_modifier`, `content/upload_exam` |
| DELETE | `admin/delete_modifier` |

### Table: note_chart_answers
| Operation | Endpoint(s) |
|-----------|-------------|
| SELECT by topic_id + student_id | `content/get_notechart_questions` |
| UPSERT | `content/save_notechart_answers` |

### Table: batch_jobs
| Operation | Endpoint(s) |
|-----------|-------------|
| SELECT recent 50 | `admin/list_batch_jobs`, `admin/get_activity` |
| INSERT | `pipeline.run_pipeline` |
| UPDATE status/steps | `pipeline.run_pipeline` |

### Table: progress
| Operation | Endpoint(s) |
|-----------|-------------|
| SELECT by topic_id + student_id | `topics/get_topic_dashboard` |

### Prompt Feature Key Mismatch
- `PROMPT_SOCKETS` in admin.py lists `"note_chart"` as the feature key
- `generators/notechart.py` queries for `"notechart"` (no underscore)
- If admin creates a prompt under `note_chart`, the generator won't find it

## A4. File Storage (R2)

### Functions in r2.py
| Function | Description |
|----------|-------------|
| `get_r2_client()` | Returns boto3 S3 client for R2 |
| `upload_text_to_r2(key, text)` | Uploads UTF-8 text, returns key |
| `download_from_r2(key)` | Downloads object, returns bytes |
| `upload_bytes_to_r2(key, data, content_type)` | Uploads raw bytes, returns key |
| `generate_presigned_url(key, expires_in=3600)` | Presigned GET URL (creates new client per call) |
| `generate_presigned_urls(keys, expires_in=3600)` | Batch wrapper for above |

### R2 Key Patterns
| Key Pattern | Content | Generator |
|-------------|---------|-----------|
| `{student_id}/{course_id}/{topic_id}/uploads/{filename}` | Raw uploaded files | topics/create_topic |
| `{topic_id}/parsed_text.txt` | Concatenated extracted text | topics/create_topic |
| `{topic_id}/learning_asset.md` | Opus learning asset | generators/learning_asset |
| `{topic_id}/podcast_script.md` | Sonnet podcast script | generators/podcast_script |
| `{topic_id}/notechart.json` | Sonnet notechart questions | generators/notechart |
| `{topic_id}/visual_overview_script.json` | Sonnet visual overview slides | generators/visual_overview |
| `{topic_id}/images/slide_{n}.png` | OpenAI images per slide | generators/images |
| `{topic_id}/podcast_audio.wav` | Gemini TTS podcast audio | generators/podcast_audio |
| `{topic_id}/narration/slide_{n}.wav` | Gemini TTS slide narration | generators/narration_audio |
| `{topic_id}/exam/{filename}` | Uploaded sample exam | content/upload_exam |
| `{topic_id}/exam_analysis.md` | Sonnet exam analysis | content/upload_exam |
| `{topic_id}/quiz.json` | Sonnet quiz (cached) | generators/quiz |

## A5. Generation Pipeline

### Trigger
- **Student**: `POST /api/topics/{topic_id}/generate` (no auth) → `asyncio.create_task(run_pipeline(...))`
- **Admin rerun**: `POST /api/admin/topics/{topic_id}/rerun` → resets all URLs → `asyncio.create_task(run_pipeline(...))`

### Pipeline Steps (in order)

| # | Step | Generator | API | Model | Input | Output | DB Column |
|---|------|-----------|-----|-------|-------|--------|-----------|
| 1 | parse_files | *skipped* (done at upload) | — | — | — | — | — |
| 2 | generate_learning_asset | `gen_learning_asset()` | Anthropic streaming | `claude-opus-4-20250514` | parsed_text.txt | learning_asset.md | learning_asset_url |
| 3 | generate_podcast_script | `gen_podcast_script()` | Anthropic streaming | `claude-sonnet-4-20250514` | learning_asset.md | podcast_script.md | podcast_script_url |
| 4 | generate_notechart | `gen_notechart()` | Anthropic streaming | `claude-sonnet-4-20250514` | learning_asset.md | notechart.json | notechart_url |
| 5 | generate_visual_overview_script | `gen_visual_overview()` | Anthropic streaming | `claude-sonnet-4-20250514` | learning_asset.md | visual_overview_script.json | visual_overview_script_url |
| 6 | generate_images | `gen_images()` | OpenAI REST (httpx) | `gpt-image-1` | visual_overview_script.json | images/slide_{n}.png | visual_overview_images |
| 7 | generate_podcast_audio | `gen_podcast_audio()` | Gemini TTS (httpx) | `gemini-2.5-flash-preview-tts` | podcast_script.md | podcast_audio.wav | podcast_audio_url |
| 8 | generate_visual_overview_audio | `gen_narration_audio()` | Gemini TTS (httpx) | `gemini-2.5-flash-preview-tts` | visual_overview_script.json | narration/slide_{n}.wav | visual_overview_audio_urls |

### Prompt Loading
All text generators query `base_prompts` table for active prompt with matching `feature` key. Prompt text prepended to source material: `f"{base_prompt}\n\n---\n\nSOURCE MATERIAL:\n\n{input_text}"`.

### Error Handling
On any step failure: batch_job status set to "failed", error_log populated, topic generation_status set to "failed". Pipeline stops — no retry, no partial completion marking.

## A6. External API Integrations

### Anthropic (Claude)
| Generator | Model | Method |
|-----------|-------|--------|
| Learning asset | `claude-opus-4-20250514` | Messages streaming |
| Podcast script | `claude-sonnet-4-20250514` | Messages streaming |
| Notechart | `claude-sonnet-4-20250514` | Messages streaming |
| Visual overview | `claude-sonnet-4-20250514` | Messages streaming |
| Quiz | `claude-sonnet-4-20250514` | Messages streaming |
| Exam analysis | `claude-sonnet-4-20250514` | Messages streaming |

All use synchronous `anthropic.Anthropic` client with streaming context manager inside async functions.

### OpenAI
| Usage | Model | Endpoint |
|-------|-------|----------|
| Image generation | `gpt-image-1` | `POST /v1/images/generations` via httpx |

Parameters: `size: "1536x1024"`, `quality: "low"`, `n: 1`, response format: base64.

### Google Gemini
| Usage | Model | Endpoint |
|-------|-------|----------|
| Multi-speaker TTS (podcast) | `gemini-2.5-flash-preview-tts` | generateContent via httpx |
| Single-speaker TTS (narration) | `gemini-2.5-flash-preview-tts` | generateContent via httpx |

Multi-speaker: voices Kore + Puck, temperature 2.0. Single-speaker: voice Kore. Audio: PCM 16-bit 24kHz mono → WAV.

### Unused
- `DEEPGRAM_API_KEY` is configured but never used anywhere in the codebase.

## A7. Auth

### Clerk JWT Validation (middleware/clerk_auth.py)
1. Extract `Authorization: Bearer <token>` from request
2. Fetch JWKS from Clerk domain (cached globally, never refreshed)
3. Decode JWT with RS256, extract `sub` as `clerk_user_id`
4. Look up student in Supabase by `clerk_id`

Two dependency functions:
- `get_current_clerk_user_id(request)` → Clerk user ID string
- `get_current_student(clerk_user_id)` → student dict or 404

### Admin Auth (routers/admin.py)
- `POST /api/admin/login` validates password against `ADMIN_PASSWORD`
- Returns `secrets.token_urlsafe(32)` stored in in-memory `_admin_tokens` set
- All admin routes check Bearer token against `_admin_tokens`
- **Tokens are in-memory only** — lost on server restart, no expiration, no logout endpoint

### Unauthenticated Routes
- `/api/health`, `/api/health/services`
- `/api/topics/{id}/generate`
- `/api/topics/{id}/content`
- `/api/content/presign`
- `/api/topics/{id}/quiz`
- `/api/topics/{id}/exam/analysis`
- `/api/topics/{id}/learning-asset`

## A8. Dependencies (requirements.txt)

```
fastapi==0.115.0
uvicorn[standard]==0.32.0
python-multipart==0.0.12
python-dotenv==1.0.1
sse-starlette==2.1.0          # unused — no SSE endpoints
anthropic>=0.42.0
supabase>=2.28.0
boto3==1.35.0
pdfplumber==0.11.4             # unused — only PyPDF2 is used
PyPDF2>=3.0.0
python-pptx==1.0.2
python-docx==1.1.2
openpyxl==3.1.5
httpx>=0.24.0,<1
pyjwt[crypto]>=2.10.1
svix==1.60.0
openai>=1.30.0                 # unused — httpx used directly for OpenAI API
```

---

# SECTION B: Student Frontend (open-path-student)

## B1. File Structure

```
.
./.env.example
./.gitignore
./middleware.js
./next.config.mjs
./package.json
./package-lock.json
./postcss.config.mjs
./app/globals.css
./app/layout.js
./app/page.js
./app/sign-in/[[...sign-in]]/page.js
./app/dashboard/layout.js
./app/dashboard/page.js
./app/dashboard/[courseId]/page.js
./app/dashboard/[courseId]/upload/page.js
./app/dashboard/[courseId]/[topicId]/page.js
./app/dashboard/[courseId]/[topicId]/visual-overview/page.js
./app/dashboard/[courseId]/[topicId]/podcast/page.js
./app/dashboard/[courseId]/[topicId]/knowledge-base/page.js
./app/dashboard/[courseId]/[topicId]/notechart/page.js
./app/dashboard/[courseId]/[topicId]/how-tested/page.js
./app/dashboard/[courseId]/[topicId]/test-me/page.js
./components/BackButton.jsx
./components/CourseCard.jsx
./components/FeatureCard.jsx
./components/Header.jsx
./components/TopicCard.jsx
./components/UploadZone.jsx
./lib/api.js
./lib/constants.js
```

Framework: Next.js 15.1 + React 19 + Tailwind CSS 4 + Clerk 6.12

## B2. Pages / Routes

| Route | Component | Data Fetched | What User Sees |
|-------|-----------|-------------|----------------|
| `/` | `Home` | None | Redirects to `/dashboard` |
| `/sign-in` | `SignInPage` | None (Clerk) | "Open Path" title + Clerk SignIn widget |
| `/dashboard` | `CoursesPage` | `GET /api/courses` | "Your Courses" + course card grid |
| `/dashboard/[courseId]` | `TopicsPage` | `GET /api/courses/{courseId}/topics` | "Topics" + topic cards with status badges + "Upload New Materials" button |
| `/dashboard/[courseId]/upload` | `UploadPage` | POST /api/topics on submit; POST /api/topics/{id}/generate on generate | Two-phase: upload form → generate button + success message |
| `/dashboard/[courseId]/[topicId]` | `TopicDashboard` | `GET /api/topics/{topicId}/dashboard` | Topic name + 6 feature cards (3×2 grid) + "View Knowledge Base" link |
| `.../visual-overview` | `VisualOverviewPage` | `GET /api/topics/{topicId}/content` | Slideshow: image display, play/pause, progress bar, slide dots |
| `.../podcast` | `PodcastPage` | `GET /api/topics/{topicId}/content` | Audio player: play/pause, seekable progress, "Pause & Ask" button (stub) |
| `.../knowledge-base` | `KnowledgeBasePage` | `GET /api/topics/{topicId}/learning-asset` | "Your Learning Asset" + download button + text content |
| `.../notechart` | `NoteChartPage` | `GET /api/topics/{topicId}/notechart/questions` | Questions by section, textareas, auto-save, "Evaluate" button (stub) |
| `.../how-tested` | `HowTestedPage` | `GET /api/topics/{topicId}/exam/analysis` | Upload zone for exam + analysis display |
| `.../test-me` | `TestMePage` | `GET /api/topics/{topicId}/quiz` | Multiple-choice quiz with scoring |

## B3. Screen Existence Audit

| # | Screen | Status |
|---|--------|--------|
| 1 | Login screen | **EXISTS** |
| 2 | Course selection screen | **EXISTS** |
| 3 | Topics screen | **EXISTS** |
| 4 | Upload screen | **EXISTS** |
| 5 | Dashboard screen (6 numbered cards) | **EXISTS** |
| 6 | Visual Overview screen (slideshow player) | **EXISTS** |
| 7 | Podcast screen (audio player) | **EXISTS** |
| 8 | Knowledge Walkthrough screen | **MISSING** — no route exists; `walkthrough` key has no route mapping in FeatureCard |
| 9 | Note Chart screen | **EXISTS** |
| 10 | Walk Through the Gaps screen | **MISSING** |
| 11 | How You're Tested screen | **EXISTS** |
| 12 | Test Me screen (quiz) | **EXISTS** |
| 13 | Progress Tracker screen | **MISSING** |

**10 screens exist, 3 are missing.**

## B4. Design System

| Token | Expected | Actual | Match |
|-------|----------|--------|-------|
| Background | `#fdfbf7` | `--bg-page: #fdfbf7` | ✅ |
| Card borders | `#E8E4DA` | `--border-card: #E8E4DA` | ✅ |
| Font display | Lora | Lora (Google Fonts) | ✅ |
| Font body | Inter | Inter (Google Fonts) | ✅ |
| Accent gold | `#8B6914` | `--accent-gold: #8B6914` | ✅ |
| Button normal | `#9B8E82` | `--btn-normal: #9B8E82` | ✅ |
| Button hover | `#A0785A` | `--btn-hover: #A0785A` | ✅ |
| Green | `#4A7C59` | `--status-green: #4A7C59` | ✅ |
| Amber | `#C4972A` | `--status-amber: #C4972A` | ✅ |
| Border radius | 8px / 12px | `--radius: 8px`, `--radius-lg: 12px` | ✅ |

**All design tokens match spec.** Styling is nearly 100% inline `style={{}}` — Tailwind is imported but essentially unused.

## B5. Auth Integration

- **ClerkProvider** wraps entire app in `layout.js`
- **Middleware** (`middleware.js`): `clerkMiddleware` protects all routes except `/sign-in(.*)` and `/api(.*)`
- **Token passing**: Each page calls `useAuth().getToken()`, passes as `Authorization: Bearer` header
- **User info**: `Header.jsx` uses `useUser()` for name display, `<UserButton>` for avatar/signout
- **Sign-in page**: Custom-styled Clerk `<SignIn>` widget, sign-up footer hidden

## B6. State Management

- **No global state** — no Context, Redux, Zustand, or stores
- **Local useState/useEffect per page** — every page fetches on mount independently
- **No caching** — navigating away loses state, returning re-fetches
- **Props down only** to child components (CourseCard, TopicCard, FeatureCard, etc.)
- **Inconsistent API helper usage**: some pages use `apiFetch()` from `lib/api.js`, others use raw `fetch()` directly

---

# SECTION C: Admin Frontend (open-path-admin)

## C1. File Structure

```
.
./.env.example
./.gitignore
./next.config.mjs
./package.json
./package-lock.json
./app/globals.css
./app/layout.js
./app/page.js
./components/ActivityTab.jsx
./components/CourseView.jsx
./components/PromptsTab.jsx
./components/StudentsTab.jsx
./lib/api.js
```

**11 source files total.** Single-page app. No Clerk SDK — auth is password-only via backend.

## C2. Pages / Routes

Single route: `/` → `AdminPage`
- **Logged out**: Password login form
- **Logged in**: Tab bar with Students, Prompts, Activity tabs
- **Sub-view**: CourseView opens when clicking a course in StudentsTab

## C3. Feature Inventory

| # | Feature | Status |
|---|---------|--------|
| 1 | Add student (Supabase + Clerk) | **EXISTS** |
| 2 | Add course (assign to student) | **EXISTS** |
| 3 | Add/manage topics | **PARTIAL** — read-only listing only, no create/edit/delete |
| 4 | Prompt management | **EXISTS** — view, edit, version history, rollback, global find & replace |
| 5 | Batch job monitoring | **EXISTS** — with auto-refresh polling |
| 6 | Batch re-run capability | **EXISTS** |
| 7 | Modifier management | **EXISTS** — scoped to course-level modifiers |

## C4. Auth

- Password-only login: `POST /api/admin/login` with `{password}`
- Token stored in `sessionStorage` + module variable
- All API calls via `adminFetch()` with `Authorization: Bearer` header
- No Clerk SDK, no token expiration handling, no auto-redirect on auth failure
- Token survives page refresh (sessionStorage) but lost on tab close
- **In-memory tokens on backend** — all tokens lost on server restart

---

# SECTION D: Database (Supabase)

## D1. Tables

### students
| Column | Type | Notes |
|--------|------|-------|
| id | UUID | PK, default gen_random_uuid() |
| clerk_id | TEXT | Unique, nullable (linked after webhook) |
| name | TEXT | |
| email | TEXT | |
| phone | TEXT | Nullable |
| archived_at | TIMESTAMPTZ | Nullable (soft delete) |
| created_at | TIMESTAMPTZ | Default now() |

### courses
| Column | Type | Notes |
|--------|------|-------|
| id | UUID | PK |
| student_id | UUID | FK → students |
| name | TEXT | |
| framework_type | TEXT | Nullable |
| active | BOOLEAN | Default true |
| archived_at | TIMESTAMPTZ | Nullable |
| created_at | TIMESTAMPTZ | Default now() |

### topics
| Column | Type | Notes |
|--------|------|-------|
| id | UUID | PK |
| course_id | UUID | FK → courses |
| name | TEXT | |
| week_number | INTEGER | Nullable |
| generation_status | TEXT | Default 'none' |
| r2_prefix | TEXT | Nullable |
| parsed_text_url | TEXT | Nullable |
| learning_asset_url | TEXT | Nullable |
| podcast_script_url | TEXT | Nullable |
| podcast_audio_url | TEXT | Nullable |
| notechart_url | TEXT | Nullable |
| visual_overview_script_url | TEXT | Nullable |
| visual_overview_images | JSONB | Default '[]' |
| visual_overview_audio_urls | JSONB | Default '[]' |
| created_at | TIMESTAMPTZ | Default now() |

### base_prompts
| Column | Type | Notes |
|--------|------|-------|
| id | UUID | PK |
| feature | TEXT | e.g., learning_asset_generator, podcast_generator |
| framework_type | TEXT | Nullable |
| content | TEXT | Prompt text |
| version | INTEGER | |
| is_active | BOOLEAN | |
| created_by | TEXT | |
| created_at | TIMESTAMPTZ | |

### modifiers
| Column | Type | Notes |
|--------|------|-------|
| id | UUID | PK |
| student_id | UUID | FK → students |
| course_id | UUID | FK → courses |
| topic_id | UUID | Nullable, FK → topics |
| modifier_type | TEXT | |
| content | TEXT | |
| created_at | TIMESTAMPTZ | |
| updated_at | TIMESTAMPTZ | |

### note_chart_answers
| Column | Type | Notes |
|--------|------|-------|
| id | UUID | PK |
| topic_id | UUID | FK → topics |
| student_id | UUID | FK → students |
| section | TEXT | |
| question | TEXT | |
| answer | TEXT | Default '' |
| updated_at | TIMESTAMPTZ | |
| created_at | TIMESTAMPTZ | |
| | | UNIQUE(topic_id, student_id, question) |

### batch_jobs
| Column | Type | Notes |
|--------|------|-------|
| id | UUID | PK |
| topic_id | UUID | FK → topics |
| student_id | UUID | FK → students (from prior build) |
| status | TEXT | running/completed/failed |
| current_step | TEXT | Nullable |
| steps_completed | JSONB | Array of step names |
| error_log | TEXT | Nullable |
| started_at | TIMESTAMPTZ | |
| completed_at | TIMESTAMPTZ | Nullable |
| created_at | TIMESTAMPTZ | |

### progress
| Column | Type | Notes |
|--------|------|-------|
| id | UUID | PK |
| topic_id | UUID | FK → topics |
| student_id | UUID | FK → students |
| feature | TEXT | |
| state | TEXT | |
| created_at | TIMESTAMPTZ | |

### walkthrough_sessions, verifier_results, written_productions
These tables exist in the schema but are **not used by any code**. Created during initial schema setup.

## D2. RLS
- RLS is enabled on all tables
- All tables have permissive "service role full access" policies: `FOR ALL USING (true) WITH CHECK (true)`
- No student-scoped RLS policies — all access control is at the application level

## D3. Relationships
- `courses.student_id` → `students.id`
- `topics.course_id` → `courses.id`
- `batch_jobs.topic_id` → `topics.id`
- `note_chart_answers.topic_id` → `topics.id`
- `note_chart_answers.student_id` → `students.id`
- `modifiers.student_id` → `students.id`
- `modifiers.course_id` → `courses.id`
- `progress.topic_id` → `topics.id`
- `progress.student_id` → `students.id`

---

# SECTION E: Deployment State

## E1. What's Deployed

| Service | Platform | URL |
|---------|----------|-----|
| Backend (engine) | Render | https://open-path-engine.onrender.com |
| Student frontend | Vercel | https://open-path-student.vercel.app |
| Admin frontend | Vercel | https://open-path-admin.vercel.app |

All three are live and operational.

## E2. Environment Variables Referenced

### Engine
- `SUPABASE_URL`
- `SUPABASE_SERVICE_KEY`
- `CLERK_PUBLISHABLE_KEY`
- `CLERK_SECRET_KEY`
- `CLERK_WEBHOOK_SECRET`
- `R2_ENDPOINT`
- `R2_ACCESS_KEY_ID`
- `R2_SECRET_ACCESS_KEY`
- `R2_BUCKET_NAME`
- `ANTHROPIC_API_KEY`
- `OPENAI_API_KEY`
- `GOOGLE_CLOUD_API_KEY`
- `DEEPGRAM_API_KEY` (configured but unused)
- `ADMIN_PASSWORD`
- `ALLOWED_ORIGINS`
- `PORT`

### Student Frontend
- `NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY`
- `CLERK_SECRET_KEY`
- `NEXT_PUBLIC_CLERK_SIGN_IN_URL`
- `NEXT_PUBLIC_CLERK_AFTER_SIGN_IN_URL`
- `NEXT_PUBLIC_API_URL`

### Admin Frontend
- `NEXT_PUBLIC_API_URL`

---

# SECTION F: End-to-End Flow Status

| # | Flow | Status | Notes |
|---|------|--------|-------|
| 1 | Student login → courses → topics → dashboard | **WORKING** | Full flow verified. Clerk auth → course list → topic list with status badges → 6-card dashboard with active/inactive states. |
| 2 | File upload → parse → store on R2 → topic updated | **WORKING** | Upload → file_parser extracts text → parsed_text.txt stored on R2 → topic.parsed_text_url updated. |
| 3 | Generate pipeline → all 8 steps → all stored | **WORKING** | All 8 steps run end-to-end: learning asset (Opus) → podcast script (Sonnet) → notechart (Sonnet) → visual overview script (Sonnet) → images (OpenAI) → podcast audio (Gemini TTS) → narration audio (Gemini TTS). All outputs stored on R2, all DB columns updated. |
| 4 | Dashboard cards reflect generated content | **WORKING** | Cards 1-4 show active (green border, "Ready") when content exists. Cards 5-6 show "Not yet available" (Phase 2 features). |
| 5 | Email notification on generation complete | **NOT BUILT** | No email sending code exists anywhere in the codebase. |

---

# Notable Issues (documented, not fixed)

1. **Bug**: `topics.py` `/api/topics/{topic_id}/status` references `topic_data` (undefined) instead of `topic`
2. **Prompt key mismatch**: Admin socket says `note_chart`, generator queries `notechart`
3. **No auth on several endpoints**: generate, content, quiz, exam analysis, learning-asset, presign
4. **JWKS cache never refreshed** — Clerk key rotation requires server restart
5. **Admin tokens in-memory only** — lost on every Render deploy
6. **Unused dependencies**: pdfplumber, sse-starlette, openai SDK
7. **Unused config**: DEEPGRAM_API_KEY
8. **Stray file** `./=3.0.0` at engine repo root
9. **generate_presigned_url creates new boto3 client per call** — inefficient for batch
10. **All Anthropic calls use sync client** inside async functions (blocks event loop during streaming)
11. **Missing screens**: Knowledge Walkthrough, Walk Through the Gaps, Progress Tracker
12. **No global state management** in student frontend — every page re-fetches independently
