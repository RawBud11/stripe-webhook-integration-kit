# IntegrationKit

Reference integration: Stripe test-mode payment → verified webhook → PostgreSQL
→ Google Sheets. Built in code (FastAPI) and in n8n.

- The full plan is in docs/plan.md. Read the relevant section before each task.
- Current milestone: 2 (update this line when a milestone is finished).
- Stack: Python 3.11+, FastAPI, PostgreSQL, Docker Compose, pytest, ruff.
- One task per prompt. Show a plan and wait for approval before writing code.
- Write tests first where asked. Tests never use the network.
- Stripe test mode only. Never commit secrets; .env stays out of git.
- No AI features in this project.
- After finishing a task, run the tests and the linter and show me the output.
- Commit in small steps with clear messages.
