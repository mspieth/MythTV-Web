# MythTV Web Server Agent Guide

## Project Description

This project is a Python web server that provides a browser-based interface to the MythTV services available locally on the server. The application is built with Flask and is intended to sit behind Apache as a reverse proxy. Flask authenticates its own local accounts for application access and role management; it does not consume Apache's authentication identity.

## Requirements

1. Use Python and Flask for the web application, following the existing app-factory structure in `mythtv_web/`.
2. Keep the project environment local in `.venv/`; declare application dependencies in `requirements.txt` and development tools in `requirements-dev.txt`.
3. Bind the application server to `0.0.0.0` on port `6555`; the host firewall must restrict direct access to trusted clients and networks.
4. Use Apache as the public-facing reverse proxy. Flask app accounts provide application authentication independently of Apache and are stored in the local account database.
5. Keep port `6555` restricted by the host firewall to trusted clients and networks; do not expose it to untrusted networks.
6. Keep MythTV integration local to this server. Add specific MythTV features incrementally as requirements are agreed and documented.
7. Preserve the existing `/` landing route and `/healthz` health endpoint unless a change is intentionally required and documented.
8. Keep this file updated as project requirements, architecture, and operational assumptions evolve.
9. Manage the background Gunicorn service with `scripts/mythtv-web.sh`, which supports `start`, `stop`, `restart`, and `status`.
10. Capture Gunicorn access and error logs in `mythtv-web-YYYY-MM-DDT000000.log` files named for the start of each local-day period, rotate at local midnight, and retain no more than 10 period files including the active file.
11. Keep MythTV service calls server-side in Flask. Configure the local backend with `MYTHTV_BACKEND_URL`; do not expose MythTV service endpoints directly to browsers.
12. Group recorded programs by title and show every recording in a selected group, consistent with MythFrontend's recordings workflow.
13. Require explicit confirmation for recording-file deletion and never force-delete recordings by default. Public access is controlled by Apache and direct internal access by the host firewall; all application actions also require a Flask account session. Schedule-rule create, edit, and delete actions are available to authenticated admins and users; guests may create and edit rules but cannot delete rules or recordings. Rule deletion still requires explicit confirmation.
14. Require Flask session authentication for application pages and APIs, except `/healthz` and static assets. Support only the `admin`, `user`, and `guest` roles; guest accounts otherwise have user permissions but cannot delete recordings or recording rules. Administrators manage accounts, roles, and passwords. Bootstrap the first admin as `admin` / `mythtv-admin` unless overridden before the first start, and require a password change on first sign-in.
15. Support embedded CEA-608 CC1 and CEA-708 service 1 through optional server-side mythccextractor extraction to WebVTT.

## Development Guidance

- Follow the current Flask and Python patterns in the repository; keep changes focused on the requested feature.
- Do not commit credentials, tokens, or other secrets. Keep local environment files and `.venv/` out of Git.
- Add or update focused tests for behavior changes. Before finishing a feature, run the relevant tests or checks and review the complete diff.
- Use Black for formatting, Ruff for linting, and pytest for tests.
- Update `README.md` when setup, run, deployment, or operational instructions change.

## Git Workflow

1. Complete one coherent feature or fix at a time.
2. Before every commit, run `black .`, `ruff check .`, and `pytest`; resolve any failures.
3. Review the staged diff before committing.
4. Commit each completed feature or fix to Git as its own focused commit, using a message that describes the change.
5. Do not include unrelated user changes in a feature commit.
