# Auto Mail Server

Self-hosted bulk mail tool. Configure your email in `config.py`, run it, open
the page, compose, and send (now or scheduled).

## Run directly (Python)
```bash
pip install -r requirements.txt
python3 app.py
```
Open http://127.0.0.1:8894  (port is 8894 by default; change with PORT=xxxx).

## Run with Docker (data kept OUTSIDE the container)
Requires Docker + Docker Compose.

IMPORTANT: keep ALL the project files in ONE folder before building —
especially `config.py` (with your email login). The folder must contain at
least: `app.py`, `config.py`, `requirements.txt`, `Dockerfile`,
`docker-compose.yml`. The easiest way is to unzip the provided archive and run
from inside that unzipped folder.

```bash
docker compose up -d --build
```
Open http://localhost:8894

- All data lives on the host in a **`data/`** folder created next to
  `docker-compose.yml` (database, `saved_files/`, `scheduled_files/`). It is
  bind-mounted into the container, so it stays outside and survives rebuilds.
- Your SMTP login (`config.py`) is built into the image. To change it later,
  edit `config.py` and rebuild: `docker compose up -d --build`.
- The container restarts automatically, which keeps scheduled sends working.

Stop it: `docker compose down`  (your `data/` folder is kept).

Change the port: edit both the `ports:` mapping and `PORT` in
`docker-compose.yml` (e.g. `"9000:9000"` and `PORT=9000`).

## Email login
Gmail/Outlook need an **App Password**, not your normal password.
- Gmail: 2-Step Verification on → Google Account → Security → App passwords.
  Host `smtp.gmail.com`, port `587`, security `tls`.
- Outlook: app password in Microsoft account security. Host
  `smtp.office365.com`, port `587`, `tls`.

## Features
Saved subjects / messages / recipient lists / files; per-recipient greeting and
per-recipient attachment grid; send now or schedule for later; recipient
autocomplete from past sends; red warning for already-emailed addresses with the
last-sent date; day-by-day notes with undo; recover bin for deleted items.

Note: data and saved files are unencrypted on disk — avoid sensitive material on
a shared machine.
