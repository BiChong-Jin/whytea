from datetime import datetime, timedelta

from jose import jwt
from passlib.context import CryptContext
from sqlalchemy.orm import Session

from .config import settings
from .models_db import User

SECRET_KEY = settings.jwt_secret_key
ALGORITHM = "HS256"
TOKEN_EXPIRE_MINUTES = 60 * 24  # 24 hours

pwd_context = CryptContext(schemes=["bcrypt"])


def hash_password(password: str) -> str:
    # use pwd_context to hash the password

    return pwd_context.hash(password)


def verify_password(plain: str, hashed: str) -> bool:
    # use pwd_context to verify

    return pwd_context.verify(plain, hashed)


def get_user(db: Session, username: str) -> User | None:
    # query the db for a User with matching username
    return db.query(User).filter(User.user_name == username).first()


def create_user(db: Session, username: str, password: str) -> User | None:
    # check if username already taken — return None if so
    # hash the password
    # create a User instance, add to db, commit, return it
    if get_user(db, username):
        return None

    hashed_password = hash_password(password)
    new_user = User(user_name=username, hashed_password=hashed_password)
    db.add(new_user)
    db.commit()
    db.refresh(new_user)

    return new_user


def create_token(username: str) -> str:
    # build a payload: {"sub": username, "exp": datetime.utcnow() + timedelta(minutes=TOKEN_EXPIRE_MINUTES)}
    # return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)
    payload = {
        "sub": username,
        "exp": datetime.utcnow() + timedelta(minutes=TOKEN_EXPIRE_MINUTES),
    }

    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def decode_token(token: str) -> str | None:
    # wrap in try/except
    # jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    # return payload["sub"] (the username)
    # return None if anything fails

    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        return payload["sub"]
    except Exception as e:
        return None
