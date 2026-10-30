# AI-Powered Knowledge & Tasks Management Platform (DigiPlus assessment)

## Approach
Flask + SQLite (single file DB at `data/app.db`) + a vanilla JS single-page UI. Meetings and work transfers ("handover") hold agenda, participants, notes, summary, decisions and action items. Every item change is written to a `history` table. Open items are *moved* (not copied) to the next meeting so their full history is kept.

## AI configuration
- Provider: Anthropic Claude via the official SDK. Set `ANTHROPIC_API_KEY` and optionally `AI_MODEL` in `.env`.
- AI is used for: (1) raw text -> structured recap (summary, decisions, action items with owners/dates), (2) catch-up brief for anyone who missed a meeting/is taking over, (3) linking meetings to knowledge-base docs, (4) Follow-up Autopilot messages.
- Prompts demand strict JSON; output is parsed and validated (dates, doc ids, owners) before saving.
- Failure handling: missing key, timeout, API error or bad JSON -> logged and a rule-based fallback is used (e.g. lines starting `Decision:` / `Action: ... @owner 2026-11-05`); the UI tells the user the fallback was used.

## Data source
SQLite, seeded with 3 knowledge-base docs and one sample meeting with two action items (one overdue). Users can add docs and paste notes/emails/chats/transcripts.

## Logging
JSON lines in `logs/app.log` (rotating) and console. Each request gets a request ID (or honours `X-Request-ID`), returned in the `X-Request-ID` response header and error bodies. Events: meeting_created, item_created, item_reassigned, item_closed, items_carried_forward, recap_applied, docs_linked; AI calls log task, model, latency_ms and outcome; errors log context and tracebacks. API keys are never logged (plus regex redaction as a safety net).

## Special feature: Follow-up Autopilot
Open items often die silently. This feature scans all items that are overdue or due within 2 days, groups them by owner and has the AI draft a short, specific, copy-ready nudge per person. It closes the "follow up until closed" loop of the problem statement. Overdue items are also highlighted in red across the UI.

## Closing rule
An item cannot be marked done/dropped without a recorded outcome.

## Assumptions & limitations
No authentication (users are plain names); no real email/Slack sending or file upload (paste text instead); SQLite is for a single instance; knowledge-base linking sends doc snippets (300 chars) to the AI, not full docs; no automated tests included.

## Deploy
`pip install gunicorn && gunicorn -w 2 -b 0.0.0.0:$PORT app:app` on Render/Railway/EC2; set env vars `ANTHROPIC_API_KEY`, `AI_MODEL`. Use a persistent disk for `data/` and `logs/`.
