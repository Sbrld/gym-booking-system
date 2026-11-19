# Deployment Guide (Render.com)

This project is ready to deploy as a Python WSGI web service using **Gunicorn** and the included `render.yaml` Blueprint.

## Prerequisites
- A GitHub (or GitLab) repository containing this project.
- A free [Render.com](https://render.com) account.

## Option 1: One-Click Blueprint Deploy (`render.yaml`)
1. Push the repository to GitHub (`.gitignore` excludes `.venv/`, `venv/`, `__pycache__/`, `gym.db`, and `.env`).
2. Log in to the Render Dashboard and click **New + -> Blueprint**.
3. Connect your repository. Render will automatically detect `render.yaml` and configure:
   - **Build Command:** `pip install -r requirements.txt`
   - **Start Command:** `gunicorn app:app`
   - **Environment Variables:**
     - `FLASK_SECRET_KEY` (auto-generated securely by Render)
     - `FLASK_DEBUG=0`
     - `DATABASE_PATH=gym.db`
     - `PYTHON_VERSION=3.13.1`
4. Click **Apply** and wait for the build to succeed.
5. Open your generated `https://<service-name>.onrender.com` URL. The SQLite database is automatically initialized and seeded on first request.

## Option 2: Manual Web Service Setup
1. In the Render Dashboard, click **New + -> Web Service** and select your repository.
2. Configure:
   - **Runtime:** Python 3
   - **Build Command:** `pip install -r requirements.txt`
   - **Start Command:** `gunicorn app:app`
3. Under **Environment**, add:
   - `FLASK_SECRET_KEY` = a strong random string
   - `FLASK_DEBUG` = `0`
   - `DATABASE_PATH` = `gym.db` (or `/var/data/gym.db` if using a Render Persistent Disk)
4. Click **Create Web Service**.

## Production Notes
- **SQLite Persistence:** Render's Free tier uses an ephemeral filesystem, so `gym.db` resets on each deploy or service restart. To persist data across deploys on a paid plan, attach a Render Disk (e.g., mounted at `/var/data`) and set `DATABASE_PATH=/var/data/gym.db`, or migrate to PostgreSQL.
- **Gunicorn Workers & SQLite:** If scaling Gunicorn beyond 1 worker (`--workers 1 --threads 4`), prefer a single worker with threads or PostgreSQL to avoid SQLite write lock contention.
- **Password Reset Links:** Because no SMTP server is configured, `/forgot-password` displays the reset URL in an on-page demo notice box. Integrate an email provider (e.g., Resend, SendGrid, SMTP) before public production use.
