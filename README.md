# Auto-mail-sever
# Auto Mail Server

A self-hosted web app for sending personalized bulk email from your own email
account. You compose a message once, give it a list of recipients, optionally
attach a different file to each person, and send immediately or schedule it for
later — all from a single page in your browser.

It runs entirely on your own machine (Python or Docker). Nothing is sent to any
third-party service except your own email provider's SMTP server (e.g. Gmail).

---

## Table of contents
1. [Features](#features)
2. [Requirements](#requirements)
3. [Quick start (Python)](#quick-start-python)
4. [Run with Docker](#run-with-docker)
5. [Configuration (config.py)](#configuration-configpy)
6. [Using the app](#using-the-app)
7. [Where your data is stored](#where-your-data-is-stored)
8. [Push this project to GitHub](#push-this-project-to-github)
9. [Security notes](#security-notes)
10. [Troubleshooting](#troubleshooting)

---

## Features

**Composing**
- **Saved subjects** — keep a dropdown of reusable subject lines; add or delete
  them on the page.
- **Saved messages (bodies)** — store reusable message templates, each marked as
  plain text or HTML, and pick one from a dropdown.
- **Plain text or HTML** — a toggle controls whether your message is sent as
  formatted HTML or plain text.
- **Default sender name** — the name recipients see in the "From" field.

**Recipients**
- **Multiple recipients** — paste or type addresses, one per line (commas and
  spaces also work).
- **Saved recipient lists** — save a group of addresses under a name and reload
  it from a dropdown anytime.
- **Recipient autocomplete** — as you type an address, the app suggests people
  you've emailed before (and addresses from your saved lists). Click a
  suggestion to fill it in.
- **Duplicate / repeat detection** — addresses you've already sent to before are
  flagged in red with the date they were last emailed, and duplicates within the
  current list are highlighted. You're asked to confirm before re-sending.

**Greetings & personalization**
- **Per-recipient greeting** — one greeting line is used for everyone, or put one
  line per recipient (in the same order as your email list) to greet each person
  by name. The greeting is added to the top of the message for each person.

**Attachments**
- **Send to everyone** — tick saved files (or upload one-off files) to attach the
  same file(s) to every recipient. Great when there's a single common PDF.
- **Per-recipient attachment grid** — a table where each row is a recipient and
  each numbered column is a saved file. Tick a box to give that specific person
  that specific file. Everyone can get a different file (or several). Files
  assigned in the grid are NOT also sent to everyone, so nothing doubles up.
- **Saved files** — upload files once, reuse them across many sends.

**Sending**
- **Send now** — sends to all recipients immediately and shows a per-recipient
  success/failure log.
- **Schedule for later** — pick a date and time; the whole message (recipients,
  attachments, greetings, the per-recipient grid, everything) is saved as a
  snapshot and sent automatically at that time by a background worker.
- **Scheduled list** — see all upcoming and past scheduled sends with their
  status, and cancel any that are still pending.
- **Send history** — view a log of recent sends.

**Side panel**
- **Calendar** — browse months; today is highlighted.
- **Day-by-day notes** — click any date to write a note for that day; notes
  auto-save, dates with notes show a dot, and you can undo to previous versions.

**Other**
- **Recover bin (undo deletes)** — deleted subjects, messages, lists, and files
  go to a recovery panel where you can restore them, plus an instant "undo" link
  right after each delete.
- **Animated background** — a light decorative scene (toggleable, and never seen
  by recipients).

---

## Requirements

- **Python option:** Python 3.10+ and `pip`.
- **Docker option:** Docker and Docker Compose.
- An email account that allows SMTP sending. For Gmail/Outlook you need an
  **App Password** (see [Configuration](#configuration-configpy)).

---

## Quick start (Python)

```bash
# 1) get the code into a folder, then inside that folder:
cp config.example.py config.py     # create your own config
nano config.py                     # fill in your email login (see below)

# 2) install and run
pip install -r requirements.txt
python3 app.py
```

Open **http://127.0.0.1:8894** in your browser.

Change the port if you like:
```bash
PORT=9000 python3 app.py
```

---

## Run with Docker

Keep **all** project files in **one** folder (`app.py`, `config.py`,
`requirements.txt`, `Dockerfile`, `docker-compose.yml`). Then:

```bash
cp config.example.py config.py     # create your config (if you haven't)
nano config.py                     # add your email login, save
docker compose up -d --build
```

Open **http://localhost:8894**.

- Your data lives on the host in a **`data/`** folder created next to
  `docker-compose.yml` (database, `saved_files/`, `scheduled_files/`). It is
  mounted into the container, so it stays **outside** the container and survives
  rebuilds.
- Your login (`config.py`) is built into the image. To change it later, edit
  `config.py` and run `docker compose up -d --build` again.
- The container auto-restarts, which keeps scheduled sends running.

Useful commands:
```bash
docker compose logs -f      # watch logs
docker compose restart      # restart
docker compose down         # stop (your data/ folder is kept)
```

Change the port: edit both the `ports:` line and `PORT` in
`docker-compose.yml` (e.g. `"9000:9000"` and `PORT=9000`).

---

## Configuration (config.py)

Copy `config.example.py` to `config.py` and edit it. Fields:

| Setting | Meaning |
|---|---|
| `SMTP_HOST` | Your provider's SMTP server. Gmail: `smtp.gmail.com`, Outlook: `smtp.office365.com`. |
| `SMTP_PORT` | `587` for STARTTLS, `465` for SSL. |
| `SECURITY` | `"tls"`, `"ssl"`, or `"none"`. |
| `USERNAME` | Your login email address. |
| `PASSWORD` | Your **App Password** (not your normal account password). |
| `FROM_NAME` | Default sender name (overridable on the page). |
| `FROM_ADDR` | Leave `""` to use `USERNAME`. |
| `SUBJECTS` | A starter list of saved subject lines. |
| `BODIES` | Saved message templates: `label -> text`. |
| `BODIES_HTML` | Which body labels are HTML. |
| `ATTACHMENTS` | Paths of built-in attachment files (put files in `attachments/`). |

**Getting a Gmail App Password:** turn on 2-Step Verification, then go to your
Google Account -> Security -> App passwords, generate one, and paste it into
`PASSWORD`. Use host `smtp.gmail.com`, port `587`, security `tls`.

---

## Using the app

1. **01 Subject** — type a subject or pick a saved one. Save/delete subjects here.
2. **02 Message** — write the body or load a saved template; toggle HTML if needed.
   Set the greeting (one line for all, or one line per recipient).
3. **03 Attachments** — tick saved files to send to everyone, or upload files.
   Use the per-recipient grid to give specific files to specific people.
4. **04 Recipients** — enter email addresses (autocomplete helps). Watch for red
   repeat warnings. Save the list for reuse if you want.
5. **Send now**, or pick a date/time and click **Schedule**. Track scheduled jobs
   in the list below the button.

> Scheduled emails only send while the app is running (the container's
> auto-restart helps). If the machine is off at the scheduled time, the email
> goes out as soon as the app is running again.

---

## Where your data is stored

Everything is stored locally (no cloud):

- `mailserver.db` — SQLite database (saved subjects, bodies, lists, files index,
  send history, notes, scheduled jobs, recovery bin).
- `saved_files/` — files you uploaded for reuse.
- `scheduled_files/` — one-off files copied aside for scheduled sends.

With Docker, all of the above live in the host `data/` folder. With plain
Python, they sit next to `app.py` (or in `DATA_DIR` if you set that environment
variable).

---

## Push this project to GitHub

The included `.gitignore` already excludes your `config.py` (your password) and
all runtime data, so only the safe project files get pushed.

```bash
# inside the project folder
git init
git add .
git commit -m "Initial commit: Auto Mail Server"

# create an empty repo on github.com first (no README), then:
git branch -M main
git remote add origin https://github.com/<your-username>/<your-repo>.git
git push -u origin main
```

After this, anyone who clones the repo runs `cp config.example.py config.py`,
fills in their own login, and they're ready.

> Double-check before pushing: run `git status` and confirm `config.py` is NOT
> listed (it should be ignored). Never commit real passwords.

---

## Security notes

- Saved data and uploaded files are stored **unencrypted** on disk. Avoid using
  sensitive material on a shared computer.
- Your email password lives in `config.py`, which is git-ignored. Keep it private.
- The app listens on all interfaces (`0.0.0.0`) so you can reach it from other
  devices on your network. If you don't want that, run it behind a firewall or
  only access it locally.

---

## Troubleshooting

- **`"/config.py": not found` during Docker build** — `config.py` isn't in the
  folder. Run `cp config.example.py config.py`, edit it, then rebuild.
- **Login/authentication errors** — you're probably using your normal password.
  Generate an **App Password** instead.
- **Scheduled mail didn't send** — the app/container must be running at that time.
  With Docker, the `restart: unless-stopped` setting keeps it up.
- **Can't reach the page** — confirm it's running and you're using the right port
  (default **8894**).
