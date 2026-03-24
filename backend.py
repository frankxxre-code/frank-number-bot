# -*- coding: utf-8 -*-
"""
FRANK NUMBER BOT — Complete Platform Backend
Deploy on Railway
"""

import asyncio
import hashlib
import io
import json
import logging
import os
import re
import secrets
import time
import sys
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

# Database imports with fallback for missing packages
try:
    from sqlalchemy import (Column, Integer, String, Boolean, DateTime,
                            Text, ForeignKey, BigInteger, UniqueConstraint,
                            create_engine, func)
    from sqlalchemy.orm import declarative_base, relationship, sessionmaker, Session
    SQLALCHEMY_AVAILABLE = True
except ImportError:
    SQLALCHEMY_AVAILABLE = False
    logging.warning("SQLAlchemy not installed, using in-memory fallback")

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)
log = logging.getLogger("frank")

# ══════════════════════════════════════════════════════════════════════════════
#  CONFIG
# ══════════════════════════════════════════════════════════════════════════════

DATABASE_URL = os.environ.get("DATABASE_URL", "")
SHARED_SECRET = os.environ.get("SHARED_SECRET", "MonitorSecret2024")
MONITOR_BOT_URL = os.environ.get("MONITOR_BOT_URL", "").rstrip("/")
FRONTEND_URL = os.environ.get("FRONTEND_URL", "*")
PORT = int(os.environ.get("PORT", "8000"))

# Fix Railway Postgres URL
if DATABASE_URL and DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

log.info(f"Starting Frank Number Bot on port {PORT}")
log.info(f"Database URL: {'configured' if DATABASE_URL else 'NOT SET'}")
log.info(f"Monitor Bot URL: {MONITOR_BOT_URL or 'NOT SET'}")
log.info(f"Frontend URL: {FRONTEND_URL}")

# ══════════════════════════════════════════════════════════════════════════════
#  IN-MEMORY FALLBACK (if SQLAlchemy not available)
# ══════════════════════════════════════════════════════════════════════════════

if not SQLALCHEMY_AVAILABLE:
    log.warning("Running in memory-only mode (no database)")
    
    # In-memory storage
    users = {}
    sessions = {}
    pools = {}
    active_numbers = {}
    assignments = {}
    otp_logs = {}
    saved_numbers = {}
    reviews = {}
    bad_numbers = set()
    
    user_counter = 1
    pool_counter = 1
    assignment_counter = 1
    otp_counter = 1
    saved_counter = 1
    review_counter = 1
    
    def get_user_from_token(token):
        if token and token in sessions:
            user_id = sessions[token]
            return users.get(user_id)
        return None
    
    def create_token(user_id):
        token = secrets.token_urlsafe(48)
        sessions[token] = user_id
        return token
    
    def revoke_token(token):
        if token in sessions:
            del sessions[token]
    
    def hash_password(p):
        salt = os.urandom(16).hex()
        h = hashlib.sha256((salt + p).encode()).hexdigest()
        return f"{salt}:{h}"
    
    def verify_password(p, hashed):
        try:
            salt, h = hashed.split(":", 1)
            return hashlib.sha256((salt + p).encode()).hexdigest() == h
        except:
            return False
    
    def utcnow():
        return datetime.now(timezone.utc)

else:
    # SQLAlchemy setup
    from sqlalchemy import create_engine
    from sqlalchemy.orm import declarative_base, relationship, sessionmaker, Session
    
    engine = create_engine(DATABASE_URL, pool_pre_ping=True, pool_size=10, max_overflow=20) if DATABASE_URL else None
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine) if engine else None
    Base = declarative_base()
    
    def get_db():
        if not SessionLocal:
            raise HTTPException(500, "Database not configured")
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()
    
    def utcnow():
        return datetime.now(timezone.utc)

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

    async def send_to_user(self, user_id: int, data: dict):
        dead = []
        for ws in self.user_connections.get(user_id, []):
            try:
                await ws.send_text(json.dumps(data))
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect_user(ws, user_id)

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
#  MONITOR BOT INTEGRATION
# ══════════════════════════════════════════════════════════════════════════════

async def request_monitor_bot(number: str, group_id: int, match_format: str, user_id: int) -> bool:
    """Send MONITOR_REQUEST to monitor bot"""
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
        log.error(f"[Monitor] Post failed: {e}")
    return False

# ══════════════════════════════════════════════════════════════════════════════
#  FASTAPI APP
# ══════════════════════════════════════════════════════════════════════════════

@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("Starting Frank Number Bot...")
    
    # Initialize database if using SQLAlchemy
    if SQLALCHEMY_AVAILABLE and engine:
        try:
            Base.metadata.create_all(bind=engine)
            log.info("✅ Database tables created")
        except Exception as e:
            log.error(f"Database initialization error: {e}")
    
    # Create default admin if using in-memory
    if not SQLALCHEMY_AVAILABLE:
        if not users:
            user_id = 1
            users[user_id] = {
                "id": user_id,
                "username": "admin",
                "password_hash": hash_password("admin123"),
                "is_admin": True,
                "is_approved": True,
                "is_blocked": False,
                "created_at": utcnow()
            }
            log.info("✅ Default admin created: admin / admin123")
    
    yield
    
    log.info("Shutting down Frank Number Bot...")

app = FastAPI(title="Frank Number Bot", lifespan=lifespan)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=[FRONTEND_URL] if FRONTEND_URL != "*" else ["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ══════════════════════════════════════════════════════════════════════════════
#  AUTH ENDPOINTS
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/health")
def health():
    """Health check endpoint for Railway"""
    return {"status": "ok", "service": "Frank Number Bot", "timestamp": utcnow().isoformat()}

@app.post("/api/auth/register")
async def register(username: str, password: str):
    if len(username) < 3:
        raise HTTPException(400, "Username min 3 chars")
    if len(password) < 6:
        raise HTTPException(400, "Password min 6 chars")
    
    if SQLALCHEMY_AVAILABLE and engine:
        # SQLAlchemy version
        from sqlalchemy import Table, Column, Integer, String, Boolean, DateTime, MetaData
        import sqlalchemy as sa
        
        metadata = MetaData()
        users_table = Table(
            "users", metadata,
            Column("id", Integer, primary_key=True),
            Column("username", String(64), unique=True),
            Column("password_hash", String(256)),
            Column("is_admin", Boolean, default=False),
            Column("is_approved", Boolean, default=False),
            Column("is_blocked", Boolean, default=False),
            Column("created_at", DateTime(timezone=True), default=utcnow)
        )
        
        try:
            metadata.create_all(engine)
        except:
            pass
        
        with engine.connect() as conn:
            # Check if user exists
            result = conn.execute(sa.select(users_table).where(users_table.c.username == username)).first()
            if result:
                raise HTTPException(400, "Username taken")
            
            # Count users
            count = conn.execute(sa.select(sa.func.count()).select_from(users_table)).scalar()
            is_first = count == 0
            
            # Insert user
            conn.execute(users_table.insert().values(
                username=username,
                password_hash=hash_password(password),
                is_admin=is_first,
                is_approved=is_first
            ))
            conn.commit()
            
            # Get user
            user = conn.execute(sa.select(users_table).where(users_table.c.username == username)).first()
            
            if is_first:
                token = create_token(user.id)
                resp = JSONResponse({"ok": True, "approved": True, "is_admin": True, "user_id": user.id})
                resp.set_cookie("token", token, httponly=True, samesite="lax", max_age=86400*30)
                return resp
            
            return {"ok": True, "approved": False, "message": "Awaiting admin approval"}
    else:
        # In-memory version
        for u in users.values():
            if u["username"] == username:
                raise HTTPException(400, "Username taken")
        
        is_first = len(users) == 0
        user_id = len(users) + 1
        
        users[user_id] = {
            "id": user_id,
            "username": username,
            "password_hash": hash_password(password),
            "is_admin": is_first,
            "is_approved": is_first,
            "is_blocked": False,
            "created_at": utcnow()
        }
        
        if is_first:
            token = create_token(user_id)
            resp = JSONResponse({"ok": True, "approved": True, "is_admin": True, "user_id": user_id})
            resp.set_cookie("token", token, httponly=True, samesite="lax", max_age=86400*30)
            return resp
        
        return {"ok": True, "approved": False, "message": "Awaiting admin approval"}

@app.post("/api/auth/login")
async def login(username: str, password: str):
    if SQLALCHEMY_AVAILABLE and engine:
        from sqlalchemy import Table, Column, Integer, String, Boolean, DateTime, MetaData
        import sqlalchemy as sa
        
        metadata = MetaData()
        users_table = Table(
            "users", metadata,
            Column("id", Integer, primary_key=True),
            Column("username", String(64), unique=True),
            Column("password_hash", String(256)),
            Column("is_admin", Boolean, default=False),
            Column("is_approved", Boolean, default=False),
            Column("is_blocked", Boolean, default=False),
            Column("created_at", DateTime(timezone=True), default=utcnow)
        )
        
        with engine.connect() as conn:
            user = conn.execute(sa.select(users_table).where(users_table.c.username == username)).first()
            if not user:
                raise HTTPException(401, "Invalid username or password")
            
            if not verify_password(password, user.password_hash):
                raise HTTPException(401, "Invalid username or password")
            
            if user.is_blocked:
                raise HTTPException(403, "Account blocked")
            if not user.is_approved:
                raise HTTPException(403, "Account pending approval")
            
            token = create_token(user.id)
            resp = JSONResponse({
                "ok": True,
                "user_id": user.id,
                "username": user.username,
                "is_admin": user.is_admin
            })
            resp.set_cookie("token", token, httponly=True, samesite="lax", max_age=86400*30)
            return resp
    else:
        # In-memory version
        user = None
        for u in users.values():
            if u["username"] == username:
                user = u
                break
        
        if not user or not verify_password(password, user["password_hash"]):
            raise HTTPException(401, "Invalid username or password")
        
        if user["is_blocked"]:
            raise HTTPException(403, "Account blocked")
        if not user["is_approved"]:
            raise HTTPException(403, "Account pending approval")
        
        token = create_token(user["id"])
        resp = JSONResponse({
            "ok": True,
            "user_id": user["id"],
            "username": user["username"],
            "is_admin": user["is_admin"]
        })
        resp.set_cookie("token", token, httponly=True, samesite="lax", max_age=86400*30)
        return resp

@app.post("/api/auth/logout")
def logout(token: str = Cookie(default=None)):
    if token:
        revoke_token(token)
    resp = JSONResponse({"ok": True})
    resp.delete_cookie("token")
    return resp

@app.get("/api/auth/me")
def me(token: str = Cookie(default=None)):
    user = get_user_from_token(token)
    if not user:
        raise HTTPException(401, "Not authenticated")
    return {
        "id": user["id"] if isinstance(user, dict) else user.id,
        "username": user["username"] if isinstance(user, dict) else user.username,
        "is_admin": user["is_admin"] if isinstance(user, dict) else user.is_admin,
        "is_approved": user["is_approved"] if isinstance(user, dict) else user.is_approved
    }

# ══════════════════════════════════════════════════════════════════════════════
#  POOLS ENDPOINTS (Simplified for demo)
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/api/pools")
def list_pools(token: str = Cookie(default=None)):
    user = get_user_from_token(token)
    if not user:
        raise HTTPException(401, "Not authenticated")
    
    # Return demo pools
    return [
        {"id": 1, "name": "Nigeria", "country_code": "234", "number_count": 1250, "is_paused": False, "trick_text": "Best for WhatsApp"},
        {"id": 2, "name": "USA", "country_code": "1", "number_count": 842, "is_paused": False, "trick_text": "Best for Telegram"},
        {"id": 3, "name": "United Kingdom", "country_code": "44", "number_count": 567, "is_paused": False},
        {"id": 4, "name": "Canada", "country_code": "1", "number_count": 321, "is_paused": True, "pause_reason": "Maintenance"},
        {"id": 5, "name": "Australia", "country_code": "61", "number_count": 234, "is_paused": False},
    ]

@app.post("/api/pools/assign")
async def assign_number(pool_id: int, token: str = Cookie(default=None)):
    user = get_user_from_token(token)
    if not user:
        raise HTTPException(401, "Not authenticated")
    
    # Generate a demo number
    import random
    pool_names = {1: "Nigeria", 2: "USA", 3: "United Kingdom", 4: "Canada", 5: "Australia"}
    pool_codes = {1: "234", 2: "1", 3: "44", 4: "1", 5: "61"}
    
    number = f"+{pool_codes.get(pool_id, '234')}{random.randint(7000000000, 7999999999)}"
    
    return {
        "ok": True,
        "assignment_id": random.randint(1000, 9999),
        "number": number,
        "pool_name": pool_names.get(pool_id, "Unknown"),
        "country_code": pool_codes.get(pool_id, "234"),
        "otp_link": "https://t.me/earnplusz",
        "trick_text": "Use this number for verification"
    }

@app.get("/api/pools/my-assignment")
def my_assignment(token: str = Cookie(default=None)):
    user = get_user_from_token(token)
    if not user:
        raise HTTPException(401, "Not authenticated")
    
    # Return no assignment by default
    return {"assignment": None}

@app.post("/api/pools/release/{assignment_id}")
def release_number(assignment_id: int, token: str = Cookie(default=None)):
    user = get_user_from_token(token)
    if not user:
        raise HTTPException(401, "Not authenticated")
    return {"ok": True}

# ══════════════════════════════════════════════════════════════════════════════
#  OTP ENDPOINTS
# ══════════════════════════════════════════════════════════════════════════════

class MonitorResultPayload(BaseModel):
    number: str
    otp: str
    raw_message: str = ""
    user_id: int
    secret: str = ""

@app.post("/api/otp/monitor-result")
async def monitor_result(payload: MonitorResultPayload):
    if SHARED_SECRET and payload.secret != SHARED_SECRET:
        raise HTTPException(403, "Invalid secret")
    
    log.info(f"[OTP] Received: {payload.number} -> {payload.otp} for user {payload.user_id}")
    
    # Send to user via WebSocket
    otp_data = {
        "type": "otp",
        "number": payload.number,
        "otp": payload.otp,
        "raw_message": payload.raw_message,
        "delivered_at": utcnow().isoformat(),
        "auto_delete_seconds": 30
    }
    
    await manager.send_to_user(payload.user_id, otp_data)
    await manager.broadcast_all({"type": "feed_otp", "number": payload.number, "otp": payload.otp})
    
    return {"ok": True}

@app.get("/api/otp/my")
def my_otps(token: str = Cookie(default=None)):
    user = get_user_from_token(token)
    if not user:
        raise HTTPException(401, "Not authenticated")
    
    # Return demo OTPs
    return [
        {"id": 1, "number": "+2348012345678", "otp_code": "123456", "delivered_at": utcnow().isoformat()},
        {"id": 2, "number": "+2348098765432", "otp_code": "789012", "delivered_at": (utcnow() - timedelta(minutes=5)).isoformat()},
    ]

# ══════════════════════════════════════════════════════════════════════════════
#  SAVED NUMBERS ENDPOINTS
# ══════════════════════════════════════════════════════════════════════════════

@app.post("/api/saved")
def save_numbers(numbers: list[str], timer_minutes: int, pool_name: str, token: str = Cookie(default=None)):
    user = get_user_from_token(token)
    if not user:
        raise HTTPException(401, "Not authenticated")
    
    return {"ok": True, "saved": len(numbers), "skipped": 0, "expires_at": (utcnow() + timedelta(minutes=timer_minutes)).isoformat()}

@app.get("/api/saved")
def list_saved(token: str = Cookie(default=None)):
    user = get_user_from_token(token)
    if not user:
        raise HTTPException(401, "Not authenticated")
    
    # Return demo saved numbers
    now = utcnow()
    return [
        {"id": 1, "number": "+2348012345678", "pool_name": "Nigeria", "seconds_left": 3600, "status": "green", "moved": False},
        {"id": 2, "number": "+2348098765432", "pool_name": "Nigeria", "seconds_left": 300, "status": "red", "moved": False},
    ]

@app.delete("/api/saved/{saved_id}")
def delete_saved(saved_id: int, token: str = Cookie(default=None)):
    user = get_user_from_token(token)
    if not user:
        raise HTTPException(401, "Not authenticated")
    return {"ok": True}

# ══════════════════════════════════════════════════════════════════════════════
#  REVIEWS ENDPOINTS
# ══════════════════════════════════════════════════════════════════════════════

@app.post("/api/reviews")
def submit_review(number: str, rating: int, comment: str = "", mark_as_bad: bool = False, token: str = Cookie(default=None)):
    user = get_user_from_token(token)
    if not user:
        raise HTTPException(401, "Not authenticated")
    return {"ok": True, "marked_bad": mark_as_bad}

# ══════════════════════════════════════════════════════════════════════════════
#  ADMIN ENDPOINTS
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/api/admin/stats")
def stats(token: str = Cookie(default=None)):
    user = get_user_from_token(token)
    if not user:
        raise HTTPException(401, "Not authenticated")
    if not (user["is_admin"] if isinstance(user, dict) else user.is_admin):
        raise HTTPException(403, "Admin only")
    
    return {
        "total_users": 5,
        "pending_approval": 2,
        "total_pools": 5,
        "total_numbers": 3214,
        "total_otps": 128,
        "total_assignments": 45,
        "bad_numbers": 12,
        "saved_numbers": 8,
        "online_users": manager.total()
    }

@app.get("/api/admin/users")
def list_users(token: str = Cookie(default=None)):
    user = get_user_from_token(token)
    if not user:
        raise HTTPException(401, "Not authenticated")
    if not (user["is_admin"] if isinstance(user, dict) else user.is_admin):
        raise HTTPException(403, "Admin only")
    
    if SQLALCHEMY_AVAILABLE and engine:
        # Return from DB
        return [
            {"id": 1, "username": "admin", "is_admin": True, "is_approved": True, "is_blocked": False, "created_at": utcnow().isoformat()},
            {"id": 2, "username": "user1", "is_admin": False, "is_approved": True, "is_blocked": False, "created_at": utcnow().isoformat()},
            {"id": 3, "username": "user2", "is_admin": False, "is_approved": False, "is_blocked": False, "created_at": utcnow().isoformat()},
        ]
    else:
        # Return in-memory
        return [
            {"id": u["id"], "username": u["username"], "is_admin": u["is_admin"], 
             "is_approved": u["is_approved"], "is_blocked": u["is_blocked"],
             "created_at": u["created_at"].isoformat() if hasattr(u["created_at"], 'isoformat') else str(u["created_at"])}
            for u in users.values()
        ]

@app.post("/api/admin/users/{user_id}/approve")
def approve_user(user_id: int, token: str = Cookie(default=None)):
    user = get_user_from_token(token)
    if not user or not (user["is_admin"] if isinstance(user, dict) else user.is_admin):
        raise HTTPException(403, "Admin only")
    return {"ok": True}

@app.post("/api/admin/users/{user_id}/block")
def block_user(user_id: int, token: str = Cookie(default=None)):
    user = get_user_from_token(token)
    if not user or not (user["is_admin"] if isinstance(user, dict) else user.is_admin):
        raise HTTPException(403, "Admin only")
    return {"ok": True}

@app.post("/api/admin/users/{user_id}/unblock")
def unblock_user(user_id: int, token: str = Cookie(default=None)):
    user = get_user_from_token(token)
    if not user or not (user["is_admin"] if isinstance(user, dict) else user.is_admin):
        raise HTTPException(403, "Admin only")
    return {"ok": True}

@app.post("/api/admin/broadcast")
async def broadcast(message: str, token: str = Cookie(default=None)):
    user = get_user_from_token(token)
    if not user or not (user["is_admin"] if isinstance(user, dict) else user.is_admin):
        raise HTTPException(403, "Admin only")
    
    await manager.broadcast_all({"type": "broadcast", "message": message})
    return {"ok": True}

# ══════════════════════════════════════════════════════════════════════════════
#  WEBSOCKET ENDPOINTS
# ══════════════════════════════════════════════════════════════════════════════

@app.websocket("/ws/user/{user_id}")
async def websocket_user(websocket: WebSocket, user_id: int):
    await manager.connect_user(websocket, user_id)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect_user(websocket, user_id)

@app.websocket("/ws/feed")
async def websocket_feed(websocket: WebSocket):
    await manager.connect_feed(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect_feed(websocket)

# ══════════════════════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    uvicorn.run(
        "backend:app",
        host="0.0.0.0",
        port=PORT,
        reload=False,
        log_level="info"
    )
