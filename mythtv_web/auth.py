import fcntl
import hmac
import os
import re
import secrets
import sqlite3
from contextlib import closing, contextmanager
from functools import wraps
from pathlib import Path
from urllib.parse import urlsplit

from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash

auth = Blueprint("auth", __name__)
USERNAME_PATTERN = re.compile(r"^[a-zA-Z0-9_.-]{1,64}$")
MIN_PASSWORD_LENGTH = 12
USER_TABLE_SQL = """CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('admin', 'user', 'guest')),
    must_change_password INTEGER NOT NULL DEFAULT 0,
    session_token TEXT NOT NULL DEFAULT (lower(hex(randomblob(32))))
)"""


@contextmanager
def _connect():
    connection = sqlite3.connect(current_app.config["AUTH_DATABASE_PATH"], timeout=5)
    connection.row_factory = sqlite3.Row
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _load_user(user_id: int) -> dict | None:
    with _connect() as connection:
        row = connection.execute(
            "SELECT id, username, role, must_change_password, session_token FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
    if row is None:
        return None
    return {
        "id": row["id"],
        "username": row["username"],
        "role": row["role"],
        "must_change_password": bool(row["must_change_password"]),
        "session_token": row["session_token"],
    }


def _get_secret_key(app) -> str:
    configured_key = app.config.get("SECRET_KEY")
    key_path = Path(app.config["AUTH_DATABASE_PATH"]).parent / "flask-secret-key"
    key_path.parent.mkdir(parents=True, exist_ok=True)
    with key_path.open("a+", encoding="utf-8") as key_file:
        fcntl.flock(key_file, fcntl.LOCK_EX)
        key_file.seek(0)
        stored_key = key_file.read().strip()
        key = configured_key or stored_key
        if not key:
            key = secrets.token_hex(32)
        if key != stored_key:
            key_file.seek(0)
            key_file.write(key)
            key_file.truncate()
            key_file.flush()
            os.fchmod(key_file.fileno(), 0o600)
        return key


def _initialize_database(app) -> None:
    database_path = Path(app.config["AUTH_DATABASE_PATH"])
    database_path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(database_path, timeout=5)) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(USER_TABLE_SQL)
        schema = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'users'"
        ).fetchone()[0]
        if (
            "'guest'" not in schema.lower()
            or "autoincrement" not in schema.lower()
            or "session_token" not in schema.lower()
        ):
            columns = {row[1] for row in connection.execute("PRAGMA table_info(users)")}
            connection.execute("ALTER TABLE users RENAME TO users_before_guest_role")
            connection.execute(USER_TABLE_SQL)
            fields = "id, username, password_hash, role, must_change_password"
            if "session_token" in columns:
                fields += ", session_token"
            connection.execute(
                f"INSERT INTO users ({fields}) SELECT {fields} FROM users_before_guest_role"
            )
            connection.execute("DROP TABLE users_before_guest_role")
        has_users = connection.execute("SELECT 1 FROM users LIMIT 1").fetchone()
        if has_users is None:
            username = app.config["INITIAL_ADMIN_USERNAME"].strip()
            password = app.config["INITIAL_ADMIN_PASSWORD"]
            if not USERNAME_PATTERN.fullmatch(username):
                raise ValueError("Initial admin username has an invalid format")
            if len(password) < MIN_PASSWORD_LENGTH:
                raise ValueError(
                    f"Initial admin password must be at least {MIN_PASSWORD_LENGTH} characters"
                )
            connection.execute(
                """INSERT INTO users
                   (username, password_hash, role, must_change_password)
                   VALUES (?, ?, 'admin', 1)""",
                (username, generate_password_hash(password)),
            )
    os.chmod(database_path, 0o600)


def init_auth(app) -> None:
    app.config.setdefault("AUTH_DATABASE_PATH", ".run/users.sqlite")
    app.config.setdefault("INITIAL_ADMIN_USERNAME", "admin")
    app.config.setdefault("INITIAL_ADMIN_PASSWORD", "mythtv-admin")
    app.config.setdefault("SESSION_COOKIE_HTTPONLY", True)
    app.secret_key = _get_secret_key(app)
    _initialize_database(app)
    app.register_blueprint(auth)

    @app.before_request
    def load_authenticated_user():
        g.current_user = None
        user_id = session.get("user_id")
        if type(user_id) is int:
            user = _load_user(user_id)
            token = session.get("session_token")
            if (
                user
                and isinstance(token, str)
                and hmac.compare_digest(token.encode(), user["session_token"].encode())
            ):
                g.current_user = user
            else:
                session.clear()

        endpoint = request.endpoint or ""
        if endpoint.startswith("auth.") and request.method in {
            "POST",
            "PUT",
            "PATCH",
            "DELETE",
        }:
            supplied_token = request.form.get("csrf_token") or request.headers.get(
                "X-CSRF-Token", ""
            )
            expected_token = session.get("csrf_token")
            if (
                not supplied_token
                or not expected_token
                or not hmac.compare_digest(
                    supplied_token.encode(), expected_token.encode()
                )
            ):
                if endpoint == "auth.login":
                    return redirect(
                        url_for(
                            "auth.login",
                            next=request.form.get("next"),
                            csrf="expired",
                        )
                    )
                abort(400, "Invalid CSRF token")

        if endpoint in {"static", "healthz", "auth.login"}:
            return None
        if g.current_user is None:
            if request.path.startswith("/api/"):
                return jsonify(error="Authentication required"), 401
            return redirect(
                url_for(
                    "auth.login",
                    next=f"{request.script_root}{request.full_path}",
                )
            )
        if g.current_user["must_change_password"] and endpoint not in {
            "auth.change_password",
            "auth.logout",
        }:
            return redirect(url_for("auth.change_password"))
        return None

    @app.context_processor
    def inject_auth_context():
        def csrf_token() -> str:
            token = session.get("csrf_token")
            if token is None:
                token = secrets.token_urlsafe(32)
                session["csrf_token"] = token
            return token

        return {"current_user": g.get("current_user"), "csrf_token": csrf_token}

    @app.after_request
    def prevent_login_form_caching(response):
        if request.endpoint == "auth.login":
            response.headers["Cache-Control"] = "no-store"
            response.headers["Pragma"] = "no-cache"
        return response


def _valid_password(password: str) -> bool:
    return MIN_PASSWORD_LENGTH <= len(password) <= 1024


def _safe_next_url(target: str | None) -> str | None:
    if not target:
        return None
    parsed = urlsplit(target)
    if (
        parsed.scheme
        or parsed.netloc
        or not target.startswith("/")
        or target.startswith("//")
        or "\\" in target
    ):
        return None
    return target


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if g.current_user["role"] != "admin":
            abort(403)
        return view(*args, **kwargs)

    return wrapped


def deletion_allowed(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if g.current_user["role"] == "guest":
            return jsonify(error="Guest accounts cannot delete items"), 403
        return view(*args, **kwargs)

    return wrapped


@auth.route("/login", methods=["GET", "POST"])
def login():
    next_url = request.values.get("next")
    if request.method == "GET":
        if g.current_user is not None:
            if g.current_user["must_change_password"]:
                return redirect(url_for("auth.change_password"))
            destination = _safe_next_url(next_url)
            if destination is None or urlsplit(destination).path in {
                url_for("index"),
                url_for("auth.login"),
            }:
                destination = url_for("index")
            return redirect(destination)
        error = (
            "Your sign-in form expired. Please enter your details again."
            if request.args.get("csrf") == "expired"
            else None
        )
        return render_template("login.html", next_url=next_url, error=error)

    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")
    with _connect() as connection:
        user = connection.execute(
            "SELECT id, password_hash, session_token, must_change_password FROM users WHERE username = ? COLLATE NOCASE",
            (username,),
        ).fetchone()
    if user is None or not check_password_hash(user["password_hash"], password):
        return (
            render_template(
                "login.html",
                next_url=next_url,
                error="Username or password is incorrect.",
            ),
            401,
        )

    session.clear()
    session.permanent = True
    session["user_id"] = user["id"]
    session["session_token"] = user["session_token"]
    session["csrf_token"] = secrets.token_urlsafe(32)
    if user["must_change_password"]:
        return redirect(url_for("auth.change_password"))
    destination = _safe_next_url(next_url)
    if destination is None or urlsplit(destination).path == "/":
        destination = url_for("index")
    return redirect(destination)


@auth.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("auth.login"))


@auth.route("/change-password", methods=["GET", "POST"])
def change_password():
    if g.current_user is None:
        return redirect(url_for("auth.login"))
    error = None
    if request.method == "POST":
        current_password = request.form.get("current_password", "")
        new_password = request.form.get("new_password", "")
        confirm_password = request.form.get("confirm_password", "")
        with _connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT password_hash, session_token FROM users WHERE id = ?",
                (g.current_user["id"],),
            ).fetchone()
            if row is None or row["session_token"] != session["session_token"]:
                session.clear()
                return redirect(url_for("auth.login"))
            if not check_password_hash(row["password_hash"], current_password):
                error = "Current password is incorrect."
            elif not _valid_password(new_password):
                error = f"Password must be at least {MIN_PASSWORD_LENGTH} characters."
            elif new_password != confirm_password:
                error = "New passwords do not match."
            else:
                new_token = secrets.token_hex(32)
                connection.execute(
                    "UPDATE users SET password_hash = ?, must_change_password = 0, session_token = ? WHERE id = ?",
                    (
                        generate_password_hash(new_password),
                        new_token,
                        g.current_user["id"],
                    ),
                )
        if error is None:
            session["session_token"] = new_token
            session["csrf_token"] = secrets.token_urlsafe(32)
            flash("Password changed.")
            return redirect(url_for("index"))
    return render_template("change_password.html", error=error)


@auth.get("/admin/users")
@admin_required
def users():
    with _connect() as connection:
        user_rows = connection.execute(
            "SELECT id, username, role, must_change_password FROM users ORDER BY username COLLATE NOCASE"
        ).fetchall()
    return render_template("users.html", users=user_rows)


@auth.post("/admin/users")
@admin_required
def add_user():
    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")
    role = request.form.get("role", "user")
    if not USERNAME_PATTERN.fullmatch(username):
        flash("Use 1-64 letters, numbers, periods, underscores, or hyphens.", "error")
    elif not _valid_password(password):
        flash(f"Password must be at least {MIN_PASSWORD_LENGTH} characters.", "error")
    elif role not in {"admin", "user", "guest"}:
        flash("Choose a valid role.", "error")
    else:
        try:
            with _connect() as connection:
                connection.execute(
                    """INSERT INTO users
                       (username, password_hash, role, must_change_password)
                       VALUES (?, ?, ?, 1)""",
                    (username, generate_password_hash(password), role),
                )
            flash(f"User {username} added.")
        except sqlite3.IntegrityError:
            flash("That username already exists.", "error")
    return redirect(url_for("auth.users"))


@auth.post("/admin/users/<int:user_id>/role")
@admin_required
def update_role(user_id: int):
    role = request.form.get("role", "")
    if role not in {"admin", "user", "guest"}:
        abort(400, "Invalid role")
    with _connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        target = connection.execute(
            "SELECT role FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        if target is None:
            abort(404)
        admin_count = connection.execute(
            "SELECT COUNT(*) FROM users WHERE role = 'admin'"
        ).fetchone()[0]
        if target["role"] == "admin" and role != "admin" and admin_count <= 1:
            flash("The last administrator cannot be demoted.", "error")
        else:
            connection.execute(
                "UPDATE users SET role = ? WHERE id = ?", (role, user_id)
            )
            flash("Role updated.")
    return redirect(url_for("auth.users"))


@auth.post("/admin/users/<int:user_id>/password")
@admin_required
def set_user_password(user_id: int):
    password = request.form.get("password", "")
    if not _valid_password(password):
        flash(f"Password must be at least {MIN_PASSWORD_LENGTH} characters.", "error")
        return redirect(url_for("auth.users"))
    with _connect() as connection:
        result = connection.execute(
            """UPDATE users SET password_hash = ?, must_change_password = ?, session_token = ?
               WHERE id = ?""",
            (
                generate_password_hash(password),
                int("must_change_password" in request.form),
                secrets.token_hex(32),
                user_id,
            ),
        )
        if result.rowcount == 0:
            abort(404)
    flash("Password updated.")
    return redirect(url_for("auth.users"))


@auth.post("/admin/users/<int:user_id>/delete")
@admin_required
def delete_user(user_id: int):
    if user_id == g.current_user["id"]:
        flash("You cannot delete your own account.", "error")
        return redirect(url_for("auth.users"))
    with _connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        target = connection.execute(
            "SELECT role FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        if target is None:
            abort(404)
        admin_count = connection.execute(
            "SELECT COUNT(*) FROM users WHERE role = 'admin'"
        ).fetchone()[0]
        if target["role"] == "admin" and admin_count <= 1:
            flash("The last administrator cannot be deleted.", "error")
        else:
            connection.execute("DELETE FROM users WHERE id = ?", (user_id,))
            flash("User deleted.")
    return redirect(url_for("auth.users"))
