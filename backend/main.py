import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import httpx
from fastapi import Depends, FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.security import OAuth2PasswordBearer
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy.orm import Session

from backend.models_db import User

from .analyzer import ChatAnalyzer
from .auth import create_token, create_user, decode_token, get_user, verify_password
from .database import get_db
from .models import WSMessage
from .youtube import YouTubeChatPoller, extract_video_id, resolve_live_chat_id

# ── WebSocket connection manager ────────────────────────────────────────────


@dataclass
class UserSession:
    tasks: list[asyncio.Task]
    analyzer: ChatAnalyzer


# Connections are kept separate from sessions so WebSocket registration
# is not affected by whether /start has been called yet
user_connections: dict[str, list[WebSocket]] = {}
sessions: dict[str, "UserSession"] = {}


async def broadcast_to_user(user_id: str, msg: WSMessage) -> None:
    connections = list(user_connections.get(user_id, []))  # snapshot to avoid mutation during iteration
    data = msg.model_dump_json()
    dead = []
    for ws in connections:
        try:
            await ws.send_text(data)
        except Exception:
            dead.append(ws)
    live = user_connections.get(user_id, [])
    for ws in dead:
        if ws in live:
            live.remove(ws)

# ── Background tasks ────────────────────────────────────────────────────────


async def polling_loop(poller: YouTubeChatPoller, user_id: str, analyzer: ChatAnalyzer) -> None:
    while True:
        try:
            comments, interval_ms = await poller.fetch_next_page()
            await analyzer.add_comments(comments)
            for c in comments:
                await broadcast_to_user(
                    user_id,
                    WSMessage(type="comment", payload=c.model_dump(mode="json")),
                )
        except Exception as exc:
            await broadcast_to_user(
                user_id, WSMessage(type="error", payload={"message": str(exc)})
            )
        await asyncio.sleep(interval_ms / 1000)


async def analysis_loop(interval_seconds: int, user_id: str, analyzer: ChatAnalyzer) -> None:
    while True:
        await asyncio.sleep(interval_seconds)
        window_end = datetime.now(timezone.utc)
        window_start = datetime.fromtimestamp(
            window_end.timestamp() - interval_seconds, tz=timezone.utc
        )
        try:
            result = await analyzer.run_analysis(window_start, window_end)
            if result:
                await broadcast_to_user(
                    user_id,
                    WSMessage(type="analysis", payload=result.model_dump(mode="json")),
                )
        except Exception as exc:
            await broadcast_to_user(
                user_id, WSMessage(type="error", payload={"message": str(exc)})
            )


# ── App lifecycle ────────────────────────────────────────────────────────────


@asynccontextmanager
async def lifespan(app: FastAPI):
    from .database import engine
    from .models_db import Base

    Base.metadata.create_all(bind=engine)
    yield
    for s in sessions.values():
        for t in s.tasks:
            t.cancel()
    sessions.clear()
    user_connections.clear()


app = FastAPI(title="Stream Audience Analyzer", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Serve frontend
frontend_dir = Path(__file__).parent.parent / "frontend"
if frontend_dir.exists():
    app.mount("/static", StaticFiles(directory=str(frontend_dir)), name="static")


# ── HTTP endpoints ───────────────────────────────────────────────────────────


class StartRequest(BaseModel):
    video_url: str


class RegisterRequest(BaseModel):
    user_name: str
    user_password: str


class LoginRequest(BaseModel):
    user_name: str
    user_password: str


oauth2_scheme = OAuth2PasswordBearer(tokenUrl="login")


def get_current_user(
    token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)
):
    # call decode_token(token) to get the username
    # if it returns None, raise HTTPException 401 with detail "invalid or expired token"
    # call get_user(db, username) to get the user from db
    # if user not found, raise HTTPException 401
    # return the user
    user_name = decode_token(token)

    if user_name is None:
        raise HTTPException(status_code=401, detail="invalid or expired token")

    user = get_user(db, user_name)

    if user is None:
        raise HTTPException(status_code=401, detail="user not found")

    return user


@app.get("/", response_class=HTMLResponse)
async def index():
    html_path = Path(__file__).parent.parent / "frontend" / "index.html"
    return HTMLResponse(html_path.read_text())


@app.get("/login", response_class=HTMLResponse)
async def login_page():
    html_path = Path(__file__).parent.parent / "frontend" / "login.html"
    return HTMLResponse(html_path.read_text())


@app.post("/register")
def register(body: RegisterRequest, db: Session = Depends(get_db)):
    # call create_user() from auth.py
    # if it returns None, the username is taken — raise HTTPException 400
    # otherwise return {"message": "registered successfully"}
    new_user: User | None = create_user(
        db=db, username=body.user_name, password=body.user_password
    )
    if new_user is None:
        raise HTTPException(status_code=400, detail="the username is taken.")
    else:
        return {"message": "registered successfully"}


@app.post("/login")
def login(body: LoginRequest, db: Session = Depends(get_db)):
    # call get_user() to find the user
    # if not found, raise HTTPException 401
    # call verify_password() to check the password
    # if wrong, raise HTTPException 401
    # call create_token() with the username
    # return {"access_token": token, "token_type": "bearer"}
    user: User | None = get_user(db=db, username=body.user_name)

    if user is None:
        raise HTTPException(status_code=401, detail="user not found")

    is_correct_password = verify_password(body.user_password, user.hashed_password)
    if not is_correct_password:
        raise HTTPException(status_code=401, detail="wrong password")

    token: str = create_token(body.user_name)
    return {"access_token": token, "token_type": "bearer"}


@app.post("/start")
async def start_monitoring(body: StartRequest, current_user=Depends(get_current_user)):
    from .config import settings

    user_id = str(current_user.id)

    existing = sessions.get(user_id)
    if existing:
        for t in existing.tasks:
            t.cancel()

    try:
        video_id = extract_video_id(body.video_url)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    try:
        live_chat_id = await resolve_live_chat_id(video_id, settings.youtube_api_key)
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 403:
            raise HTTPException(
                status_code=502,
                detail=(
                    "YouTube API returned 403 Forbidden. "
                    "Make sure the YouTube Data API v3 is enabled in your Google Cloud project "
                    "and that your API key has no restrictive settings blocking server requests."
                ),
            )
        raise HTTPException(
            status_code=502, detail=f"YouTube API error: {e.response.status_code}"
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    analyzer = ChatAnalyzer()
    poller = YouTubeChatPoller(live_chat_id, settings.youtube_api_key)
    session = UserSession(tasks=[], analyzer=analyzer)
    session.tasks.append(asyncio.create_task(polling_loop(poller, user_id, analyzer)))
    session.tasks.append(asyncio.create_task(analysis_loop(settings.analysis_interval_seconds, user_id, analyzer)))
    sessions[user_id] = session

    return {"status": "monitoring", "video_id": video_id, "live_chat_id": live_chat_id}


@app.post("/stop")
async def stop_monitoring(current_user=Depends(get_current_user)):
    user_id = str(current_user.id)
    current_session = sessions.pop(user_id, None)
    if current_session:
        for t in current_session.tasks:
            t.cancel()
        await broadcast_to_user(
            user_id,
            WSMessage(type="status", payload={"state": "idle", "message": "Monitoring stopped."}),
        )
    return {"status": "stopped"}


@app.get("/status")
async def get_status():
    total_tasks_nums = 0
    total_connected_clients = len(sessions)

    for _, user_session in sessions.items():
        current_task_nums = len(user_session.tasks)
        total_tasks_nums += current_task_nums

    return {
        "active_tasks": total_tasks_nums,
        "connected_clients": total_connected_clients,
        "monitoring": total_tasks_nums > 0,
    }


# ── WebSocket ────────────────────────────────────────────────────────────────


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket, token: str | None = None):
    if not token:
        await ws.close(code=4001)
        return
    username = decode_token(token)
    if not username:
        await ws.close(code=4001)
        return

    db = next(get_db())
    user = get_user(db, username)
    db.close()
    if not user:
        await ws.close(code=4001)
        return

    await ws.accept()
    user_id = str(user.id)
    user_connections.setdefault(user_id, []).append(ws)

    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        conns = user_connections.get(user_id, [])
        if ws in conns:
            conns.remove(ws)
