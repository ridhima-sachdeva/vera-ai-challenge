# Vera Challenge Bot — Starter

This is a submission-ready FastAPI service implementing the five required challenge endpoints:

- `POST /v1/context`
- `POST /v1/tick`
- `POST /v1/reply`
- `GET /v1/healthz`
- `GET /v1/metadata`

It also implements optional `POST /v1/teardown`.

## Local run

```bash
python -m venv .venv
# Windows:
.venv\Scripts\activate
# macOS/Linux:
# source .venv/bin/activate

pip install -r requirements.txt
uvicorn app:app --host 0.0.0.0 --port 8080
```

Then open:

- http://localhost:8080/v1/healthz
- http://localhost:8080/v1/metadata

## Render deployment

1. Put these files in a GitHub repo.
2. Render -> New -> Web Service -> connect the repo.
3. Build command: `pip install -r requirements.txt`
4. Start command: `uvicorn app:app --host 0.0.0.0 --port $PORT`
5. Add env vars:
   - `TEAM_NAME`
   - `TEAM_MEMBERS`
   - `CONTACT_EMAIL`
6. Deploy.
7. Verify:
   - `https://YOUR-SERVICE.onrender.com/v1/healthz`
   - `https://YOUR-SERVICE.onrender.com/v1/metadata`
8. Submit the BASE URL only:
   - `https://YOUR-SERVICE.onrender.com`

## Important

Do not submit a URL ending in `/v1`.
The judge appends `/v1/context`, `/v1/tick`, etc.
