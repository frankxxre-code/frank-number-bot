# -*- coding: utf-8 -*-
"""
NEON GRID NETWORK — COMPLETE PLATFORM BACKEND
=================================================
All features from numberbot.py converted to web platform
- Pool management with match formats
- Number assignments with prefix filter
- OTP monitoring via monitor bot
- Saved numbers with timer expiry (supports 30m, 2h, 2s, 1d)
- Bad numbers tracking
- User reviews and feedback
- Admin panel with user management
- Pool access control (restricted pools)
- Broadcast messaging
- Platform stock watcher
- Custom dashboard buttons
- Pause/Resume pools with reason
- Admin-only pools
- Trick text per pool
- Multiple monitoring modes (0=Telegram,1=Platform,2=Both)
- Compliance tracking
- OTP auto-delete after 30 seconds
- WebSocket real-time OTP delivery
- PostgreSQL persistence
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
import csv
import random
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Any
from collections import defaultdict

import aiohttp
import uvicorn
from fastapi import (FastAPI, WebSocket, WebSocketDisconnect, Request,
                     Depends, HTTPException, Cookie, Header,
                     UploadFile, File, BackgroundTasks, Form, Query)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, HTMLResponse
from pydantic import BaseModel

# SQLAlchemy for PostgreSQL
try:
    from sqlalchemy import (create_engine, Column, Integer, String, Boolean, DateTime,
                            Text, ForeignKey, BigInteger, UniqueConstraint, select, func, text)
    from sqlalchemy.ext.declarative import declarative_base
    from sqlalchemy.orm import sessionmaker, Session, relationship
    from sqlalchemy.pool import QueuePool
    SQLALCHEMY_AVAILABLE = True
except ImportError:
    SQLALCHEMY_AVAILABLE = False

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
log = logging.getLogger("frank")

# ══════════════════════════════════════════════════════════════════════════════
#  CONFIGURATION
# ══════════════════════════════════════════════════════════════════════════════

DATABASE_URL = os.environ.get("DATABASE_URL", "")
SHARED_SECRET = os.environ.get("SHARED_SECRET", "MonitorSecret2024")
MONITOR_BOT_URL = os.environ.get("MONITOR_BOT_URL", "").rstrip("/")
FRONTEND_URL = os.environ.get("FRONTEND_URL", "*")
PORT = int(os.environ.get("PORT", "8080"))

# Fix Railway Postgres URL
if DATABASE_URL and DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

# Platform config
PLATFORM_URL = os.environ.get("PLATFORM_URL", "http://198.135.52.238")
PLATFORM_USERNAME = os.environ.get("PLATFORM_USERNAME", "Frankhustle")
PLATFORM_PASSWORD = os.environ.get("PLATFORM_PASSWORD", "f11111")

# Monitoring config
PLATFORM_MONITOR_TTL = 600
PLATFORM_POLL_INTERVAL = 5
PLATFORM_STOCK_INTERVAL = 300
OTP_AUTO_DELETE_DELAY = 30

# Global state (always initialized, used by both DB and memory modes)
bot_settings = {"approval_mode": "on", "otp_redirect_mode": "pool"}
_compliance_counters: Dict[int, int] = {}

# Default values
DEFAULT_LOCAL_PREFIX = "+234"
OTP_CHANNEL_LINK = "https://t.me/earnplusz"
NUMBER_CHANNEL_LINK = "https://t.me/Finalsearchbot"
HARDCODED_OTP_GROUP = "https://t.me/earnplusz"

# Monitoring modes
MONITOR_TELEGRAM = 0
MONITOR_PLATFORM = 1
MONITOR_BOTH = 2

# Compliance
COMPLIANCE_BRIDGE_GROUP_ID = -1003817159179
COMPLIANCE_CODES_PER_CHECK = 5

# Cooldown check URL
COOLDOWN_CHECK_URL = "http://127.0.0.1:8003/check_numbers_exist"

log.info(f"Starting NEON GRID NETWORK on port {PORT}")
log.info(f"Database: {'PostgreSQL' if DATABASE_URL else 'Memory (fallback)'}")
log.info(f"Monitor Bot URL: {MONITOR_BOT_URL or 'NOT SET'}")

# ══════════════════════════════════════════════════════════════════════════════
#  DATABASE SETUP
# ══════════════════════════════════════════════════════════════════════════════

if SQLALCHEMY_AVAILABLE and DATABASE_URL:
    engine = create_engine(
        DATABASE_URL,
        poolclass=QueuePool,
        pool_size=10,
        max_overflow=20,
        pool_pre_ping=True,
        echo=False
    )
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base = declarative_base()
else:
    engine = SessionLocal = None
    Base = None
    log.warning("No DATABASE_URL, running in memory-only mode")

# ══════════════════════════════════════════════════════════════════════════════
#  DATABASE MODELS
# ══════════════════════════════════════════════════════════════════════════════

if Base:
    class User(Base):
        __tablename__ = "users"
        id = Column(Integer, primary_key=True, index=True)
        username = Column(String(64), unique=True, nullable=False, index=True)
        password_hash = Column(String(256), nullable=False)
        is_admin = Column(Boolean, default=False)
        is_approved = Column(Boolean, default=False)
        is_blocked = Column(Boolean, default=False)
        created_at = Column(DateTime(timezone=True), default=datetime.now(timezone.utc))

    class UserSession(Base):
        __tablename__ = "user_sessions"
        id = Column(Integer, primary_key=True, index=True)
        user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
        token = Column(String(256), unique=True, nullable=False, index=True)
        expires_at = Column(DateTime(timezone=True), nullable=False)
        created_at = Column(DateTime(timezone=True), default=datetime.now(timezone.utc))

    class Pool(Base):
        __tablename__ = "pools"
        id = Column(Integer, primary_key=True, index=True)
        name = Column(String(128), unique=True, nullable=False)
        country_code = Column(String(10), nullable=False)
        otp_group_id = Column(BigInteger, nullable=True)
        otp_link = Column(String(256), default="")
        match_format = Column(String(32), default="5+4")
        telegram_match_format = Column(String(32), default="")
        uses_platform = Column(Integer, default=0)
        is_paused = Column(Boolean, default=False)
        pause_reason = Column(Text, default="")
        trick_text = Column(Text, default="")
        is_admin_only = Column(Boolean, default=False)
        last_restocked = Column(DateTime(timezone=True), nullable=True)
        created_at = Column(DateTime(timezone=True), default=datetime.now(timezone.utc))
        
        numbers = relationship("ActiveNumber", back_populates="pool", cascade="all, delete")
        assignments = relationship("Assignment", back_populates="pool")
        access_list = relationship("PoolAccess", back_populates="pool", cascade="all, delete")

    class ActiveNumber(Base):
        __tablename__ = "active_numbers"
        id = Column(Integer, primary_key=True, index=True)
        pool_id = Column(Integer, ForeignKey("pools.id"), nullable=False)
        number = Column(String(32), unique=True, nullable=False, index=True)
        created_at = Column(DateTime(timezone=True), default=datetime.now(timezone.utc))
        pool = relationship("Pool", back_populates="numbers")

    class Assignment(Base):
        __tablename__ = "assignments"
        id = Column(Integer, primary_key=True, index=True)
        user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
        pool_id = Column(Integer, ForeignKey("pools.id"), nullable=False)
        number = Column(String(32), nullable=False, index=True)
        assigned_at = Column(DateTime(timezone=True), default=datetime.now(timezone.utc))
        released_at = Column(DateTime(timezone=True), nullable=True)
        feedback = Column(String(64), default="")
        user = relationship("User")
        pool = relationship("Pool", back_populates="assignments")
        otps = relationship("OTPLog", back_populates="assignment", cascade="all, delete")

    class OTPLog(Base):
        __tablename__ = "otp_logs"
        id = Column(Integer, primary_key=True, index=True)
        assignment_id = Column(Integer, ForeignKey("assignments.id"), nullable=True)
        user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
        number = Column(String(32), nullable=False)
        otp_code = Column(String(32), nullable=False)
        raw_message = Column(Text, default="")
        delivered_at = Column(DateTime(timezone=True), default=datetime.now(timezone.utc))
        assignment = relationship("Assignment", back_populates="otps")

    class SavedNumber(Base):
        __tablename__ = "saved_numbers"
        id = Column(Integer, primary_key=True, index=True)
        user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
        number = Column(String(32), nullable=False)
        country = Column(String(64), default="")
        pool_name = Column(String(128), nullable=False)
        expires_at = Column(DateTime(timezone=True), nullable=False)
        moved = Column(Boolean, default=False)
        created_at = Column(DateTime(timezone=True), default=datetime.now(timezone.utc))
        user = relationship("User")

    class NumberReview(Base):
        __tablename__ = "number_reviews"
        id = Column(Integer, primary_key=True, index=True)
        user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
        number = Column(String(32), nullable=False)
        rating = Column(Integer, default=5)
        comment = Column(Text, default="")
        created_at = Column(DateTime(timezone=True), default=datetime.now(timezone.utc))
        user = relationship("User")

    class BadNumber(Base):
        __tablename__ = "bad_numbers"
        id = Column(Integer, primary_key=True, index=True)
        number = Column(String(32), unique=True, nullable=False, index=True)
        reason = Column(String(256), default="not available")
        flagged_by = Column(Integer, ForeignKey("users.id"), nullable=True)
        created_at = Column(DateTime(timezone=True), default=datetime.now(timezone.utc))

    class CustomButton(Base):
        __tablename__ = "custom_buttons"
        id = Column(Integer, primary_key=True, index=True)
        label = Column(String(128), nullable=False)
        url = Column(String(512), nullable=False)
        position = Column(Integer, default=0)

    class PoolAccess(Base):
        __tablename__ = "pool_access"
        __table_args__ = (UniqueConstraint("pool_id", "user_id"),)
        id = Column(Integer, primary_key=True, index=True)
        pool_id = Column(Integer, ForeignKey("pools.id"), nullable=False)
        user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
        granted_at = Column(DateTime(timezone=True), default=datetime.now(timezone.utc))
        pool = relationship("Pool", back_populates="access_list")
        user = relationship("User")

    def init_db():
        if engine:
            with engine.connect() as conn:
                try:
                    conn.execute(text("ALTER TABLE users DROP COLUMN IF EXISTS email"))
                    conn.commit()
                    log.info("Dropped email column if it existed")
                except Exception as e:
                    log.info(f"Email column drop not needed: {e}")
            
            Base.metadata.create_all(bind=engine)
            log.info("Database tables created")

            with SessionLocal() as db:
                if db.query(User).count() == 0:
                    admin = User(
                        username="admin",
                        password_hash=hash_password("admin123"),
                        is_admin=True,
                        is_approved=True
                    )
                    db.add(admin)
                    db.commit()
                    log.info("Default admin created: admin / admin123")
                else:
                    # Force-fix: ensure ALL admin users are approved (direct SQL, guaranteed)
                    result = db.execute(text(
                        "UPDATE users SET is_approved = TRUE WHERE is_admin = TRUE AND (is_approved = FALSE OR is_approved IS NULL)"
                    ))
                    if result.rowcount > 0:
                        db.commit()
                        log.info(f"[init_db] Fixed {result.rowcount} admin(s) to is_approved=TRUE")

                if db.query(Pool).count() == 0:
                    default_pools = [
                        {"name": "Nigeria", "country_code": "234", "number_count": 1250, "otp_group_id": -1003388744078, "trick_text": "Best for WhatsApp"},
                        {"name": "USA", "country_code": "1", "number_count": 842, "otp_group_id": -1003388744078, "trick_text": "Best for Telegram"},
                        {"name": "United Kingdom", "country_code": "44", "number_count": 567, "otp_group_id": -1003388744078},
                        {"name": "Canada", "country_code": "1", "number_count": 321, "otp_group_id": -1003388744078},
                        {"name": "Australia", "country_code": "61", "number_count": 234, "otp_group_id": -1003388744078},
                    ]
                    for p in default_pools:
                        pool = Pool(
                            name=p["name"],
                            country_code=p["country_code"],
                            otp_group_id=p["otp_group_id"],
                            otp_link="https://t.me/earnplusz",
                            match_format="5+4",
                            trick_text=p.get("trick_text", "")
                        )
                        db.add(pool)
                        db.flush()
                        for _ in range(p["number_count"]):
                            num = f"+{p['country_code']}{random.randint(7000000000, 7999999999)}"
                            db.add(ActiveNumber(pool_id=pool.id, number=num))
                    db.commit()
                    log.info(f"Created {len(default_pools)} default pools")

                if db.query(CustomButton).count() == 0:
                    db.add_all([
                        CustomButton(label="📢 Join Channel", url="https://t.me/earnplusz", position=0),
                        CustomButton(label="📱 Number Channel", url="https://t.me/Finalsearchbot", position=1),
                    ])
                    db.commit()
else:
    def init_db():
        log.warning("Using in-memory fallback")
    
    # In-memory storage
    users = {1: {"id": 1, "username": "admin", "password_hash": hash_password("admin123"), "is_admin": True, "is_approved": True, "is_blocked": False}}
    sessions = {}
    pools = {
        1: {"id": 1, "name": "Nigeria", "country_code": "234", "otp_group_id": -1003388744078, "otp_link": "https://t.me/earnplusz", "match_format": "5+4", "telegram_match_format": "", "uses_platform": 0, "is_paused": False, "pause_reason": "", "trick_text": "Best for WhatsApp", "is_admin_only": False, "last_restocked": None},
        2: {"id": 2, "name": "USA", "country_code": "1", "otp_group_id": -1003388744078, "otp_link": "https://t.me/earnplusz", "match_format": "5+4", "telegram_match_format": "", "uses_platform": 0, "is_paused": False, "pause_reason": "", "trick_text": "Best for Telegram", "is_admin_only": False, "last_restocked": None},
        3: {"id": 3, "name": "United Kingdom", "country_code": "44", "otp_group_id": -1003388744078, "otp_link": "https://t.me/earnplusz", "match_format": "5+4", "telegram_match_format": "", "uses_platform": 0, "is_paused": False, "pause_reason": "", "trick_text": "", "is_admin_only": False, "last_restocked": None},
        4: {"id": 4, "name": "Canada", "country_code": "1", "otp_group_id": -1003388744078, "otp_link": "https://t.me/earnplusz", "match_format": "5+4", "telegram_match_format": "", "uses_platform": 0, "is_paused": False, "pause_reason": "", "trick_text": "", "is_admin_only": False, "last_restocked": None},
        5: {"id": 5, "name": "Australia", "country_code": "61", "otp_group_id": -1003388744078, "otp_link": "https://t.me/earnplusz", "match_format": "5+4", "telegram_match_format": "", "uses_platform": 0, "is_paused": False, "pause_reason": "", "trick_text": "", "is_admin_only": False, "last_restocked": None},
    }
    active_numbers = {
        1: [f"+234{random.randint(7000000000, 7999999999)}" for _ in range(1250)],
        2: [f"+1{random.randint(7000000000, 7999999999)}" for _ in range(842)],
        3: [f"+44{random.randint(7000000000, 7999999999)}" for _ in range(567)],
        4: [f"+1{random.randint(7000000000, 7999999999)}" for _ in range(321)],
        5: [f"+61{random.randint(7000000000, 7999999999)}" for _ in range(234)],
    }
    archived_numbers = []
    otp_logs = []
    saved_numbers = []
    reviews = []
    bad_numbers = {}
    custom_buttons = [
        {"id": 1, "label": "📢 Join Channel", "url": "https://t.me/earnplusz", "position": 0},
        {"id": 2, "label": "📱 Number Channel", "url": "https://t.me/Finalsearchbot", "position": 1},
    ]
    user_connections = {}
    feed_connections = []
    pool_access = {}
    uploaded_numbers = set()
    feedbacks = []
    _counters = {"user": 6, "pool": 6, "assignment": 1, "otp": 1, "saved": 1, "review": 1, "feedback": 1, "button": 3}

# ══════════════════════════════════════════════════════════════════════════════
#  UTILITY FUNCTIONS
# ══════════════════════════════════════════════════════════════════════════════

def utcnow():
    return datetime.now(timezone.utc)

def hash_password(p: str) -> str:
    salt = os.urandom(16).hex()
    h = hashlib.sha256((salt + p).encode()).hexdigest()
    return f"{salt}:{h}"

def verify_password(p: str, hashed: str) -> bool:
    try:
        salt, h = hashed.split(":", 1)
        return hashlib.sha256((salt + p).encode()).hexdigest() == h
    except:
        return False

def create_token(user_id: int) -> str:
    token = secrets.token_urlsafe(48)
    expires = utcnow() + timedelta(days=30)
    
    if SessionLocal:
        with SessionLocal() as db:
            session = UserSession(user_id=user_id, token=token, expires_at=expires)
            db.add(session)
            db.commit()
    else:
        sessions[token] = user_id
    return token

def revoke_token(token: str):
    if SessionLocal:
        with SessionLocal() as db:
            db.query(UserSession).filter(UserSession.token == token).delete()
            db.commit()
    else:
        sessions.pop(token, None)

def get_user_from_token(token: str):
    if not token:
        return None
    # Strip "Bearer " prefix if sent as Authorization header value
    if token.startswith("Bearer "):
        token = token[7:]
    if not token:
        return None

    if SessionLocal:
        with SessionLocal() as db:
            user_session = db.query(UserSession).filter(
                UserSession.token == token,
                UserSession.expires_at > utcnow()
            ).first()
            if user_session:
                user = db.query(User).filter(User.id == user_session.user_id).first()
                if user and not user.is_blocked:
                    return {"id": user.id, "username": user.username, "is_admin": user.is_admin, "is_approved": user.is_approved}
    else:
        if token in sessions:
            user_id = sessions[token]
            user = users.get(user_id)
            if user and not user.get("is_blocked"):
                return user
    return None

def get_token(request: Request) -> str:
    """Read token from cookie OR Authorization header — whichever is present."""
    auth_header = request.headers.get("authorization", "")
    if auth_header.startswith("Bearer "):
        return auth_header[7:]
    return request.cookies.get("token", "")

def is_admin(user_id: int) -> bool:
    if SessionLocal:
        with SessionLocal() as db:
            user = db.query(User).filter(User.id == user_id).first()
            return user.is_admin if user else False
    else:
        user = users.get(user_id)
        return user.get("is_admin", False) if user else False

def is_blocked(user_id: int) -> bool:
    if SessionLocal:
        with SessionLocal() as db:
            user = db.query(User).filter(User.id == user_id).first()
            return user.is_blocked if user else False
    else:
        user = users.get(user_id)
        return user.get("is_blocked", False) if user else False

def is_approved(user_id: int) -> bool:
    if SessionLocal:
        with SessionLocal() as db:
            user = db.query(User).filter(User.id == user_id).first()
            return user.is_approved if user else False
    else:
        user = users.get(user_id)
        return user.get("is_approved", False) if user else False

def block_user(user_id: int):
    if SessionLocal:
        with SessionLocal() as db:
            db.query(User).filter(User.id == user_id).update({"is_blocked": True, "is_approved": False})
            db.commit()
    else:
        if user_id in users:
            users[user_id]["is_blocked"] = True
            users[user_id]["is_approved"] = False

def unblock_user(user_id: int):
    if SessionLocal:
        with SessionLocal() as db:
            db.query(User).filter(User.id == user_id).update({"is_blocked": False, "is_approved": True})
            db.commit()
    else:
        if user_id in users:
            users[user_id]["is_blocked"] = False
            users[user_id]["is_approved"] = True

def approve_user(user_id: int):
    if SessionLocal:
        with SessionLocal() as db:
            db.query(User).filter(User.id == user_id).update({"is_approved": True, "is_blocked": False})
            db.commit()
    else:
        if user_id in users:
            users[user_id]["is_approved"] = True
            users[user_id]["is_blocked"] = False

def deny_user(user_id: int):
    if SessionLocal:
        with SessionLocal() as db:
            db.query(User).filter(User.id == user_id).update({"is_approved": False, "is_blocked": False})
            db.commit()
    else:
        if user_id in users:
            users[user_id]["is_approved"] = False
            users[user_id]["is_blocked"] = False

def can_use_bot(user_id: int) -> bool:
    if is_admin(user_id):
        return True
    if is_blocked(user_id):
        return False
    if bot_settings.get("approval_mode") == "on" and not is_approved(user_id):
        return False
    return True

def get_approval_mode() -> bool:
    return bot_settings.get("approval_mode") == "on"

def set_approval_mode(enabled: bool):
    bot_settings["approval_mode"] = "on" if enabled else "off"

def get_otp_redirect_mode() -> str:
    return bot_settings.get("otp_redirect_mode", "pool")

def set_otp_redirect_mode(mode: str):
    bot_settings["otp_redirect_mode"] = mode

def resolve_otp_url(pool_otp_link: str) -> str:
    if get_otp_redirect_mode() == "hardcoded":
        return HARDCODED_OTP_GROUP
    return pool_otp_link or "https://t.me/frank_otp_forwardeer"

def has_pool_access(pool_id: int, user_id: int) -> bool:
    if is_admin(user_id):
        return True
    if SessionLocal:
        with SessionLocal() as db:
            count = db.query(PoolAccess).filter(PoolAccess.pool_id == pool_id).count()
            if count == 0:
                return True
            return db.query(PoolAccess).filter(
                PoolAccess.pool_id == pool_id,
                PoolAccess.user_id == user_id
            ).first() is not None
    else:
        restricted = pool_access.get(pool_id, set())
        if not restricted:
            return True
        return user_id in restricted

def grant_pool_access(pool_id: int, user_id: int):
    if SessionLocal:
        with SessionLocal() as db:
            existing = db.query(PoolAccess).filter(
                PoolAccess.pool_id == pool_id,
                PoolAccess.user_id == user_id
            ).first()
            if not existing:
                db.add(PoolAccess(pool_id=pool_id, user_id=user_id))
                db.commit()
    else:
        pool_access.setdefault(pool_id, set()).add(user_id)

def revoke_pool_access(pool_id: int, user_id: int):
    if SessionLocal:
        with SessionLocal() as db:
            db.query(PoolAccess).filter(
                PoolAccess.pool_id == pool_id,
                PoolAccess.user_id == user_id
            ).delete()
            db.commit()
    else:
        if pool_id in pool_access:
            pool_access[pool_id].discard(user_id)

def get_pool_access_users(pool_id: int) -> List[Dict]:
    result = []
    if SessionLocal:
        with SessionLocal() as db:
            accesses = db.query(PoolAccess).filter(PoolAccess.pool_id == pool_id).all()
            for a in accesses:
                user = db.query(User).filter(User.id == a.user_id).first()
                if user:
                    result.append({
                        "user_id": a.user_id,
                        "username": user.username,
                        "first_name": user.username
                    })
    else:
        for uid in pool_access.get(pool_id, set()):
            u = users.get(uid)
            if u:
                result.append({
                    "user_id": uid,
                    "username": u.get("username", ""),
                    "first_name": u.get("username", "")
                })
    return result

def pool_is_restricted(pool_id: int) -> bool:
    if SessionLocal:
        with SessionLocal() as db:
            return db.query(PoolAccess).filter(PoolAccess.pool_id == pool_id).count() > 0
    else:
        return len(pool_access.get(pool_id, set())) > 0

def normalize_number(raw: str) -> str:
    s = re.sub(r'[^\d+]', '', raw)
    if s.startswith('+'):
        return s
    if s.startswith('0'):
        return DEFAULT_LOCAL_PREFIX + s[1:]
    return '+' + s

def get_remaining_count(pool_id: int) -> int:
    if SessionLocal:
        with SessionLocal() as db:
            return db.query(ActiveNumber).filter(ActiveNumber.pool_id == pool_id).count()
    else:
        return len(active_numbers.get(pool_id, []))

def add_bad_number(number: str, marked_by: int, reason: str = "marked as bad"):
    if SessionLocal:
        with SessionLocal() as db:
            existing = db.query(BadNumber).filter(BadNumber.number == number).first()
            if not existing:
                db.add(BadNumber(number=number, reason=reason, flagged_by=marked_by))
            db.query(ActiveNumber).filter(ActiveNumber.number == number).delete()
            db.commit()
    else:
        bad_numbers[number] = {
            "number": number,
            "reason": reason,
            "marked_by": marked_by,
            "marked_at": utcnow().isoformat()
        }
        for pid, numbers in active_numbers.items():
            if number in numbers:
                numbers.remove(number)

def save_feedback(number: str, user_id: int, feedback: str):
    if SessionLocal:
        with SessionLocal() as db:
            db.execute(
                text("INSERT INTO feedbacks (number, user_id, feedback, created_at) VALUES (:number, :user_id, :feedback, :created_at)"),
                {"number": number, "user_id": user_id, "feedback": feedback, "created_at": utcnow().isoformat()}
            )
            db.commit()
    else:
        feedbacks.append({
            "id": _counters["feedback"],
            "number": number,
            "user_id": user_id,
            "feedback": feedback,
            "created_at": utcnow().isoformat()
        })
        _counters["feedback"] += 1

def assign_one_number(user_id: int, pool_id: int, prefix: Optional[str] = None) -> Optional[Dict]:
    if SessionLocal:
        with SessionLocal() as db:
            query = db.query(ActiveNumber).filter(ActiveNumber.pool_id == pool_id)
            if prefix:
                query = query.filter(ActiveNumber.number.like(f"+{prefix}%"))
            number_row = query.order_by(ActiveNumber.id.desc()).first()
            if not number_row:
                return None
            
            number = number_row.number
            db.delete(number_row)
            
            pool = db.query(Pool).filter(Pool.id == pool_id).first()
            
            assignment = Assignment(
                user_id=user_id,
                pool_id=pool_id,
                number=number
            )
            db.add(assignment)
            db.commit()
            db.refresh(assignment)
            
            return {
                "assignment_id": assignment.id,
                "number": number,
                "pool_name": pool.name if pool else "Unknown",
                "pool_code": pool.country_code if pool else "",
                "otp_link": pool.otp_link if pool else "",
                "otp_group_id": pool.otp_group_id if pool else None,
                "uses_platform": pool.uses_platform if pool else 0,
                "match_format": pool.match_format if pool else "5+4",
                "telegram_match_format": pool.telegram_match_format if pool else "",
                "trick_text": pool.trick_text if pool else "",
                "pool_id": pool_id
            }
    else:
        numbers = active_numbers.get(pool_id, [])
        if not numbers:
            return None
        
        selected = None
        if prefix:
            for num in numbers:
                if num.startswith(f"+{prefix}"):
                    selected = num
                    break
        if not selected and numbers:
            selected = numbers[-1]
        
        if not selected:
            return None
        
        numbers.remove(selected)
        active_numbers[pool_id] = numbers
        
        pool = pools.get(pool_id, {})
        assignment = {
            "id": _counters["assignment"],
            "user_id": user_id,
            "pool_id": pool_id,
            "number": selected,
            "assigned_at": utcnow().isoformat(),
            "released_at": None,
            "feedback": ""
        }
        archived_numbers.append(assignment)
        _counters["assignment"] += 1
        
        return {
            "assignment_id": assignment["id"],
            "number": selected,
            "pool_name": pool.get("name", "Unknown"),
            "pool_code": pool.get("country_code", ""),
            "otp_link": pool.get("otp_link", ""),
            "otp_group_id": pool.get("otp_group_id"),
            "uses_platform": pool.get("uses_platform", 0),
            "match_format": pool.get("match_format", "5+4"),
            "telegram_match_format": pool.get("telegram_match_format", ""),
            "trick_text": pool.get("trick_text", ""),
            "pool_id": pool_id
        }

def release_assignment(user_id: int, assignment_id: int = None):
    if SessionLocal:
        with SessionLocal() as db:
            query = db.query(Assignment).filter(
                Assignment.user_id == user_id,
                Assignment.released_at == None
            )
            if assignment_id:
                query = query.filter(Assignment.id == assignment_id)
            query.update({"released_at": utcnow()})
            db.commit()
    else:
        for a in archived_numbers:
            if a["user_id"] == user_id and a.get("released_at") is None:
                if assignment_id is None or a["id"] == assignment_id:
                    a["released_at"] = utcnow().isoformat()
                    return a

def get_current_assignment(user_id: int) -> Optional[Dict]:
    if SessionLocal:
        with SessionLocal() as db:
            assignment = db.query(Assignment).filter(
                Assignment.user_id == user_id,
                Assignment.released_at == None
            ).order_by(Assignment.assigned_at.desc()).first()
            if assignment:
                pool = db.query(Pool).filter(Pool.id == assignment.pool_id).first()
                return {
                    "assignment_id": assignment.id,
                    "number": assignment.number,
                    "pool_name": pool.name if pool else "Unknown",
                    "pool_id": assignment.pool_id,
                    "country_code": pool.country_code if pool else "",
                    "otp_link": pool.otp_link if pool else "",
                    "otp_group_id": pool.otp_group_id if pool else None,
                    "trick_text": pool.trick_text if pool else "",
                    "match_format": pool.match_format if pool else "5+4",
                    "telegram_match_format": pool.telegram_match_format if pool else ""
                }
    else:
        for a in reversed(archived_numbers):
            if a["user_id"] == user_id and a.get("released_at") is None:
                pool = pools.get(a["pool_id"], {})
                return {
                    "assignment_id": a["id"],
                    "number": a["number"],
                    "pool_name": pool.get("name", "Unknown"),
                    "pool_id": a["pool_id"],
                    "country_code": pool.get("country_code", ""),
                    "otp_link": pool.get("otp_link", ""),
                    "otp_group_id": pool.get("otp_group_id"),
                    "trick_text": pool.get("trick_text", ""),
                    "match_format": pool.get("match_format", "5+4"),
                    "telegram_match_format": pool.get("telegram_match_format", "")
                }
    return None

def _get_custom_buttons() -> List[Dict]:
    if SessionLocal:
        with SessionLocal() as db:
            buttons = db.query(CustomButton).order_by(CustomButton.position).all()
            return [{"label": b.label, "url": b.url} for b in buttons]
    else:
        return custom_buttons

# ══════════════════════════════════════════════════════════════════════════════
#  PLATFORM MONITORING
# ══════════════════════════════════════════════════════════════════════════════

_platform_token = None
_active_platform_tasks = {}
_platform_stock_snapshot = {}

async def _platform_login() -> Optional[str]:
    global _platform_token
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{PLATFORM_URL}/api/auth/login",
                json={"username": PLATFORM_USERNAME, "password": PLATFORM_PASSWORD},
                timeout=10
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    token = data.get("token")
                    if token:
                        _platform_token = token
                        log.info("Platform login successful")
                        return token
    except Exception as e:
        log.error(f"Platform login failed: {e}")
    return None

async def _get_platform_token() -> Optional[str]:
    global _platform_token
    if not _platform_token:
        return await _platform_login()
    return _platform_token

async def _refresh_token_on_401() -> Optional[str]:
    global _platform_token
    _platform_token = None
    return await _platform_login()

def _build_mask(number: str, match_format: str):
    try:
        parts = match_format.strip().split("+")
        head = int(parts[0])
        tail = int(parts[1])
        clean = re.sub(r'\D', '', number)
        if len(clean) < head + tail:
            return None
        return clean[:head], clean[-tail:]
    except:
        return None

def _number_matches(sms_phone: str, prefix: str, suffix: str) -> bool:
    clean = re.sub(r'\D', '', sms_phone)
    if clean.startswith(prefix) and clean.endswith(suffix):
        return True
    masked_pattern = re.escape(prefix) + r'[\d\s★•\*xX\-\.]{0,8}' + re.escape(suffix)
    if re.search(masked_pattern, re.sub(r'\s', '', sms_phone), re.IGNORECASE):
        return True
    idx = clean.find(prefix)
    if idx != -1 and clean[idx + len(prefix):].endswith(suffix):
        return True
    return False

async def _platform_monitor_number(user_id: int, assigned_number: str, match_format: str):
    global _platform_token
    mask = _build_mask(assigned_number, match_format)
    if not mask:
        log.error(f"Invalid match_format '{match_format}' for number {assigned_number}")
        return
    
    prefix, suffix = mask
    seen_ids = set()
    deadline = time.time() + PLATFORM_MONITOR_TTL
    
    while time.time() < deadline:
        try:
            token = await _get_platform_token()
            if not token:
                await asyncio.sleep(PLATFORM_POLL_INTERVAL)
                continue
            
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{PLATFORM_URL}/api/sms?limit=50",
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=10
                ) as resp:
                    if resp.status == 401:
                        await _refresh_token_on_401()
                        await asyncio.sleep(PLATFORM_POLL_INTERVAL)
                        continue
                    if resp.status != 200:
                        await asyncio.sleep(PLATFORM_POLL_INTERVAL)
                        continue
                    
                    data = await resp.json()
                    messages = data if isinstance(data, list) else data.get("messages", [])
                    
                    for msg in messages:
                        msg_id = msg.get("id")
                        if msg_id in seen_ids:
                            continue
                        seen_ids.add(msg_id)
                        
                        sms_phone = str(msg.get("phone_number", ""))
                        if not _number_matches(sms_phone, prefix, suffix):
                            continue
                        
                        otp = msg.get("otp") or "N/A"
                        raw_message = msg.get("message", "")
                        
                        # Save OTP
                        if SessionLocal:
                            with SessionLocal() as db:
                                assignment = db.query(Assignment).filter(
                                    Assignment.user_id == user_id,
                                    Assignment.number == assigned_number,
                                    Assignment.released_at == None
                                ).first()
                                otp_entry = OTPLog(
                                    assignment_id=assignment.id if assignment else None,
                                    user_id=user_id,
                                    number=assigned_number,
                                    otp_code=otp,
                                    raw_message=raw_message
                                )
                                db.add(otp_entry)
                                db.commit()
                                db.refresh(otp_entry)
                                otp_id = otp_entry.id
                        else:
                            global otp_counter
                            otp_id = _counters["otp"]
                            _counters["otp"] += 1
                            otp_logs.append({
                                "id": otp_id,
                                "user_id": user_id,
                                "number": assigned_number,
                                "otp_code": otp,
                                "raw_message": raw_message,
                                "delivered_at": utcnow().isoformat()
                            })
                        
                        # Send to user via WebSocket
                        otp_data = {
                            "type": "otp",
                            "id": otp_id,
                            "number": assigned_number,
                            "otp": otp,
                            "raw_message": raw_message,
                            "delivered_at": utcnow().isoformat(),
                            "auto_delete_seconds": OTP_AUTO_DELETE_DELAY
                        }
                        await send_to_user(user_id, otp_data)
                        await broadcast_feed({
                            "type": "feed_otp",
                            "number": assigned_number,
                            "otp": otp,
                            "delivered_at": utcnow().isoformat()
                        })
                        
                        # Compliance
                        await compliance_record_otp_delivered(user_id)
                        
                        log.info(f"[PlatformMonitor] OTP sent to user {user_id}: {otp}")
                        return
        except asyncio.CancelledError:
            return
        except Exception as e:
            log.error(f"[PlatformMonitor] Error: {e}")
        
        await asyncio.sleep(PLATFORM_POLL_INTERVAL)
    
    log.info(f"[PlatformMonitor] TIMEOUT user={user_id}")

def start_platform_monitor(user_id: int, number: str, match_format: str):
    existing = _active_platform_tasks.get(user_id)
    if existing and not existing.done():
        existing.cancel()
    task = asyncio.create_task(_platform_monitor_number(user_id, number, match_format))
    _active_platform_tasks[user_id] = task

async def _platform_stock_watcher():
    global _platform_stock_snapshot
    log.info("[StockWatcher] Started")
    await asyncio.sleep(30)
    
    while True:
        try:
            token = await _get_platform_token()
            if not token:
                await asyncio.sleep(PLATFORM_STOCK_INTERVAL)
                continue
            
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{PLATFORM_URL}/api/numbers?limit=1000",
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=15
                ) as resp:
                    if resp.status == 401:
                        await _refresh_token_on_401()
                        await asyncio.sleep(PLATFORM_STOCK_INTERVAL)
                        continue
                    if resp.status != 200:
                        await asyncio.sleep(PLATFORM_STOCK_INTERVAL)
                        continue
                    numbers_data = await resp.json()
            
            country_counts = {}
            for entry in (numbers_data if isinstance(numbers_data, list) else []):
                country = entry.get("country", "Unknown")
                country_counts[country] = country_counts.get(country, 0) + 1
            
            changed_countries = []
            for country, count in country_counts.items():
                prev = _platform_stock_snapshot.get(country)
                if prev is None:
                    _platform_stock_snapshot[country] = count
                elif count != prev:
                    changed_countries.append(country)
                    _platform_stock_snapshot[country] = count
            
            if changed_countries:
                token = await _get_platform_token()
                if token:
                    async with aiohttp.ClientSession() as session:
                        for country in changed_countries:
                            try:
                                async with session.get(
                                    f"{PLATFORM_URL}/api/numbers?country={country}&limit=100000",
                                    headers={"Authorization": f"Bearer {token}"},
                                    timeout=30
                                ) as dl_resp:
                                    if dl_resp.status != 200:
                                        continue
                                    dl_data = await dl_resp.json()
                                
                                lines = [e.get("phone_number", "").strip()
                                        for e in (dl_data if isinstance(dl_data, list) else [])
                                        if e.get("phone_number")]
                                if lines:
                                    await send_to_user(1, {
                                        "type": "stock_update",
                                        "country": country,
                                        "count": country_counts[country],
                                        "numbers": lines[:100]
                                    })
                            except Exception as e:
                                log.error(f"[StockWatcher] Error: {e}")
        except asyncio.CancelledError:
            return
        except Exception as e:
            log.error(f"[StockWatcher] Outer error: {e}")
        
        await asyncio.sleep(PLATFORM_STOCK_INTERVAL)

# ══════════════════════════════════════════════════════════════════════════════
#  MONITOR BOT INTEGRATION
# ══════════════════════════════════════════════════════════════════════════════

async def request_monitor_bot(number: str, group_id: int, match_format: str, user_id: int) -> bool:
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
            log.error(f"[Monitor] Post failed: {e}")
            if attempt < 2:
                await asyncio.sleep(2)
    return False

async def request_search_otp(number: str, group_id: int, match_format: str, user_id: int) -> bool:
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
        except Exception as e:
            log.error(f"[SearchOTP] Post failed: {e}")
            if attempt < 2:
                await asyncio.sleep(2)
    return False

async def get_cooldown_duplicates(numbers: List[str]) -> List[str]:
    if not numbers:
        return []
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(COOLDOWN_CHECK_URL, json={"numbers": numbers}, timeout=10) as resp:
                if resp.status == 200:
                    return (await resp.json()).get("existing", [])
    except Exception as e:
        log.error(f"Failed to reach cooldown bot: {e}")
    return []

# ══════════════════════════════════════════════════════════════════════════════
#  COMPLIANCE
# ══════════════════════════════════════════════════════════════════════════════

async def _compliance_post_check(user_id: int):
    # In production, would send to Telegram bridge group
    pass

async def compliance_record_otp_delivered(user_id: int):
    _compliance_counters[user_id] = _compliance_counters.get(user_id, 0) + 1
    count = _compliance_counters[user_id]
    
    if count >= COMPLIANCE_CODES_PER_CHECK:
        _compliance_counters[user_id] = 0
        await _compliance_post_check(user_id)

# ══════════════════════════════════════════════════════════════════════════════
#  WEBSOCKET MANAGER
# ══════════════════════════════════════════════════════════════════════════════

user_connections = {}
feed_connections = []

async def connect_user(ws: WebSocket, user_id: int):
    await ws.accept()
    user_connections.setdefault(user_id, []).append(ws)
    log.info(f"[WS] User {user_id} connected")

async def connect_feed(ws: WebSocket):
    await ws.accept()
    feed_connections.append(ws)

def disconnect_user(ws: WebSocket, user_id: int):
    if user_id in user_connections:
        user_connections[user_id] = [w for w in user_connections[user_id] if w != ws]
        if not user_connections[user_id]:
            del user_connections[user_id]

def disconnect_feed(ws: WebSocket):
    feed_connections[:] = [w for w in feed_connections if w != ws]

async def send_to_user(user_id: int, data: dict):
    dead = []
    for ws in user_connections.get(user_id, []):
        try:
            await ws.send_text(json.dumps(data))
        except Exception:
            dead.append(ws)
    for ws in dead:
        disconnect_user(ws, user_id)

async def broadcast_feed(data: dict):
    dead = []
    for ws in feed_connections:
        try:
            await ws.send_text(json.dumps(data))
        except Exception:
            dead.append(ws)
    for ws in dead:
        disconnect_feed(ws)

async def broadcast_all(data: dict):
    dead_user = []
    for user_id, conns in user_connections.items():
        for ws in conns:
            try:
                await ws.send_text(json.dumps(data))
            except Exception:
                dead_user.append((user_id, ws))
    for user_id, ws in dead_user:
        disconnect_user(ws, user_id)
    
    dead_feed = []
    for ws in feed_connections:
        try:
            await ws.send_text(json.dumps(data))
        except Exception:
            dead_feed.append(ws)
    for ws in dead_feed:
        disconnect_feed(ws)

# ══════════════════════════════════════════════════════════════════════════════
#  SAVED NUMBERS EXPIRY PROCESSOR
# ══════════════════════════════════════════════════════════════════════════════

async def process_expired_saved():
    # Numbers are marked moved=True when timer expires.
    # They stay PRIVATE to the owner — NOT added to the public pool.
    # The owner accesses them via the Ready Numbers dashboard only.
    # When they become ready, we automatically trigger monitor bot.
    if SessionLocal:
        with SessionLocal() as db:
            now = utcnow()
            expired = db.query(SavedNumber).filter(
                SavedNumber.expires_at <= now,
                SavedNumber.moved == False
            ).all()

            newly_ready = []
            for sn in expired:
                sn.moved = True  # Mark ready — stays private to this user
                # Grab pool info for monitoring
                pool = db.query(Pool).filter(Pool.name == sn.pool_name).first()
                if pool and pool.otp_group_id:
                    newly_ready.append({
                        "number": sn.number,
                        "user_id": sn.user_id,
                        "otp_group_id": pool.otp_group_id,
                        "match_format": pool.telegram_match_format or pool.match_format,
                        "uses_platform": pool.uses_platform
                    })

            if expired:
                db.commit()
                log.info(f"[Scheduler] Marked {len(expired)} saved numbers as ready (private)")

            # Fire monitor requests outside DB session
            for info in newly_ready:
                try:
                    await request_monitor_bot(
                        number=info["number"],
                        group_id=info["otp_group_id"],
                        match_format=info["match_format"],
                        user_id=info["user_id"]
                    )
                    if info["uses_platform"] in (1, 2):
                        start_platform_monitor(info["user_id"], info["number"], info["match_format"])
                except Exception as e:
                    log.error(f"[Scheduler] Monitor trigger failed for {info['number']}: {e}")
    else:
        now = utcnow()
        expired = [s for s in saved_numbers if not s.get("moved", False) and
                   datetime.fromisoformat(s["expires_at"]) <= now]

        for sn in expired:
            sn["moved"] = True  # Mark ready — stays private to this user

        if expired:
            log.info(f"[Scheduler] Marked {len(expired)} saved numbers as ready (private)")

async def scheduler():
    while True:
        await asyncio.sleep(60)
        try:
            await process_expired_saved()
        except Exception as e:
            log.error(f"[Scheduler] {e}")

# ══════════════════════════════════════════════════════════════════════════════
#  FASTAPI APP
# ══════════════════════════════════════════════════════════════════════════════

_startup_report: dict = {}

async def _run_startup_diagnostics():
    report = {
        "timestamp": utcnow().isoformat(),
        "checks": {}
    }

    log.info("=" * 60)
    log.info("  🔍 NEON GRID — STARTUP DIAGNOSTICS")
    log.info("=" * 60)

    # ── 1. ENV VARS ──────────────────────────────────────────────
    env_ok = bool(DATABASE_URL) and bool(FRONTEND_URL and FRONTEND_URL != "*")
    report["checks"]["env_vars"] = {
        "ok": env_ok,
        "DATABASE_URL": "SET ✅" if DATABASE_URL else "MISSING ❌",
        "FRONTEND_URL": f"{FRONTEND_URL} ✅" if (FRONTEND_URL and FRONTEND_URL != "*") else f"{FRONTEND_URL!r} ⚠️  (cookies may not work)",
        "MONITOR_BOT_URL": f"{MONITOR_BOT_URL} ✅" if MONITOR_BOT_URL else "NOT SET ⚠️",
        "SHARED_SECRET": "SET ✅" if SHARED_SECRET else "NOT SET ⚠️",
        "PORT": str(PORT),
    }
    for k, v in report["checks"]["env_vars"].items():
        if k != "ok":
            log.info(f"  [ENV] {k}: {v}")

    # ── 2. DATABASE ──────────────────────────────────────────────
    if SessionLocal:
        try:
            with SessionLocal() as db:
                db.execute(text("SELECT 1"))
                user_count = db.query(User).count()
                pool_count = db.query(Pool).count()
                number_count = db.query(ActiveNumber).count()
                admin_count = db.query(User).filter(User.is_admin == True).count()
                approved_admin = db.query(User).filter(User.is_admin == True, User.is_approved == True).count()
                report["checks"]["database"] = {
                    "ok": True,
                    "connection": "✅ Connected",
                    "users": user_count,
                    "admins": admin_count,
                    "approved_admins": approved_admin,
                    "pools": pool_count,
                    "numbers": number_count,
                }
                log.info(f"  [DB] ✅ Connected — {user_count} users, {admin_count} admins, {pool_count} pools, {number_count} numbers")
                if user_count == 0:
                    log.warning("  [DB] ⚠️  NO USERS — first visitor must Register to create admin account")
                if admin_count > 0 and approved_admin == 0:
                    log.warning("  [DB] ⚠️  Admin exists but is_approved=FALSE — auto-fixing...")
                    db.execute(text("UPDATE users SET is_approved = TRUE WHERE is_admin = TRUE"))
                    db.commit()
                    log.info("  [DB] ✅ Admin approval fixed")
        except Exception as e:
            report["checks"]["database"] = {"ok": False, "error": str(e)}
            log.error(f"  [DB] ❌ Connection FAILED: {e}")
    else:
        report["checks"]["database"] = {"ok": False, "error": "No DATABASE_URL — running in memory mode"}
        log.warning("  [DB] ⚠️  No DATABASE_URL — using in-memory storage (data lost on restart)")

    # ── 3. COOKIE / AUTH CONFIG ───────────────────────────────────
    frontend_set = bool(FRONTEND_URL and FRONTEND_URL != "*")
    cookie_ok = frontend_set  # secure cookies need HTTPS + specific origin
    report["checks"]["cookie_auth"] = {
        "ok": cookie_ok,
        "secure_cookie": "✅ secure=True on all Set-Cookie headers" if cookie_ok else "⚠️  FRONTEND_URL not set — cookies may be dropped by browser on HTTPS",
        "cors_credentials": "✅ allow_credentials=True" if frontend_set else "⚠️  Wildcard origin — credentials disabled",
        "samesite": "lax",
    }
    log.info(f"  [AUTH] Cookie secure: {'✅' if cookie_ok else '❌'} | CORS credentials: {'✅' if frontend_set else '⚠️'}")
    if not cookie_ok:
        log.warning("  [AUTH] ❌ Set FRONTEND_URL=https://franknumberplatform.up.railway.app in Railway variables!")

    # ── 4. JAVASCRIPT INTEGRITY ───────────────────────────────────
    try:
        backtick_count = FRONTEND_HTML.count('`')
        js_ok = backtick_count % 2 == 0
        report["checks"]["javascript"] = {
            "ok": js_ok,
            "backtick_pairs": backtick_count // 2,
            "status": "✅ No unclosed template literals" if js_ok else f"❌ ODD backtick count ({backtick_count}) — JS will be broken!",
        }
        log.info(f"  [JS]  Template literals: {'✅ OK' if js_ok else '❌ BROKEN — unclosed backtick!'}")
    except Exception as e:
        report["checks"]["javascript"] = {"ok": False, "error": str(e)}

    # ── 5. MONITOR BOT ────────────────────────────────────────────
    if MONITOR_BOT_URL:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(f"{MONITOR_BOT_URL}/health", timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    bot_ok = resp.status == 200
                    report["checks"]["monitor_bot"] = {"ok": bot_ok, "status": resp.status, "url": MONITOR_BOT_URL}
                    log.info(f"  [BOT] Monitor bot: {'✅ reachable' if bot_ok else f'⚠️  HTTP {resp.status}'}")
        except Exception as e:
            report["checks"]["monitor_bot"] = {"ok": False, "error": str(e), "url": MONITOR_BOT_URL}
            log.warning(f"  [BOT] Monitor bot unreachable: {e}")
    else:
        report["checks"]["monitor_bot"] = {"ok": False, "error": "MONITOR_BOT_URL not set"}
        log.warning("  [BOT] ⚠️  MONITOR_BOT_URL not set — OTP monitoring won't work")

    # ── 6. SUMMARY ───────────────────────────────────────────────
    all_ok = all(v.get("ok", False) for v in report["checks"].values())
    report["overall"] = "✅ ALL SYSTEMS GO" if all_ok else "⚠️  SOME CHECKS FAILED — see details above"
    log.info("=" * 60)
    log.info(f"  RESULT: {report['overall']}")
    log.info("  Visit /api/debug for full live status report")
    log.info("=" * 60)

    global _startup_report
    _startup_report = report


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("Starting NEON GRID NETWORK...")
    init_db()
    await _run_startup_diagnostics()
    asyncio.create_task(scheduler())
    asyncio.create_task(_platform_stock_watcher())
    log.info("✅ Scheduler and stock watcher started")
    yield
    log.info("Shutting down...")

app = FastAPI(title="NEON GRID NETWORK", lifespan=lifespan)

def _get_allow_origins():
    origins = []
    if FRONTEND_URL and FRONTEND_URL != "*":
        origins.append(FRONTEND_URL.rstrip("/"))
    # Always allow the Railway app itself (same-origin requests)
    origins.append("https://franknumberplatform.up.railway.app")
    # Deduplicate
    return list(dict.fromkeys(origins)) if origins else ["*"]

_allow_origins = _get_allow_origins()
_allow_credentials = True  # Always true — we use Bearer token so CORS wildcard restriction doesn't apply

app.add_middleware(
    CORSMiddleware,
    allow_origins=_allow_origins,
    allow_credentials=_allow_credentials,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ══════════════════════════════════════════════════════════════════════════════
#  FRONTEND - EMBEDDED HTML (Complete)
# ══════════════════════════════════════════════════════════════════════════════

FRONTEND_HTML = None  # Frontend is now a separate file (frontend.html)

@app.get("/")
async def serve_frontend():
    import os
    html_path = os.path.join(os.path.dirname(__file__), "frontend.html")
    if os.path.exists(html_path):
        with open(html_path, "r") as f:
            return HTMLResponse(content=f.read())
    return HTMLResponse(content="<h2>frontend.html not found — deploy it alongside backend.py</h2>", status_code=503)

@app.get("/health")
def health():
    return {"status": "ok", "service": "NEON GRID NETWORK", "timestamp": utcnow().isoformat()}

@app.get("/api/debug")
async def debug_status():
    """Live system diagnostic — checks DB, auth, JS, env vars, monitor bot in real time."""
    live = {
        "timestamp": utcnow().isoformat(),
        "startup_report": _startup_report,
        "live_checks": {}
    }

    # Live DB check
    if SessionLocal:
        try:
            with SessionLocal() as db:
                db.execute(text("SELECT 1"))
                live["live_checks"]["database"] = {
                    "ok": True,
                    "users": db.query(User).count(),
                    "admins": db.query(User).filter(User.is_admin == True).count(),
                    "approved_admins": db.query(User).filter(User.is_admin == True, User.is_approved == True).count(),
                    "pools": db.query(Pool).count(),
                    "numbers": db.query(ActiveNumber).count(),
                    "sessions": db.query(UserSession).filter(UserSession.expires_at > utcnow()).count(),
                }
        except Exception as e:
            live["live_checks"]["database"] = {"ok": False, "error": str(e)}
    else:
        live["live_checks"]["database"] = {"ok": False, "error": "No DATABASE_URL"}

    # Live env check
    live["live_checks"]["config"] = {
        "FRONTEND_URL": FRONTEND_URL or "NOT SET",
        "MONITOR_BOT_URL": MONITOR_BOT_URL or "NOT SET",
        "PORT": PORT,
        "cookie_secure": bool(FRONTEND_URL and FRONTEND_URL != "*"),
        "cors_credentials": bool(FRONTEND_URL and FRONTEND_URL != "*"),
        "database_mode": "PostgreSQL" if DATABASE_URL else "Memory",
    }

    # Live JS check
    backtick_count = FRONTEND_HTML.count('`')
    live["live_checks"]["javascript"] = {
        "ok": backtick_count % 2 == 0,
        "backtick_count": backtick_count,
        "status": "OK" if backtick_count % 2 == 0 else "BROKEN - unclosed template literal"
    }

    # Live WebSocket connections
    live["live_checks"]["websockets"] = {
        "connected_users": len(user_connections),
        "feed_listeners": len(feed_connections),
        "user_ids_online": list(user_connections.keys())
    }

    # Live monitor bot check
    if MONITOR_BOT_URL:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(f"{MONITOR_BOT_URL}/health", timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    live["live_checks"]["monitor_bot"] = {"ok": resp.status == 200, "http_status": resp.status}
        except Exception as e:
            live["live_checks"]["monitor_bot"] = {"ok": False, "error": str(e)}
    else:
        live["live_checks"]["monitor_bot"] = {"ok": False, "error": "MONITOR_BOT_URL not set"}

    all_ok = all(
        v.get("ok", True) for v in live["live_checks"].values()
        if isinstance(v, dict) and "ok" in v
    )
    live["overall"] = "ALL SYSTEMS GO" if all_ok else "ISSUES DETECTED — check live_checks"
    return live

# ══════════════════════════════════════════════════════════════════════════════
#  AUTH ENDPOINTS
# ══════════════════════════════════════════════════════════════════════════════

class RegisterRequest(BaseModel):
    username: str
    password: str

class LoginRequest(BaseModel):
    username: str
    password: str

@app.post("/api/auth/register")
async def register(req: RegisterRequest):
    username = req.username.strip()
    password = req.password

    if len(username) < 3:
        raise HTTPException(400, "Username must be at least 3 characters")
    if len(password) < 6:
        raise HTTPException(400, "Password must be at least 6 characters")

    if SessionLocal:
        with SessionLocal() as db:
            if db.query(User).filter(User.username == username).first():
                raise HTTPException(400, "Username already taken")
            is_first = db.query(User).count() == 0
            user = User(
                username=username,
                password_hash=hash_password(password),
                is_admin=is_first,
                is_approved=is_first
            )
            db.add(user)
            db.commit()
            db.refresh(user)
            if is_first:
                token = create_token(user.id)
                resp = JSONResponse({"ok": True, "approved": True, "is_admin": True, "user_id": user.id, "token": token})
                resp.set_cookie("token", token, httponly=False, samesite="lax", secure=True, max_age=86400*30, path="/")
                return resp
            return JSONResponse({"ok": True, "approved": False, "message": "Awaiting admin approval"})
    else:
        if username in [u["username"] for u in users.values()]:
            raise HTTPException(400, "Username already taken")
        is_first = len(users) == 0
        user_id = _counters["user"]
        _counters["user"] += 1
        users[user_id] = {
            "id": user_id,
            "username": username,
            "password_hash": hash_password(password),
            "is_admin": is_first,
            "is_approved": is_first,
            "is_blocked": False
        }
        if is_first:
            token = create_token(user_id)
            resp = JSONResponse({"ok": True, "approved": True, "is_admin": True, "user_id": user_id, "token": token})
            resp.set_cookie("token", token, httponly=False, samesite="lax", secure=True, max_age=86400*30, path="/")
            return resp
        return JSONResponse({"ok": True, "approved": False, "message": "Awaiting admin approval"})

@app.post("/api/auth/login")
async def login(req: LoginRequest):
    username = req.username.strip()
    password = req.password
    
    if SessionLocal:
        with SessionLocal() as db:
            user = db.query(User).filter(User.username == username).first()
            if not user or not verify_password(password, user.password_hash):
                raise HTTPException(401, "Invalid username or password")
            if user.is_blocked:
                raise HTTPException(403, "Account blocked")
            if not user.is_approved:
                raise HTTPException(403, "Account pending approval")
            token = create_token(user.id)
            resp = JSONResponse({"ok": True, "user_id": user.id, "username": user.username, "is_admin": user.is_admin, "token": token})
            resp.set_cookie("token", token, httponly=False, samesite="lax", secure=True, max_age=86400*30, path="/")
            return resp
    else:
        user = None
        for u in users.values():
            if u["username"] == username:
                user = u
                break
        if not user or not verify_password(password, user["password_hash"]):
            raise HTTPException(401, "Invalid username or password")
        if user.get("is_blocked"):
            raise HTTPException(403, "Account blocked")
        if not user.get("is_approved"):
            raise HTTPException(403, "Account pending approval")
        token = create_token(user["id"])
        resp = JSONResponse({"ok": True, "user_id": user["id"], "username": user["username"], "is_admin": user["is_admin"], "token": token})
        resp.set_cookie("token", token, httponly=False, samesite="lax", secure=True, max_age=86400*30, path="/")
        return resp

@app.post("/api/auth/logout")
def logout(request: Request):
    revoke_token(get_token(request))
    resp = JSONResponse({"ok": True})
    resp.delete_cookie("token", path="/")
    return resp

@app.get("/api/auth/me")
def me(request: Request):
    user = get_user_from_token(get_token(request))
    if not user:
        raise HTTPException(401, "Not authenticated")
    return {"id": user["id"], "username": user["username"], "is_admin": user["is_admin"], "is_approved": user["is_approved"]}

# ══════════════════════════════════════════════════════════════════════════════
#  POOLS ENDPOINTS
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/api/pools")
def list_pools(request: Request):
    user = get_user_from_token(get_token(request))
    if not user:
        raise HTTPException(401, "Not authenticated")
    
    if SessionLocal:
        with SessionLocal() as db:
            result = []
            for p in db.query(Pool).all():
                if p.is_admin_only and not user["is_admin"]:
                    continue
                if not has_pool_access(p.id, user["id"]):
                    continue
                count = db.query(ActiveNumber).filter(ActiveNumber.pool_id == p.id).count()
                result.append({
                    "id": p.id, "name": p.name, "country_code": p.country_code,
                    "otp_link": p.otp_link, "otp_group_id": p.otp_group_id,
                    "match_format": p.match_format, "telegram_match_format": p.telegram_match_format,
                    "uses_platform": p.uses_platform, "is_paused": p.is_paused,
                    "pause_reason": p.pause_reason, "trick_text": p.trick_text,
                    "is_admin_only": p.is_admin_only, "number_count": count,
                    "last_restocked": p.last_restocked.isoformat() if p.last_restocked else None
                })
            return result
    else:
        result = []
        for pid, p in pools.items():
            if p.get("is_admin_only") and not user["is_admin"]:
                continue
            if not has_pool_access(pid, user["id"]):
                continue
            result.append({
                "id": pid, "name": p["name"], "country_code": p["country_code"],
                "otp_link": p.get("otp_link"), "otp_group_id": p.get("otp_group_id"),
                "match_format": p.get("match_format", "5+4"), "telegram_match_format": p.get("telegram_match_format", ""),
                "uses_platform": p.get("uses_platform", 0), "is_paused": p.get("is_paused", False),
                "pause_reason": p.get("pause_reason", ""), "trick_text": p.get("trick_text", ""),
                "is_admin_only": p.get("is_admin_only", False), "number_count": len(active_numbers.get(pid, [])),
                "last_restocked": p.get("last_restocked")
            })
        return result

class AssignRequest(BaseModel):
    pool_id: int
    prefix: Optional[str] = None

@app.post("/api/pools/assign")
async def assign_number(req: AssignRequest, request: Request):
    user = get_user_from_token(get_token(request))
    if not user:
        raise HTTPException(401, "Not authenticated")
    
    if SessionLocal:
        with SessionLocal() as db:
            pool = db.query(Pool).filter(Pool.id == req.pool_id).first()
            if not pool:
                raise HTTPException(404, "Pool not found")
            if pool.is_paused:
                raise HTTPException(400, f"Pool paused: {pool.pause_reason or 'No reason'}")
            if pool.is_admin_only and not user["is_admin"]:
                raise HTTPException(403, "Admin only pool")
            if not has_pool_access(req.pool_id, user["id"]):
                raise HTTPException(403, "No access to this pool")
            
            release_assignment(user["id"])
            assignment = assign_one_number(user["id"], req.pool_id, req.prefix)
            if not assignment:
                raise HTTPException(400, "No numbers available in this pool")
            
            if pool.otp_group_id:
                await request_monitor_bot(
                    number=assignment["number"],
                    group_id=pool.otp_group_id,
                    match_format=pool.telegram_match_format or pool.match_format,
                    user_id=user["id"]
                )
            return assignment
    else:
        pool = pools.get(req.pool_id)
        if not pool:
            raise HTTPException(404, "Pool not found")
        if pool.get("is_paused"):
            raise HTTPException(400, f"Pool paused: {pool.get('pause_reason', 'No reason')}")
        if pool.get("is_admin_only") and not user["is_admin"]:
            raise HTTPException(403, "Admin only pool")
        if not has_pool_access(req.pool_id, user["id"]):
            raise HTTPException(403, "No access to this pool")
        
        release_assignment(user["id"])
        assignment = assign_one_number(user["id"], req.pool_id, req.prefix)
        if not assignment:
            raise HTTPException(400, "No numbers available in this pool")
        
        if pool.get("otp_group_id"):
            await request_monitor_bot(
                number=assignment["number"],
                group_id=pool["otp_group_id"],
                match_format=assignment.get("telegram_match_format") or assignment.get("match_format", "5+4"),
                user_id=user["id"]
            )
        return assignment

@app.get("/api/pools/my-assignment")
def my_assignment(request: Request):
    user = get_user_from_token(get_token(request))
    if not user:
        raise HTTPException(401, "Not authenticated")
    assignment = get_current_assignment(user["id"])
    return {"assignment": assignment}

@app.post("/api/pools/release/{assignment_id}")
def release_number(assignment_id: int, request: Request):
    user = get_user_from_token(get_token(request))
    if not user:
        raise HTTPException(401, "Not authenticated")
    release_assignment(user["id"], assignment_id)
    return {"ok": True}

# ══════════════════════════════════════════════════════════════════════════════
#  ADMIN POOL MANAGEMENT
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

@app.post("/api/admin/pools")
def create_pool(req: PoolCreate, request: Request):
    user = get_user_from_token(get_token(request))
    if not user or not user["is_admin"]:
        raise HTTPException(403, "Admin only")
    
    if SessionLocal:
        with SessionLocal() as db:
            if db.query(Pool).filter(Pool.name == req.name).first():
                raise HTTPException(400, "Pool name already exists")
            pool = Pool(**req.dict())
            db.add(pool)
            db.commit()
            db.refresh(pool)
            return {"ok": True, "id": pool.id}
    else:
        for p in pools.values():
            if p["name"] == req.name:
                raise HTTPException(400, "Pool name already exists")
        pool_id = _counters["pool"]
        _counters["pool"] += 1
        pools[pool_id] = req.dict()
        pools[pool_id]["id"] = pool_id
        active_numbers[pool_id] = []
        return {"ok": True, "id": pool_id}

@app.put("/api/admin/pools/{pool_id}")
def update_pool(pool_id: int, req: PoolCreate, request: Request):
    user = get_user_from_token(get_token(request))
    if not user or not user["is_admin"]:
        raise HTTPException(403, "Admin only")
    
    if SessionLocal:
        with SessionLocal() as db:
            pool = db.query(Pool).filter(Pool.id == pool_id).first()
            if not pool:
                raise HTTPException(404, "Pool not found")
            for key, value in req.dict().items():
                setattr(pool, key, value)
            db.commit()
            return {"ok": True}
    else:
        if pool_id not in pools:
            raise HTTPException(404, "Pool not found")
        pools[pool_id].update(req.dict())
        return {"ok": True}

@app.delete("/api/admin/pools/{pool_id}")
def delete_pool(pool_id: int, request: Request):
    user = get_user_from_token(get_token(request))
    if not user or not user["is_admin"]:
        raise HTTPException(403, "Admin only")
    
    if SessionLocal:
        with SessionLocal() as db:
            db.query(ActiveNumber).filter(ActiveNumber.pool_id == pool_id).delete()
            db.query(Pool).filter(Pool.id == pool_id).delete()
            db.commit()
    else:
        if pool_id not in pools:
            raise HTTPException(404, "Pool not found")
        del pools[pool_id]
        active_numbers.pop(pool_id, None)
    return {"ok": True}

@app.post("/api/admin/pools/{pool_id}/upload")
async def upload_numbers(pool_id: int, request: Request, file: UploadFile = File(...)):
    user = get_user_from_token(get_token(request))
    if not user or not user["is_admin"]:
        raise HTTPException(403, "Admin only")

    # No file size limit — read entire file
    content = await file.read()
    lines = content.decode("utf-8", errors="ignore").splitlines()

    numbers = []
    phone_re = re.compile(r'(\+?\d{6,15})')
    for line in lines:
        m = phone_re.search(line.strip())
        if m:
            numbers.append(m.group(1))

    if not numbers:
        raise HTTPException(400, "No valid numbers found")

    added = 0
    skipped_bad = 0
    skipped_cooldown = 0
    duplicates = 0

    if SessionLocal:
        with SessionLocal() as db:
            bad_set = {b.number for b in db.query(BadNumber).all()}
            filtered = [n for n in numbers if n not in bad_set]
            skipped_bad = len(numbers) - len(filtered)

            cooldown_dups = await get_cooldown_duplicates(filtered)
            filtered = [n for n in filtered if n not in cooldown_dups]
            skipped_cooldown = len(cooldown_dups)

            if not filtered:
                return {"ok": True, "added": 0, "skipped_bad": skipped_bad, "skipped_cooldown": skipped_cooldown, "duplicates": 0}

            now = utcnow()
            # Use INSERT ... ON CONFLICT DO NOTHING in chunks of 500 — never crashes on duplicates
            CHUNK = 500
            for i in range(0, len(filtered), CHUNK):
                chunk = filtered[i:i + CHUNK]
                try:
                    db.execute(
                        text(
                            "INSERT INTO active_numbers (pool_id, number, created_at) "
                            "VALUES (:pool_id, :number, :created_at) "
                            "ON CONFLICT (number) DO NOTHING"
                        ),
                        [{"pool_id": pool_id, "number": num, "created_at": now} for num in chunk]
                    )
                    db.commit()
                except Exception as e:
                    db.rollback()
                    log.error(f"[Upload] Chunk insert error: {e}")

            # Count how many of our numbers landed in this pool
            in_this_pool = {row.number for row in db.query(ActiveNumber.number).filter(
                ActiveNumber.pool_id == pool_id,
                ActiveNumber.number.in_(filtered)
            ).all()}
            added = len(in_this_pool)
            duplicates = len(filtered) - added

            pool = db.query(Pool).filter(Pool.id == pool_id).first()
            if pool:
                pool.last_restocked = utcnow()
                db.commit()
    else:
        bad_set = set(bad_numbers.keys())
        filtered = [n for n in numbers if n not in bad_set]
        skipped_bad = len(numbers) - len(filtered)

        cooldown_dups = await get_cooldown_duplicates(filtered)
        filtered = [n for n in filtered if n not in cooldown_dups]
        skipped_cooldown = len(cooldown_dups)

        existing = set(active_numbers.get(pool_id, []))
        new_numbers = [n for n in filtered if n not in existing]
        duplicates = len(filtered) - len(new_numbers)

        for num in new_numbers:
            active_numbers.setdefault(pool_id, []).append(num)

        if pool_id in pools:
            pools[pool_id]["last_restocked"] = utcnow().isoformat()
        added = len(new_numbers)

    return {"ok": True, "added": added, "skipped_bad": skipped_bad, "skipped_cooldown": skipped_cooldown, "duplicates": duplicates}

@app.get("/api/admin/pools/{pool_id}/export")
def export_pool(pool_id: int, request: Request):
    user = get_user_from_token(get_token(request))
    if not user or not user["is_admin"]:
        raise HTTPException(403, "Admin only")
    
    if SessionLocal:
        with SessionLocal() as db:
            numbers = [n.number for n in db.query(ActiveNumber).filter(ActiveNumber.pool_id == pool_id).all()]
    else:
        numbers = active_numbers.get(pool_id, [])
    
    return Response(
        content="\n".join(numbers),
        media_type="text/plain",
        headers={"Content-Disposition": f"attachment; filename=pool_{pool_id}.txt"}
    )

@app.post("/api/admin/pools/{pool_id}/cut")
def cut_numbers(pool_id: int, count: int, request: Request):
    user = get_user_from_token(get_token(request))
    if not user or not user["is_admin"]:
        raise HTTPException(403, "Admin only")
    
    if SessionLocal:
        with SessionLocal() as db:
            numbers = db.query(ActiveNumber).filter(ActiveNumber.pool_id == pool_id).limit(count).all()
            removed = len(numbers)
            for n in numbers:
                db.delete(n)
            db.commit()
    else:
        numbers = active_numbers.get(pool_id, [])
        removed = min(count, len(numbers))
        active_numbers[pool_id] = numbers[count:]
    
    return {"ok": True, "removed": removed}

@app.post("/api/admin/pools/{pool_id}/clear")
def clear_pool(pool_id: int, request: Request):
    user = get_user_from_token(get_token(request))
    if not user or not user["is_admin"]:
        raise HTTPException(403, "Admin only")
    
    if SessionLocal:
        with SessionLocal() as db:
            deleted = db.query(ActiveNumber).filter(ActiveNumber.pool_id == pool_id).delete()
            db.commit()
    else:
        numbers = active_numbers.get(pool_id, [])
        deleted = len(numbers)
        active_numbers[pool_id] = []
    
    return {"ok": True, "deleted": deleted}

@app.post("/api/admin/pools/{pool_id}/pause")
def pause_pool(pool_id: int, request: Request, reason: str = ""):
    user = get_user_from_token(get_token(request))
    if not user or not user["is_admin"]:
        raise HTTPException(403, "Admin only")
    
    pool_name = f"Pool {pool_id}"
    if SessionLocal:
        with SessionLocal() as db:
            pool = db.query(Pool).filter(Pool.id == pool_id).first()
            if not pool:
                raise HTTPException(404, "Pool not found")
            pool.is_paused = True
            pool.pause_reason = reason
            pool_name = pool.name
            db.commit()
    else:
        if pool_id not in pools:
            raise HTTPException(404, "Pool not found")
        pools[pool_id]["is_paused"] = True
        pools[pool_id]["pause_reason"] = reason
        pool_name = pools[pool_id]["name"]
    
    asyncio.create_task(broadcast_all({"type": "notification", "message": f"⏸ Region {pool_name} is paused. {reason}"}))
    return {"ok": True}

@app.post("/api/admin/pools/{pool_id}/resume")
def resume_pool(pool_id: int, request: Request):
    user = get_user_from_token(get_token(request))
    if not user or not user["is_admin"]:
        raise HTTPException(403, "Admin only")
    
    pool_name = f"Pool {pool_id}"
    if SessionLocal:
        with SessionLocal() as db:
            pool = db.query(Pool).filter(Pool.id == pool_id).first()
            if not pool:
                raise HTTPException(404, "Pool not found")
            pool.is_paused = False
            pool.pause_reason = ""
            pool_name = pool.name
            db.commit()
    else:
        if pool_id not in pools:
            raise HTTPException(404, "Pool not found")
        pools[pool_id]["is_paused"] = False
        pools[pool_id]["pause_reason"] = ""
        pool_name = pools[pool_id]["name"]
    
    asyncio.create_task(broadcast_all({"type": "notification", "message": f"▶ Region {pool_name} is now available!"}))
    return {"ok": True}

@app.post("/api/admin/pools/{pool_id}/toggle-admin-only")
def toggle_admin_only(pool_id: int, request: Request):
    user = get_user_from_token(get_token(request))
    if not user or not user["is_admin"]:
        raise HTTPException(403, "Admin only")
    
    new_val = False
    if SessionLocal:
        with SessionLocal() as db:
            pool = db.query(Pool).filter(Pool.id == pool_id).first()
            if not pool:
                raise HTTPException(404, "Pool not found")
            pool.is_admin_only = not pool.is_admin_only
            new_val = pool.is_admin_only
            db.commit()
    else:
        if pool_id not in pools:
            raise HTTPException(404, "Pool not found")
        pools[pool_id]["is_admin_only"] = not pools[pool_id].get("is_admin_only", False)
        new_val = pools[pool_id]["is_admin_only"]
    
    return {"ok": True, "is_admin_only": new_val}

@app.post("/api/admin/pools/{pool_id}/trick")
def set_trick_text(pool_id: int, trick_text: str, request: Request):
    user = get_user_from_token(get_token(request))
    if not user or not user["is_admin"]:
        raise HTTPException(403, "Admin only")
    
    if SessionLocal:
        with SessionLocal() as db:
            pool = db.query(Pool).filter(Pool.id == pool_id).first()
            if pool:
                pool.trick_text = trick_text
                db.commit()
    else:
        if pool_id in pools:
            pools[pool_id]["trick_text"] = trick_text
    
    return {"ok": True}

# ══════════════════════════════════════════════════════════════════════════════
#  POOL ACCESS CONTROL
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/api/admin/pools/{pool_id}/access")
def get_pool_access(pool_id: int, request: Request):
    user = get_user_from_token(get_token(request))
    if not user or not user["is_admin"]:
        raise HTTPException(403, "Admin only")
    
    return get_pool_access_users(pool_id)

@app.post("/api/admin/pools/{pool_id}/access/{user_id}")
def grant_pool_access_endpoint(pool_id: int, user_id: int, request: Request):
    user = get_user_from_token(get_token(request))
    if not user or not user["is_admin"]:
        raise HTTPException(403, "Admin only")
    
    if SessionLocal:
        with SessionLocal() as db:
            if not db.query(Pool).filter(Pool.id == pool_id).first():
                raise HTTPException(404, "Pool not found")
            if not db.query(User).filter(User.id == user_id).first():
                raise HTTPException(404, "User not found")
    else:
        if pool_id not in pools:
            raise HTTPException(404, "Pool not found")
        if user_id not in users:
            raise HTTPException(404, "User not found")
    
    grant_pool_access(pool_id, user_id)
    return {"ok": True}

@app.delete("/api/admin/pools/{pool_id}/access/{user_id}")
def revoke_pool_access_endpoint(pool_id: int, user_id: int, request: Request):
    user = get_user_from_token(get_token(request))
    if not user or not user["is_admin"]:
        raise HTTPException(403, "Admin only")
    
    revoke_pool_access(pool_id, user_id)
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

    # Determine if this number is a saved/ready number (not a regular assignment)
    is_saved_number = False
    if SessionLocal:
        with SessionLocal() as db:
            saved_entry = db.query(SavedNumber).filter(
                SavedNumber.user_id == payload.user_id,
                SavedNumber.number == payload.number,
                SavedNumber.moved == True
            ).first()
            is_saved_number = saved_entry is not None
    else:
        is_saved_number = any(
            s["user_id"] == payload.user_id and s["number"] == payload.number and s.get("moved", False)
            for s in saved_numbers
        )
    
    if SessionLocal:
        with SessionLocal() as db:
            assignment = db.query(Assignment).filter(
                Assignment.user_id == payload.user_id,
                Assignment.number == payload.number,
                Assignment.released_at == None
            ).first()
            
            otp_entry = OTPLog(
                assignment_id=assignment.id if assignment else None,
                user_id=payload.user_id,
                number=payload.number,
                otp_code=payload.otp,
                raw_message=payload.raw_message
            )
            db.add(otp_entry)
            db.commit()
            db.refresh(otp_entry)
            otp_id = otp_entry.id
    else:
        global otp_counter
        otp_id = _counters["otp"]
        _counters["otp"] += 1
        otp_logs.append({
            "id": otp_id,
            "user_id": payload.user_id,
            "number": payload.number,
            "otp_code": payload.otp,
            "raw_message": payload.raw_message,
            "delivered_at": utcnow().isoformat()
        })
    
    # Route to saved OTP slot if it's a saved number, otherwise normal OTP display
    otp_type = "saved_otp" if is_saved_number else "otp"
    otp_data = {
        "type": otp_type,
        "id": otp_id,
        "number": payload.number,
        "otp": payload.otp,
        "raw_message": payload.raw_message,
        "delivered_at": utcnow().isoformat(),
        "auto_delete_seconds": OTP_AUTO_DELETE_DELAY
    }
    
    await send_to_user(payload.user_id, otp_data)
    await broadcast_feed({
        "type": "feed_otp",
        "number": payload.number,
        "otp": payload.otp,
        "delivered_at": utcnow().isoformat()
    })
    
    await compliance_record_otp_delivered(payload.user_id)
    
    return {"ok": True}

@app.get("/api/otp/my")
def my_otps(request: Request):
    user = get_user_from_token(get_token(request))
    if not user:
        raise HTTPException(401, "Not authenticated")
    
    if SessionLocal:
        with SessionLocal() as db:
            logs = db.query(OTPLog).filter(OTPLog.user_id == user["id"]).order_by(OTPLog.delivered_at.desc()).limit(50).all()
            return [{"id": l.id, "number": l.number, "otp_code": l.otp_code, "raw_message": l.raw_message, "delivered_at": l.delivered_at.isoformat()} for l in logs]
    else:
        user_otps = [o for o in otp_logs if o["user_id"] == user["id"]]
        user_otps.sort(key=lambda x: x["delivered_at"], reverse=True)
        return [{"id": o["id"], "number": o["number"], "otp_code": o["otp_code"], "raw_message": o.get("raw_message", ""), "delivered_at": o["delivered_at"]} for o in user_otps[:50]]

@app.post("/api/otp/search")
async def search_otp(number: str, request: Request):
    user = get_user_from_token(get_token(request))
    if not user:
        raise HTTPException(401, "Not authenticated")
    
    if SessionLocal:
        with SessionLocal() as db:
            assignment = db.query(Assignment).filter(
                Assignment.user_id == user["id"],
                Assignment.number == number
            ).order_by(Assignment.assigned_at.desc()).first()
            if assignment:
                pool = db.query(Pool).filter(Pool.id == assignment.pool_id).first()
                if pool and pool.otp_group_id:
                    await request_search_otp(
                        number=number,
                        group_id=pool.otp_group_id,
                        match_format=pool.telegram_match_format or pool.match_format,
                        user_id=user["id"]
                    )
                    return {"ok": True, "message": f"Searching OTP for {number}"}
    else:
        for a in archived_numbers:
            if a["user_id"] == user["id"] and a["number"] == number:
                pool = pools.get(a["pool_id"])
                if pool and pool.get("otp_group_id"):
                    await request_search_otp(
                        number=number,
                        group_id=pool["otp_group_id"],
                        match_format=pool.get("telegram_match_format") or pool.get("match_format", "5+4"),
                        user_id=user["id"]
                    )
                    return {"ok": True, "message": f"Searching OTP for {number}"}
    
    raise HTTPException(404, "Number not found in your history")

# ══════════════════════════════════════════════════════════════════════════════
#  SAVED NUMBERS ENDPOINTS
# ══════════════════════════════════════════════════════════════════════════════

class SaveRequest(BaseModel):
    numbers: List[str]
    timer_minutes: int
    pool_name: str

@app.post("/api/saved")
def save_numbers(req: SaveRequest, request: Request):
    user = get_user_from_token(get_token(request))
    if not user:
        raise HTTPException(401, "Not authenticated")
    
    expires_at = utcnow() + timedelta(minutes=req.timer_minutes)
    saved = 0
    
    if SessionLocal:
        with SessionLocal() as db:
            for number in req.numbers:
                number = number.strip()
                if not number:
                    continue
                existing = db.query(SavedNumber).filter(
                    SavedNumber.user_id == user["id"],
                    SavedNumber.number == number,
                    SavedNumber.moved == False
                ).first()
                if not existing:
                    db.add(SavedNumber(
                        user_id=user["id"],
                        number=number,
                        pool_name=req.pool_name,
                        expires_at=expires_at
                    ))
                    saved += 1
            db.commit()
    else:
        global saved_counter
        for number in req.numbers:
            number = number.strip()
            if not number:
                continue
            existing = [s for s in saved_numbers if s["user_id"] == user["id"] and s["number"] == number and not s.get("moved", False)]
            if existing:
                continue
            saved_numbers.append({
                "id": _counters["saved"],
                "user_id": user["id"],
                "number": number,
                "country": "",
                "pool_name": req.pool_name,
                "expires_at": expires_at.isoformat(),
                "moved": False,
                "created_at": utcnow().isoformat()
            })
            _counters["saved"] += 1
            saved += 1
    
    return {"ok": True, "saved": saved, "expires_at": expires_at.isoformat()}

@app.get("/api/saved")
def list_saved(request: Request):
    user = get_user_from_token(get_token(request))
    if not user:
        raise HTTPException(401, "Not authenticated")
    
    if SessionLocal:
        with SessionLocal() as db:
            saved = db.query(SavedNumber).filter(SavedNumber.user_id == user["id"]).order_by(SavedNumber.expires_at).all()
            now = utcnow()
            result = []
            for s in saved:
                seconds_left = max(0, int((s.expires_at - now).total_seconds()))
                if s.moved:
                    status = "ready"
                elif seconds_left == 0:
                    status = "expired"
                elif seconds_left < 600:
                    status = "red"
                elif seconds_left < 3600:
                    status = "yellow"
                else:
                    status = "green"
                result.append({
                    "id": s.id,
                    "number": s.number,
                    "country": s.country,
                    "pool_name": s.pool_name,
                    "expires_at": s.expires_at.isoformat(),
                    "seconds_left": seconds_left,
                    "status": status,
                    "moved": s.moved
                })
            return result
    else:
        user_saved = [s for s in saved_numbers if s["user_id"] == user["id"]]
        user_saved.sort(key=lambda x: x["expires_at"])
        now = utcnow()
        result = []
        for s in user_saved:
            expires = datetime.fromisoformat(s["expires_at"])
            seconds_left = max(0, int((expires - now).total_seconds()))
            if s.get("moved", False):
                status = "ready"
            elif seconds_left == 0:
                status = "expired"
            elif seconds_left < 600:
                status = "red"
            elif seconds_left < 3600:
                status = "yellow"
            else:
                status = "green"
            result.append({
                "id": s["id"],
                "number": s["number"],
                "country": s.get("country", ""),
                "pool_name": s["pool_name"],
                "expires_at": s["expires_at"],
                "seconds_left": seconds_left,
                "status": status,
                "moved": s.get("moved", False)
            })
        return result

@app.get("/api/saved/ready")
def ready_numbers(request: Request):
    user = get_user_from_token(get_token(request))
    if not user:
        raise HTTPException(401, "Not authenticated")
    
    if SessionLocal:
        with SessionLocal() as db:
            ready = db.query(SavedNumber).filter(
                SavedNumber.user_id == user["id"],
                SavedNumber.moved == True
            ).order_by(SavedNumber.created_at.desc()).all()
            result = []
            for s in ready:
                pool = db.query(Pool).filter(Pool.name == s.pool_name).first()
                in_pool = False
                if pool:
                    in_pool = db.query(ActiveNumber).filter(
                        ActiveNumber.pool_id == pool.id,
                        ActiveNumber.number == s.number
                    ).first() is not None
                result.append({
                    "id": s.id,
                    "number": s.number,
                    "country": s.country,
                    "pool_name": s.pool_name,
                    "pool_id": pool.id if pool else None,
                    "in_pool": in_pool
                })
            return result
    else:
        ready = [s for s in saved_numbers if s["user_id"] == user["id"] and s.get("moved", False)]
        ready.sort(key=lambda x: x["created_at"], reverse=True)
        result = []
        for s in ready:
            pool = next((p for p in pools.values() if p["name"] == s["pool_name"]), None)
            in_pool = pool and s["number"] in active_numbers.get(pool["id"], [])
            result.append({
                "id": s["id"],
                "number": s["number"],
                "country": s.get("country", ""),
                "pool_name": s["pool_name"],
                "pool_id": pool["id"] if pool else None,
                "in_pool": in_pool
            })
        return result

@app.put("/api/saved/{saved_id}")
def update_saved(saved_id: int, timer_minutes: int, request: Request):
    user = get_user_from_token(get_token(request))
    if not user:
        raise HTTPException(401, "Not authenticated")
    
    if SessionLocal:
        with SessionLocal() as db:
            saved = db.query(SavedNumber).filter(
                SavedNumber.id == saved_id,
                SavedNumber.user_id == user["id"]
            ).first()
            if not saved:
                raise HTTPException(404, "Not found")
            saved.expires_at = utcnow() + timedelta(minutes=timer_minutes)
            saved.moved = False
            db.commit()
    else:
        for s in saved_numbers:
            if s["id"] == saved_id and s["user_id"] == user["id"]:
                s["expires_at"] = (utcnow() + timedelta(minutes=timer_minutes)).isoformat()
                s["moved"] = False
                return {"ok": True}
        raise HTTPException(404, "Not found")
    
    return {"ok": True}

@app.delete("/api/saved/{saved_id}")
def delete_saved(saved_id: int, request: Request):
    user = get_user_from_token(get_token(request))
    if not user:
        raise HTTPException(401, "Not authenticated")
    
    if SessionLocal:
        with SessionLocal() as db:
            saved = db.query(SavedNumber).filter(
                SavedNumber.id == saved_id,
                SavedNumber.user_id == user["id"]
            ).first()
            if not saved:
                raise HTTPException(404, "Not found")
            db.delete(saved)
            db.commit()
    else:
        for i, s in enumerate(saved_numbers):
            if s["id"] == saved_id and s["user_id"] == user["id"]:
                saved_numbers.pop(i)
                return {"ok": True}
        raise HTTPException(404, "Not found")
    
    return {"ok": True}

@app.post("/api/saved/{saved_id}/next-number")
def ready_next_number(saved_id: int, request: Request):
    """Replace this ready number with the next available number from the same pool."""
    user = get_user_from_token(get_token(request))
    if not user:
        raise HTTPException(401, "Not authenticated")

    if SessionLocal:
        with SessionLocal() as db:
            saved = db.query(SavedNumber).filter(
                SavedNumber.id == saved_id,
                SavedNumber.user_id == user["id"],
                SavedNumber.moved == True
            ).first()
            if not saved:
                raise HTTPException(404, "Ready number not found")

            pool = db.query(Pool).filter(Pool.name == saved.pool_name).first()
            if not pool:
                raise HTTPException(404, "Pool not found")

            # Pick next number from the pool (excluding current one)
            next_num_row = db.query(ActiveNumber).filter(
                ActiveNumber.pool_id == pool.id,
                ActiveNumber.number != saved.number
            ).order_by(ActiveNumber.id.desc()).first()

            if not next_num_row:
                raise HTTPException(404, "No more numbers available in this pool")

            old_number = saved.number
            new_number = next_num_row.number

            # Remove next number from pool (it's now assigned to this saved slot)
            db.delete(next_num_row)
            # Put the old number back into the pool so others can use it
            existing_old = db.query(ActiveNumber).filter(ActiveNumber.number == old_number).first()
            if not existing_old:
                db.add(ActiveNumber(pool_id=pool.id, number=old_number))

            saved.number = new_number
            db.commit()
            return {"ok": True, "number": new_number, "pool_name": saved.pool_name}
    else:
        for s in saved_numbers:
            if s["id"] == saved_id and s["user_id"] == user["id"] and s.get("moved", False):
                pool = next((p for p in pools.values() if p["name"] == s["pool_name"]), None)
                if not pool:
                    raise HTTPException(404, "Pool not found")
                pid = pool["id"]
                nums = [n for n in active_numbers.get(pid, []) if n != s["number"]]
                if not nums:
                    raise HTTPException(404, "No more numbers available in this pool")
                old_number = s["number"]
                new_number = nums[-1]
                nums.pop()
                # Put old number back
                if old_number not in nums:
                    nums.append(old_number)
                active_numbers[pid] = nums
                s["number"] = new_number
                return {"ok": True, "number": new_number, "pool_name": s["pool_name"]}
        raise HTTPException(404, "Ready number not found")

@app.post("/api/saved/{saved_id}/switch-pool")
def ready_switch_pool(saved_id: int, new_pool_name: str, request: Request):
    """Move this ready number slot to a different pool, picking the next number from that pool."""
    user = get_user_from_token(get_token(request))
    if not user:
        raise HTTPException(401, "Not authenticated")

    new_pool_name = new_pool_name.strip()
    if not new_pool_name:
        raise HTTPException(400, "Pool name required")

    if SessionLocal:
        with SessionLocal() as db:
            saved = db.query(SavedNumber).filter(
                SavedNumber.id == saved_id,
                SavedNumber.user_id == user["id"],
                SavedNumber.moved == True
            ).first()
            if not saved:
                raise HTTPException(404, "Ready number not found")

            new_pool = db.query(Pool).filter(Pool.name == new_pool_name).first()
            if not new_pool:
                raise HTTPException(404, "Pool not found")

            # Pick next number from the new pool
            next_num_row = db.query(ActiveNumber).filter(
                ActiveNumber.pool_id == new_pool.id
            ).order_by(ActiveNumber.id.desc()).first()
            if not next_num_row:
                raise HTTPException(404, "No numbers available in that pool")

            old_pool = db.query(Pool).filter(Pool.name == saved.pool_name).first()
            old_number = saved.number
            new_number = next_num_row.number

            # Remove new number from new pool
            db.delete(next_num_row)
            # Return old number to old pool
            if old_pool:
                existing_old = db.query(ActiveNumber).filter(ActiveNumber.number == old_number).first()
                if not existing_old:
                    db.add(ActiveNumber(pool_id=old_pool.id, number=old_number))

            saved.number = new_number
            saved.pool_name = new_pool_name
            db.commit()
            return {"ok": True, "number": new_number, "pool_name": new_pool_name}
    else:
        new_pool = next((p for p in pools.values() if p["name"] == new_pool_name), None)
        if not new_pool:
            raise HTTPException(404, "Pool not found")
        pid_new = new_pool["id"]
        new_nums = active_numbers.get(pid_new, [])
        if not new_nums:
            raise HTTPException(404, "No numbers available in that pool")

        for s in saved_numbers:
            if s["id"] == saved_id and s["user_id"] == user["id"] and s.get("moved", False):
                old_pool = next((p for p in pools.values() if p["name"] == s["pool_name"]), None)
                old_number = s["number"]
                new_number = new_nums[-1]
                new_nums.pop()
                active_numbers[pid_new] = new_nums
                if old_pool:
                    old_nums = active_numbers.get(old_pool["id"], [])
                    if old_number not in old_nums:
                        old_nums.append(old_number)
                s["number"] = new_number
                s["pool_name"] = new_pool_name
                return {"ok": True, "number": new_number, "pool_name": new_pool_name}
        raise HTTPException(404, "Ready number not found")

@app.get("/api/saved/ready-pools")
def list_ready_pools(request: Request):
    """Return pools that have ready numbers (moved=True saved numbers), with their stock count."""
    user = get_user_from_token(get_token(request))
    if not user:
        raise HTTPException(401, "Not authenticated")

    if SessionLocal:
        with SessionLocal() as db:
            # Get all pool_names that have moved saved numbers for this user
            from sqlalchemy import distinct
            user_pool_names = [r.pool_name for r in db.query(SavedNumber.pool_name).filter(
                SavedNumber.user_id == user["id"],
                SavedNumber.moved == True
            ).distinct().all()]

            result = []
            for pname in user_pool_names:
                pool = db.query(Pool).filter(Pool.name == pname).first()
                if pool:
                    count = db.query(ActiveNumber).filter(ActiveNumber.pool_id == pool.id).count()
                    result.append({"pool_name": pname, "pool_id": pool.id, "count": count})
            return result
    else:
        user_pool_names = list({s["pool_name"] for s in saved_numbers
                                if s["user_id"] == user["id"] and s.get("moved", False)})
        result = []
        for pname in user_pool_names:
            pool = next((p for p in pools.values() if p["name"] == pname), None)
            if pool:
                count = len(active_numbers.get(pool["id"], []))
                result.append({"pool_name": pname, "pool_id": pool["id"], "count": count})
        return result


class TriggerMonitorRequest(BaseModel):
    number: str

@app.post("/api/saved/trigger-monitor")
async def trigger_saved_monitor(req: TriggerMonitorRequest, request: Request):
    """
    Trigger monitor bot for a saved/ready number.
    Looks up the pool from the number's saved_number record,
    gets group_id and match_format, sends monitor request.
    """
    user = get_user_from_token(get_token(request))
    if not user:
        raise HTTPException(401, "Not authenticated")

    number = req.number.strip()
    pool_info = None

    if SessionLocal:
        with SessionLocal() as db:
            # Find which pool this number belongs to via saved_numbers record
            saved = db.query(SavedNumber).filter(
                SavedNumber.user_id == user["id"],
                SavedNumber.number == number
            ).order_by(SavedNumber.created_at.desc()).first()
            if saved:
                pool = db.query(Pool).filter(Pool.name == saved.pool_name).first()
                if pool:
                    pool_info = {
                        "pool_id": pool.id,
                        "otp_group_id": pool.otp_group_id,
                        "match_format": pool.telegram_match_format or pool.match_format,
                        "uses_platform": pool.uses_platform
                    }
    else:
        saved = next((s for s in saved_numbers if s["user_id"] == user["id"] and s["number"] == number), None)
        if saved:
            pool = next((p for p in pools.values() if p["name"] == saved["pool_name"]), None)
            if pool:
                pool_info = {
                    "pool_id": pool["id"],
                    "otp_group_id": pool.get("otp_group_id"),
                    "match_format": pool.get("telegram_match_format") or pool.get("match_format", "5+4"),
                    "uses_platform": pool.get("uses_platform", 0)
                }

    if not pool_info:
        raise HTTPException(404, "Could not find pool for this number")

    if not pool_info["otp_group_id"]:
        raise HTTPException(400, "Pool has no OTP group configured")

    # Send monitor request — tag it as saved_number so result goes to saved OTP slot
    ok = await request_monitor_bot(
        number=number,
        group_id=pool_info["otp_group_id"],
        match_format=pool_info["match_format"],
        user_id=user["id"]
    )

    if pool_info["uses_platform"] in (1, 2):
        start_platform_monitor(user["id"], number, pool_info["match_format"])

    return {"ok": ok, "message": f"Monitoring started for {number}"}

# ══════════════════════════════════════════════════════════════════════════════
#  REVIEWS ENDPOINTS
# ══════════════════════════════════════════════════════════════════════════════

class ReviewRequest(BaseModel):
    number: str
    rating: int
    comment: str = ""
    mark_as_bad: bool = False

@app.post("/api/reviews")
def submit_review(req: ReviewRequest, request: Request):
    user = get_user_from_token(get_token(request))
    if not user:
        raise HTTPException(401, "Not authenticated")
    
    if not (1 <= req.rating <= 5):
        raise HTTPException(400, "Rating must be 1-5")
    
    if SessionLocal:
        with SessionLocal() as db:
            db.add(NumberReview(
                user_id=user["id"],
                number=req.number,
                rating=req.rating,
                comment=req.comment
            ))
            
            if req.mark_as_bad or req.rating == 1:
                existing = db.query(BadNumber).filter(BadNumber.number == req.number).first()
                if not existing:
                    db.add(BadNumber(
                        number=req.number,
                        reason=req.comment or "Flagged by user",
                        flagged_by=user["id"]
                    ))
                db.query(ActiveNumber).filter(ActiveNumber.number == req.number).delete()
            
            assignment = db.query(Assignment).filter(
                Assignment.user_id == user["id"],
                Assignment.number == req.number,
                Assignment.released_at == None
            ).first()
            if assignment:
                assignment.released_at = utcnow()
                assignment.feedback = req.comment or ("bad" if req.mark_as_bad else "ok")
            
            db.commit()
    else:
        global review_counter
        reviews.append({
            "id": _counters["review"],
            "user_id": user["id"],
            "number": req.number,
            "rating": req.rating,
            "comment": req.comment,
            "created_at": utcnow().isoformat()
        })
        _counters["review"] += 1
        
        if req.mark_as_bad or req.rating == 1:
            add_bad_number(req.number, user["id"], req.comment or "Flagged by user")
        
        current = get_current_assignment(user["id"])
        if current and current["number"] == req.number:
            release_assignment(user["id"])
    
    return {"ok": True, "marked_bad": req.mark_as_bad or req.rating == 1}

# ══════════════════════════════════════════════════════════════════════════════
#  ADMIN ENDPOINTS
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/api/admin/stats")
def stats(request: Request):
    user = get_user_from_token(get_token(request))
    if not user or not user["is_admin"]:
        raise HTTPException(403, "Admin only")
    
    if SessionLocal:
        with SessionLocal() as db:
            return {
                "total_users": db.query(User).count(),
                "pending_approval": db.query(User).filter(User.is_approved == False, User.is_blocked == False).count(),
                "total_pools": db.query(Pool).count(),
                "total_numbers": db.query(ActiveNumber).count(),
                "total_otps": db.query(OTPLog).count(),
                "total_assignments": db.query(Assignment).count(),
                "bad_numbers": db.query(BadNumber).count(),
                "saved_numbers": db.query(SavedNumber).filter(SavedNumber.moved == False).count(),
                "online_users": sum(len(conns) for conns in user_connections.values())
            }
    else:
        return {
            "total_users": len(users),
            "pending_approval": sum(1 for u in users.values() if not u["is_approved"] and not u.get("is_blocked", False)),
            "total_pools": len(pools),
            "total_numbers": sum(len(n) for n in active_numbers.values()),
            "total_otps": len(otp_logs),
            "total_assignments": len(archived_numbers),
            "bad_numbers": len(bad_numbers),
            "saved_numbers": len([s for s in saved_numbers if not s.get("moved", False)]),
            "online_users": len(user_connections)
        }

@app.get("/api/admin/users")
def list_users(request: Request):
    user = get_user_from_token(get_token(request))
    if not user or not user["is_admin"]:
        raise HTTPException(403, "Admin only")
    
    if SessionLocal:
        with SessionLocal() as db:
            users_list = db.query(User).order_by(User.created_at.desc()).all()
            return [{"id": u.id, "username": u.username, "is_admin": u.is_admin, "is_approved": u.is_approved, "is_blocked": u.is_blocked, "created_at": u.created_at.isoformat()} for u in users_list]
    else:
        return [{"id": u["id"], "username": u["username"], "is_admin": u["is_admin"], "is_approved": u["is_approved"], "is_blocked": u.get("is_blocked", False), "created_at": u.get("created_at", utcnow().isoformat())} for u in users.values()]

@app.post("/api/admin/users/{user_id}/approve")
async def approve_user_endpoint(user_id: int, request: Request):
    user = get_user_from_token(get_token(request))
    if not user or not user["is_admin"]:
        raise HTTPException(403, "Admin only")
    approve_user(user_id)
    await send_to_user(user_id, {"type": "notification", "message": "✅ Your account has been approved!"})
    return {"ok": True}

@app.post("/api/admin/users/{user_id}/block")
async def block_user_endpoint(user_id: int, request: Request):
    user = get_user_from_token(get_token(request))
    if not user or not user["is_admin"]:
        raise HTTPException(403, "Admin only")
    block_user(user_id)
    await send_to_user(user_id, {"type": "notification", "message": "🚫 Your account has been blocked."})
    return {"ok": True}

@app.post("/api/admin/users/{user_id}/unblock")
async def unblock_user_endpoint(user_id: int, request: Request):
    user = get_user_from_token(get_token(request))
    if not user or not user["is_admin"]:
        raise HTTPException(403, "Admin only")
    unblock_user(user_id)
    await send_to_user(user_id, {"type": "notification", "message": "✅ Your account has been unblocked!"})
    return {"ok": True}

@app.get("/api/admin/bad-numbers")
def list_bad_numbers(request: Request):
    user = get_user_from_token(get_token(request))
    if not user or not user["is_admin"]:
        raise HTTPException(403, "Admin only")
    
    if SessionLocal:
        with SessionLocal() as db:
            bad = db.query(BadNumber).order_by(BadNumber.created_at.desc()).all()
            return [{"number": b.number, "reason": b.reason, "created_at": b.created_at.isoformat()} for b in bad]
    else:
        return [{"number": num, "reason": data.get("reason", ""), "created_at": data.get("marked_at", "")} for num, data in bad_numbers.items()]

@app.delete("/api/admin/bad-numbers")
def remove_bad_number(number: str, request: Request):
    user = get_user_from_token(get_token(request))
    if not user or not user["is_admin"]:
        raise HTTPException(403, "Admin only")
    
    if SessionLocal:
        with SessionLocal() as db:
            db.query(BadNumber).filter(BadNumber.number == number).delete()
            db.commit()
    else:
        bad_numbers.pop(number, None)
    
    return {"ok": True}

@app.get("/api/admin/reviews")
def list_reviews(request: Request):
    user = get_user_from_token(get_token(request))
    if not user or not user["is_admin"]:
        raise HTTPException(403, "Admin only")
    
    if SessionLocal:
        with SessionLocal() as db:
            reviews_list = db.query(NumberReview).order_by(NumberReview.created_at.desc()).limit(100).all()
            return [{"id": r.id, "user_id": r.user_id, "number": r.number, "rating": r.rating, "comment": r.comment, "created_at": r.created_at.isoformat()} for r in reviews_list]
    else:
        return [{"id": r["id"], "user_id": r["user_id"], "number": r["number"], "rating": r["rating"], "comment": r["comment"], "created_at": r["created_at"]} for r in sorted(reviews, key=lambda x: x["created_at"], reverse=True)[:100]]

@app.post("/api/admin/broadcast")
async def broadcast_message(message: str, request: Request):
    user = get_user_from_token(get_token(request))
    if not user or not user["is_admin"]:
        raise HTTPException(403, "Admin only")
    
    await broadcast_all({"type": "broadcast", "message": message})
    return {"ok": True}

@app.post("/api/admin/settings/approval")
def set_approval_mode_endpoint(enabled: bool, request: Request):
    user = get_user_from_token(get_token(request))
    if not user or not user["is_admin"]:
        raise HTTPException(403, "Admin only")
    
    set_approval_mode(enabled)
    return {"ok": True, "mode": "on" if enabled else "off"}

@app.post("/api/admin/settings/otp-redirect")
def set_otp_redirect_mode_endpoint(mode: str, request: Request):
    user = get_user_from_token(get_token(request))
    if not user or not user["is_admin"]:
        raise HTTPException(403, "Admin only")
    
    if mode not in ["pool", "hardcoded"]:
        raise HTTPException(400, "Mode must be 'pool' or 'hardcoded'")
    
    set_otp_redirect_mode(mode)
    return {"ok": True, "mode": mode}

# ══════════════════════════════════════════════════════════════════════════════
#  CUSTOM BUTTONS ENDPOINTS
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/api/buttons")
def get_buttons(request: Request):
    user = get_user_from_token(get_token(request))
    if not user:
        raise HTTPException(401, "Not authenticated")
    
    return _get_custom_buttons()

@app.post("/api/admin/buttons")
def add_button(label: str, url: str, request: Request):
    user = get_user_from_token(get_token(request))
    if not user or not user["is_admin"]:
        raise HTTPException(403, "Admin only")
    
    if SessionLocal:
        with SessionLocal() as db:
            max_pos = db.query(func.max(CustomButton.position)).scalar() or 0
            db.add(CustomButton(label=label, url=url, position=max_pos + 1))
            db.commit()
    else:
        global button_counter
        custom_buttons.append({"id": _counters["button"], "label": label, "url": url, "position": len(custom_buttons)})
        _counters["button"] += 1
    
    return {"ok": True}

@app.delete("/api/admin/buttons/{button_id}")
def delete_button(button_id: int, request: Request):
    user = get_user_from_token(get_token(request))
    if not user or not user["is_admin"]:
        raise HTTPException(403, "Admin only")
    
    if SessionLocal:
        with SessionLocal() as db:
            db.query(CustomButton).filter(CustomButton.id == button_id).delete()
            db.commit()
    else:
        for i, b in enumerate(custom_buttons):
            if b["id"] == button_id:
                custom_buttons.pop(i)
                return {"ok": True}
        raise HTTPException(404, "Button not found")
    
    return {"ok": True}

# ══════════════════════════════════════════════════════════════════════════════
#  WEBSOCKET ENDPOINTS
# ══════════════════════════════════════════════════════════════════════════════

@app.websocket("/ws/user/{user_id}")
async def websocket_user(websocket: WebSocket, user_id: int):
    await connect_user(websocket, user_id)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        disconnect_user(websocket, user_id)

@app.websocket("/ws/feed")
async def websocket_feed(websocket: WebSocket):
    await connect_feed(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        disconnect_feed(websocket)

# ══════════════════════════════════════════════════════════════════════════════
#  MONITOR RESULT ALIAS — numberbot POSTs directly here
# ══════════════════════════════════════════════════════════════════════════════

class MonitorResultInbound(BaseModel):
    number: str
    otp: str
    raw_message: str = ""
    user_id: int
    secret: str = ""

@app.post("/monitor-result")
async def monitor_result_inbound(payload: MonitorResultInbound):
    """Alias for /api/otp/monitor-result — accepts numberbot's direct POST."""
    if SHARED_SECRET and payload.secret != SHARED_SECRET:
        raise HTTPException(403, "Invalid secret")
    fake = MonitorResultPayload(
        number=payload.number, otp=payload.otp,
        raw_message=payload.raw_message, user_id=payload.user_id,
        secret=payload.secret
    )
    return await monitor_result(fake)


# ══════════════════════════════════════════════════════════════════════════════
#  ADMIN — DENY USER
# ══════════════════════════════════════════════════════════════════════════════

@app.post("/api/admin/users/{user_id}/deny")
async def deny_user_endpoint(user_id: int, request: Request):
    user = get_user_from_token(get_token(request))
    if not user or not user["is_admin"]:
        raise HTTPException(403, "Admin only")
    deny_user(user_id)
    await send_to_user(user_id, {"type": "notification", "message": "❌ Your access has been denied."})
    return {"ok": True}


# ══════════════════════════════════════════════════════════════════════════════
#  ADMIN — BLOCK ALL USERS
# ══════════════════════════════════════════════════════════════════════════════

@app.post("/api/admin/block-all")
async def block_all_users(request: Request):
    user = get_user_from_token(get_token(request))
    if not user or not user["is_admin"]:
        raise HTTPException(403, "Admin only")
    blocked = 0
    if SessionLocal:
        with SessionLocal() as db:
            non_admins = db.query(User).filter(User.is_admin == False, User.is_blocked == False).all()
            for u in non_admins:
                u.is_blocked = True
                u.is_approved = False
                blocked += 1
            db.commit()
    else:
        for uid, u in users.items():
            if not u.get("is_admin") and not u.get("is_blocked"):
                u["is_blocked"] = True
                u["is_approved"] = False
                blocked += 1
    await broadcast_all({"type": "notification", "message": "🚫 Access suspended by administrator."})
    log.info(f"[Admin] Block all — {blocked} users blocked")
    return {"ok": True, "blocked": blocked}


# ══════════════════════════════════════════════════════════════════════════════
#  ADMIN — USER INFO BY ID
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/api/admin/users/{user_id}/info")
def user_info(user_id: int, request: Request):
    user = get_user_from_token(get_token(request))
    if not user or not user["is_admin"]:
        raise HTTPException(403, "Admin only")
    if SessionLocal:
        with SessionLocal() as db:
            u = db.query(User).filter(User.id == user_id).first()
            if not u:
                raise HTTPException(404, "User not found")
            nums_used = db.query(Assignment).filter(Assignment.user_id == user_id).count()
            return {
                "id": u.id, "username": u.username,
                "is_admin": u.is_admin, "is_approved": u.is_approved,
                "is_blocked": u.is_blocked, "nums_used": nums_used,
                "created_at": u.created_at.isoformat()
            }
    else:
        u = users.get(user_id)
        if not u:
            raise HTTPException(404, "User not found")
        nums_used = sum(1 for a in archived_numbers if a["user_id"] == user_id)
        return {**u, "nums_used": nums_used}


# ══════════════════════════════════════════════════════════════════════════════
#  ADMIN — PLATFORM STOCK FILES
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/api/admin/platform-files")
async def platform_files(request: Request):
    """Fetch all numbers from platform API grouped by country."""
    user = get_user_from_token(get_token(request))
    if not user or not user["is_admin"]:
        raise HTTPException(403, "Admin only")
    token_val = await _get_platform_token()
    if not token_val:
        raise HTTPException(503, "Platform login failed")
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{PLATFORM_URL}/api/numbers?limit=500000",
                headers={"Authorization": f"Bearer {token_val}"},
                timeout=aiohttp.ClientTimeout(total=60)
            ) as resp:
                if resp.status != 200:
                    raise HTTPException(502, f"Platform returned {resp.status}")
                all_numbers = await resp.json()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(503, str(e))
    country_map: Dict[str, List[str]] = defaultdict(list)
    for entry in (all_numbers if isinstance(all_numbers, list) else []):
        country = entry.get("country", "Unknown").strip()
        phone = entry.get("phone_number", "").strip()
        if phone:
            country_map[country].append(phone)
    return [{"country": k, "count": len(v), "numbers": v} for k, v in sorted(country_map.items())]


# ══════════════════════════════════════════════════════════════════════════════
#  DIAGNOSTIC ENDPOINT
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/api/test")
def test_endpoint():
    """Quick connectivity test — verifies DB and admin status."""
    db_ok = False
    admin_exists = False
    admin_approved = False
    if SessionLocal:
        try:
            with SessionLocal() as db:
                db_ok = True
                admin = db.query(User).filter(User.username == "admin").first()
                if admin:
                    admin_exists = True
                    admin_approved = admin.is_approved
                    if not admin.is_approved or not admin.is_admin:
                        admin.is_approved = True
                        admin.is_admin = True
                        db.commit()
                        admin_approved = True
                        log.info("[test] Auto-fixed admin approval")
        except Exception as e:
            return {"status": "db_error", "error": str(e)}
    else:
        db_ok = True
        admin_exists = True
        admin_approved = True
    return {
        "status": "ok",
        "db": db_ok,
        "admin_exists": admin_exists,
        "admin_approved": admin_approved,
        "message": "If admin_approved is false, it has been auto-fixed — try logging in now."
    }


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
