from backend.auth import (
    create_token,
    create_user,
    decode_token,
    get_user,
    hash_password,
    verify_password,
)


def test_hash_password_does_not_return_plaintext():
    hashed = hash_password("correct-password")
    assert hashed != "correct-password"


def test_verify_password_roundtrip():
    hashed = hash_password("correct-password")
    assert verify_password("correct-password", hashed) is True


def test_verify_password_rejects_wrong_password():
    hashed = hash_password("correct-password")
    assert verify_password("wrong-password", hashed) is False


def test_create_user_persists_and_is_retrievable(db_session):
    user = create_user(db_session, "alice", "supersecret1")
    assert user is not None
    assert user.user_name == "alice"

    fetched = get_user(db_session, "alice")
    assert fetched is not None
    assert fetched.id == user.id


def test_create_user_rejects_duplicate_username(db_session):
    create_user(db_session, "bob", "supersecret1")
    duplicate = create_user(db_session, "bob", "different-password")
    assert duplicate is None


def test_get_user_returns_none_for_unknown_username(db_session):
    assert get_user(db_session, "nobody") is None


def test_create_token_and_decode_token_roundtrip():
    token = create_token("carol")
    assert decode_token(token) == "carol"


def test_decode_token_rejects_garbage_token():
    assert decode_token("not-a-real-token") is None
