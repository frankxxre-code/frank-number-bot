# -*- coding: utf-8 -*-
"""
Frank Number Bot — Complete Backend (single file)
===================================================
FastAPI + PostgreSQL + WebSocket
Deploy on Railway. Set these environment variables:
  DATABASE_URL       — auto-set by Railway Postgres plugin
  SHARED_SECRET      — same secret as your monitor bot
  MONITOR_BOT_URL    — your monitor bot Railway URL
  PLATFORM_URL       — http://198.135.52.238
  PLATFORM_USERNAME  — Frankhustle
  PLATFORM_PASSWORD  — f11111
  FRONTEND_URL       — your frontend domain (for CORS)
"""

import asyncio, hashlib, io, json, logging, os, re, secrets, time
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional

import aiohttp
import uvicorn
from fastapi import (FastAPI, WebSocket, WebSocketDisconnect,
                     APIRouter, Depends, HTTPException, Cookie,
                     UploadFile, File)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import (Column, Integer, String, Boolean, DateTime,
                        Text, ForeignKey, BigInteger, UniqueConstraint,
                        create_engine)
from sqlalchemy.orm import declarative_base, relationship, sessionmaker, Session

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("frank")

# ══════════════════════════════════════════════════════════════════════════════
#  CONFIG
# ══════════════════════════════════════════════════════════════════════════════

DATABASE_URL      = os.environ.get("DATABASE_URL", "")
SHARED_SECRET     = os.environ.get("SHARED_SECRET",     "MonitorSecret2024")
MONITOR_BOT_URL   = os.environ.get("MONITOR_BOT_URL",   "").rstrip("/")
PLATFORM_URL      = os.environ.get("PLATFORM_URL",      "http://198.135.52.238")
PLATFORM_USER     = os.environ.get("PLATFORM_USERNAME", "Frankhustle")
PLATFORM_PASS     = os.environ.get("PLATFORM_PASSWORD", "f11111")
FRONTEND_URL      = os.environ.get("FRONTEND_URL",      "*")

if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

# ══════════════════════════════════════════════════════════════════════════════
#  DATABASE MODELS
# ══════════════════════════════════════════════════════════════════════════════

Base = declarative_base()

def utcnow():
    return datetime.now(timezone.utc)

class User(Base):
    __tablename__ = "users"
    id            = Column(Integer, primary_key=True, index=True)
    username      = Column(String(64),  unique=True, nullable=False, index=True)
    email         = Column(String(128), unique=True, nullable=False, index=True)
    password_hash = Column(String(256), nullable=False)
    is_admin      = Column(Boolean, default=False)
    is_approved   = Column(Boolean, default=False)
    is_blocked    = Column(Boolean, default=False)
    created_at    = Column(DateTime(timezone=True), default=utcnow)
    sessions      = relationship("UserSession",  back_populates="user", cascade="all, delete")
    assignments   = relationship("Assignment",   back_populates="user")
    saved_numbers = relationship("SavedNumber",  back_populates="user", cascade="all, delete")
    reviews       = relationship("NumberReview", back_populates="user", cascade="all, delete")

class Pool(Base):
    __tablename__ = "pools"
    id             = Column(Integer, primary_key=True, index=True)
    name           = Column(String(128), unique=True, nullable=False)
    country_code   = Column(String(10),  nullable=False)
    otp_group_id   = Column(BigInteger,  nullable=True)
    otp_link       = Column(String(256), default="")
    match_format   = Column(String(32),  default="5+4")
    telegram_match_format = Column(String(32), default="")
    uses_platform  = Column(Integer, default=0)
    is_paused      = Column(Boolean, default=False)
    pause_reason   = Column(Text,    default="")
    trick_text     = Column(Text,    default="")
    is_admin_only  = Column(Boolean, default=False)
    last_restocked = Column(DateTime(timezone=True), nullable=True)
    created_at     = Column(DateTime(timezone=True), default=utcnow)
    numbers        = relationship("ActiveNumber", back_populates="pool", cascade="all, delete")
    assignments    = relationship("Assignment",   back_populates="pool")
    access_list    = relationship("PoolAccess",   back_populates="pool", cascade="all, delete")

class PoolAccess(Base):
    __tablename__ = "pool_access"
    __table_args__ = (UniqueConstraint("pool_id", "user_id"),)
    id         = Column(Integer, primary_key=True, index=True)
    pool_id    = Column(Integer, ForeignKey("pools.id"),  nullable=False)
    user_id    = Column(Integer, ForeignKey("users.id"),  nullable=False)
    granted_at = Column(DateTime(timezone=True), default=utcnow)
    pool       = relationship("Pool", back_populates="access_list")
    user       = relationship("User")

class ActiveNumber(Base):
    __tablename__ = "active_numbers"
    id         = Column(Integer, primary_key=True, index=True)
    pool_id    = Column(Integer, ForeignKey("pools.id"), nullable=False)
    number     = Column(String(32), unique=True, nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), default=utcnow)
    pool       = relationship("Pool", back_populates="numbers")

class BadNumber(Base):
    __tablename__ = "bad_numbers"
    id         = Column(Integer, primary_key=True, index=True)
    number     = Column(String(32), unique=True, nullable=False, index=True)
    reason     = Column(String(256), default="not available")
    flagged_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime(timezone=True), default=utcnow)

class Assignment(Base):
    __tablename__ = "assignments"
    id          = Column(Integer, primary_key=True, index=True)
    user_id     = Column(Integer, ForeignKey("users.id"), nullable=False)
    pool_id     = Column(Integer, ForeignKey("pools.id"), nullable=False)
    number      = Column(String(32), nullable=False, index=True)
    assigned_at = Column(DateTime(timezone=True), default=utcnow)
    released_at = Column(DateTime(timezone=True), nullable=True)
    feedback    = Column(String(64), default="")
    user        = relationship("User", back_populates="assignments")
    pool        = relationship("Pool", back_populates="assignments")
    otps        = relationship("OTPLog", back_populates="assignment", cascade="all, delete")

class OTPLog(Base):
    __tablename__ = "otp_logs"
    id            = Column(Integer, primary_key=True, index=True)
    assignment_id = Column(Integer, ForeignKey("assignments.id"), nullable=True)
    user_id       = Column(Integer, ForeignKey("users.id"), nullable=False)
    number        = Column(String(32), nullable=False)
    otp_code      = Column(String(32), nullable=False)
    raw_message   = Column(Text, default="")
    delivered_at  = Column(DateTime(timezone=True), default=utcnow)
    assignment    = relationship("Assignment", back_populates="otps")

class SavedNumber(Base):
    __tablename__ = "saved_numbers"
    id         = Column(Integer, primary_key=True, index=True)
    user_id    = Column(Integer, ForeignKey("users.id"), nullable=False)
    number     = Column(String(32), nullable=False)
    country    = Column(String(64),  default="")
    pool_name  = Column(String(128), nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    moved      = Column(Boolean, default=False)
    created_at = Column(DateTime(timezone=True), default=utcnow)
    user       = relationship("User", back_populates="saved_numbers")

class NumberReview(Base):
    __tablename__ = "number_reviews"
    id         = Column(Integer, primary_key=True, index=True)
    user_id    = Column(Integer, ForeignKey("users.id"), nullable=False)
    number     = Column(String(32), nullable=False)
    rating     = Column(Integer, default=5)
    comment    = Column(Text, default="")
    created_at = Column(DateTime(timezone=True), default=utcnow)
    user       = relationship("User", back_populates="reviews")

class UserSession(Base):
    __tablename__ = "user_sessions"
    id         = Column(Integer, primary_key=True, index=True)
    user_id    = Column(Integer, ForeignKey("users.id"), nullable=False)
    token      = Column(String(256), unique=True, nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), default=utcnow)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    user       = relationship("User", back_populates="sessions")

class Broadcast(Base):
    __tablename__ = "broadcasts"
    id         = Column(Integer, primary_key=True, index=True)
    message    = Column(Text, nullable=False)
    sent_by    = Column(Integer, ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime(timezone=True), default=utcnow)

# ══════════════════════════════════════════════════════════════════════════════
#  DATABASE CONNECTION
# ══════════════════════════════════════════════════════════════════════════════

engine       = create_engine(DATABASE_URL, pool_pre_ping=True, pool_size=10, max_overflow=20)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

def create_tables():
    Base.metadata.create_all(bind=engine)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# ══════════════════════════════════════════════════════════════════════════════
#  AUTH HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def hash_password(p: str) -> str:
    salt = os.urandom(16).hex()
    h    = hashlib.sha256((salt + p).encode()).hexdigest()
    return f"{salt}:{h}"

def verify_password(p: str, hashed: str) -> bool:
    try:
        salt, h = hashed.split(":", 1)
        return hashlib.sha256((salt + p).encode()).hexdigest() == h
    except Exception:
        return False

def create_token(db: Session, user_id: int) -> str:
    token   = secrets.token_urlsafe(48)
    expires = datetime.now(timezone.utc) + timedelta(days=30)
    db.add(UserSession(user_id=user_id, token=token, expires_at=expires))
    db.commit()
    return token

def get_user_from_token(db: Session, token: str):
    if not token:
        return None
    now  = datetime.now(timezone.utc)
    sess = db.query(UserSession).filter(
        UserSession.token == token,
        UserSession.expires_at > now
    ).first()
    if not sess:
        return None
    user = db.query(User).filter(User.id == sess.user_id).first()
    if not user or user.is_blocked:
        return None
    return user

def revoke_token(db: Session, token: str):
    db.query(UserSession).filter(UserSession.token == token).delete()
    db.commit()

# ══════════════════════════════════════════════════════════════════════════════
#  WEBSOCKET MANAGER
# ══════════════════════════════════════════════════════════════════════════════

class ConnectionManager:
    def __init__(self):
        self.user_connections: Dict[int, List[WebSocket]] = {}
        self.feed_connections: List[WebSocket] = []

    async def connect_user(self, ws: WebSocket, user_id: int):
        await ws.accept()
        self.user_connections.setdefault(user_id, []).append(ws)

    async def connect_feed(self, ws: WebSocket):
        await ws.accept()
        self.feed_connections.append(ws)

    def disconnect_user(self, ws: WebSocket, user_id: int):
        conns = self.user_connections.get(user_id, [])
        self.user_connections[user_id] = [c for c in conns if c != ws]
        if not self.user_connections[user_id]:
            del self.user_connections[user_id]

    def disconnect_feed(self, ws: WebSocket):
        self.feed_connections = [c for c in self.feed_connections if c != ws]

    async def send_to_user(self, user_id: int, data: dict):
        dead = []
        for ws in self.user_connections.get(user_id, []):
            try:
                await ws.send_text(json.dumps(data))
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect_user(ws, user_id)

    async def broadcast_feed(self, data: dict):
        dead = []
        for ws in self.feed_connections:
            try:
                await ws.send_text(json.dumps(data))
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect_feed(ws)

    async def broadcast_all(self, data: dict):
        all_ws = list(self.feed_connections)
        for conns in self.user_connections.values():
            all_ws.extend(conns)
        for ws in all_ws:
            try:
                await ws.send_text(json.dumps(data))
            except Exception:
                pass

    def total(self) -> int:
        return sum(len(v) for v in self.user_connections.values())

manager = ConnectionManager()

# ══════════════════════════════════════════════════════════════════════════════
#  DEPENDENCY HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def req_user(token: str = Cookie(default=None), db: Session = Depends(get_db)):
    u = get_user_from_token(db, token)
    if not u:              raise HTTPException(401, "Not authenticated")
    if not u.is_approved:  raise HTTPException(403, "Account pending approval")
    return u

def req_admin(token: str = Cookie(default=None), db: Session = Depends(get_db)):
    u = get_user_from_token(db, token)
    if not u or not u.is_admin: raise HTTPException(403, "Admin only")
    return u

def has_pool_access(db, pool_id: int, user_id: int, is_admin: bool) -> bool:
    if is_admin: return True
    count = db.query(PoolAccess).filter(PoolAccess.pool_id == pool_id).count()
    if count == 0: return True
    return db.query(PoolAccess).filter(
        PoolAccess.pool_id == pool_id, PoolAccess.user_id == user_id
    ).first() is not None

# ══════════════════════════════════════════════════════════════════════════════
#  PLATFORM POLLING (uses_platform = 1 or 2)
# ══════════════════════════════════════════════════════════════════════════════

_platform_token       = None
_platform_token_lock  = asyncio.Lock()
_active_platform_tasks: dict = {}

async def _get_platform_token() -> Optional[str]:
    global _platform_token
    if _platform_token: return _platform_token
    async with _platform_token_lock:
        try:
            async with aiohttp.ClientSession() as s:
                async with s.post(
                    f"{PLATFORM_URL}/api/login",
                    json={"username": PLATFORM_USER, "password": PLATFORM_PASS},
                    timeout=aiohttp.ClientTimeout(total=10)
                ) as r:
                    if r.status == 200:
                        d = await r.json()
                        _platform_token = d.get("token") or d.get("access_token")
                        return _platform_token
        except Exception as e:
            log.error(f"[Platform] Login failed: {e}")
    return None

def _build_mask(number: str, fmt: str):
    try:
        parts = fmt.strip().split("+")
        head, tail = int(parts[0]), int(parts[1])
        clean = re.sub(r'\D', '', number)
        if len(clean) < head + tail: return None
        return clean[:head], clean[-tail:]
    except Exception:
        return None

def _matches(sms_phone: str, prefix: str, suffix: str) -> bool:
    clean = re.sub(r'\D', '', sms_phone)
    if clean.startswith(prefix) and clean.endswith(suffix): return True
    pattern = re.escape(prefix) + r'[\d\s★•\*xX\-\.]{0,8}' + re.escape(suffix)
    if re.search(pattern, re.sub(r'\s', '', sms_phone), re.IGNORECASE): return True
    idx = clean.find(prefix)
    if idx != -1 and clean[idx + len(prefix):].endswith(suffix): return True
    return False

async def _platform_poll(user_id: int, number: str, match_format: str):
    global _platform_token
    mask = _build_mask(number, match_format)
    if not mask: return
    prefix, suffix = mask
    seen     = set()
    deadline = time.time() + 600
    while time.time() < deadline:
        try:
            token = await _get_platform_token()
            if not token: await asyncio.sleep(5); continue
            async with aiohttp.ClientSession() as s:
                async with s.get(
                    f"{PLATFORM_URL}/api/sms?limit=50",
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=aiohttp.ClientTimeout(total=10)
                ) as resp:
                    if resp.status == 401:
                        _platform_token = None; await asyncio.sleep(5); continue
                    if resp.status != 200: await asyncio.sleep(5); continue
                    data = await resp.json()
                    messages = data if isinstance(data, list) else data.get("messages", [])
                    for msg in messages:
                        mid = msg.get("id")
                        if mid in seen: continue
                        seen.add(mid)
                        if not _matches(str(msg.get("phone_number", "")), prefix, suffix): continue
                        otp = msg.get("otp") or "N/A"
                        raw = msg.get("message", "")
                        db  = next(get_db())
                        a   = db.query(Assignment).filter(
                            Assignment.user_id == user_id,
                            Assignment.number  == number,
                            Assignment.released_at == None
                        ).first()
                        entry = OTPLog(
                            assignment_id=a.id if a else None,
                            user_id=user_id, number=number,
                            otp_code=otp, raw_message=raw
                        )
                        db.add(entry); db.commit(); db.refresh(entry)
                        db.close()
                        payload = {"type": "otp", "number": number, "otp": otp,
                                   "raw_message": raw,
                                   "delivered_at": entry.delivered_at.isoformat()}
                        await manager.send_to_user(user_id, payload)
                        await manager.broadcast_feed(
                            {"type": "feed_otp", "number": number,
                             "otp": otp, "delivered_at": entry.delivered_at.isoformat()}
                        )
                        log.info(f"[Platform] OTP {otp} → user {user_id}")
                        return
        except asyncio.CancelledError:
            return
        except Exception as e:
            log.error(f"[Platform] Poll error: {e}")
        await asyncio.sleep(5)

def start_platform_monitor(user_id: int, number: str, match_format: str):
    existing = _active_platform_tasks.get(user_id)
    if existing and not existing.done(): existing.cancel()
    _active_platform_tasks[user_id] = asyncio.create_task(
        _platform_poll(user_id, number, match_format)
    )

# ══════════════════════════════════════════════════════════════════════════════
#  SAVED NUMBERS — EXPIRY PROCESSOR (runs every 60s)
# ══════════════════════════════════════════════════════════════════════════════

async def process_expired_saved(db: Session):
    now     = datetime.now(timezone.utc)
    expired = db.query(SavedNumber).filter(
        SavedNumber.expires_at <= now,
        SavedNumber.moved == False
    ).all()
    for sn in expired:
        pool = db.query(Pool).filter(Pool.name == sn.pool_name).first()
        if not pool:
            pool = Pool(name=sn.pool_name, country_code=sn.country or "unknown")
            db.add(pool); db.flush()
        bad = db.query(BadNumber).filter(BadNumber.number == sn.number).first()
        if not bad:
            exists = db.query(ActiveNumber).filter(ActiveNumber.number == sn.number).first()
            if not exists:
                db.add(ActiveNumber(pool_id=pool.id, number=sn.number))
        sn.moved = True
    if expired:
        db.commit()
        log.info(f"[Scheduler] Moved {len(expired)} expired saved numbers to pools")

async def scheduler():
    while True:
        await asyncio.sleep(60)
        try:
            db = next(get_db())
            await process_expired_saved(db)
            db.close()
        except Exception as e:
            log.error(f"[Scheduler] {e}")

# ══════════════════════════════════════════════════════════════════════════════
#  ROUTERS
# ══════════════════════════════════════════════════════════════════════════════

auth_router    = APIRouter(prefix="/api/auth",   tags=["auth"])
pools_router   = APIRouter(prefix="/api/pools",  tags=["pools"])
otp_router     = APIRouter(prefix="/api/otp",    tags=["otp"])
admin_router   = APIRouter(prefix="/api/admin",  tags=["admin"])
saved_router   = APIRouter(prefix="/api/saved",  tags=["saved"])
reviews_router = APIRouter(prefix="/api/reviews",tags=["reviews"])

# ─── AUTH ─────────────────────────────────────────────────────────────────────

class RegisterReq(BaseModel):
    username: str; email: str; password: str

class LoginReq(BaseModel):
    username: str; password: str

@auth_router.post("/register")
def register(req: RegisterReq, db: Session = Depends(get_db)):
    if len(req.username) < 3:  raise HTTPException(400, "Username min 3 chars")
    if len(req.password) < 6:  raise HTTPException(400, "Password min 6 chars")
    if db.query(User).filter(User.username == req.username).first():
        raise HTTPException(400, "Username taken")
    if db.query(User).filter(User.email == req.email).first():
        raise HTTPException(400, "Email already registered")
    first = db.query(User).count() == 0
    user  = User(username=req.username, email=req.email,
                 password_hash=hash_password(req.password),
                 is_admin=first, is_approved=first)
    db.add(user); db.commit(); db.refresh(user)
    if first:
        token = create_token(db, user.id)
        resp  = JSONResponse({"ok": True, "approved": True, "is_admin": True})
        resp.set_cookie("token", token, httponly=True, samesite="lax", max_age=86400*30)
        return resp
    return {"ok": True, "approved": False, "message": "Awaiting admin approval"}

@auth_router.post("/login")
def login(req: LoginReq, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.username == req.username).first()
    if not user or not verify_password(req.password, user.password_hash):
        raise HTTPException(401, "Invalid username or password")
    if user.is_blocked:   raise HTTPException(403, "Account blocked. Contact admin.")
    if not user.is_approved: raise HTTPException(403, "Account pending admin approval.")
    token = create_token(db, user.id)
    resp  = JSONResponse({"ok": True, "user": {"id": user.id,
                          "username": user.username, "is_admin": user.is_admin}})
    resp.set_cookie("token", token, httponly=True, samesite="lax", max_age=86400*30)
    return resp

@auth_router.post("/logout")
def logout(token: str = Cookie(default=None), db: Session = Depends(get_db)):
    if token: revoke_token(db, token)
    resp = JSONResponse({"ok": True})
    resp.delete_cookie("token")
    return resp

@auth_router.get("/me")
def me(token: str = Cookie(default=None), db: Session = Depends(get_db)):
    user = get_user_from_token(db, token)
    if not user: raise HTTPException(401, "Not authenticated")
    return {"id": user.id, "username": user.username, "email": user.email,
            "is_admin": user.is_admin, "is_approved": user.is_approved}

# ─── POOLS ────────────────────────────────────────────────────────────────────

class PoolCreate(BaseModel):
    name: str; country_code: str
    otp_group_id: Optional[int] = None
    otp_link: Optional[str] = ""
    match_format: Optional[str] = "5+4"
    telegram_match_format: Optional[str] = ""
    uses_platform: Optional[int] = 0
    trick_text: Optional[str] = ""
    is_admin_only: Optional[bool] = False

class PoolUpdate(BaseModel):
    name: Optional[str] = None; country_code: Optional[str] = None
    otp_group_id: Optional[int] = None; otp_link: Optional[str] = None
    match_format: Optional[str] = None; telegram_match_format: Optional[str] = None
    uses_platform: Optional[int] = None; trick_text: Optional[str] = None
    is_paused: Optional[bool] = None; pause_reason: Optional[str] = None
    is_admin_only: Optional[bool] = None

class AssignReq(BaseModel):
    pool_id: int
    prefix: Optional[str] = None

def _assign_resp(a, pool):
    return {"ok": True, "assignment_id": a.id, "number": a.number,
            "pool_id": pool.id, "pool_name": pool.name,
            "country_code": pool.country_code,
            "otp_group_id": pool.otp_group_id, "otp_link": pool.otp_link,
            "match_format": pool.match_format,
            "telegram_match_format": pool.telegram_match_format or pool.match_format,
            "uses_platform": pool.uses_platform, "trick_text": pool.trick_text}

@pools_router.get("")
def list_pools(user=Depends(req_user), db: Session = Depends(get_db)):
    result = []
    for p in db.query(Pool).all():
        if not user.is_admin and p.is_admin_only: continue
        if not has_pool_access(db, p.id, user.id, user.is_admin): continue
        count      = db.query(ActiveNumber).filter(ActiveNumber.pool_id == p.id).count()
        restricted = db.query(PoolAccess).filter(PoolAccess.pool_id == p.id).count() > 0
        result.append({"id": p.id, "name": p.name, "country_code": p.country_code,
                        "otp_link": p.otp_link, "match_format": p.match_format,
                        "uses_platform": p.uses_platform, "is_paused": p.is_paused,
                        "pause_reason": p.pause_reason, "trick_text": p.trick_text,
                        "is_admin_only": p.is_admin_only, "number_count": count,
                        "restricted": restricted,
                        "last_restocked": p.last_restocked.isoformat() if p.last_restocked else None,
                        "telegram_match_format": p.telegram_match_format or ""})
    return result

@pools_router.post("")
def create_pool(req: PoolCreate, user=Depends(req_admin), db: Session = Depends(get_db)):
    if db.query(Pool).filter(Pool.name == req.name).first():
        raise HTTPException(400, "Pool name already exists")
    pool = Pool(**req.dict()); db.add(pool); db.commit(); db.refresh(pool)
    return {"ok": True, "id": pool.id}

@pools_router.put("/{pool_id}")
def update_pool(pool_id: int, req: PoolUpdate, user=Depends(req_admin), db: Session = Depends(get_db)):
    pool = db.query(Pool).filter(Pool.id == pool_id).first()
    if not pool: raise HTTPException(404, "Pool not found")
    for k, v in req.dict(exclude_none=True).items():
        setattr(pool, k, v)
    db.commit()
    return {"ok": True}

@pools_router.delete("/{pool_id}")
def delete_pool(pool_id: int, user=Depends(req_admin), db: Session = Depends(get_db)):
    pool = db.query(Pool).filter(Pool.id == pool_id).first()
    if not pool: raise HTTPException(404, "Pool not found")
    db.delete(pool); db.commit()
    return {"ok": True}

@pools_router.post("/{pool_id}/upload")
async def upload_numbers(pool_id: int, file: UploadFile = File(...),
                          user=Depends(req_admin), db: Session = Depends(get_db)):
    pool = db.query(Pool).filter(Pool.id == pool_id).first()
    if not pool: raise HTTPException(404, "Pool not found")
    content  = await file.read()
    lines    = content.decode("utf-8", errors="ignore").splitlines()
    bad_set  = {r.number for r in db.query(BadNumber).all()}
    saved_set= {r.number for r in db.query(SavedNumber).filter(SavedNumber.moved == False).all()}
    existing = {r.number for r in db.query(ActiveNumber).all()}
    added = skipped_bad = skipped_saved = skipped_dup = 0
    PHONE = re.compile(r'(\+?\d{6,15})')
    for line in lines:
        m = PHONE.search(line.strip())
        if not m: continue
        number = m.group(1)
        if number in bad_set:    skipped_bad   += 1; continue
        if number in saved_set:  skipped_saved  += 1; continue
        if number in existing:   skipped_dup    += 1; continue
        db.add(ActiveNumber(pool_id=pool_id, number=number))
        existing.add(number); added += 1
    pool.last_restocked = datetime.now(timezone.utc)
    db.commit()
    return {"ok": True, "added": added, "skipped_bad": skipped_bad,
            "skipped_saved": skipped_saved, "skipped_duplicate": skipped_dup}

@pools_router.get("/{pool_id}/export")
def export_pool(pool_id: int, user=Depends(req_admin), db: Session = Depends(get_db)):
    numbers = db.query(ActiveNumber).filter(ActiveNumber.pool_id == pool_id).all()
    return Response(content="\n".join(n.number for n in numbers),
                    media_type="text/plain",
                    headers={"Content-Disposition": f"attachment; filename=pool_{pool_id}.txt"})

@pools_router.post("/{pool_id}/cut")
def cut_pool(pool_id: int, count: int, user=Depends(req_admin), db: Session = Depends(get_db)):
    rows = db.query(ActiveNumber).filter(ActiveNumber.pool_id == pool_id).limit(count).all()
    for r in rows: db.delete(r)
    db.commit()
    return {"ok": True, "removed": len(rows)}

@pools_router.get("/{pool_id}/access")
def get_access(pool_id: int, user=Depends(req_admin), db: Session = Depends(get_db)):
    rows = db.query(PoolAccess).filter(PoolAccess.pool_id == pool_id).all()
    return [{"user_id": r.user_id,
             "username": db.query(User).filter(User.id == r.user_id).first().username,
             "granted_at": r.granted_at.isoformat()} for r in rows]

@pools_router.post("/{pool_id}/access/{user_id}")
def grant_access(pool_id: int, user_id: int, user=Depends(req_admin), db: Session = Depends(get_db)):
    if not db.query(PoolAccess).filter(PoolAccess.pool_id == pool_id, PoolAccess.user_id == user_id).first():
        db.add(PoolAccess(pool_id=pool_id, user_id=user_id)); db.commit()
    return {"ok": True}

@pools_router.delete("/{pool_id}/access/{user_id}")
def revoke_access(pool_id: int, user_id: int, user=Depends(req_admin), db: Session = Depends(get_db)):
    db.query(PoolAccess).filter(PoolAccess.pool_id == pool_id, PoolAccess.user_id == user_id).delete()
    db.commit()
    return {"ok": True}

@pools_router.post("/assign")
def assign_number(req: AssignReq, user=Depends(req_user), db: Session = Depends(get_db)):
    pool = db.query(Pool).filter(Pool.id == req.pool_id).first()
    if not pool: raise HTTPException(404, "Pool not found")
    if pool.is_paused: raise HTTPException(400, f"Pool paused: {pool.pause_reason or 'No reason'}")
    if pool.is_admin_only and not user.is_admin: raise HTTPException(403, "Admin only pool")
    if not has_pool_access(db, pool.id, user.id, user.is_admin):
        raise HTTPException(403, "No access to this pool")
    existing = db.query(Assignment).filter(
        Assignment.user_id == user.id, Assignment.pool_id == req.pool_id,
        Assignment.released_at == None
    ).first()
    if existing:
        return _assign_resp(existing, pool)
    q = db.query(ActiveNumber).filter(ActiveNumber.pool_id == req.pool_id)
    if req.prefix:
        clean = re.sub(r'\D', '', req.prefix)
        q = q.filter(ActiveNumber.number.like(f"%{clean}%"))
    number_row = q.first()
    if not number_row:
        raise HTTPException(400, "No numbers available" + (f" with prefix {req.prefix}" if req.prefix else ""))
    db.delete(number_row)
    a = Assignment(user_id=user.id, pool_id=pool.id, number=number_row.number)
    db.add(a); db.commit(); db.refresh(a)
    return _assign_resp(a, pool)

@pools_router.post("/release/{assignment_id}")
def release_number(assignment_id: int, user=Depends(req_user), db: Session = Depends(get_db)):
    a = db.query(Assignment).filter(
        Assignment.id == assignment_id, Assignment.user_id == user.id
    ).first()
    if not a: raise HTTPException(404, "Assignment not found")
    a.released_at = datetime.now(timezone.utc); db.commit()
    return {"ok": True}

@pools_router.get("/my-assignment")
def my_assignment(user=Depends(req_user), db: Session = Depends(get_db)):
    a = db.query(Assignment).filter(
        Assignment.user_id == user.id, Assignment.released_at == None
    ).order_by(Assignment.assigned_at.desc()).first()
    if not a: return {"assignment": None}
    pool = db.query(Pool).filter(Pool.id == a.pool_id).first()
    return {"assignment": _assign_resp(a, pool) if pool else None}

# ─── OTP ──────────────────────────────────────────────────────────────────────

class MonitorResult(BaseModel):
    number: str; otp: str
    raw_message: Optional[str] = ""
    user_id: int; secret: Optional[str] = ""

class MonitorReq(BaseModel):
    number: str; group_id: int; match_format: str; user_id: int

@otp_router.post("/monitor-result")
async def monitor_result(req: MonitorResult, db: Session = Depends(get_db)):
    if SHARED_SECRET and req.secret != SHARED_SECRET:
        raise HTTPException(403, "Invalid secret")
    a = db.query(Assignment).filter(
        Assignment.user_id == req.user_id, Assignment.number == req.number,
        Assignment.released_at == None
    ).first()
    entry = OTPLog(assignment_id=a.id if a else None, user_id=req.user_id,
                   number=req.number, otp_code=req.otp, raw_message=req.raw_message or "")
    db.add(entry); db.commit(); db.refresh(entry)
    payload = {"type": "otp", "number": req.number, "otp": req.otp,
               "raw_message": req.raw_message,
               "delivered_at": entry.delivered_at.isoformat()}
    await manager.send_to_user(req.user_id, payload)
    await manager.broadcast_feed({"type": "feed_otp", "number": req.number,
                                   "otp": req.otp,
                                   "delivered_at": entry.delivered_at.isoformat()})
    log.info(f"[OTP] {req.otp} → user {req.user_id} for {req.number}")
    return {"ok": True}

@otp_router.post("/request-monitor")
async def request_monitor(req: MonitorReq, user=Depends(req_user)):
    if not MONITOR_BOT_URL: raise HTTPException(503, "Monitor bot URL not configured")
    payload = {"number": req.number, "group_id": req.group_id,
               "match_format": req.match_format, "user_id": req.user_id,
               "secret": SHARED_SECRET}
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(f"{MONITOR_BOT_URL}/monitor-request", json=payload,
                              timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    raise HTTPException(502, "Monitor bot error")
    except aiohttp.ClientError as e:
        raise HTTPException(502, f"Monitor bot unreachable: {e}")
    return {"ok": True}

@otp_router.post("/search-otp")
async def search_otp(req: MonitorReq, user=Depends(req_user)):
    if not MONITOR_BOT_URL: raise HTTPException(503, "Monitor bot URL not configured")
    payload = {"number": req.number, "group_id": req.group_id,
               "match_format": req.match_format, "user_id": req.user_id,
               "secret": SHARED_SECRET}
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(f"{MONITOR_BOT_URL}/search-otp-request", json=payload,
                              timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    raise HTTPException(502, "Monitor bot error")
    except aiohttp.ClientError as e:
        raise HTTPException(502, f"Monitor bot unreachable: {e}")
    return {"ok": True}

@otp_router.get("/logs")
def otp_logs(limit: int = 100, user=Depends(req_admin), db: Session = Depends(get_db)):
    logs = db.query(OTPLog).order_by(OTPLog.delivered_at.desc()).limit(limit).all()
    return [{"id": l.id, "user_id": l.user_id, "number": l.number,
             "otp_code": l.otp_code, "raw_message": l.raw_message,
             "delivered_at": l.delivered_at.isoformat()} for l in logs]

@otp_router.get("/my")
def my_otps(user=Depends(req_user), db: Session = Depends(get_db)):
    logs = db.query(OTPLog).filter(OTPLog.user_id == user.id).order_by(
        OTPLog.delivered_at.desc()).limit(50).all()
    return [{"number": l.number, "otp_code": l.otp_code,
             "delivered_at": l.delivered_at.isoformat()} for l in logs]

# ─── ADMIN ────────────────────────────────────────────────────────────────────

@admin_router.get("/users")
def list_users(user=Depends(req_admin), db: Session = Depends(get_db)):
    return [{"id": u.id, "username": u.username, "email": u.email,
             "is_admin": u.is_admin, "is_approved": u.is_approved,
             "is_blocked": u.is_blocked, "created_at": u.created_at.isoformat()}
            for u in db.query(User).order_by(User.created_at.desc()).all()]

@admin_router.post("/users/{uid}/approve")
def approve_user(uid: int, user=Depends(req_admin), db: Session = Depends(get_db)):
    u = db.query(User).filter(User.id == uid).first()
    if not u: raise HTTPException(404, "User not found")
    u.is_approved = True; u.is_blocked = False; db.commit()
    return {"ok": True}

@admin_router.post("/users/{uid}/reject")
def reject_user(uid: int, user=Depends(req_admin), db: Session = Depends(get_db)):
    u = db.query(User).filter(User.id == uid).first()
    if not u: raise HTTPException(404, "User not found")
    u.is_approved = False; db.commit()
    return {"ok": True}

@admin_router.post("/users/{uid}/block")
def block_user(uid: int, user=Depends(req_admin), db: Session = Depends(get_db)):
    u = db.query(User).filter(User.id == uid).first()
    if not u: raise HTTPException(404, "User not found")
    u.is_blocked = True; u.is_approved = False; db.commit()
    return {"ok": True}

@admin_router.post("/users/{uid}/unblock")
def unblock_user(uid: int, user=Depends(req_admin), db: Session = Depends(get_db)):
    u = db.query(User).filter(User.id == uid).first()
    if not u: raise HTTPException(404, "User not found")
    u.is_blocked = False; u.is_approved = True; db.commit()
    return {"ok": True}

@admin_router.post("/users/{uid}/make-admin")
def make_admin(uid: int, user=Depends(req_admin), db: Session = Depends(get_db)):
    u = db.query(User).filter(User.id == uid).first()
    if not u: raise HTTPException(404, "User not found")
    u.is_admin = True; db.commit()
    return {"ok": True}

@admin_router.get("/bad-numbers")
def bad_numbers(user=Depends(req_admin), db: Session = Depends(get_db)):
    return [{"id": b.id, "number": b.number, "reason": b.reason,
             "created_at": b.created_at.isoformat()}
            for b in db.query(BadNumber).order_by(BadNumber.created_at.desc()).all()]

class BadNumReq(BaseModel):
    number: str; reason: Optional[str] = "manually added"

@admin_router.post("/bad-numbers")
def add_bad(req: BadNumReq, user=Depends(req_admin), db: Session = Depends(get_db)):
    if db.query(BadNumber).filter(BadNumber.number == req.number).first():
        return {"ok": True, "message": "Already in bad list"}
    db.add(BadNumber(number=req.number, reason=req.reason, flagged_by=user.id))
    db.query(ActiveNumber).filter(ActiveNumber.number == req.number).delete()
    db.commit()
    return {"ok": True}

@admin_router.delete("/bad-numbers/{bid}")
def remove_bad(bid: int, user=Depends(req_admin), db: Session = Depends(get_db)):
    b = db.query(BadNumber).filter(BadNumber.id == bid).first()
    if not b: raise HTTPException(404, "Not found")
    db.delete(b); db.commit()
    return {"ok": True}

@admin_router.get("/reviews")
def all_reviews(user=Depends(req_admin), db: Session = Depends(get_db)):
    return [{"id": r.id, "user_id": r.user_id, "number": r.number,
             "rating": r.rating, "comment": r.comment,
             "created_at": r.created_at.isoformat()}
            for r in db.query(NumberReview).order_by(NumberReview.created_at.desc()).limit(200).all()]

class BroadcastReq(BaseModel):
    message: str

@admin_router.post("/broadcast")
async def broadcast(req: BroadcastReq, user=Depends(req_admin), db: Session = Depends(get_db)):
    db.add(Broadcast(message=req.message, sent_by=user.id)); db.commit()
    await manager.broadcast_all({"type": "broadcast", "message": req.message})
    return {"ok": True}

@admin_router.get("/stats")
def stats(user=Depends(req_admin), db: Session = Depends(get_db)):
    return {
        "total_users":       db.query(User).count(),
        "pending_approval":  db.query(User).filter(User.is_approved == False, User.is_blocked == False).count(),
        "total_pools":       db.query(Pool).count(),
        "total_numbers":     db.query(ActiveNumber).count(),
        "total_otps":        db.query(OTPLog).count(),
        "total_assignments": db.query(Assignment).count(),
        "bad_numbers":       db.query(BadNumber).count(),
        "saved_numbers":     db.query(SavedNumber).filter(SavedNumber.moved == False).count(),
        "online_users":      manager.total(),
    }

# ─── SAVED NUMBERS ────────────────────────────────────────────────────────────

class SaveReq(BaseModel):
    numbers: List[str]; timer_minutes: int; pool_name: str

class UpdateSavedReq(BaseModel):
    number: Optional[str] = None; country: Optional[str] = None
    pool_name: Optional[str] = None; timer_minutes: Optional[int] = None

def _saved_row(r, now):
    secs = max(0, int((r.expires_at - now).total_seconds()))
    if r.moved:      status = "ready"
    elif secs == 0:  status = "expired"
    elif secs < 600: status = "red"
    elif secs < 3600:status = "yellow"
    else:            status = "green"
    return {"id": r.id, "number": r.number, "country": r.country,
            "pool_name": r.pool_name, "expires_at": r.expires_at.isoformat(),
            "seconds_left": secs, "status": status, "moved": r.moved,
            "created_at": r.created_at.isoformat()}

@saved_router.post("")
def save_numbers(req: SaveReq, user=Depends(req_user), db: Session = Depends(get_db)):
    if req.timer_minutes < 1: raise HTTPException(400, "Timer must be at least 1 minute")
    if not req.numbers:        raise HTTPException(400, "No numbers provided")
    if not req.pool_name.strip(): raise HTTPException(400, "Pool name required")
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=req.timer_minutes)
    bad_set    = {b.number for b in db.query(BadNumber).all()}
    saved = skipped = 0
    for raw in req.numbers:
        number = raw.strip()
        if not number: continue
        if number in bad_set: skipped += 1; continue
        if db.query(SavedNumber).filter(SavedNumber.user_id == user.id,
                                        SavedNumber.number == number,
                                        SavedNumber.moved == False).first():
            skipped += 1; continue
        db.add(SavedNumber(user_id=user.id, number=number,
                           pool_name=req.pool_name.strip(), expires_at=expires_at))
        saved += 1
    db.commit()
    return {"ok": True, "saved": saved, "skipped": skipped,
            "expires_at": expires_at.isoformat()}

@saved_router.get("")
def list_saved(user=Depends(req_user), db: Session = Depends(get_db)):
    rows = db.query(SavedNumber).filter(SavedNumber.user_id == user.id).order_by(SavedNumber.expires_at).all()
    now  = datetime.now(timezone.utc)
    return [_saved_row(r, now) for r in rows]

@saved_router.get("/ready")
def ready_numbers(user=Depends(req_user), db: Session = Depends(get_db)):
    rows = db.query(SavedNumber).filter(
        SavedNumber.user_id == user.id, SavedNumber.moved == True
    ).order_by(SavedNumber.created_at.desc()).all()
    result = []
    for r in rows:
        pool   = db.query(Pool).filter(Pool.name == r.pool_name).first()
        active = db.query(ActiveNumber).filter(ActiveNumber.number == r.number).first() if pool else None
        result.append({"id": r.id, "number": r.number, "country": r.country,
                        "pool_name": r.pool_name, "pool_id": pool.id if pool else None,
                        "in_pool": active is not None,
                        "moved_at": r.expires_at.isoformat()})
    return result

@saved_router.put("/{sid}")
def update_saved(sid: int, req: UpdateSavedReq, user=Depends(req_user), db: Session = Depends(get_db)):
    row = db.query(SavedNumber).filter(SavedNumber.id == sid, SavedNumber.user_id == user.id).first()
    if not row: raise HTTPException(404, "Not found")
    if req.number:    row.number    = req.number
    if req.country is not None: row.country = req.country
    if req.pool_name: row.pool_name = req.pool_name
    if req.timer_minutes and req.timer_minutes > 0:
        row.expires_at = datetime.now(timezone.utc) + timedelta(minutes=req.timer_minutes)
        row.moved = False
    db.commit()
    return {"ok": True}

@saved_router.delete("/{sid}")
def delete_saved(sid: int, user=Depends(req_user), db: Session = Depends(get_db)):
    row = db.query(SavedNumber).filter(SavedNumber.id == sid, SavedNumber.user_id == user.id).first()
    if not row: raise HTTPException(404, "Not found")
    db.delete(row); db.commit()
    return {"ok": True}

# ─── REVIEWS ──────────────────────────────────────────────────────────────────

class ReviewReq(BaseModel):
    number: str; rating: int
    comment: Optional[str] = ""; mark_as_bad: bool = False

@reviews_router.post("")
def submit_review(req: ReviewReq, user=Depends(req_user), db: Session = Depends(get_db)):
    if not (1 <= req.rating <= 5): raise HTTPException(400, "Rating must be 1-5")
    db.add(NumberReview(user_id=user.id, number=req.number,
                        rating=req.rating, comment=req.comment or ""))
    if req.mark_as_bad or req.rating == 1:
        if not db.query(BadNumber).filter(BadNumber.number == req.number).first():
            db.add(BadNumber(number=req.number,
                             reason=req.comment or "Flagged by user",
                             flagged_by=user.id))
        db.query(ActiveNumber).filter(ActiveNumber.number == req.number).delete()
    a = db.query(Assignment).filter(
        Assignment.user_id == user.id, Assignment.number == req.number,
        Assignment.released_at == None
    ).first()
    if a:
        a.released_at = datetime.now(timezone.utc)
        a.feedback    = req.comment or ("bad" if req.mark_as_bad else "ok")
    db.commit()
    return {"ok": True, "marked_bad": req.mark_as_bad or req.rating == 1}

# ══════════════════════════════════════════════════════════════════════════════
#  FASTAPI APP
# ══════════════════════════════════════════════════════════════════════════════

@asynccontextmanager
async def lifespan(app: FastAPI):
    create_tables()
    log.info("✅ DB tables ready")
    asyncio.create_task(scheduler())
    log.info("✅ Scheduler started")
    yield

app = FastAPI(title="Frank Number Bot", lifespan=lifespan)

app.add_middleware(CORSMiddleware,
    allow_origins=[FRONTEND_URL] if FRONTEND_URL != "*" else ["*"],
    allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

app.include_router(auth_router)
app.include_router(pools_router)
app.include_router(otp_router)
app.include_router(admin_router)
app.include_router(saved_router)
app.include_router(reviews_router)

@app.websocket("/ws/user/{user_id}")
async def ws_user(websocket: WebSocket, user_id: int, token: str = ""):
    db   = next(get_db())
    user = get_user_from_token(db, token)
    db.close()
    if not user or user.id != user_id:
        await websocket.close(code=4001); return
    await manager.connect_user(websocket, user_id)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect_user(websocket, user_id)

@app.websocket("/ws/feed")
async def ws_feed(websocket: WebSocket, token: str = ""):
    db   = next(get_db())
    user = get_user_from_token(db, token)
    db.close()
    if not user:
        await websocket.close(code=4001); return
    await manager.connect_feed(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect_feed(websocket)

@app.get("/health")
def health():
    return {"status": "ok", "service": "Frank Number Bot"}

# Serve frontend static files
# On Railway all files are in same directory — serve from current dir
FRONTEND_DIR = os.path.dirname(os.path.abspath(__file__))
if os.path.isfile(os.path.join(FRONTEND_DIR, "index.html")):
    app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")

if __name__ == "__main__":
    uvicorn.run("backend:app", host="0.0.0.0", port=int(os.environ.get("PORT", 8000)), reload=False)
