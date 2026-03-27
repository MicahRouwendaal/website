# Deployment Package

## Included Files
- generated-site.html
- maison-olive-config.json
- database/bookings.json
- local_booking_server.py
- start_server.bat
- docker-compose.yml
- backend/Dockerfile
- backend/app.py
- backend/requirements.txt
- backend/generate_password_hash.py
- Caddyfile
- .env.example
- deploy.ps1
- deploy.sh

## Run Locally
1. Open a terminal in this folder.
2. Run:
py local_booking_server.py --site-file "generated-site.html"
3. Open:
   - http://127.0.0.1:8000/ (website)
   - http://127.0.0.1:8000/admin (admin)
   - http://127.0.0.1:8000/staff (staff)

## One-Command Full Deployment (Frontend + Backend + Database + Domain)
1. Install Docker and Docker Compose on your server.
2. Point your domain DNS A/AAAA record to your server IP.
3. Fill production settings in `.env`:
   - `DOMAIN`
   - `TOKEN_SECRET`
   - `DATABASE_URL`
   - `ADMIN_USERNAME`
   - `ADMIN_PASSWORD_HASH` (generate via `python backend/generate_password_hash.py "YourStrongPassword"`)
4. Choose database profile (current export): **Local PostgreSQL (Free, self-hosted)**
- Uses Docker PostgreSQL in the same stack.
- Fully free to start (server cost only).
5. From this folder run one of:
   - PowerShell (Windows): `./deploy.ps1 -Domain your-domain.com`
   - Bash (Linux/macOS): `bash deploy.sh your-domain.com`
6. Open:
   - https://your-domain.com/
   - https://your-domain.com/admin
   - https://your-domain.com/staff

If you skip the domain argument, scripts use `.env` (default: maison-olive.example.com).
Caddy handles HTTPS certificates automatically once DNS is correct.

## Notes
- Local mode uses database/bookings.json.
- Production mode uses SQL storage from DATABASE_URL.
- Production admin login uses ADMIN_USERNAME + ADMIN_PASSWORD_HASH from .env.
- Local fallback login is only available in editor/file-preview mode.
- You can re-import maison-olive-config.json into the generator editor.
- After edits in the generator, export a new ZIP and re-run deploy to update production.
- Production stack runs FastAPI + SQL database + token-based auth.
- For strict API protection keep `AUTH_REQUIRED=true` in `.env`.

## Production Hardening Checklist
- Set strong secrets: `TOKEN_SECRET`, `ADMIN_PASSWORD_HASH`, and DB credentials.
- Restrict DB network access to the API host/VPC only (no public wide-open DB).
- Enable automated DB backups and test restore monthly.
- Put your domain behind HTTPS only (Caddy already provisions TLS).
- Keep containers updated and rebuild regularly (`docker compose pull && docker compose up -d --build`).
- Monitor API health and logs (`/api/health`, container logs, alerts for repeated 401/429).
- Scale API workers with `WEB_CONCURRENCY` and tune DB pool env vars for load.
