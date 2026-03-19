# Open Path Engine

Backend API for Open Path — AI-powered learning platform.

## Setup

```bash
cp .env.example .env
# Fill in .env with real values
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

## Health Check

```
GET /api/health
```
