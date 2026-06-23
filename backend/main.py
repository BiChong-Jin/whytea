import asyncio
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.security import OAuth2PasswordBearer
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, field_validator
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from sqlalchemy.orm import Session

from backend.models_db import User

from .analyzer import ChatAnalyzer
from .auth import create_token, create_user, decode_token, get_user, verify_password
from .config import settings
from .database import get_db
from .models import WSMessage
from .youtube import YouTubeChatPoller, QuotaExceededError, extract_video_id, resolve_live_chat_id

# ── Logging ──────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger(__name__)

# ── Rate limiter ──────────────────────────────────────────────────────────────

limiter = Limiter(key_func=get_remote_address)

# ── WebSocket connection manager ─────────────────────────────────────────────


@dataclass
class UserSession:
    tasks: list[asyncio.Task]
    analyzer: ChatAnalyzer
    poller: "YouTubeChatPoller | None" = None
    language: str = "en"


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

# ── Background tasks ─────────────────────────────────────────────────────────


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
        except QuotaExceededError as exc:
            logger.warning("Quota exceeded for user %s: %s", user_id, exc)
            await broadcast_to_user(
                user_id, WSMessage(type="error", payload={"message": str(exc)})
            )
            # Stop polling — no point retrying until quota resets (midnight Pacific time)
            return
        except Exception as exc:
            logger.error("Polling error for user %s: %s", user_id, exc)
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
            session = sessions.get(user_id)
            language = session.language if session else "en"
            result = await analyzer.run_analysis(window_start, window_end, language)
            if result:
                await broadcast_to_user(
                    user_id,
                    WSMessage(type="analysis", payload=result.model_dump(mode="json")),
                )
        except Exception as exc:
            logger.error("Analysis error for user %s: %s", user_id, exc)
            await broadcast_to_user(
                user_id, WSMessage(type="error", payload={"message": str(exc)})
            )


# ── App lifecycle ─────────────────────────────────────────────────────────────


async def cleanup_loop() -> None:
    """Periodically remove dead WebSocket connections that never sent a clean disconnect."""
    while True:
        await asyncio.sleep(60)
        for user_id, conns in list(user_connections.items()):
            dead = [ws for ws in conns if ws.client_state.value != 1]  # 1 = CONNECTED
            for ws in dead:
                conns.remove(ws)
            if not conns:
                user_connections.pop(user_id, None)


@asynccontextmanager
async def lifespan(app: FastAPI):
    from .database import engine
    from .models_db import Base

    Base.metadata.create_all(bind=engine)
    cleanup_task = asyncio.create_task(cleanup_loop())
    logger.info("StreamPulse started — CORS origins: %s", settings.cors_origins)
    yield
    cleanup_task.cancel()
    for s in sessions.values():
        for t in s.tasks:
            t.cancel()
    sessions.clear()
    user_connections.clear()
    logger.info("StreamPulse shutdown complete")


app = FastAPI(title="Whytea Comments Analyzer", lifespan=lifespan)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

cors_origins = [o.strip() for o in settings.cors_origins.split(",")]
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Serve frontend
frontend_dir = Path(__file__).parent.parent / "frontend"
if frontend_dir.exists():
    app.mount("/static", StaticFiles(directory=str(frontend_dir)), name="static")


# ── HTTP endpoints ────────────────────────────────────────────────────────────


class StartRequest(BaseModel):
    video_url: str


class ReanalyzeRequest(BaseModel):
    language: str = "en"  # "en", "zh", "ja"


class RegisterRequest(BaseModel):
    user_name: str
    user_password: str

    @field_validator("user_name")
    @classmethod
    def validate_username(cls, v: str) -> str:
        v = v.strip()
        if len(v) < 3:
            raise ValueError("Username must be at least 3 characters")
        if len(v) > 32:
            raise ValueError("Username must be at most 32 characters")
        return v

    @field_validator("user_password")
    @classmethod
    def validate_password(cls, v: str) -> str:
        if len(v) < 8:
            raise ValueError("Password must be at least 8 characters")
        if len(v) > 72:
            raise ValueError("Password must be at most 72 characters")
        return v


class LoginRequest(BaseModel):
    user_name: str
    user_password: str


oauth2_scheme = OAuth2PasswordBearer(tokenUrl="login")


def get_current_user(
    token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)
):
    user_name = decode_token(token)

    if user_name is None:
        raise HTTPException(status_code=401, detail="invalid or expired token")

    user = get_user(db, user_name)

    if user is None:
        raise HTTPException(status_code=401, detail="user not found")

    return user


@app.get("/", response_class=HTMLResponse)
async def landing():
    html_path = Path(__file__).parent.parent / "frontend" / "landing.html"
    return HTMLResponse(html_path.read_text())


@app.get("/app", response_class=HTMLResponse)
async def index():
    html_path = Path(__file__).parent.parent / "frontend" / "index.html"
    return HTMLResponse(html_path.read_text())


@app.get("/login", response_class=HTMLResponse)
async def login_page():
    html_path = Path(__file__).parent.parent / "frontend" / "login.html"
    return HTMLResponse(html_path.read_text())


@app.get("/health")
async def health():
    return {"status": "ok", "timestamp": datetime.now(timezone.utc).isoformat()}


@app.post("/register")
@limiter.limit("5/minute")
def register(request: Request, body: RegisterRequest, db: Session = Depends(get_db)):
    new_user: User | None = create_user(
        db=db, username=body.user_name, password=body.user_password
    )
    if new_user is None:
        raise HTTPException(status_code=400, detail="username is already taken")
    logger.info("New user registered: %s", body.user_name)
    return {"message": "registered successfully"}


@app.post("/login")
@limiter.limit("10/minute")
def login(request: Request, body: LoginRequest, db: Session = Depends(get_db)):
    user: User | None = get_user(db=db, username=body.user_name)

    if user is None:
        raise HTTPException(status_code=401, detail="invalid credentials")

    is_correct_password = verify_password(body.user_password, user.hashed_password)
    if not is_correct_password:
        raise HTTPException(status_code=401, detail="invalid credentials")

    token: str = create_token(body.user_name)
    logger.info("User logged in: %s", body.user_name)
    return {"access_token": token, "token_type": "bearer"}


@app.post("/start")
async def start_monitoring(body: StartRequest, current_user=Depends(get_current_user)):
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
        await resolve_live_chat_id(video_id, settings.youtube_api_key)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    analyzer = ChatAnalyzer()
    poller = YouTubeChatPoller(video_id)
    session = UserSession(tasks=[], analyzer=analyzer, poller=poller)
    session.tasks.append(asyncio.create_task(polling_loop(poller, user_id, analyzer)))
    session.tasks.append(asyncio.create_task(analysis_loop(settings.analysis_interval_seconds, user_id, analyzer)))
    sessions[user_id] = session

    logger.info("User %s started monitoring video %s", current_user.user_name, video_id)
    return {"status": "monitoring", "video_id": video_id}


@app.post("/stop")
async def stop_monitoring(current_user=Depends(get_current_user)):
    user_id = str(current_user.id)
    current_session = sessions.pop(user_id, None)
    if current_session:
        for t in current_session.tasks:
            t.cancel()
        if current_session.poller:
            current_session.poller.terminate()
        await broadcast_to_user(
            user_id,
            WSMessage(type="status", payload={"state": "idle", "message": "Monitoring stopped."}),
        )
        logger.info("User %s stopped monitoring", current_user.user_name)
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


@app.post("/reanalyze")
async def reanalyze(body: ReanalyzeRequest, current_user=Depends(get_current_user)):
    if body.language not in ("en", "zh", "ja"):
        raise HTTPException(status_code=400, detail="language must be one of: en, zh, ja")

    user_id = str(current_user.id)
    session = sessions.get(user_id)
    if not session:
        raise HTTPException(status_code=400, detail="No active monitoring session.")

    session.language = body.language

    window_end = datetime.now(timezone.utc)
    window_start = datetime.fromtimestamp(
        window_end.timestamp() - 30, tz=timezone.utc
    )
    result = await session.analyzer.reanalyze(window_start, window_end, body.language)
    if not result:
        raise HTTPException(status_code=400, detail="No comments to analyze yet.")

    return result.model_dump(mode="json")


# ── WebSocket ─────────────────────────────────────────────────────────────────


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
    logger.info("WebSocket connected: user %s", username)

    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        conns = user_connections.get(user_id, [])
        if ws in conns:
            conns.remove(ws)
        # Remove the key entirely if no connections left
        if not conns:
            user_connections.pop(user_id, None)
        logger.info("WebSocket disconnected: user %s", username)
