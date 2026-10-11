import re
import sqlite3
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qs, urlsplit

from mythtv_web import create_app

INITIAL_PASSWORD = "known-initial-password"
UPDATED_ADMIN_PASSWORD = "updated-admin-password"


def make_app(tmp_path):
    app = create_app(
        {
            "AUTH_DATABASE_PATH": str(tmp_path / "users.sqlite"),
            "SECRET_KEY": "test-only-secret-key",
            "INITIAL_ADMIN_PASSWORD": INITIAL_PASSWORD,
        }
    )
    app.testing = True
    return app


def csrf_token(response):
    return re.search(
        r'name="csrf_token" value="([^"]+)"', response.get_data(as_text=True)
    ).group(1)


def login(client, username, password):
    page = client.get("/login")
    return client.post(
        "/login",
        data={
            "csrf_token": csrf_token(page),
            "username": username,
            "password": password,
        },
    )


def rotate_password(client, current_password, new_password):
    page = client.get("/change-password")
    return client.post(
        "/change-password",
        data={
            "csrf_token": csrf_token(page),
            "current_password": current_password,
            "new_password": new_password,
            "confirm_password": new_password,
        },
    )


def user_id(app, username):
    with sqlite3.connect(app.config["AUTH_DATABASE_PATH"]) as connection:
        return connection.execute(
            "SELECT id FROM users WHERE username = ?", (username,)
        ).fetchone()[0]


def test_initial_admin_must_change_password_and_anonymous_routes_are_restricted(
    tmp_path,
):
    app = make_app(tmp_path)
    client = app.test_client()

    assert client.get("/healthz").status_code == 200
    assert client.get("/").status_code == 302
    assert client.get("/api/programs").status_code == 401
    assert client.get("/login").status_code == 200

    response = login(client, "admin", INITIAL_PASSWORD)
    assert response.status_code == 302
    assert response.location.endswith("/change-password")
    session_cookie = next(
        value
        for value in response.headers.getlist("Set-Cookie")
        if value.startswith("session=")
    )
    expires = parsedate_to_datetime(
        session_cookie.split("Expires=", 1)[1].split(";", 1)[0]
    )
    remaining_lifetime = (expires - datetime.now(timezone.utc)).total_seconds()
    assert 31_535_990 <= remaining_lifetime <= 31_536_000
    assert client.get("/").location.endswith("/change-password")

    response = rotate_password(client, INITIAL_PASSWORD, UPDATED_ADMIN_PASSWORD)
    assert response.status_code == 302
    assert response.location.endswith("/")
    assert client.get("/").status_code == 200
    assert client.get("/admin/users").status_code == 200


def test_configured_secret_key_survives_restart_without_environment_key(
    tmp_path, monkeypatch
):
    monkeypatch.delenv("MYTHTV_SECRET_KEY", raising=False)
    database_path = tmp_path / "users.sqlite"
    configured_key = "stable-configured-test-key"
    first_app = create_app(
        {
            "AUTH_DATABASE_PATH": str(database_path),
            "SECRET_KEY": configured_key,
            "INITIAL_ADMIN_PASSWORD": INITIAL_PASSWORD,
        }
    )
    first_client = first_app.test_client()
    response = login(first_client, "admin", INITIAL_PASSWORD)
    session_cookie = first_client.get_cookie("session")

    assert response.location.endswith("/change-password")
    assert session_cookie is not None
    assert (tmp_path / "flask-secret-key").read_text() == configured_key

    restarted_app = create_app(
        {
            "AUTH_DATABASE_PATH": str(database_path),
            "INITIAL_ADMIN_PASSWORD": INITIAL_PASSWORD,
        }
    )
    restarted_client = restarted_app.test_client()
    restarted_client.set_cookie("session", session_cookie.value)

    assert restarted_client.get("/").location.endswith("/change-password")


def test_users_page_has_back_to_library_link(tmp_path):
    app = make_app(tmp_path)
    client = app.test_client()
    login(client, "admin", INITIAL_PASSWORD)
    rotate_password(client, INITIAL_PASSWORD, UPDATED_ADMIN_PASSWORD)

    response = client.get("/admin/users")

    assert response.status_code == 200
    assert b'class="back-button users-back-link" href="/"' in response.data
    assert b"Back to library" in response.data


def test_login_requires_a_nonempty_csrf_token_and_sets_samesite(tmp_path):
    app = make_app(tmp_path)
    client = app.test_client()
    assert (
        client.post(
            "/login", data={"username": "admin", "password": INITIAL_PASSWORD}
        ).status_code
        == 302
    )
    page = client.get("/login")
    assert "SameSite=Strict" in page.headers["Set-Cookie"]
    assert (
        client.post(
            "/login", data={"username": "admin", "password": INITIAL_PASSWORD}
        ).status_code
        == 302
    )
    assert client.post("/login", data={"csrf_token": "invalid-☃"}).status_code == 302
    assert login(client, "admin", INITIAL_PASSWORD).status_code == 302


def test_password_change_revokes_other_sessions_but_keeps_current_session(tmp_path):
    app = make_app(tmp_path)
    first, second = app.test_client(), app.test_client()
    login(first, "admin", INITIAL_PASSWORD)
    login(second, "admin", INITIAL_PASSWORD)
    assert (
        rotate_password(first, INITIAL_PASSWORD, UPDATED_ADMIN_PASSWORD).status_code
        == 302
    )
    assert first.get("/").status_code == 200
    assert second.get("/api/programs").status_code == 401


def test_admin_password_reset_revokes_existing_sessions(tmp_path):
    app = make_app(tmp_path)
    admin = app.test_client()
    login(admin, "admin", INITIAL_PASSWORD)
    rotate_password(admin, INITIAL_PASSWORD, UPDATED_ADMIN_PASSWORD)
    other = app.test_client()
    login(other, "admin", UPDATED_ADMIN_PASSWORD)
    page = admin.get("/admin/users")
    admin.post(
        "/admin/users/1/password",
        data={"csrf_token": csrf_token(page), "password": "replacement-password"},
    )
    assert other.get("/api/programs").status_code == 401
    assert admin.get("/api/programs").status_code == 401
    assert login(other, "admin", "replacement-password").location == "/"


def test_deleted_account_cookies_cannot_authenticate_as_replacements(tmp_path):
    app = make_app(tmp_path)
    with sqlite3.connect(app.config["AUTH_DATABASE_PATH"]) as connection:
        connection.execute(
            "INSERT INTO users (username, password_hash, role) VALUES ('old-user', 'hash', 'user')"
        )
        old_id, token = connection.execute(
            "SELECT id, session_token FROM users WHERE username = 'old-user'"
        ).fetchone()
    old = app.test_client()
    with old.session_transaction() as saved:
        saved["user_id"] = old_id
        saved["session_token"] = token
    with sqlite3.connect(app.config["AUTH_DATABASE_PATH"]) as connection:
        connection.execute("DELETE FROM users WHERE id = ?", (old_id,))
        connection.execute(
            "INSERT INTO users (username, password_hash, role) VALUES ('replacement', 'hash', 'admin')"
        )
        new_id = connection.execute(
            "SELECT id FROM users WHERE username = 'replacement'"
        ).fetchone()[0]
    assert new_id > old_id
    assert old.get("/api/programs").status_code == 401
    # Even an explicitly restored numerical ID cannot resurrect a signed cookie.
    with sqlite3.connect(app.config["AUTH_DATABASE_PATH"]) as connection:
        connection.execute("UPDATE users SET id = ? WHERE id = ?", (old_id, new_id))
    with old.session_transaction() as saved:
        saved["user_id"] = old_id
        saved["session_token"] = token
    assert old.get("/admin/users").status_code == 302


def test_legacy_sessions_are_rejected_and_migration_is_idempotent(tmp_path):
    app = make_app(tmp_path)
    client = app.test_client()
    with client.session_transaction() as saved:
        saved["user_id"] = 1
    assert client.get("/api/programs").status_code == 401
    login(client, "admin", INITIAL_PASSWORD)
    rotate_password(client, INITIAL_PASSWORD, UPDATED_ADMIN_PASSWORD)
    cookie = client.get_cookie("session").value
    restarted = make_app(tmp_path).test_client()
    restarted.set_cookie("session", cookie)
    assert restarted.get("/").status_code == 200


def test_login_round_trip_keeps_forwarded_prefix(tmp_path):
    app = make_app(tmp_path)
    client = app.test_client()
    prefix_headers = {"X-Forwarded-Prefix": "/mythtv-web"}
    with sqlite3.connect(app.config["AUTH_DATABASE_PATH"]) as connection:
        connection.execute("UPDATE users SET must_change_password = 0")

    response = client.get("/", headers=prefix_headers)
    login_location = urlsplit(response.location)
    assert login_location.path == "/mythtv-web/login"
    next_url = parse_qs(login_location.query)["next"][0]
    assert next_url.startswith("/mythtv-web/")

    login_page = client.get(f"/login?{login_location.query}", headers=prefix_headers)
    assert b'action="/mythtv-web/login"' in login_page.data
    response = client.post(
        "/login",
        headers=prefix_headers,
        data={
            "csrf_token": csrf_token(login_page),
            "username": "admin",
            "password": INITIAL_PASSWORD,
            "next": next_url,
        },
    )

    assert response.status_code == 302
    assert response.location.startswith("/mythtv-web/")


def test_authenticated_browser_back_from_library_skips_login_form(tmp_path):
    app = make_app(tmp_path)
    client = app.test_client()
    with sqlite3.connect(app.config["AUTH_DATABASE_PATH"]) as connection:
        connection.execute("UPDATE users SET must_change_password = 0")

    response = login(client, "admin", INITIAL_PASSWORD)
    assert response.status_code == 302

    back_response = client.get("/login")

    assert back_response.status_code == 302
    assert back_response.location == "/"
    assert client.get(back_response.location).status_code == 200


def test_login_root_destination_stays_inside_forwarded_prefix(tmp_path):
    app = make_app(tmp_path)
    client = app.test_client()
    prefix_headers = {"X-Forwarded-Prefix": "/mythtv-web"}
    with sqlite3.connect(app.config["AUTH_DATABASE_PATH"]) as connection:
        connection.execute("UPDATE users SET must_change_password = 0")

    login_page = client.get("/login?next=/%3F", headers=prefix_headers)
    response = client.post(
        "/login",
        headers=prefix_headers,
        data={
            "csrf_token": csrf_token(login_page),
            "username": "admin",
            "password": INITIAL_PASSWORD,
            "next": "/?",
        },
    )

    assert response.status_code == 302
    assert response.location == "/mythtv-web/"


def test_stale_login_csrf_refreshes_form_without_accepting_submission(tmp_path):
    app = make_app(tmp_path)
    client = app.test_client()

    login_page = client.get("/login")
    assert login_page.headers["Cache-Control"] == "no-store"
    rejected = client.post(
        "/login",
        data={
            "csrf_token": "stale-token",
            "username": "admin",
            "password": INITIAL_PASSWORD,
        },
    )

    assert rejected.status_code == 302
    refreshed = client.get(rejected.location)
    assert refreshed.status_code == 200
    assert b"Your sign-in form expired" in refreshed.data
    assert csrf_token(refreshed) != "stale-token"

    response = client.post(
        "/login",
        data={
            "csrf_token": csrf_token(refreshed),
            "username": "admin",
            "password": INITIAL_PASSWORD,
        },
    )
    assert response.status_code == 302
    assert response.location.endswith("/change-password")


def test_admin_manages_accounts_and_regular_users_cannot_manage_users(tmp_path):
    app = make_app(tmp_path)
    admin = app.test_client()
    login(admin, "admin", INITIAL_PASSWORD)
    rotate_password(admin, INITIAL_PASSWORD, UPDATED_ADMIN_PASSWORD)

    page = admin.get("/admin/users")
    response = admin.post(
        "/admin/users",
        data={
            "csrf_token": csrf_token(page),
            "username": "viewer",
            "password": "temporary-user-password",
            "role": "user",
        },
    )
    assert response.status_code == 302
    viewer_id = user_id(app, "viewer")

    page = admin.get("/admin/users")
    response = admin.post(
        f"/admin/users/{viewer_id}/role",
        data={"csrf_token": csrf_token(page), "role": "admin"},
    )
    assert response.status_code == 302
    with sqlite3.connect(app.config["AUTH_DATABASE_PATH"]) as connection:
        assert (
            connection.execute(
                "SELECT role FROM users WHERE id = ?", (viewer_id,)
            ).fetchone()[0]
            == "admin"
        )

    page = admin.get("/admin/users")
    admin.post(
        f"/admin/users/{viewer_id}/role",
        data={"csrf_token": csrf_token(page), "role": "user"},
    )

    page = admin.get("/admin/users")
    response = admin.post(
        f"/admin/users/{viewer_id}/password",
        data={
            "csrf_token": csrf_token(page),
            "password": "replacement-user-password",
            "must_change_password": "on",
        },
    )
    assert response.status_code == 302

    viewer = app.test_client()
    response = login(viewer, "viewer", "replacement-user-password")
    assert response.location.endswith("/change-password")
    rotate_password(viewer, "replacement-user-password", "permanent-user-password")
    assert viewer.get("/").status_code == 200
    assert viewer.get("/admin/users").status_code == 403

    page = admin.get("/admin/users")
    admin.post(
        f"/admin/users/{viewer_id}/delete",
        data={"csrf_token": csrf_token(page)},
    )
    assert admin.get("/admin/users").status_code == 200
    with sqlite3.connect(app.config["AUTH_DATABASE_PATH"]) as connection:
        assert (
            connection.execute(
                "SELECT 1 FROM users WHERE username = 'viewer'"
            ).fetchone()
            is None
        )


def test_existing_user_database_migrates_to_guest_role(tmp_path):
    database_path = tmp_path / "legacy-users.sqlite"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT NOT NULL UNIQUE COLLATE NOCASE, "
            "password_hash TEXT NOT NULL, role TEXT NOT NULL CHECK (role IN ('admin', 'user')), "
            "must_change_password INTEGER NOT NULL DEFAULT 0)"
        )
        connection.execute(
            "INSERT INTO users (id, username, password_hash, role) VALUES (1, 'admin', 'hash', 'admin')"
        )
        connection.execute(
            "INSERT INTO users (id, username, password_hash, role) VALUES (2, 'viewer', 'hash', 'user')"
        )

    app = create_app(
        {
            "AUTH_DATABASE_PATH": str(database_path),
            "SECRET_KEY": "test-only-secret-key",
            "INITIAL_ADMIN_PASSWORD": INITIAL_PASSWORD,
        }
    )

    with sqlite3.connect(database_path) as connection:
        connection.execute("UPDATE users SET role = 'guest' WHERE username = 'viewer'")
        rows = connection.execute(
            "SELECT id, username, role FROM users ORDER BY id"
        ).fetchall()

    assert rows == [(1, "admin", "admin"), (2, "viewer", "guest")]
    assert app.test_client().get("/").status_code == 302


def test_guest_can_be_created_and_cannot_manage_users(tmp_path):
    app = make_app(tmp_path)
    admin = app.test_client()
    login(admin, "admin", INITIAL_PASSWORD)
    rotate_password(admin, INITIAL_PASSWORD, UPDATED_ADMIN_PASSWORD)
    page = admin.get("/admin/users")
    response = admin.post(
        "/admin/users",
        data={
            "csrf_token": csrf_token(page),
            "username": "guest-viewer",
            "password": "temporary-guest-password",
            "role": "guest",
        },
    )
    assert response.status_code == 302
    guest_id = user_id(app, "guest-viewer")
    page = admin.get("/admin/users")
    admin.post(
        f"/admin/users/{guest_id}/role",
        data={"csrf_token": csrf_token(page), "role": "user"},
    )
    page = admin.get("/admin/users")
    role_update = admin.post(
        f"/admin/users/{guest_id}/role",
        data={"csrf_token": csrf_token(page), "role": "guest"},
    )
    assert role_update.status_code == 302
    guest = app.test_client()
    assert login(guest, "guest-viewer", "temporary-guest-password").location.endswith(
        "/change-password"
    )
    rotate_password(guest, "temporary-guest-password", "permanent-guest-password")
    assert guest.get("/").status_code == 200
    assert guest.get("/admin/users").status_code == 403


def test_admin_management_requires_csrf_and_preserves_last_admin(tmp_path):
    app = make_app(tmp_path)
    client = app.test_client()
    login(client, "admin", INITIAL_PASSWORD)
    rotate_password(client, INITIAL_PASSWORD, UPDATED_ADMIN_PASSWORD)
    admin_id = user_id(app, "admin")

    assert client.post("/admin/users", data={"username": "forged"}).status_code == 400
    page = client.get("/admin/users")
    response = client.post(
        f"/admin/users/{admin_id}/role",
        data={"csrf_token": csrf_token(page), "role": "user"},
    )
    assert response.status_code == 302
    with sqlite3.connect(app.config["AUTH_DATABASE_PATH"]) as connection:
        assert (
            connection.execute(
                "SELECT role FROM users WHERE id = ?", (admin_id,)
            ).fetchone()[0]
            == "admin"
        )

    response = client.post(
        f"/admin/users/{admin_id}/delete",
        data={"csrf_token": csrf_token(page)},
    )
    assert response.status_code == 302
    with sqlite3.connect(app.config["AUTH_DATABASE_PATH"]) as connection:
        assert (
            connection.execute(
                "SELECT 1 FROM users WHERE id = ?", (admin_id,)
            ).fetchone()
            is not None
        )
