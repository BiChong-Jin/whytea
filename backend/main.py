import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import httpx
from fastapi import Depends, FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy.orm import Session

from backend.models_db import User

from .analyzer import ChatAnalyzer
from .auth import create_token, create_user, get_user, verify_password
from .database import get_db
from .models import WSMessage
from .youtube import YouTubeChatPoller, extract_video_id, resolve_live_chat_id

# ── WebSocket connection manager ────────────────────────────────────────────


class ConnectionManager:
    def __init__(self) -> None:
        self.active: list[WebSocket] = []

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self.active.append(ws)

    def disconnect(self, ws: WebSocket) -> None:
        self.active.remove(ws)

    async def broadcast(self, msg: WSMessage) -> None:
        data = msg.model_dump_json()
        dead: list[WebSocket] = []
        for ws in self.active:
            try:
                await ws.send_text(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.active.remove(ws)


manager = ConnectionManager()
analyzer = ChatAnalyzer()

# Active background tasks
_tasks: list[asyncio.Task] = []


# ── Background tasks ────────────────────────────────────────────────────────


async def polling_loop(poller: YouTubeChatPoller) -> None:
    while True:
        try:
            comments, interval_ms = await poller.fetch_next_page()
            await analyzer.add_comments(comments)
            for c in comments:
                await manager.broadcast(
                    WSMessage(type="comment", payload=c.model_dump(mode="json"))
                )
        except Exception as exc:
            await manager.broadcast(
                WSMessage(type="error", payload={"message": str(exc)})
            )
        await asyncio.sleep(interval_ms / 1000)


async def analysis_loop(interval_seconds: int) -> None:
    from .config import settings

    while True:
        await asyncio.sleep(interval_seconds)
        window_end = datetime.now(timezone.utc)
        # approximate window start
        window_start = datetime.fromtimestamp(
            window_end.timestamp() - interval_seconds, tz=timezone.utc
        )
        try:
            result = await analyzer.run_analysis(window_start, window_end)
            if result:
                await manager.broadcast(
                    WSMessage(type="analysis", payload=result.model_dump(mode="json"))
                )
        except Exception as exc:
            await manager.broadcast(
                WSMessage(type="error", payload={"message": str(exc)})
            )


# ── App lifecycle ────────────────────────────────────────────────────────────


@asynccontextmanager
async def lifespan(app: FastAPI):
    from .database import engine
    from .models_db import Base

    Base.metadata.create_all(bind=engine)
    yield
    for t in _tasks:
        t.cancel()


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


@app.get("/", response_class=HTMLResponse)
async def index():
    html_path = Path(__file__).parent.parent / "frontend" / "index.html"
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
        raise HTTPException(status_code=404, detail="the username is taken.")
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
async def start_monitoring(body: StartRequest):
    from .config import settings

    global _tasks

    # Cancel existing tasks
    for t in _tasks:
        t.cancel()
    _tasks.clear()

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

    poller = YouTubeChatPoller(live_chat_id, settings.youtube_api_key)

    _tasks.append(asyncio.create_task(polling_loop(poller)))
    _tasks.append(
        asyncio.create_task(analysis_loop(settings.analysis_interval_seconds))
    )

    await manager.broadcast(
        WSMessage(
            type="status",
            payload={"state": "monitoring", "message": f"Monitoring video {video_id}"},
        )
    )

    return {"status": "monitoring", "video_id": video_id, "live_chat_id": live_chat_id}


@app.post("/stop")
async def stop_monitoring():
    for t in _tasks:
        t.cancel()
    _tasks.clear()
    await manager.broadcast(
        WSMessage(
            type="status", payload={"state": "idle", "message": "Monitoring stopped."}
        )
    )
    return {"status": "stopped"}


@app.get("/status")
async def get_status():
    return {
        "active_tasks": len(_tasks),
        "connected_clients": len(manager.active),
        "monitoring": len(_tasks) > 0,
    }


# ── WebSocket ────────────────────────────────────────────────────────────────


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await manager.connect(ws)
    try:
        while True:
            await ws.receive_text()  # keep connection alive; we only push from server
    except WebSocketDisconnect:
        manager.disconnect(ws)
