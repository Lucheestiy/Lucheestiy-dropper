Droppr — File Browser Share Links

Purpose
- Simple local file hosting for videos/pictures with login-free share links for recipients.

Structure
- docker-compose.yml: runs `filebrowser/filebrowser` behind an Nginx proxy (`droppr`) that adds `override=true` on uploads (prevents 409 conflicts on retries).
- data/: files you upload/share (bind-mounted to `/srv`).
- database/: File Browser DB (bind-mounted to `/database`).
- config/: File Browser settings (bind-mounted to `/config`).

Commands
- Start: `docker compose up -d`
- Start with tunnel: `docker compose --profile tunnel up -d`
- Logs: `docker logs -f droppr`
- Stop: `docker compose down`
