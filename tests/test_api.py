def _register(client, username="testuser", password="testpass123"):
    return client.post(
        "/register",
        json={"user_name": username, "user_password": password},
    )


def _login(client, username="testuser", password="testpass123"):
    return client.post(
        "/login",
        json={"user_name": username, "user_password": password},
    )


def test_health_check(client):
    res = client.get("/health")
    assert res.status_code == 200
    assert res.json()["status"] == "ok"


def test_landing_page_served_at_root(client):
    res = client.get("/")
    assert res.status_code == 200
    assert "text/html" in res.headers["content-type"]


def test_dashboard_served_at_app(client):
    res = client.get("/app")
    assert res.status_code == 200
    assert "text/html" in res.headers["content-type"]


def test_register_then_login_succeeds(client):
    res = _register(client)
    assert res.status_code == 200

    res = _login(client)
    assert res.status_code == 200
    body = res.json()
    assert body["token_type"] == "bearer"
    assert body["access_token"]


def test_register_rejects_duplicate_username(client):
    _register(client)
    res = _register(client)
    assert res.status_code == 400


def test_register_rejects_short_password(client):
    res = _register(client, password="short")
    assert res.status_code == 422


def test_register_rejects_short_username(client):
    res = _register(client, username="ab")
    assert res.status_code == 422


def test_login_rejects_wrong_password(client):
    _register(client)
    res = _login(client, password="wrongpassword")
    assert res.status_code == 401


def test_login_rejects_unknown_user(client):
    res = _login(client, username="ghostuser")
    assert res.status_code == 401


def test_protected_endpoint_rejects_missing_token(client):
    res = client.post("/stop")
    assert res.status_code == 401


def test_protected_endpoint_accepts_valid_token(client):
    _register(client)
    token = _login(client).json()["access_token"]

    res = client.post("/stop", headers={"Authorization": f"Bearer {token}"})
    assert res.status_code == 200
    assert res.json()["status"] == "stopped"


def test_start_rejects_unparseable_video_url(client):
    _register(client)
    token = _login(client).json()["access_token"]

    res = client.post(
        "/start",
        json={"video_url": "not a youtube url"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res.status_code == 400


def test_status_reports_no_active_sessions_by_default(client):
    res = client.get("/status")
    assert res.status_code == 200
    body = res.json()
    assert body["monitoring"] is False
    assert body["connected_clients"] == 0
