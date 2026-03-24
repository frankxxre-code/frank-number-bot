# -*- coding: utf-8 -*-
"""
FRANK NUMBER BOT — Complete Platform Backend
===============================================
FastAPI + PostgreSQL + WebSocket + Monitor Bot Integration
Deploy on Railway with these environment variables:
  DATABASE_URL       — Railway Postgres
  SHARED_SECRET      — same as monitor bot
  MONITOR_BOT_URL    — your monitor bot Railway URL
  FRONTEND_URL       — frontend domain for CORS
"""

import asyncio, hashlib, io, json, logging, os, re, secrets, time
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional
from collections import defaultdict

import aiohttp
import uvicorn
from fastapi import (FastAPI, WebSocket, WebSocketDisconnect,
                     APIRouter, Depends, HTTPException, Cookie,
                     UploadFile, File, BackgroundTasks)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel
from sqlalchemy import (Column, Integer, String, Boolean, DateTime,
                        Text, ForeignKey, BigInteger, UniqueConstraint,
                        create_engine, func)
from sqlalchemy.orm import declarative_base, relationship, sessionmaker, Session

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("frank")

# ══════════════════════════════════════════════════════════════════════════════
#  CONFIG
# ══════════════════════════════════════════════════════════════════════════════

DATABASE_URL      = os.environ.get("DATABASE_URL", "")
SHARED_SECRET     = os.environ.get("SHARED_SECRET", "MonitorSecret2024")
MONITOR_BOT_URL   = os.environ.get("MONITOR_BOT_URL", "").rstrip("/")
FRONTEND_URL      = os.environ.get("FRONTEND_URL", "*")
OTP_AUTO_DELETE_SECONDS = 30

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
    username      = Column(String(64), unique=True, nullable=False, index=True)
    password_hash = Column(String(256), nullable=False)
    is_admin      = Column(Boolean, default=False)
    is_approved   = Column(Boolean, default=False)
    is_blocked    = Column(Boolean, default=False)
    created_at    = Column(DateTime(timezone=True), default=utcnow)
    sessions      = relationship("UserSession", back_populates="user", cascade="all, delete")
    assignments   = relationship("Assignment", back_populates="user")
    saved_numbers = relationship("SavedNumber", back_populates="user", cascade="all, delete")
    reviews       = relationship("NumberReview", back_populates="user", cascade="all, delete")

class Pool(Base):
    __tablename__ = "pools"
    id             = Column(Integer, primary_key=True, index=True)
    name           = Column(String(128), unique=True, nullable=False)
    country_code   = Column(String(10), nullable=False)
    otp_group_id   = Column(BigInteger, nullable=True)
    otp_link       = Column(String(256), default="")
    match_format   = Column(String(32), default="5+4")
    telegram_match_format = Column(String(32), default="")
    uses_platform  = Column(Integer, default=0)
    is_paused      = Column(Boolean, default=False)
    pause_reason   = Column(Text, default="")
    trick_text     = Column(Text, default="")
    is_admin_only  = Column(Boolean, default=False)
    last_restocked = Column(DateTime(timezone=True), nullable=True)
    created_at     = Column(DateTime(timezone=True), default=utcnow)
    numbers        = relationship("ActiveNumber", back_populates="pool", cascade="all, delete")
    assignments    = relationship("Assignment", back_populates="pool")
    access_list    = relationship("PoolAccess", back_populates="pool", cascade="all, delete")

class PoolAccess(Base):
    __tablename__ = "pool_access"
    __table_args__ = (UniqueConstraint("pool_id", "user_id"),)
    id         = Column(Integer, primary_key=True, index=True)
    pool_id    = Column(Integer, ForeignKey("pools.id"), nullable=False)
    user_id    = Column(Integer, ForeignKey("users.id"), nullable=False)
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
    country    = Column(String(64), default="")
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
#  WEBSOCKET MANAGER (Real-time OTP delivery)
# ══════════════════════════════════════════════════════════════════════════════

class ConnectionManager:
    def __init__(self):
        self.user_connections: Dict[int, List[WebSocket]] = {}
        self.feed_connections: List[WebSocket] = []

    async def connect_user(self, ws: WebSocket, user_id: int):
        await ws.accept()
        self.user_connections.setdefault(user_id, []).append(ws)
        log.info(f"[WS] User {user_id} connected. Total: {self.total()}")

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

    async def send_otp_to_user(self, user_id: int, otp_data: dict):
        """Send OTP to user's WebSocket with timestamp for auto-delete"""
        dead = []
        for ws in self.user_connections.get(user_id, []):
            try:
                await ws.send_text(json.dumps(otp_data))
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

    def total(self) -> int:
        return sum(len(v) for v in self.user_connections.values())

manager = ConnectionManager()

# ══════════════════════════════════════════════════════════════════════════════
#  DEPENDENCY HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def req_user(token: str = Cookie(default=None), db: Session = Depends(get_db)):
    u = get_user_from_token(db, token)
    if not u:
        raise HTTPException(401, "Not authenticated")
    if u.is_blocked:
        raise HTTPException(403, "Account blocked")
    if not u.is_approved:
        raise HTTPException(403, "Account pending approval")
    return u

def req_admin(token: str = Cookie(default=None), db: Session = Depends(get_db)):
    u = get_user_from_token(db, token)
    if not u or not u.is_admin:
        raise HTTPException(403, "Admin only")
    return u

def has_pool_access(db, pool_id: int, user_id: int, is_admin: bool) -> bool:
    if is_admin:
        return True
    count = db.query(PoolAccess).filter(PoolAccess.pool_id == pool_id).count()
    if count == 0:
        return True
    return db.query(PoolAccess).filter(
        PoolAccess.pool_id == pool_id, PoolAccess.user_id == user_id
    ).first() is not None

# ══════════════════════════════════════════════════════════════════════════════
#  MONITOR BOT INTEGRATION
# ══════════════════════════════════════════════════════════════════════════════

async def request_monitor_bot(number: str, group_id: int, match_format: str, user_id: int) -> bool:
    """Send MONITOR_REQUEST to monitor bot to start watching for OTPs"""
    if not MONITOR_BOT_URL:
        log.error("[Monitor] MONITOR_BOT_URL not set!")
        return False
    
    url = f"{MONITOR_BOT_URL}/monitor-request"
    payload = {
        "number": number,
        "group_id": group_id,
        "match_format": match_format,
        "user_id": user_id,
        "secret": SHARED_SECRET
    }
    
    for attempt in range(3):
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload, timeout=10) as resp:
                    if resp.status == 200:
                        log.info(f"[Monitor] ✅ MONITOR_REQUEST sent for {number} user={user_id}")
                        return True
                    else:
                        body = await resp.text()
                        log.error(f"[Monitor] HTTP {resp.status}: {body[:100]}")
        except Exception as e:
            log.error(f"[Monitor] Post failed (attempt {attempt+1}/3): {e}")
            if attempt < 2:
                await asyncio.sleep(2)
    return False

async def request_search_otp(number: str, group_id: int, match_format: str, user_id: int) -> bool:
    """Send SEARCH_OTP_REQUEST to monitor bot to search history + watch live"""
    if not MONITOR_BOT_URL:
        log.error("[SearchOTP] MONITOR_BOT_URL not set!")
        return False
    
    url = f"{MONITOR_BOT_URL}/search-otp-request"
    payload = {
        "number": number,
        "group_id": group_id,
        "match_format": match_format,
        "user_id": user_id,
        "secret": SHARED_SECRET
    }
    
    for attempt in range(3):
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload, timeout=10) as resp:
                    if resp.status == 200:
                        log.info(f"[SearchOTP] ✅ SEARCH_OTP_REQUEST sent for {number} user={user_id}")
                        return True
                    else:
                        body = await resp.text()
                        log.error(f"[SearchOTP] HTTP {resp.status}: {body[:100]}")
        except Exception as e:
            log.error(f"[SearchOTP] Post failed (attempt {attempt+1}/3): {e}")
            if attempt < 2:
                await asyncio.sleep(2)
    return False

# ══════════════════════════════════════════════════════════════════════════════
#  NUMBER ASSIGNMENT
# ══════════════════════════════════════════════════════════════════════════════

def assign_number_to_user(db: Session, user_id: int, pool_id: int) -> Optional[dict]:
    """Assign a number from pool to user. Returns number info or None if no numbers."""
    pool = db.query(Pool).filter(Pool.id == pool_id).first()
    if not pool or pool.is_paused:
        return None
    
    # Get one number from pool
    number_row = db.query(ActiveNumber).filter(ActiveNumber.pool_id == pool_id).first()
    if not number_row:
        return None
    
    # Delete from active numbers
    db.delete(number_row)
    
    # Create assignment record
    assignment = Assignment(
        user_id=user_id,
        pool_id=pool_id,
        number=number_row.number
    )
    db.add(assignment)
    db.commit()
    db.refresh(assignment)
    
    # Release any previous assignment for this user
    db.query(Assignment).filter(
        Assignment.user_id == user_id,
        Assignment.id != assignment.id,
        Assignment.released_at == None
    ).update({"released_at": utcnow()})
    
    db.commit()
    
    return {
        "assignment_id": assignment.id,
        "number": assignment.number,
        "pool_name": pool.name,
        "pool_code": pool.country_code,
        "otp_group_id": pool.otp_group_id,
        "otp_link": pool.otp_link,
        "match_format": pool.match_format,
        "telegram_match_format": pool.telegram_match_format or pool.match_format,
        "uses_platform": pool.uses_platform,
        "trick_text": pool.trick_text
    }

def release_current_assignment(db: Session, user_id: int):
    """Release the user's current assignment"""
    assignment = db.query(Assignment).filter(
        Assignment.user_id == user_id,
        Assignment.released_at == None
    ).first()
    if assignment:
        assignment.released_at = utcnow()
        db.commit()
        return assignment
    return None

# ══════════════════════════════════════════════════════════════════════════════
#  SAVED NUMBERS EXPIRY PROCESSOR
# ══════════════════════════════════════════════════════════════════════════════

async def process_expired_saved(db: Session):
    """Move expired saved numbers back to active pool"""
    now = utcnow()
    expired = db.query(SavedNumber).filter(
        SavedNumber.expires_at <= now,
        SavedNumber.moved == False
    ).all()
    
    for sn in expired:
        # Find or create pool by name
        pool = db.query(Pool).filter(Pool.name == sn.pool_name).first()
        if not pool:
            # Create a new pool for these numbers
            pool = Pool(
                name=sn.pool_name,
                country_code=sn.country or "unknown",
                match_format="5+4"
            )
            db.add(pool)
            db.flush()
        
        # Check if number is bad
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
    """Background task to process expired saved numbers"""
    while True:
        await asyncio.sleep(60)
        try:
            db = next(get_db())
            await process_expired_saved(db)
            db.close()
        except Exception as e:
            log.error(f"[Scheduler] {e}")

# ══════════════════════════════════════════════════════════════════════════════
#  AUTH ROUTER
# ══════════════════════════════════════════════════════════════════════════════

auth_router = APIRouter(prefix="/api/auth", tags=["auth"])

class RegisterReq(BaseModel):
    username: str
    password: str

class LoginReq(BaseModel):
    username: str
    password: str

@auth_router.post("/register")
def register(req: RegisterReq, db: Session = Depends(get_db)):
    if len(req.username) < 3:
        raise HTTPException(400, "Username min 3 chars")
    if len(req.password) < 6:
        raise HTTPException(400, "Password min 6 chars")
    
    if db.query(User).filter(User.username == req.username).first():
        raise HTTPException(400, "Username taken")
    
    # First user becomes admin
    is_first = db.query(User).count() == 0
    
    user = User(
        username=req.username,
        password_hash=hash_password(req.password),
        is_admin=is_first,
        is_approved=is_first
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    
    if is_first:
        token = create_token(db, user.id)
        resp = JSONResponse({"ok": True, "approved": True, "is_admin": True, "user_id": user.id})
        resp.set_cookie("token", token, httponly=True, samesite="lax", max_age=86400*30)
        return resp
    
    return {"ok": True, "approved": False, "message": "Awaiting admin approval"}

@auth_router.post("/login")
def login(req: LoginReq, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.username == req.username).first()
    if not user or not verify_password(req.password, user.password_hash):
        raise HTTPException(401, "Invalid username or password")
    if user.is_blocked:
        raise HTTPException(403, "Account blocked. Contact admin.")
    if not user.is_approved:
        raise HTTPException(403, "Account pending admin approval.")
    
    token = create_token(db, user.id)
    resp = JSONResponse({
        "ok": True,
        "user_id": user.id,
        "username": user.username,
        "is_admin": user.is_admin
    })
    resp.set_cookie("token", token, httponly=True, samesite="lax", max_age=86400*30)
    return resp

@auth_router.post("/logout")
def logout(token: str = Cookie(default=None), db: Session = Depends(get_db)):
    if token:
        revoke_token(db, token)
    resp = JSONResponse({"ok": True})
    resp.delete_cookie("token")
    return resp

@auth_router.get("/me")
def me(token: str = Cookie(default=None), db: Session = Depends(get_db)):
    user = get_user_from_token(db, token)
    if not user:
        raise HTTPException(401, "Not authenticated")
    return {
        "id": user.id,
        "username": user.username,
        "is_admin": user.is_admin,
        "is_approved": user.is_approved
    }

# ══════════════════════════════════════════════════════════════════════════════
#  POOLS ROUTER
# ══════════════════════════════════════════════════════════════════════════════

pools_router = APIRouter(prefix="/api/pools", tags=["pools"])

class AssignNumberRequest(BaseModel):
    pool_id: int
    prefix: Optional[str] = None

@pools_router.get("")
def list_pools(user=Depends(req_user), db: Session = Depends(get_db)):
    result = []
    for p in db.query(Pool).all():
        if not user.is_admin and p.is_admin_only:
            continue
        if not has_pool_access(db, p.id, user.id, user.is_admin):
            continue
        
        count = db.query(ActiveNumber).filter(ActiveNumber.pool_id == p.id).count()
        restricted = db.query(PoolAccess).filter(PoolAccess.pool_id == p.id).count() > 0
        
        # Check if user has current assignment in this pool
        current = db.query(Assignment).filter(
            Assignment.user_id == user.id,
            Assignment.pool_id == p.id,
            Assignment.released_at == None
        ).first()
        
        result.append({
            "id": p.id,
            "name": p.name,
            "country_code": p.country_code,
            "otp_link": p.otp_link,
            "match_format": p.match_format,
            "telegram_match_format": p.telegram_match_format or p.match_format,
            "uses_platform": p.uses_platform,
            "is_paused": p.is_paused,
            "pause_reason": p.pause_reason,
            "trick_text": p.trick_text,
            "is_admin_only": p.is_admin_only,
            "number_count": count,
            "restricted": restricted,
            "has_current": current is not None,
            "last_restocked": p.last_restocked.isoformat() if p.last_restocked else None
        })
    return result

@pools_router.post("/assign")
async def assign_number(req: AssignNumberRequest, user=Depends(req_user), db: Session = Depends(get_db)):
    """Assign a number to the user and start monitoring"""
    pool = db.query(Pool).filter(Pool.id == req.pool_id).first()
    if not pool:
        raise HTTPException(404, "Pool not found")
    if pool.is_paused:
        raise HTTPException(400, f"Pool paused: {pool.pause_reason or 'No reason'}")
    if pool.is_admin_only and not user.is_admin:
        raise HTTPException(403, "Admin only pool")
    if not has_pool_access(db, pool.id, user.id, user.is_admin):
        raise HTTPException(403, "No access to this pool")
    
    # Release current assignment if any
    release_current_assignment(db, user.id)
    
    # Assign new number
    assignment_info = assign_number_to_user(db, user.id, req.pool_id)
    if not assignment_info:
        raise HTTPException(400, "No numbers available in this pool")
    
    # Send monitor request to monitor bot
    if assignment_info["otp_group_id"]:
        await request_monitor_bot(
            number=assignment_info["number"],
            group_id=assignment_info["otp_group_id"],
            match_format=assignment_info["telegram_match_format"],
            user_id=user.id
        )
    
    return {
        "ok": True,
        "assignment_id": assignment_info["assignment_id"],
        "number": assignment_info["number"],
        "pool_name": assignment_info["pool_name"],
        "country_code": assignment_info["pool_code"],
        "otp_link": assignment_info["otp_link"],
        "trick_text": assignment_info["trick_text"]
    }

@pools_router.post("/release/{assignment_id}")
def release_number(assignment_id: int, user=Depends(req_user), db: Session = Depends(get_db)):
    """Release a specific assignment"""
    a = db.query(Assignment).filter(
        Assignment.id == assignment_id,
        Assignment.user_id == user.id
    ).first()
    if not a:
        raise HTTPException(404, "Assignment not found")
    a.released_at = utcnow()
    db.commit()
    return {"ok": True}

@pools_router.get("/my-assignment")
def my_assignment(user=Depends(req_user), db: Session = Depends(get_db)):
    """Get user's current active assignment"""
    a = db.query(Assignment).filter(
        Assignment.user_id == user.id,
        Assignment.released_at == None
    ).order_by(Assignment.assigned_at.desc()).first()
    
    if not a:
        return {"assignment": None}
    
    pool = db.query(Pool).filter(Pool.id == a.pool_id).first()
    if not pool:
        return {"assignment": None}
    
    return {
        "assignment": {
            "assignment_id": a.id,
            "number": a.number,
            "pool_name": pool.name,
            "country_code": pool.country_code,
            "otp_link": pool.otp_link,
            "trick_text": pool.trick_text,
            "otp_group_id": pool.otp_group_id,
            "match_format": pool.match_format,
            "telegram_match_format": pool.telegram_match_format or pool.match_format
        }
    }

# ══════════════════════════════════════════════════════════════════════════════
#  OTP ROUTER
# ══════════════════════════════════════════════════════════════════════════════

otp_router = APIRouter(prefix="/api/otp", tags=["otp"])

class MonitorResultPayload(BaseModel):
    number: str
    otp: str
    raw_message: str = ""
    user_id: int
    secret: str = ""

@otp_router.post("/monitor-result")
async def monitor_result(payload: MonitorResultPayload, db: Session = Depends(get_db)):
    """Receive OTP from monitor bot and push to user via WebSocket"""
    if SHARED_SECRET and payload.secret != SHARED_SECRET:
        raise HTTPException(403, "Invalid secret")
    
    log.info(f"[OTP] Received from monitor: {payload.number} -> {payload.otp} for user {payload.user_id}")
    
    # Find active assignment
    assignment = db.query(Assignment).filter(
        Assignment.user_id == payload.user_id,
        Assignment.number == payload.number,
        Assignment.released_at == None
    ).first()
    
    # Save OTP log
    entry = OTPLog(
        assignment_id=assignment.id if assignment else None,
        user_id=payload.user_id,
        number=payload.number,
        otp_code=payload.otp,
        raw_message=payload.raw_message
    )
    db.add(entry)
    db.commit()
    db.refresh(entry)
    
    # Prepare OTP data for WebSocket
    otp_data = {
        "type": "otp",
        "id": entry.id,
        "number": payload.number,
        "otp": payload.otp,
        "raw_message": payload.raw_message,
        "delivered_at": entry.delivered_at.isoformat(),
        "auto_delete_seconds": 30
    }
    
    # Push to user's WebSocket
    await manager.send_otp_to_user(payload.user_id, otp_data)
    
    # Also broadcast to feed
    await manager.broadcast_feed({
        "type": "feed_otp",
        "number": payload.number,
        "otp": payload.otp,
        "delivered_at": entry.delivered_at.isoformat()
    })
    
    return {"ok": True}

@otp_router.post("/search")
async def search_otp(number: str, user=Depends(req_user), db: Session = Depends(get_db)):
    """User requests to search OTP for a number"""
    # Find which pool this number belongs to
    assignment = db.query(Assignment).filter(
        Assignment.user_id == user.id,
        Assignment.number == number
    ).order_by(Assignment.assigned_at.desc()).first()
    
    if not assignment:
        raise HTTPException(404, "Number not found in your history")
    
    pool = db.query(Pool).filter(Pool.id == assignment.pool_id).first()
    if not pool or not pool.otp_group_id:
        raise HTTPException(400, "No OTP group configured for this pool")
    
    await request_search_otp(
        number=number,
        group_id=pool.otp_group_id,
        match_format=pool.telegram_match_format or pool.match_format,
        user_id=user.id
    )
    
    return {"ok": True, "message": f"Searching OTP for {number}"}

@otp_router.get("/my")
def my_otps(limit: int = 50, user=Depends(req_user), db: Session = Depends(get_db)):
    """Get user's recent OTPs"""
    logs = db.query(OTPLog).filter(OTPLog.user_id == user.id).order_by(
        OTPLog.delivered_at.desc()
    ).limit(limit).all()
    
    return [{
        "id": l.id,
        "number": l.number,
        "otp_code": l.otp_code,
        "raw_message": l.raw_message[:200] if l.raw_message else "",
        "delivered_at": l.delivered_at.isoformat()
    } for l in logs]

# ══════════════════════════════════════════════════════════════════════════════
#  SAVED NUMBERS ROUTER
# ══════════════════════════════════════════════════════════════════════════════

saved_router = APIRouter(prefix="/api/saved", tags=["saved"])

class SaveNumberRequest(BaseModel):
    numbers: List[str]
    timer_minutes: int
    pool_name: str

class UpdateSavedRequest(BaseModel):
    timer_minutes: Optional[int] = None
    pool_name: Optional[str] = None

@saved_router.post("")
def save_numbers(req: SaveNumberRequest, user=Depends(req_user), db: Session = Depends(get_db)):
    """Save numbers with timer - will auto-move to pool when expired"""
    if req.timer_minutes < 1:
        raise HTTPException(400, "Timer must be at least 1 minute")
    if not req.numbers:
        raise HTTPException(400, "No numbers provided")
    
    expires_at = utcnow() + timedelta(minutes=req.timer_minutes)
    bad_set = {b.number for b in db.query(BadNumber).all()}
    
    saved = 0
    skipped = 0
    
    for raw in req.numbers:
        number = raw.strip()
        if not number:
            continue
        if number in bad_set:
            skipped += 1
            continue
        
        # Check if already saved
        existing = db.query(SavedNumber).filter(
            SavedNumber.user_id == user.id,
            SavedNumber.number == number,
            SavedNumber.moved == False
        ).first()
        if existing:
            skipped += 1
            continue
        
        db.add(SavedNumber(
            user_id=user.id,
            number=number,
            pool_name=req.pool_name.strip(),
            expires_at=expires_at
        ))
        saved += 1
    
    db.commit()
    
    return {
        "ok": True,
        "saved": saved,
        "skipped": skipped,
        "expires_at": expires_at.isoformat()
    }

@saved_router.get("")
def list_saved(user=Depends(req_user), db: Session = Depends(get_db)):
    """Get user's saved numbers with status"""
    rows = db.query(SavedNumber).filter(
        SavedNumber.user_id == user.id
    ).order_by(SavedNumber.expires_at).all()
    
    now = utcnow()
    result = []
    
    for r in rows:
        seconds_left = max(0, int((r.expires_at - now).total_seconds()))
        
        if r.moved:
            status = "ready"
        elif seconds_left == 0:
            status = "expired"
        elif seconds_left < 600:  # less than 10 minutes
            status = "red"
        elif seconds_left < 3600:  # less than 1 hour
            status = "yellow"
        else:
            status = "green"
        
        result.append({
            "id": r.id,
            "number": r.number,
            "country": r.country,
            "pool_name": r.pool_name,
            "expires_at": r.expires_at.isoformat(),
            "seconds_left": seconds_left,
            "status": status,
            "moved": r.moved
        })
    
    return result

@saved_router.get("/ready")
def ready_numbers(user=Depends(req_user), db: Session = Depends(get_db)):
    """Get numbers that have expired and are ready to use"""
    rows = db.query(SavedNumber).filter(
        SavedNumber.user_id == user.id,
        SavedNumber.moved == True
    ).order_by(SavedNumber.created_at.desc()).all()
    
    result = []
    for r in rows:
        pool = db.query(Pool).filter(Pool.name == r.pool_name).first()
        in_pool = False
        if pool:
            in_pool = db.query(ActiveNumber).filter(
                ActiveNumber.pool_id == pool.id,
                ActiveNumber.number == r.number
            ).first() is not None
        
        result.append({
            "id": r.id,
            "number": r.number,
            "country": r.country,
            "pool_name": r.pool_name,
            "pool_id": pool.id if pool else None,
            "in_pool": in_pool
        })
    
    return result

@saved_router.put("/{saved_id}")
def update_saved(saved_id: int, req: UpdateSavedRequest, user=Depends(req_user), db: Session = Depends(get_db)):
    """Update saved number timer"""
    row = db.query(SavedNumber).filter(
        SavedNumber.id == saved_id,
        SavedNumber.user_id == user.id
    ).first()
    
    if not row:
        raise HTTPException(404, "Not found")
    
    if req.timer_minutes:
        row.expires_at = utcnow() + timedelta(minutes=req.timer_minutes)
        row.moved = False
    
    if req.pool_name:
        row.pool_name = req.pool_name
    
    db.commit()
    return {"ok": True}

@saved_router.delete("/{saved_id}")
def delete_saved(saved_id: int, user=Depends(req_user), db: Session = Depends(get_db)):
    """Delete a saved number"""
    row = db.query(SavedNumber).filter(
        SavedNumber.id == saved_id,
        SavedNumber.user_id == user.id
    ).first()
    
    if not row:
        raise HTTPException(404, "Not found")
    
    db.delete(row)
    db.commit()
    return {"ok": True}

# ══════════════════════════════════════════════════════════════════════════════
#  REVIEWS ROUTER
# ══════════════════════════════════════════════════════════════════════════════

reviews_router = APIRouter(prefix="/api/reviews", tags=["reviews"])

class ReviewRequest(BaseModel):
    number: str
    rating: int
    comment: Optional[str] = ""
    mark_as_bad: bool = False

@reviews_router.post("")
def submit_review(req: ReviewRequest, user=Depends(req_user), db: Session = Depends(get_db)):
    """Submit feedback for a number"""
    if not (1 <= req.rating <= 5):
        raise HTTPException(400, "Rating must be 1-5")
    
    db.add(NumberReview(
        user_id=user.id,
        number=req.number,
        rating=req.rating,
        comment=req.comment or ""
    ))
    
    if req.mark_as_bad or req.rating == 1:
        # Mark as bad number
        existing = db.query(BadNumber).filter(BadNumber.number == req.number).first()
        if not existing:
            db.add(BadNumber(
                number=req.number,
                reason=req.comment or "Flagged by user",
                flagged_by=user.id
            ))
        # Remove from active numbers
        db.query(ActiveNumber).filter(ActiveNumber.number == req.number).delete()
    
    # Release current assignment if this is the active number
    assignment = db.query(Assignment).filter(
        Assignment.user_id == user.id,
        Assignment.number == req.number,
        Assignment.released_at == None
    ).first()
    
    if assignment:
        assignment.released_at = utcnow()
        assignment.feedback = req.comment or ("bad" if req.mark_as_bad else "ok")
    
    db.commit()
    
    return {"ok": True, "marked_bad": req.mark_as_bad or req.rating == 1}

# ══════════════════════════════════════════════════════════════════════════════
#  ADMIN ROUTER
# ══════════════════════════════════════════════════════════════════════════════

admin_router = APIRouter(prefix="/api/admin", tags=["admin"])

@admin_router.get("/users")
def list_users(user=Depends(req_admin), db: Session = Depends(get_db)):
    return [{
        "id": u.id,
        "username": u.username,
        "is_admin": u.is_admin,
        "is_approved": u.is_approved,
        "is_blocked": u.is_blocked,
        "created_at": u.created_at.isoformat()
    } for u in db.query(User).order_by(User.created_at.desc()).all()]

@admin_router.post("/users/{user_id}/approve")
def approve_user(user_id: int, user=Depends(req_admin), db: Session = Depends(get_db)):
    u = db.query(User).filter(User.id == user_id).first()
    if not u:
        raise HTTPException(404, "User not found")
    u.is_approved = True
    u.is_blocked = False
    db.commit()
    return {"ok": True}

@admin_router.post("/users/{user_id}/block")
def block_user(user_id: int, user=Depends(req_admin), db: Session = Depends(get_db)):
    u = db.query(User).filter(User.id == user_id).first()
    if not u:
        raise HTTPException(404, "User not found")
    u.is_blocked = True
    u.is_approved = False
    db.commit()
    return {"ok": True}

@admin_router.post("/users/{user_id}/unblock")
def unblock_user(user_id: int, user=Depends(req_admin), db: Session = Depends(get_db)):
    u = db.query(User).filter(User.id == user_id).first()
    if not u:
        raise HTTPException(404, "User not found")
    u.is_blocked = False
    u.is_approved = True
    db.commit()
    return {"ok": True}

@admin_router.get("/stats")
def stats(user=Depends(req_admin), db: Session = Depends(get_db)):
    return {
        "total_users": db.query(User).count(),
        "pending_approval": db.query(User).filter(User.is_approved == False, User.is_blocked == False).count(),
        "total_pools": db.query(Pool).count(),
        "total_numbers": db.query(ActiveNumber).count(),
        "total_otps": db.query(OTPLog).count(),
        "total_assignments": db.query(Assignment).count(),
        "bad_numbers": db.query(BadNumber).count(),
        "saved_numbers": db.query(SavedNumber).filter(SavedNumber.moved == False).count(),
        "online_users": manager.total()
    }

@admin_router.post("/broadcast")
async def broadcast(message: str, user=Depends(req_admin), db: Session = Depends(get_db)):
    """Send broadcast message to all connected users"""
    db.add(Broadcast(message=message, sent_by=user.id))
    db.commit()
    
    await manager.broadcast_all({
        "type": "broadcast",
        "message": message
    })
    
    return {"ok": True}

# ══════════════════════════════════════════════════════════════════════════════
#  POOL MANAGEMENT (Admin)
# ══════════════════════════════════════════════════════════════════════════════

class PoolCreate(BaseModel):
    name: str
    country_code: str
    otp_group_id: Optional[int] = None
    otp_link: Optional[str] = ""
    match_format: Optional[str] = "5+4"
    telegram_match_format: Optional[str] = ""
    uses_platform: Optional[int] = 0
    trick_text: Optional[str] = ""
    is_admin_only: Optional[bool] = False
    is_paused: Optional[bool] = False
    pause_reason: Optional[str] = ""

@pools_router.post("")
def create_pool(req: PoolCreate, user=Depends(req_admin), db: Session = Depends(get_db)):
    if db.query(Pool).filter(Pool.name == req.name).first():
        raise HTTPException(400, "Pool name already exists")
    
    pool = Pool(**req.dict())
    db.add(pool)
    db.commit()
    db.refresh(pool)
    
    return {"ok": True, "id": pool.id}

@pools_router.put("/{pool_id}")
def update_pool(pool_id: int, req: PoolCreate, user=Depends(req_admin), db: Session = Depends(get_db)):
    pool = db.query(Pool).filter(Pool.id == pool_id).first()
    if not pool:
        raise HTTPException(404, "Pool not found")
    
    for key, value in req.dict().items():
        setattr(pool, key, value)
    
    db.commit()
    return {"ok": True}

@pools_router.delete("/{pool_id}")
def delete_pool(pool_id: int, user=Depends(req_admin), db: Session = Depends(get_db)):
    pool = db.query(Pool).filter(Pool.id == pool_id).first()
    if not pool:
        raise HTTPException(404, "Pool not found")
    
    db.delete(pool)
    db.commit()
    return {"ok": True}

@pools_router.post("/{pool_id}/upload")
async def upload_numbers(pool_id: int, file: UploadFile = File(...),
                         user=Depends(req_admin), db: Session = Depends(get_db)):
    """Upload numbers to a pool"""
    pool = db.query(Pool).filter(Pool.id == pool_id).first()
    if not pool:
        raise HTTPException(404, "Pool not found")
    
    content = await file.read()
    lines = content.decode("utf-8", errors="ignore").splitlines()
    
    bad_set = {b.number for b in db.query(BadNumber).all()}
    saved_set = {s.number for s in db.query(SavedNumber).filter(SavedNumber.moved == False).all()}
    existing = {n.number for n in db.query(ActiveNumber).all()}
    
    added = 0
    skipped_bad = 0
    skipped_saved = 0
    skipped_dup = 0
    
    phone_re = re.compile(r'(\+?\d{6,15})')
    
    for line in lines:
        m = phone_re.search(line.strip())
        if not m:
            continue
        
        number = m.group(1)
        
        if number in bad_set:
            skipped_bad += 1
            continue
        if number in saved_set:
            skipped_saved += 1
            continue
        if number in existing:
            skipped_dup += 1
            continue
        
        db.add(ActiveNumber(pool_id=pool_id, number=number))
        existing.add(number)
        added += 1
    
    pool.last_restocked = utcnow()
    db.commit()
    
    return {
        "ok": True,
        "added": added,
        "skipped_bad": skipped_bad,
        "skipped_saved": skipped_saved,
        "skipped_duplicate": skipped_dup
    }

@pools_router.get("/{pool_id}/export")
def export_pool(pool_id: int, user=Depends(req_admin), db: Session = Depends(get_db)):
    numbers = db.query(ActiveNumber).filter(ActiveNumber.pool_id == pool_id).all()
    return Response(
        content="\n".join(n.number for n in numbers),
        media_type="text/plain",
        headers={"Content-Disposition": f"attachment; filename=pool_{pool_id}.txt"}
    )

# ══════════════════════════════════════════════════════════════════════════════
#  WEBSOCKET ENDPOINTS
# ══════════════════════════════════════════════════════════════════════════════

@app.websocket("/ws/user/{user_id}")
async def websocket_user(websocket: WebSocket, user_id: int, token: str = ""):
    """User-specific WebSocket for real-time OTPs"""
    db = next(get_db())
    user = get_user_from_token(db, token)
    db.close()
    
    if not user or user.id != user_id:
        await websocket.close(code=4001)
        return
    
    await manager.connect_user(websocket, user_id)
    
    try:
        while True:
            # Keep connection alive
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect_user(websocket, user_id)

@app.websocket("/ws/feed")
async def websocket_feed(websocket: WebSocket, token: str = ""):
    """Public feed WebSocket for live OTP broadcasts"""
    db = next(get_db())
    user = get_user_from_token(db, token)
    db.close()
    
    if not user:
        await websocket.close(code=4001)
        return
    
    await manager.connect_feed(websocket)
    
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect_feed(websocket)

# ══════════════════════════════════════════════════════════════════════════════
#  MAIN APP
# ══════════════════════════════════════════════════════════════════════════════

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Create tables
    Base.metadata.create_all(bind=engine)
    log.info("✅ Database tables ready")
    
    # Start scheduler for expired saved numbers
    asyncio.create_task(scheduler())
    log.info("✅ Scheduler started")
    
    # Create default admin if no users exist
    db = next(get_db())
    if db.query(User).count() == 0:
        admin = User(
            username="admin",
            password_hash=hash_password("admin123"),
            is_admin=True,
            is_approved=True
        )
        db.add(admin)
        db.commit()
        log.info("✅ Default admin created: admin / admin123")
    db.close()
    
    yield

app = FastAPI(title="Frank Number Bot", lifespan=lifespan)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=[FRONTEND_URL] if FRONTEND_URL != "*" else ["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include routers
app.include_router(auth_router)
app.include_router(pools_router)
app.include_router(otp_router)
app.include_router(saved_router)
app.include_router(reviews_router)
app.include_router(admin_router)

@app.get("/health")
def health():
    return {"status": "ok", "service": "Frank Number Bot"}

# ══════════════════════════════════════════════════════════════════════════════
#  MAIN ENTRY
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    uvicorn.run(
        "backend:app",
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 8000)),
        reload=False
    )
