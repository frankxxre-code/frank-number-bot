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
from fastapi import (FastAPI, WebSocket, WebSocketDisconnect,
                     Depends, HTTPException, Cookie,
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
    if SessionLocal:
        with SessionLocal() as db:
            now = utcnow()
            expired = db.query(SavedNumber).filter(
                SavedNumber.expires_at <= now,
                SavedNumber.moved == False
            ).all()
            
            for sn in expired:
                pool = db.query(Pool).filter(Pool.name == sn.pool_name).first()
                if not pool:
                    pool = Pool(
                        name=sn.pool_name,
                        country_code=sn.country or "unknown",
                        match_format="5+4"
                    )
                    db.add(pool)
                    db.flush()
                
                bad = db.query(BadNumber).filter(BadNumber.number == sn.number).first()
                if not bad:
                    exists = db.query(ActiveNumber).filter(ActiveNumber.number == sn.number).first()
                    if not exists:
                        db.add(ActiveNumber(pool_id=pool.id, number=sn.number))
                
                sn.moved = True
            
            if expired:
                db.commit()
                log.info(f"[Scheduler] Moved {len(expired)} expired saved numbers")
    else:
        now = utcnow()
        expired = [s for s in saved_numbers if not s.get("moved", False) and 
                   datetime.fromisoformat(s["expires_at"]) <= now]
        
        for sn in expired:
            pool = None
            for p in pools.values():
                if p["name"] == sn["pool_name"]:
                    pool = p
                    break
            
            if not pool:
                pool_id = _counters["pool"]
                _counters["pool"] += 1
                pool = {
                    "id": pool_id,
                    "name": sn["pool_name"],
                    "country_code": sn.get("country", "unknown"),
                    "otp_group_id": None,
                    "otp_link": "",
                    "match_format": "5+4",
                    "telegram_match_format": "",
                    "uses_platform": 0,
                    "is_paused": False,
                    "pause_reason": "",
                    "trick_text": "",
                    "is_admin_only": False,
                    "last_restocked": utcnow().isoformat(),
                    "created_at": utcnow().isoformat()
                }
                pools[pool_id] = pool
                active_numbers[pool_id] = []
            
            if sn["number"] not in bad_numbers:
                if sn["number"] not in active_numbers.get(pool["id"], []):
                    active_numbers.setdefault(pool["id"], []).append(sn["number"])
            
            sn["moved"] = True
        
        if expired:
            log.info(f"[Scheduler] Moved {len(expired)} expired saved numbers")

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

@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("Starting NEON GRID NETWORK...")
    init_db()
    asyncio.create_task(scheduler())
    asyncio.create_task(_platform_stock_watcher())
    log.info("✅ Scheduler and stock watcher started")
    yield
    log.info("Shutting down...")

app = FastAPI(title="NEON GRID NETWORK", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[FRONTEND_URL] if FRONTEND_URL != "*" else ["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ══════════════════════════════════════════════════════════════════════════════
#  FRONTEND - EMBEDDED HTML (Complete)
# ══════════════════════════════════════════════════════════════════════════════

FRONTEND_HTML = '''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no, viewport-fit=cover">
    <title>NEON GRID NETWORK</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
        body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: radial-gradient(circle at 10% 20%, #0a0f1a, #03060c); min-height: 100vh; padding-bottom: 70px; color: #eef2ff; }
        .status-bar { background: rgba(10,132,255,0.95); backdrop-filter: blur(10px); padding: 12px 16px 8px; color: white; font-size: 14px; font-weight: 500; display: flex; justify-content: space-between; position: sticky; top: 0; z-index: 100; }
        .header { background: rgba(15,25,35,0.8); backdrop-filter: blur(10px); padding: 16px; border-bottom: 1px solid rgba(10,132,255,0.2); }
        .user-info { display: flex; justify-content: space-between; align-items: center; }
        .user-name { font-size: 20px; font-weight: 700; background: linear-gradient(135deg, #0a84ff, #5e2aff); -webkit-background-clip: text; -webkit-text-fill-color: transparent; }
        .user-id { font-size: 12px; color: #7e8a9a; background: rgba(30,41,59,0.6); padding: 4px 10px; border-radius: 20px; }
        .balance-card { background: linear-gradient(135deg, #0a84ff, #5e2aff); border-radius: 28px; padding: 24px; margin: 16px; color: white; box-shadow: 0 20px 35px -10px rgba(10,132,255,0.3); }
        .balance-label { font-size: 14px; opacity: 0.8; letter-spacing: 1px; margin-bottom: 8px; }
        .balance-amount { font-size: 48px; font-weight: 800; letter-spacing: -1px; margin-bottom: 8px; }
        .balance-sub { font-size: 12px; opacity: 0.7; }
        .withdraw-btn { background: rgba(255,255,255,0.2); border: 1px solid rgba(255,255,255,0.3); padding: 12px 24px; border-radius: 40px; color: white; font-weight: 600; font-size: 14px; margin-top: 16px; display: inline-block; cursor: pointer; transition: all 0.2s; }
        .withdraw-btn:active { transform: scale(0.96); background: rgba(255,255,255,0.3); }
        .section-title { font-size: 16px; font-weight: 600; color: #7e8a9a; padding: 20px 16px 12px; letter-spacing: 0.5px; text-transform: uppercase; }
        .menu-grid { display: grid; grid-template-columns: repeat(2, 1fr); gap: 12px; padding: 8px 16px; }
        .menu-card { background: rgba(20,30,45,0.6); backdrop-filter: blur(10px); border-radius: 20px; padding: 20px 16px; text-align: center; cursor: pointer; border: 1px solid rgba(10,132,255,0.2); transition: all 0.2s; }
        .menu-card:active { transform: scale(0.97); background: rgba(30,45,65,0.8); border-color: rgba(10,132,255,0.5); }
        .menu-icon { font-size: 36px; margin-bottom: 8px; }
        .menu-label { font-size: 14px; font-weight: 600; color: #eef2ff; }
        .menu-desc { font-size: 11px; color: #7e8a9a; margin-top: 4px; }
        .number-card { background: rgba(20,30,45,0.6); backdrop-filter: blur(10px); margin: 16px; border-radius: 28px; padding: 24px; border: 1px solid rgba(10,132,255,0.2); }
        .number-label { font-size: 12px; color: #7e8a9a; text-transform: uppercase; letter-spacing: 1px; margin-bottom: 12px; }
        .number-value { font-size: 28px; font-weight: 700; font-family: monospace; color: #0a84ff; word-break: break-all; margin-bottom: 16px; }
        .region-badge { display: inline-block; background: rgba(30,41,59,0.8); padding: 6px 14px; border-radius: 30px; font-size: 12px; color: #9ca3af; }
        .otp-card { background: linear-gradient(135deg, #10b981, #059669); margin: 16px; border-radius: 28px; padding: 24px; color: white; animation: slideIn 0.3s ease; box-shadow: 0 10px 25px -5px rgba(16,185,129,0.3); }
        @keyframes slideIn { from { opacity: 0; transform: translateY(20px); } to { opacity: 1; transform: translateY(0); } }
        .otp-code { font-size: 52px; font-weight: 800; font-family: monospace; letter-spacing: 8px; text-align: center; margin: 16px 0; cursor: pointer; }
        .otp-timer { text-align: center; font-size: 13px; opacity: 0.9; }
        .otp-message { font-size: 11px; opacity: 0.8; text-align: center; margin-top: 8px; word-break: break-all; }
        .region-list { padding: 8px 16px; }
        .region-item { background: rgba(20,30,45,0.6); backdrop-filter: blur(10px); border-radius: 20px; padding: 16px; margin-bottom: 10px; display: flex; justify-content: space-between; align-items: center; border: 1px solid rgba(10,132,255,0.2); cursor: pointer; transition: all 0.2s; }
        .region-item:active { background: rgba(30,45,65,0.8); transform: scale(0.98); }
        .region-name { font-weight: 600; color: #eef2ff; }
        .region-code { font-size: 12px; color: #7e8a9a; margin-top: 4px; }
        .region-count { background: rgba(10,132,255,0.2); padding: 4px 12px; border-radius: 30px; font-size: 12px; font-weight: 600; color: #0a84ff; }
        .filter-row { display: flex; gap: 12px; padding: 12px 16px; background: rgba(20,30,45,0.6); backdrop-filter: blur(10px); margin: 8px 16px; border-radius: 50px; border: 1px solid rgba(10,132,255,0.2); }
        .filter-input { flex: 1; border: none; outline: none; font-size: 14px; background: transparent; color: #eef2ff; }
        .filter-input::placeholder { color: #5a6a7a; }
        .filter-btn { background: linear-gradient(135deg, #0a84ff, #5e2aff); color: white; border: none; padding: 8px 20px; border-radius: 40px; font-size: 13px; font-weight: 600; cursor: pointer; transition: all 0.2s; }
        .filter-btn:active { transform: scale(0.96); }
        .saved-list { padding: 8px 16px; }
        .saved-item { background: rgba(20,30,45,0.6); backdrop-filter: blur(10px); border-radius: 20px; padding: 16px; margin-bottom: 10px; display: flex; justify-content: space-between; align-items: center; border: 1px solid rgba(10,132,255,0.2); }
        .saved-number { font-family: monospace; font-weight: 600; color: #0a84ff; }
        .saved-timer { font-size: 12px; color: #7e8a9a; margin-top: 4px; }
        .timer-badge { padding: 4px 12px; border-radius: 30px; font-size: 11px; font-weight: 600; }
        .timer-green { background: rgba(16,185,129,0.2); color: #10b981; }
        .timer-yellow { background: rgba(245,158,11,0.2); color: #f59e0b; }
        .timer-red { background: rgba(239,68,68,0.2); color: #ef4444; }
        .timer-ready { background: rgba(10,132,255,0.2); color: #0a84ff; }
        .history-item { background: rgba(20,30,45,0.6); backdrop-filter: blur(10px); border-radius: 20px; padding: 16px; margin-bottom: 10px; border: 1px solid rgba(10,132,255,0.2); }
        .history-number { font-family: monospace; font-size: 12px; color: #7e8a9a; }
        .history-otp { font-size: 24px; font-weight: 700; font-family: monospace; color: #10b981; margin: 8px 0; cursor: pointer; }
        .history-time { font-size: 11px; color: #5a6a7a; margin-top: 4px; }
        .bottom-nav { position: fixed; bottom: 0; left: 0; right: 0; background: rgba(10,15,25,0.95); backdrop-filter: blur(10px); display: flex; justify-content: space-around; padding: 8px 16px 20px; border-top: 1px solid rgba(10,132,255,0.2); z-index: 100; }
        .nav-item { display: flex; flex-direction: column; align-items: center; gap: 4px; cursor: pointer; padding: 8px 16px; border-radius: 40px; transition: all 0.2s; }
        .nav-item:active { background: rgba(10,132,255,0.1); }
        .nav-icon { font-size: 24px; }
        .nav-label { font-size: 11px; font-weight: 500; color: #7e8a9a; }
        .nav-item.active .nav-label { color: #0a84ff; }
        .page { display: none; padding-bottom: 20px; }
        .page.active { display: block; }
        .toast { position: fixed; bottom: 100px; left: 50%; transform: translateX(-50%); background: #1e293b; color: white; padding: 12px 24px; border-radius: 60px; font-size: 14px; z-index: 1000; max-width: 90%; text-align: center; animation: fadeInOut 2s ease; }
        @keyframes fadeInOut { 0% { opacity: 0; transform: translateX(-50%) translateY(20px); } 15% { opacity: 1; } 85% { opacity: 1; } 100% { opacity: 0; transform: translateX(-50%) translateY(-20px); } }
        .loading { text-align: center; padding: 40px; color: #7e8a9a; }
        .spinner { width: 40px; height: 40px; border: 3px solid rgba(10,132,255,0.2); border-top-color: #0a84ff; border-radius: 50%; animation: spin 0.8s linear infinite; margin: 0 auto 12px; }
        @keyframes spin { to { transform: rotate(360deg); } }
        .modal { display: none; position: fixed; top: 0; left: 0; right: 0; bottom: 0; background: rgba(0,0,0,0.8); backdrop-filter: blur(5px); z-index: 1000; align-items: center; justify-content: center; }
        .modal.show { display: flex; }
        .modal-content { background: #0f172a; border-radius: 32px; max-height: 85vh; overflow-y: auto; width: 100%; max-width: 500px; margin: 20px; border: 1px solid rgba(10,132,255,0.3); }
        .modal-header { padding: 20px; border-bottom: 1px solid rgba(10,132,255,0.2); font-weight: 700; font-size: 18px; color: #eef2ff; }
        .feedback-grid { display: grid; grid-template-columns: repeat(2, 1fr); gap: 12px; padding: 20px; }
        .feedback-btn { background: rgba(30,41,59,0.8); border: none; padding: 14px; border-radius: 50px; font-size: 14px; font-weight: 500; cursor: pointer; color: #eef2ff; transition: all 0.2s; }
        .feedback-btn:active { transform: scale(0.96); }
        .feedback-btn.bad { background: rgba(239,68,68,0.2); color: #f87171; }
        .feedback-btn.good { background: rgba(16,185,129,0.2); color: #34d399; }
        .auth-container { min-height: 100vh; display: flex; align-items: center; justify-content: center; padding: 20px; background: radial-gradient(circle at 30% 20%, #0a84ff, #03060c); }
        .auth-card { background: rgba(15,25,35,0.9); backdrop-filter: blur(10px); border-radius: 48px; padding: 40px 32px; width: 100%; max-width: 340px; border: 1px solid rgba(10,132,255,0.3); }
        .auth-logo { text-align: center; font-size: 56px; margin-bottom: 24px; }
        .auth-input { width: 100%; padding: 16px; border: 1px solid rgba(10,132,255,0.3); border-radius: 60px; font-size: 16px; margin-bottom: 16px; outline: none; background: rgba(30,41,59,0.6); color: #eef2ff; }
        .auth-input:focus { border-color: #0a84ff; }
        .auth-btn { width: 100%; background: linear-gradient(135deg, #0a84ff, #5e2aff); color: white; border: none; padding: 16px; border-radius: 60px; font-size: 16px; font-weight: 700; margin-top: 8px; cursor: pointer; transition: all 0.2s; }
        .auth-btn:active { transform: scale(0.97); }
        .error-msg { color: #f87171; font-size: 12px; margin-top: 8px; text-align: center; }
        .admin-badge { background: linear-gradient(135deg, #f59e0b, #d97706); color: white; padding: 4px 12px; border-radius: 30px; font-size: 10px; font-weight: 600; margin-left: 8px; }
        .admin-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(140px, 1fr)); gap: 12px; padding: 8px 16px; }
        .admin-card { background: rgba(20,30,45,0.6); backdrop-filter: blur(10px); border-radius: 20px; padding: 16px; cursor: pointer; border: 1px solid rgba(10,132,255,0.2); text-align: center; transition: all 0.2s; }
        .admin-card:active { transform: scale(0.96); background: rgba(30,45,65,0.8); }
        .admin-icon { font-size: 28px; margin-bottom: 8px; }
        .admin-label { font-size: 12px; font-weight: 600; color: #eef2ff; }
        .btn-sm { padding: 6px 12px; font-size: 12px; border-radius: 30px; }
        .btn { padding: 10px 20px; border-radius: 40px; font-size: 13px; font-weight: 500; cursor: pointer; border: none; transition: all 0.2s; }
        .btn-primary { background: linear-gradient(135deg, #0a84ff, #5e2aff); color: white; }
        .btn-danger { background: rgba(239,68,68,0.8); color: white; }
        .btn-secondary { background: rgba(30,41,59,0.8); color: #eef2ff; }
        .fg { margin-bottom: 20px; }
        .fg label { display: block; font-size: 12px; font-weight: 600; color: #7e8a9a; margin-bottom: 8px; text-transform: uppercase; letter-spacing: 1px; }
        .fg input, .fg select, .fg textarea { width: 100%; padding: 14px; border: 1px solid rgba(10,132,255,0.3); border-radius: 20px; background: rgba(30,41,59,0.6); color: #eef2ff; outline: none; font-size: 14px; }
        .fg input:focus, .fg select:focus { border-color: #0a84ff; }
        .brow { display: flex; gap: 12px; justify-content: flex-end; margin-top: 20px; }
        .hidden { display: none; }
        .timer-input-group { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
        .timer-input-group input { flex: 1; min-width: 100px; }
        .timer-preset { display: flex; gap: 8px; margin-top: 8px; flex-wrap: wrap; }
        .preset-btn { background: rgba(30,41,59,0.6); border: 1px solid rgba(10,132,255,0.3); padding: 8px 12px; border-radius: 30px; font-size: 12px; cursor: pointer; transition: all 0.2s; color: #eef2ff; }
        .preset-btn:active { transform: scale(0.95); background: rgba(10,132,255,0.2); }
    </style>
</head>
<body>

<div id="authContainer" style="display: flex;">
    <div class="auth-container">
        <div class="auth-card">
            <div class="auth-logo">⚡</div>
            <h2 style="text-align: center; margin-bottom: 16px; background: linear-gradient(135deg, #0a84ff, #5e2aff); -webkit-background-clip: text; -webkit-text-fill-color: transparent;">NEON GRID</h2>
            <div style="display: flex; gap: 12px; margin-bottom: 24px;">
                <button id="authLoginTab" class="auth-btn" style="background: #0a84ff; margin: 0; padding: 12px;">Login</button>
                <button id="authRegisterTab" class="auth-btn" style="background: rgba(30,41,59,0.6); color: #9ca3af; margin: 0; padding: 12px;">Register</button>
            </div>
            <div id="loginForm">
                <input type="text" id="loginUsername" class="auth-input" placeholder="Username">
                <input type="password" id="loginPassword" class="auth-input" placeholder="Password">
                <button class="auth-btn" onclick="doLogin()">Login</button>
                <div id="loginError" class="error-msg"></div>
            </div>
            <div id="registerForm" style="display: none;">
                <input type="text" id="regUsername" class="auth-input" placeholder="Username">
                <input type="password" id="regPassword" class="auth-input" placeholder="Password (min 6 chars)">
                <button class="auth-btn" onclick="doRegister()">Create Account</button>
                <div id="regError" class="error-msg"></div>
            </div>
        </div>
    </div>
</div>

<div id="appContainer" style="display: none;">
    <div class="status-bar"><span id="currentTime">--:--</span><span>⚡ NEON GRID</span><span>●</span></div>
    <div class="header"><div class="user-info"><div><div class="user-name" id="userName">User</div><div class="user-id" id="userId">ID: --</div></div><div id="adminBadge" style="display: none;"><span class="admin-badge">ADMIN</span></div></div></div>

    <div id="homePage" class="page active">
        <div class="balance-card"><div class="balance-label">ACCOUNT BALANCE</div><div class="balance-amount" id="balanceAmount">0</div><div class="balance-sub">≈ ₱0.00 NGN</div><div class="withdraw-btn" onclick="showToast('Withdrawal feature coming soon')">Withdraw Now</div></div>
        <div class="section-title">ACCOUNT MANAGEMENT</div>
        <div class="menu-grid">
            <div class="menu-card" onclick="showToast('Withdrawal feature coming soon')"><div class="menu-icon">💰</div><div class="menu-label">Withdraw</div><div class="menu-desc">Convert points to cash</div></div>
            <div class="menu-card" onclick="showToast('Earnings details coming soon')"><div class="menu-icon">📊</div><div class="menu-label">Earnings Details</div><div class="menu-desc">View your earning records</div></div>
            <div class="menu-card" onclick="showToast('Withdrawal history coming soon')"><div class="menu-icon">📦</div><div class="menu-label">Withdrawal Orders</div><div class="menu-desc">Track your withdrawals</div></div>
            <div class="menu-card" onclick="showToast('Leaderboard coming soon')"><div class="menu-icon">🏆</div><div class="menu-label">Daily Lead Card</div><div class="menu-desc">Top performers</div></div>
        </div>
    </div>

    <div id="numbersPage" class="page">
        <div class="number-card" id="currentNumberCard">
            <div class="number-label">YOUR ACTIVE NUMBER</div>
            <div class="number-value" id="currentNumber">—</div>
            <div style="display: flex; justify-content: space-between; align-items: center;">
                <span class="region-badge" id="currentRegion">No region selected</span>
                <div style="display: flex; gap: 12px;">
                    <button class="withdraw-btn" style="background: rgba(30,41,59,0.8); color: #eef2ff; padding: 8px 16px; font-size: 13px;" onclick="changeNumber()">🔄 Change</button>
                    <button class="withdraw-btn" style="background: rgba(30,41,59,0.8); color: #eef2ff; padding: 8px 16px; font-size: 13px;" onclick="copyNumber()">📋 Copy</button>
                </div>
            </div>
        </div>
        <div id="otpDisplay" style="display: none;"></div>
        <div class="section-title">SELECT REGION</div>
        <div class="filter-row"><input type="text" id="prefixFilter" class="filter-input" placeholder="Filter by prefix (e.g., 8101)"><button class="filter-btn" onclick="applyFilter()">Filter</button></div>
        <div id="regionList" class="region-list"><div class="loading"><div class="spinner"></div>Loading regions...</div></div>
    </div>

    <div id="savedPage" class="page">
        <div class="section-title">💾 SAVED NUMBERS</div>
        <div style="padding: 0 16px 16px;">
            <div class="filter-row" style="margin: 0 0 12px 0;">
                <textarea id="savedNumbersInput" class="filter-input" placeholder="Enter numbers (one per line)" style="height: 80px; resize: vertical;"></textarea>
            </div>
            <div class="filter-row" style="margin: 0 0 12px 0;">
                <div class="timer-input-group">
                    <input type="text" id="timerInput" class="filter-input" placeholder="Timer (e.g., 30m, 2h, 1d, 2s, 120)" value="30m">
                    <button class="filter-btn" onclick="setTimerPreset('30m')">30m</button>
                    <button class="filter-btn" onclick="setTimerPreset('2h')">2h</button>
                    <button class="filter-btn" onclick="setTimerPreset('1d')">1d</button>
                    <button class="filter-btn" onclick="setTimerPreset('2s')">2s</button>
                </div>
            </div>
            <div class="filter-row" style="margin: 0 0 12px 0;">
                <input type="text" id="poolNameInput" class="filter-input" placeholder="Pool name">
                <button class="filter-btn" onclick="saveNumbers()">Save</button>
            </div>
        </div>
        <div class="section-title" style="padding-top:8px;">⏳ ACTIVE TIMERS</div>
        <div id="savedList" class="saved-list"><div class="loading">No active timers</div></div>
        <div class="section-title" style="padding-top:8px;">✅ READY NUMBERS</div>
        <div style="padding: 0 16px 8px; font-size: 12px; color: #7e8a9a;">These numbers are now in their pools — get them from the Numbers page</div>
        <div id="readyList" class="saved-list"><div class="loading">No ready numbers</div></div>
    </div>

    <div id="historyPage" class="page">
        <div class="section-title">📜 OTP HISTORY</div>
        <div id="historyList" class="saved-list"></div>
    </div>

    <div id="adminPage" class="page hidden">
        <div class="section-title">🛠️ ADMIN PANEL</div>
        <div class="admin-grid">
            <div class="admin-card" onclick="loadAdminStats()"><div class="admin-icon">📊</div><div class="admin-label">Stats</div></div>
            <div class="admin-card" onclick="openCreatePoolModal()"><div class="admin-icon">➕</div><div class="admin-label">Create Pool</div></div>
            <div class="admin-card" onclick="openUploadModal()"><div class="admin-icon">📁</div><div class="admin-label">Upload Numbers</div></div>
            <div class="admin-card" onclick="loadUsersList()"><div class="admin-icon">👥</div><div class="admin-label">Users</div></div>
            <div class="admin-card" onclick="loadBadNumbers()"><div class="admin-icon">🚫</div><div class="admin-label">Bad Numbers</div></div>
            <div class="admin-card" onclick="loadReviews()"><div class="admin-icon">📝</div><div class="admin-label">Reviews</div></div>
            <div class="admin-card" onclick="showBroadcast()"><div class="admin-icon">📢</div><div class="admin-label">Broadcast</div></div>
            <div class="admin-card" onclick="showSettings()"><div class="admin-icon">⚙️</div><div class="admin-label">Settings</div></div>
        </div>
        <div id="adminStatsDiv" class="number-card" style="display: none;"></div>
        <div id="adminUsersDiv" class="saved-list" style="display: none;"></div>
        <div id="adminBadDiv" class="saved-list" style="display: none;"></div>
        <div id="adminReviewsDiv" class="saved-list" style="display: none;"></div>
        <div id="adminBroadcastDiv" class="number-card" style="display: none;"><textarea id="broadcastMsg" rows="3" style="width:100%;padding:12px;border-radius:20px;background:rgba(30,41,59,0.6);color:#eef2ff;border:1px solid rgba(10,132,255,0.3);"></textarea><button class="filter-btn" style="margin-top:12px;" onclick="sendBroadcast()">Send Broadcast</button></div>
        <div id="adminSettingsDiv" class="number-card" style="display: none;">
            <div class="fg"><label>Approval Mode</label><select id="approvalMode"><option value="on">ON - New users need approval</option><option value="off">OFF - All users can access</option></select></div>
            <div class="fg"><label>OTP Redirect</label><select id="otpRedirect"><option value="pool">Per-Pool Link</option><option value="hardcoded">Hardcoded: https://t.me/earnplusz</option></select></div>
            <button class="filter-btn" onclick="saveSettings()">Save Settings</button>
        </div>
        <div id="poolsList" class="saved-list"></div>
    </div>

    <div class="bottom-nav">
        <div class="nav-item active" data-page="home"><div class="nav-icon">🏠</div><div class="nav-label">Home</div></div>
        <div class="nav-item" data-page="numbers"><div class="nav-icon">📱</div><div class="nav-label">Numbers</div></div>
        <div class="nav-item" data-page="saved"><div class="nav-icon">💾</div><div class="nav-label">Saved</div></div>
        <div class="nav-item" data-page="history"><div class="nav-icon">📜</div><div class="nav-label">History</div></div>
        <div class="nav-item" data-page="admin" id="adminNavItem" style="display: none;"><div class="nav-icon">⚙️</div><div class="nav-label">Admin</div></div>
    </div>
</div>

<!-- Modals -->
<div id="poolModal" class="modal"><div class="modal-content"><div class="modal-header" id="poolModalTitle">Create New Pool</div><div style="padding: 20px;"><div class="fg"><label>Pool Name *</label><input type="text" id="poolName" placeholder="e.g., Nigeria"></div><div class="fg"><label>Country Code *</label><input type="text" id="poolCode" placeholder="e.g., 234"></div><div class="fg"><label>OTP Group ID *</label><input type="text" id="poolGroupId" placeholder="e.g., -1001234567890"></div><div class="fg"><label>OTP Link</label><input type="text" id="poolOtpLink" placeholder="https://t.me/your_channel"></div><div class="fg"><label>Match Format *</label><input type="text" id="poolMatchFormat" value="5+4" placeholder="e.g., 5+4"></div><div class="fg"><label>Telegram Match Format</label><input type="text" id="poolTelegramMatchFormat" placeholder="Leave blank to use Match Format"></div><div class="fg"><label>Monitoring Mode</label><select id="poolUsesPlatform"><option value="0">0 - Telegram Only 📱</option><option value="1">1 - Platform Only 🖥️</option><option value="2">2 - Both 📱+🖥️</option></select></div><div class="fg"><label>Trick Text (Guide for users)</label><textarea id="poolTrickText" rows="2" placeholder="Tips for using numbers..."></textarea></div><div style="display: flex; gap: 16px; margin: 16px 0;"><label><input type="checkbox" id="poolAdminOnly"> Admin Only</label><label><input type="checkbox" id="poolPaused" onchange="document.getElementById('pauseReasonDiv').style.display=this.checked?'block':'none'"> Paused</label></div><div id="pauseReasonDiv" style="display: none;" class="fg"><label>Pause Reason</label><input type="text" id="poolPauseReason" placeholder="Reason for pausing"></div><div class="brow"><button class="btn btn-secondary" onclick="closePoolModal()">Cancel</button><button class="btn btn-primary" onclick="savePool()">Save</button></div></div></div></div>

<div id="uploadModal" class="modal"><div class="modal-content"><div class="modal-header">Upload Numbers</div><div style="padding: 20px;"><div class="fg"><label>Select Pool</label><select id="uploadPoolSelect"></select></div><div class="fg"><label>Upload File (.txt or .csv)</label><input type="file" id="uploadFile" accept=".txt,.csv"></div><div class="brow"><button class="btn btn-secondary" onclick="closeUploadModal()">Cancel</button><button class="btn btn-primary" onclick="uploadNumbers()">Upload</button></div><div id="uploadResult" style="margin-top: 16px;"></div></div></div></div>

<div id="feedbackModal" class="modal"><div class="modal-content"><div class="modal-header">Rate Your Number</div><div style="padding: 20px;"><div id="feedbackNumber" style="font-family: monospace; font-size: 18px; text-align: center; margin-bottom: 20px;"></div><div class="feedback-grid"><button class="feedback-btn good" onclick="submitFeedback('worked')">✅ Worked</button><button class="feedback-btn bad" onclick="submitFeedback('bad')">❌ Not Available</button><button class="feedback-btn" onclick="submitFeedback('email')">📧 Email Only</button><button class="feedback-btn" onclick="submitFeedback('other_devices')">📱 Other Devices</button><button class="feedback-btn" onclick="submitFeedback('try_later')">⏳ Try Later</button><button class="feedback-btn" onclick="showOtherFeedback()">📝 Other</button></div><div id="otherFeedbackDiv" style="display: none; margin-top: 16px;"><textarea id="otherFeedbackText" rows="2" placeholder="Describe the issue..." style="width:100%;padding:12px;border-radius:20px;background:rgba(30,41,59,0.6);color:#eef2ff;border:1px solid rgba(10,132,255,0.3);"></textarea><button class="filter-btn" style="margin-top:12px;width:100%;" onclick="submitFeedback('other')">Submit</button></div></div></div></div>

<script>
const API_BASE = window.location.origin;
let currentUser = null;
let currentAssignment = null;
let currentPoolId = null;
let ws = null;
let otpTimer = null;
let currentFilter = null;
let allRegions = [];

function showToast(msg) { const t = document.createElement('div'); t.className = 'toast'; t.textContent = msg; document.body.appendChild(t); setTimeout(() => t.remove(), 2000); }
function formatTime() { document.getElementById('currentTime').textContent = new Date().toLocaleTimeString([], { hour:'2-digit', minute:'2-digit' }); }
setInterval(formatTime, 1000); formatTime();
function copyText(t) { navigator.clipboard.writeText(t); showToast('Copied!'); }
function setTimerPreset(value) { document.getElementById('timerInput').value = value; }

async function checkAuth() {
    try {
        const res = await fetch(`${API_BASE}/api/auth/me`, { credentials: 'include' });
        if (res.ok) {
            currentUser = await res.json();
            document.getElementById('authContainer').style.display = 'none';
            document.getElementById('appContainer').style.display = 'block';
            document.getElementById('userName').textContent = currentUser.username;
            document.getElementById('userId').textContent = `ID: ${currentUser.id}`;
            if (currentUser.is_admin) {
                document.getElementById('adminBadge').style.display = 'inline-block';
                document.getElementById('adminNavItem').style.display = 'flex';
                document.getElementById('adminPage').classList.remove('hidden');
                loadAdminPools();
            }
            connectWebSocket();
            loadRegions();
            loadCurrentAssignment();
            loadSavedNumbers();
            loadHistory();
            return true;
        }
    } catch(e) {}
    document.getElementById('authContainer').style.display = 'flex';
    document.getElementById('appContainer').style.display = 'none';
    return false;
}

async function doLogin() {
    const username = document.getElementById('loginUsername').value;
    const password = document.getElementById('loginPassword').value;
    const errorEl = document.getElementById('loginError');
    try {
        const res = await fetch(`${API_BASE}/api/auth/login`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' }, credentials: 'include',
            body: JSON.stringify({ username, password })
        });
        const data = await res.json();
        if (res.ok) { checkAuth(); }
        else { errorEl.textContent = data.detail || 'Login failed'; }
    } catch(e) { errorEl.textContent = 'Network error'; }
}

async function doRegister() {
    const username = document.getElementById('regUsername').value;
    const password = document.getElementById('regPassword').value;
    const errorEl = document.getElementById('regError');
    if (password.length < 6) { errorEl.textContent = 'Password must be at least 6 characters'; return; }
    try {
        const res = await fetch(`${API_BASE}/api/auth/register`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' }, credentials: 'include',
            body: JSON.stringify({ username, password })
        });
        const data = await res.json();
        if (res.ok) {
            if (data.approved) { checkAuth(); }
            else { errorEl.textContent = 'Awaiting admin approval'; }
        } else { errorEl.textContent = data.detail || 'Registration failed'; }
    } catch(e) { errorEl.textContent = 'Network error'; }
}

function connectWebSocket() {
    const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
    ws = new WebSocket(`${protocol}//${location.host}/ws/user/${currentUser.id}`);
    ws.onmessage = (e) => {
        const data = JSON.parse(e.data);
        if (data.type === 'otp') displayOTP(data);
        else if (data.type === 'broadcast') showToast(`📢 ${data.message}`);
    };
    ws.onclose = () => setTimeout(connectWebSocket, 5000);
}

function displayOTP(data) {
    const otpDiv = document.getElementById('otpDisplay');
    if (otpTimer) clearTimeout(otpTimer);
    otpDiv.innerHTML = `<div class="otp-card"><div style="text-align:center;font-size:12px;">🔑 OTP CODE</div><div class="otp-code" onclick="copyText('${data.otp}')">${data.otp}</div><div class="otp-timer" id="otpCountdown">Auto-delete in 30s</div><div class="otp-message">${escapeHtml(data.raw_message || '')}</div></div>`;
    otpDiv.style.display = 'block';
    let seconds = 30;
    const timer = setInterval(() => { seconds--; const cd = document.getElementById('otpCountdown'); if (cd) cd.textContent = `Auto-delete in ${seconds}s`; if (seconds <= 0) { clearInterval(timer); otpDiv.style.display = 'none'; } }, 1000);
    otpTimer = setTimeout(() => { clearInterval(timer); otpDiv.style.display = 'none'; }, 30000);
    loadHistory();
}

async function loadRegions() {
    try {
        const res = await fetch(`${API_BASE}/api/pools`, { credentials: 'include' });
        if (!res.ok) throw new Error();
        allRegions = await res.json();
        const container = document.getElementById('regionList');
        let filtered = allRegions;
        if (currentFilter) filtered = allRegions.filter(r => r.name.toLowerCase().includes(currentFilter.toLowerCase()) || r.country_code.includes(currentFilter));
        if (!filtered.length) { container.innerHTML = '<div class="loading">No regions available</div>'; return; }
        container.innerHTML = filtered.map(r => `<div class="region-item" onclick="selectRegion(${r.id})"><div><div class="region-name">${escapeHtml(r.name)}</div><div class="region-code">+${r.country_code}</div>${r.trick_text ? `<div style="font-size:11px;color:#f59e0b;">💡 ${escapeHtml(r.trick_text)}</div>` : ''}</div><div>${r.is_paused ? '<span style="color:#ef4444;font-size:12px;">⏸ Paused</span>' : `<span class="region-count">${r.number_count}</span>`}</div></div>`).join('');
    } catch(e) { document.getElementById('regionList').innerHTML = '<div class="loading">Failed to load regions</div>'; }
}

function applyFilter() { currentFilter = document.getElementById('prefixFilter').value.trim(); loadRegions(); }

async function selectRegion(poolId) {
    const region = allRegions.find(r => r.id === poolId);
    if (region.is_paused) { showToast(`Region paused: ${region.pause_reason || 'Temporarily unavailable'}`); return; }
    try {
        const res = await fetch(`${API_BASE}/api/pools/assign`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' }, credentials: 'include',
            body: JSON.stringify({ pool_id: poolId })
        });
        const data = await res.json();
        if (res.ok) {
            currentAssignment = data;
            currentPoolId = data.pool_id;
            document.getElementById('currentNumber').textContent = data.number;
            document.getElementById('currentRegion').textContent = data.pool_name;
            showToast(`Number assigned: ${data.number}`);
            document.getElementById('otpDisplay').style.display = 'none';
            if (otpTimer) clearTimeout(otpTimer);
        } else { showToast(data.detail || 'Failed to assign number'); }
    } catch(e) { showToast('Network error'); }
}

async function loadCurrentAssignment() {
    try {
        const res = await fetch(`${API_BASE}/api/pools/my-assignment`, { credentials: 'include' });
        const data = await res.json();
        if (data.assignment) {
            currentAssignment = data.assignment;
            currentPoolId = data.assignment.pool_id;
            document.getElementById('currentNumber').textContent = currentAssignment.number;
            document.getElementById('currentRegion').textContent = currentAssignment.pool_name;
        }
    } catch(e) {}
}

async function changeNumber() {
    if (!currentAssignment) { showToast('No active number to change'); return; }
    document.getElementById('feedbackNumber').textContent = currentAssignment.number;
    document.getElementById('feedbackModal').classList.add('show');
}

async function submitFeedback(type) {
    const comment = type === 'other' ? document.getElementById('otherFeedbackText').value : type;
    const markAsBad = type === 'bad';
    
    if (currentAssignment) {
        await fetch(`${API_BASE}/api/reviews`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' }, credentials: 'include',
            body: JSON.stringify({ number: currentAssignment.number, rating: markAsBad ? 1 : 4, comment: comment, mark_as_bad: markAsBad })
        });
        
        await fetch(`${API_BASE}/api/pools/release/${currentAssignment.assignment_id}`, { method: 'POST', credentials: 'include' });
        
        if (currentPoolId) {
            const res = await fetch(`${API_BASE}/api/pools/assign`, {
                method: 'POST', headers: { 'Content-Type': 'application/json' }, credentials: 'include',
                body: JSON.stringify({ pool_id: currentPoolId })
            });
            const data = await res.json();
            if (res.ok) {
                currentAssignment = data;
                document.getElementById('currentNumber').textContent = data.number;
                document.getElementById('currentRegion').textContent = data.pool_name;
                document.getElementById('otpDisplay').style.display = 'none';
                if (otpTimer) clearTimeout(otpTimer);
                showToast(`New number assigned: ${data.number}`);
            } else {
                showToast('No more numbers in this pool, please select another region');
                currentAssignment = null;
                currentPoolId = null;
                document.getElementById('currentNumber').textContent = '—';
                document.getElementById('currentRegion').textContent = 'No region selected';
            }
        }
    }
    document.getElementById('feedbackModal').classList.remove('show');
    document.getElementById('otherFeedbackDiv').style.display = 'none';
    document.getElementById('otherFeedbackText').value = '';
    loadRegions();
}

function showOtherFeedback() { document.getElementById('otherFeedbackDiv').style.display = 'block'; }
function copyNumber() { if (currentAssignment) copyText(currentAssignment.number); else showToast('No number to copy'); }

function parseTimer(timer) {
    const match = timer.match(/^(\d+)([smhd])$/i);
    if (match) {
        const num = parseInt(match[1]);
        const unit = match[2].toLowerCase();
        if (unit === 's') return Math.max(1, Math.ceil(num / 60));
        if (unit === 'h') return num * 60;
        if (unit === 'd') return num * 1440;
    }
    const num = parseInt(timer);
    return isNaN(num) ? 30 : num;
}

async function saveNumbers() {
    const numbers = document.getElementById('savedNumbersInput').value.split('\\n').map(l => l.trim()).filter(l => l);
    const timerRaw = document.getElementById('timerInput').value.trim();
    const timerMinutes = parseTimer(timerRaw);
    const poolName = document.getElementById('poolNameInput').value.trim() || 'Default Pool';
    if (!numbers.length) { showToast('Enter at least one number'); return; }
    try {
        const res = await fetch(`${API_BASE}/api/saved`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' }, credentials: 'include',
            body: JSON.stringify({ numbers, timer_minutes: timerMinutes, pool_name: poolName })
        });
        const data = await res.json();
        if (res.ok) { showToast(`Saved ${data.saved} numbers`); document.getElementById('savedNumbersInput').value = ''; loadSavedNumbers(); }
        else { showToast(data.detail || 'Failed to save'); }
    } catch(e) { showToast('Network error'); }
}

async function loadSavedNumbers() {
    try {
        const res = await fetch(`${API_BASE}/api/saved`, { credentials: 'include' });
        const data = await res.json();
        const active = data.filter(i => !i.moved);
        const ready = data.filter(i => i.moved);

        const activeContainer = document.getElementById('savedList');
        if (!active.length) {
            activeContainer.innerHTML = '<div class="loading">No active timers</div>';
        } else {
            activeContainer.innerHTML = active.map(item => {
                let cls = 'timer-green';
                let mins = Math.floor(item.seconds_left / 60);
                let secs = item.seconds_left % 60;
                let time = mins > 0 ? `${mins}m ${secs}s` : `${secs}s`;
                if (item.status === 'yellow') cls = 'timer-yellow';
                if (item.status === 'red') cls = 'timer-red';
                return `<div class="saved-item">
                    <div>
                        <div class="saved-number">${escapeHtml(item.number)}</div>
                        <div class="saved-timer">📦 ${escapeHtml(item.pool_name)} — will enter pool when expired</div>
                    </div>
                    <div style="display:flex;align-items:center;gap:8px;">
                        <span class="timer-badge ${cls}">${time}</span>
                        <button onclick="deleteSaved(${item.id})" style="background:none;border:none;font-size:20px;cursor:pointer;">🗑️</button>
                    </div>
                </div>`;
            }).join('');
        }

        const readyContainer = document.getElementById('readyList');
        if (!ready.length) {
            readyContainer.innerHTML = '<div class="loading">No ready numbers yet</div>';
        } else {
            const byPool = {};
            ready.forEach(item => {
                if (!byPool[item.pool_name]) byPool[item.pool_name] = [];
                byPool[item.pool_name].push(item);
            });
            readyContainer.innerHTML = Object.entries(byPool).map(([poolName, items]) => `
                <div style="margin-bottom:16px;">
                    <div style="font-size:13px;font-weight:700;color:#0a84ff;padding:8px 4px 6px;">
                        📦 ${escapeHtml(poolName.toUpperCase())} POOL
                        <span style="background:rgba(10,132,255,0.15);padding:2px 10px;border-radius:20px;font-size:11px;margin-left:6px;">${items.length} ready</span>
                    </div>
                    ${items.map(item => `
                    <div class="saved-item" style="margin-bottom:8px;">
                        <div style="flex:1;">
                            <div class="saved-number">${escapeHtml(item.number)}</div>
                            <div class="saved-timer" style="color:#10b981;">✅ In pool — get it from the Numbers page</div>
                        </div>
                        <div style="display:flex;gap:6px;flex-wrap:wrap;justify-content:flex-end;">
                            <button class="btn btn-sm btn-secondary" onclick="changeReadyNumber(${item.id}, '${escapeHtml(item.number)}', '${escapeHtml(item.pool_name)}')">🔄 Change</button>
                            <button class="btn btn-sm btn-secondary" onclick="changeReadyPool(${item.id}, '${escapeHtml(item.pool_name)}')">🌐 Pool</button>
                            <button onclick="deleteSaved(${item.id})" style="background:none;border:none;font-size:18px;cursor:pointer;">🗑️</button>
                        </div>
                    </div>`).join('')}
                </div>
            `).join('');
        }
    } catch(e) { console.error(e); }
}

async function deleteSaved(id) {
    await fetch(`${API_BASE}/api/saved/${id}`, { method: 'DELETE', credentials: 'include' });
    loadSavedNumbers();
}

async function changeReadyNumber(id, currentNumber, poolName) {
    const newNum = prompt(`Change number in "${poolName}" pool:\nCurrent: ${currentNumber}\n\nEnter new number:`);
    if (!newNum || !newNum.trim()) return;
    const res = await fetch(`${API_BASE}/api/saved/${id}/number`, {
        method: 'PUT', headers: { 'Content-Type': 'application/json' }, credentials: 'include',
        body: JSON.stringify({ number: newNum.trim() })
    });
    if (res.ok) { showToast('Number updated'); loadSavedNumbers(); }
    else { const e = await res.json(); showToast(e.detail || 'Failed to update'); }
}

async function changeReadyPool(id, currentPool) {
    const poolsRes = await fetch(`${API_BASE}/api/pools`, { credentials: 'include' });
    const poolsList = await poolsRes.json();
    const options = poolsList.map(p => p.name).filter(n => n !== currentPool);
    if (!options.length) { showToast('No other pools available'); return; }
    const newPool = prompt(`Move to which pool?\nCurrent: ${currentPool}\n\nAvailable:\n${options.map((n,i)=>`${i+1}. ${n}`).join('\n')}\n\nType the pool name:`);
    if (!newPool || !newPool.trim()) return;
    const res = await fetch(`${API_BASE}/api/saved/${id}/pool`, {
        method: 'PUT', headers: { 'Content-Type': 'application/json' }, credentials: 'include',
        body: JSON.stringify({ pool_name: newPool.trim() })
    });
    if (res.ok) { showToast(`Moved to ${newPool} pool`); loadSavedNumbers(); }
    else { const e = await res.json(); showToast(e.detail || 'Pool not found'); }
}



async function loadHistory() {
    try {
        const res = await fetch(`${API_BASE}/api/otp/my`, { credentials: 'include' });
        const data = await res.json();
        const container = document.getElementById('historyList');
        if (!data.length) { container.innerHTML = '<div class="loading">No OTP history</div>'; return; }
        container.innerHTML = data.map(item => `<div class="history-item"><div class="history-number">${escapeHtml(item.number)}</div><div class="history-otp" onclick="copyText('${item.otp_code}')">${item.otp_code} 📋</div><div class="history-time">${new Date(item.delivered_at).toLocaleString()}</div></div>`).join('');
    } catch(e) {}
}

// Admin functions
let currentEditPoolId = null;

function openCreatePoolModal() {
    currentEditPoolId = null;
    document.getElementById('poolModalTitle').textContent = 'Create New Pool';
    document.getElementById('poolName').value = '';
    document.getElementById('poolCode').value = '';
    document.getElementById('poolGroupId').value = '';
    document.getElementById('poolOtpLink').value = '';
    document.getElementById('poolMatchFormat').value = '5+4';
    document.getElementById('poolTelegramMatchFormat').value = '';
    document.getElementById('poolUsesPlatform').value = '0';
    document.getElementById('poolTrickText').value = '';
    document.getElementById('poolAdminOnly').checked = false;
    document.getElementById('poolPaused').checked = false;
    document.getElementById('pauseReasonDiv').style.display = 'none';
    document.getElementById('poolModal').classList.add('show');
}

async function openEditPoolModal(poolId) {
    currentEditPoolId = poolId;
    try {
        const res = await fetch(`${API_BASE}/api/pools`, { credentials: 'include' });
        const pools = await res.json();
        const pool = pools.find(p => p.id === poolId);
        if (!pool) return;
        document.getElementById('poolModalTitle').textContent = `Edit Pool: ${pool.name}`;
        document.getElementById('poolName').value = pool.name;
        document.getElementById('poolCode').value = pool.country_code;
        document.getElementById('poolGroupId').value = pool.otp_group_id || '';
        document.getElementById('poolOtpLink').value = pool.otp_link || '';
        document.getElementById('poolMatchFormat').value = pool.match_format || '5+4';
        document.getElementById('poolTelegramMatchFormat').value = pool.telegram_match_format || '';
        document.getElementById('poolUsesPlatform').value = pool.uses_platform || 0;
        document.getElementById('poolTrickText').value = pool.trick_text || '';
        document.getElementById('poolAdminOnly').checked = pool.is_admin_only || false;
        document.getElementById('poolPaused').checked = pool.is_paused || false;
        document.getElementById('pauseReasonDiv').style.display = pool.is_paused ? 'block' : 'none';
        document.getElementById('poolPauseReason').value = pool.pause_reason || '';
        document.getElementById('poolModal').classList.add('show');
    } catch(e) { showToast('Failed to load pool data'); }
}

async function savePool() {
    const data = {
        name: document.getElementById('poolName').value.trim(),
        country_code: document.getElementById('poolCode').value.trim(),
        otp_group_id: parseInt(document.getElementById('poolGroupId').value) || null,
        otp_link: document.getElementById('poolOtpLink').value.trim(),
        match_format: document.getElementById('poolMatchFormat').value.trim(),
        telegram_match_format: document.getElementById('poolTelegramMatchFormat').value.trim(),
        uses_platform: parseInt(document.getElementById('poolUsesPlatform').value),
        trick_text: document.getElementById('poolTrickText').value.trim(),
        is_admin_only: document.getElementById('poolAdminOnly').checked,
        is_paused: document.getElementById('poolPaused').checked,
        pause_reason: document.getElementById('poolPauseReason').value.trim()
    };
    if (!data.name || !data.country_code) { showToast('Pool name and country code are required'); return; }
    try {
        const url = currentEditPoolId ? `/api/admin/pools/${currentEditPoolId}` : '/api/admin/pools';
        const method = currentEditPoolId ? 'PUT' : 'POST';
        const res = await fetch(url, { method, headers: { 'Content-Type': 'application/json' }, credentials: 'include', body: JSON.stringify(data) });
        if (res.ok) {
            showToast(currentEditPoolId ? 'Pool updated!' : 'Pool created!');
            closePoolModal();
            loadAdminPools();
        } else { const err = await res.json(); showToast(err.detail || 'Failed to save pool'); }
    } catch(e) { showToast('Network error'); }
}

function closePoolModal() { document.getElementById('poolModal').classList.remove('show'); }

async function loadAdminPools() {
    try {
        const res = await fetch(`${API_BASE}/api/pools`, { credentials: 'include' });
        const pools = await res.json();
        const container = document.getElementById('poolsList');
        if (!pools.length) { container.innerHTML = '<div class="loading">No pools — create one above</div>'; return; }
        container.innerHTML = pools.map(p => `
            <div class="saved-item" style="flex-direction:column;align-items:stretch;gap:10px;">
                <div style="display:flex;justify-content:space-between;align-items:center;">
                    <div>
                        <div class="saved-number">${escapeHtml(p.name)} (+${p.country_code})</div>
                        <div class="saved-timer">${p.number_count} numbers • Mode: ${p.uses_platform} • Format: ${p.match_format}${p.is_paused ? ' • <span style="color:#ef4444;">⏸ Paused</span>' : ''}${p.is_admin_only ? ' • 🔒' : ''}</div>
                        ${p.trick_text ? `<div style="font-size:11px;color:#f59e0b;margin-top:2px;">💡 ${escapeHtml(p.trick_text)}</div>` : ''}
                    </div>
                    <div style="display:flex;gap:4px;">
                        <button class="btn btn-sm btn-primary" onclick="openEditPoolModal(${p.id})">✏️</button>
                        <button class="btn btn-sm btn-danger" onclick="deletePool(${p.id}, '${escapeHtml(p.name).replace(/'/g,"\\'")}')">🗑️</button>
                    </div>
                </div>
                <div style="display:flex;gap:6px;flex-wrap:wrap;">
                    <button class="btn btn-sm btn-secondary" onclick="cutPoolNumbers(${p.id})">✂️ Cut</button>
                    <button class="btn btn-sm btn-secondary" onclick="exportPool(${p.id})">📤 Export</button>
                    <button class="btn btn-sm btn-danger" onclick="clearPool(${p.id}, '${escapeHtml(p.name).replace(/'/g,"\\'")}')">🧹 Clear</button>
                    <button class="btn btn-sm btn-secondary" onclick="${p.is_paused ? `resumePool(${p.id})` : `pausePool(${p.id})`}">${p.is_paused ? '▶ Resume' : '⏸ Pause'}</button>
                    <button class="btn btn-sm btn-secondary" onclick="toggleAdminOnly(${p.id})">${p.is_admin_only ? '🔓 Public' : '🔒 Admin Only'}</button>
                </div>
            </div>`).join('');
    } catch(e) { document.getElementById('poolsList').innerHTML = '<div class="loading">Failed to load pools</div>'; }
}

async function cutPoolNumbers(poolId) {
    const count = prompt('How many numbers to cut from the top?');
    if (!count || isNaN(parseInt(count))) return;
    const res = await fetch(`${API_BASE}/api/admin/pools/${poolId}/cut?count=${parseInt(count)}`, { method: 'POST', credentials: 'include' });
    const data = await res.json();
    if (res.ok) { showToast(`✂️ Removed ${data.removed} numbers`); loadAdminPools(); }
    else showToast(data.detail || 'Cut failed');
}

function exportPool(poolId) {
    window.open(`${API_BASE}/api/admin/pools/${poolId}/export`, '_blank');
}

async function clearPool(poolId, poolName) {
    if (!confirm(`⚠️ Clear ALL numbers from "${poolName}"? This cannot be undone!`)) return;
    const res = await fetch(`${API_BASE}/api/admin/pools/${poolId}/clear`, { method: 'POST', credentials: 'include' });
    const data = await res.json();
    if (res.ok) { showToast(`🧹 Cleared ${data.deleted} numbers`); loadAdminPools(); }
    else showToast(data.detail || 'Clear failed');
}

async function pausePool(poolId) {
    const reason = prompt('Pause reason (optional):') || '';
    const res = await fetch(`${API_BASE}/api/admin/pools/${poolId}/pause?reason=${encodeURIComponent(reason)}`, { method: 'POST', credentials: 'include' });
    if (res.ok) { showToast('⏸ Pool paused'); loadAdminPools(); }
    else showToast('Failed to pause pool');
}

async function resumePool(poolId) {
    const res = await fetch(`${API_BASE}/api/admin/pools/${poolId}/resume`, { method: 'POST', credentials: 'include' });
    if (res.ok) { showToast('▶ Pool resumed'); loadAdminPools(); }
    else showToast('Failed to resume pool');
}

async function toggleAdminOnly(poolId) {
    const res = await fetch(`${API_BASE}/api/admin/pools/${poolId}/toggle-admin-only`, { method: 'POST', credentials: 'include' });
    const data = await res.json();
    if (res.ok) { showToast(data.is_admin_only ? '🔒 Now Admin Only' : '🔓 Now Public'); loadAdminPools(); }
    else showToast('Toggle failed');
}

async function deletePool(poolId, poolName) {
    if (!confirm(`⚠️ Delete pool "${poolName}" and all its numbers? This cannot be undone!`)) return;
    try {
        const res = await fetch(`${API_BASE}/api/admin/pools/${poolId}`, { method: 'DELETE', credentials: 'include' });
        if (res.ok) { showToast(`Pool "${poolName}" deleted`); loadAdminPools(); }
        else { showToast('Failed to delete pool'); }
    } catch(e) { showToast('Network error'); }
}

async function openUploadModal() {
    try {
        const res = await fetch(`${API_BASE}/api/pools`, { credentials: 'include' });
        const pools = await res.json();
        const select = document.getElementById('uploadPoolSelect');
        select.innerHTML = '<option value="">-- Select Pool --</option>' + pools.map(p => `<option value="${p.id}">${p.name} (+${p.country_code}) - ${p.number_count} numbers</option>`).join('');
        document.getElementById('uploadModal').classList.add('show');
    } catch(e) { showToast('Failed to load pools'); }
}

function closeUploadModal() { document.getElementById('uploadModal').classList.remove('show'); document.getElementById('uploadResult').innerHTML = ''; document.getElementById('uploadFile').value = ''; }

async function uploadNumbers() {
    const poolId = document.getElementById('uploadPoolSelect').value;
    const fileInput = document.getElementById('uploadFile');
    if (!poolId) { showToast('Select a pool'); return; }
    if (!fileInput.files || !fileInput.files[0]) { showToast('Select a file'); return; }
    const formData = new FormData(); formData.append('file', fileInput.files[0]);
    try {
        const res = await fetch(`/api/admin/pools/${poolId}/upload`, { method: 'POST', credentials: 'include', body: formData });
        const data = await res.json();
        if (res.ok) {
            document.getElementById('uploadResult').innerHTML = `<div style="background:#10b98120;padding:12px;border-radius:8px;">✅ Added: ${data.added}<br>🚫 Bad skipped: ${data.skipped_bad}<br>⏳ Cooldown skipped: ${data.skipped_cooldown}<br>🔁 Duplicates: ${data.duplicates}</div>`;
            showToast(`${data.added} numbers uploaded!`);
            loadAdminPools();
        } else { showToast(data.detail || 'Upload failed'); }
    } catch(e) { showToast('Network error'); }
}

const ADMIN_DIVS = ['adminStatsDiv','adminUsersDiv','adminBadDiv','adminReviewsDiv','adminBroadcastDiv','adminSettingsDiv'];
function hideOtherAdminDivs(showId) {
    ADMIN_DIVS.forEach(id => {
        document.getElementById(id).style.display = (id === showId) ? 'block' : 'none';
    });
}

function loadAdminStats() {
    fetch(`${API_BASE}/api/admin/stats`, { credentials: 'include' }).then(r => r.json()).then(stats => {
        document.getElementById('adminStatsDiv').innerHTML = `
            <div class="number-label" style="margin-bottom:16px;">System Stats</div>
            <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;font-size:14px;">
                <span>📊 Users</span><span style="font-weight:700;color:#0a84ff;">${stats.total_users}</span>
                <span>⏳ Pending</span><span style="font-weight:700;color:#f59e0b;">${stats.pending_approval}</span>
                <span>🌍 Pools</span><span style="font-weight:700;color:#0a84ff;">${stats.total_pools}</span>
                <span>📞 Numbers</span><span style="font-weight:700;color:#10b981;">${stats.total_numbers}</span>
                <span>🔑 OTPs</span><span style="font-weight:700;color:#10b981;">${stats.total_otps}</span>
                <span>🚫 Bad</span><span style="font-weight:700;color:#ef4444;">${stats.bad_numbers}</span>
                <span>💾 Saved</span><span style="font-weight:700;color:#0a84ff;">${stats.saved_numbers}</span>
                <span>🟢 Online</span><span style="font-weight:700;color:#10b981;">${stats.online_users}</span>
            </div>`;
        hideOtherAdminDivs('adminStatsDiv');
    }).catch(() => showToast('Failed to load stats'));
}

function loadUsersList() {
    fetch(`${API_BASE}/api/admin/users`, { credentials: 'include' }).then(r => r.json()).then(users => {
        if (!users.length) { document.getElementById('adminUsersDiv').innerHTML = '<div class="loading">No users</div>'; hideOtherAdminDivs('adminUsersDiv'); return; }
        document.getElementById('adminUsersDiv').innerHTML = users.map(u => {
            const status = u.is_admin ? '👑 Admin' : (u.is_blocked ? '🚫 Blocked' : (u.is_approved ? '✅ Approved' : '⏳ Pending'));
            const approveBtn = (!u.is_approved && !u.is_blocked && !u.is_admin)
                ? `<button class="btn btn-sm btn-primary" onclick="approveUser(${u.id})" style="margin-right:4px;">✅ Approve</button>` : '';
            const blockBtn = u.is_admin ? '' : (u.is_blocked
                ? `<button class="btn btn-sm btn-primary" onclick="toggleUser(${u.id}, false)">Unblock</button>`
                : `<button class="btn btn-sm btn-danger" onclick="toggleUser(${u.id}, true)">Block</button>`);
            return `<div class="saved-item"><div><div class="saved-number">${escapeHtml(u.username)}</div><div class="saved-timer">ID: ${u.id} • ${status}</div></div><div style="display:flex;gap:4px;flex-wrap:wrap;">${approveBtn}${blockBtn}</div></div>`;
        }).join('');
        hideOtherAdminDivs('adminUsersDiv');
    }).catch(() => showToast('Failed to load users'));
}

async function approveUser(userId) {
    const res = await fetch(`${API_BASE}/api/admin/users/${userId}/approve`, { method: 'POST', credentials: 'include' });
    if (res.ok) { showToast('User approved ✅'); loadUsersList(); }
    else showToast('Failed to approve user');
}

async function toggleUser(userId, block) {
    const url = block ? `/api/admin/users/${userId}/block` : `/api/admin/users/${userId}/unblock`;
    const res = await fetch(url, { method: 'POST', credentials: 'include' });
    if (res.ok) { showToast(block ? 'User blocked 🚫' : 'User unblocked ✅'); loadUsersList(); }
    else showToast('Action failed');
}

function loadBadNumbers() {
    fetch(`${API_BASE}/api/admin/bad-numbers`, { credentials: 'include' }).then(r => r.json()).then(bad => {
        document.getElementById('adminBadDiv').innerHTML = bad.length
            ? bad.map(b => `<div class="saved-item"><div><div class="saved-number">${escapeHtml(b.number)}</div><div class="saved-timer">${escapeHtml(b.reason || '')}</div></div><div><button class="btn btn-sm btn-primary" onclick="removeBadNumber('${b.number.replace(/'/g,"\\'").replace(/"/g,'&quot;')}')">Remove</button></div></div>`).join('')
            : '<div class="loading">No bad numbers 🎉</div>';
        hideOtherAdminDivs('adminBadDiv');
    }).catch(() => showToast('Failed to load bad numbers'));
}

async function removeBadNumber(number) {
    await fetch(`${API_BASE}/api/admin/bad-numbers?number=${encodeURIComponent(number)}`, { method: 'DELETE', credentials: 'include' });
    showToast('Removed from bad numbers');
    loadBadNumbers();
}

function loadReviews() {
    fetch(`${API_BASE}/api/admin/reviews`, { credentials: 'include' }).then(r => r.json()).then(reviews => {
        document.getElementById('adminReviewsDiv').innerHTML = reviews.length
            ? reviews.map(r => `<div class="saved-item"><div><div class="saved-number">${escapeHtml(r.number)}</div><div class="saved-timer">Rating: ${'⭐'.repeat(Math.max(0,r.rating))} • ${escapeHtml(r.comment || 'No comment')}</div></div></div>`).join('')
            : '<div class="loading">No reviews yet</div>';
        hideOtherAdminDivs('adminReviewsDiv');
    }).catch(() => showToast('Failed to load reviews'));
}

function showBroadcast() { hideOtherAdminDivs('adminBroadcastDiv'); }

async function sendBroadcast() {
    const msg = document.getElementById('broadcastMsg').value.trim();
    if (!msg) { showToast('Enter a message'); return; }
    const res = await fetch(`${API_BASE}/api/admin/broadcast?message=${encodeURIComponent(msg)}`, { method: 'POST', credentials: 'include' });
    if (res.ok) { showToast('📢 Broadcast sent!'); document.getElementById('broadcastMsg').value = ''; }
    else showToast('Broadcast failed');
}

function showSettings() { hideOtherAdminDivs('adminSettingsDiv'); }

async function saveSettings() {
    const approval = document.getElementById('approvalMode').value;
    const redirect = document.getElementById('otpRedirect').value;
    await fetch(`${API_BASE}/api/admin/settings/approval?enabled=${approval === 'on'}`, { method: 'POST', credentials: 'include' });
    await fetch(`${API_BASE}/api/admin/settings/otp-redirect?mode=${redirect}`, { method: 'POST', credentials: 'include' });
    showToast('Settings saved');
}

function navigateTo(page) {
    document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
    document.getElementById(`${page}Page`).classList.add('active');
    document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
    document.querySelector(`.nav-item[data-page="${page}"]`).classList.add('active');
    if (page === 'numbers') loadRegions();
    if (page === 'saved') loadSavedNumbers();
    if (page === 'history') loadHistory();
}

document.querySelectorAll('.nav-item').forEach(i => i.addEventListener('click', () => navigateTo(i.dataset.page)));
document.getElementById('authLoginTab').onclick = () => { document.getElementById('loginForm').style.display = 'block'; document.getElementById('registerForm').style.display = 'none'; document.getElementById('authLoginTab').style.background = '#0a84ff'; document.getElementById('authLoginTab').style.color = 'white'; document.getElementById('authRegisterTab').style.background = 'rgba(30,41,59,0.6)'; document.getElementById('authRegisterTab').style.color = '#9ca3af'; };
document.getElementById('authRegisterTab').onclick = () => { document.getElementById('loginForm').style.display = 'none'; document.getElementById('registerForm').style.display = 'block'; document.getElementById('authRegisterTab').style.background = '#0a84ff'; document.getElementById('authRegisterTab').style.color = 'white'; document.getElementById('authLoginTab').style.background = 'rgba(30,41,59,0.6)'; document.getElementById('authLoginTab').style.color = '#9ca3af'; };

function escapeHtml(t) { if (!t) return ''; return t.replace(/[&<>]/g, m => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;' }[m])); }

checkAuth();
</script>
</body>
</html>'''

@app.get("/")
async def serve_frontend():
    return HTMLResponse(content=FRONTEND_HTML)

@app.get("/health")
def health():
    return {"status": "ok", "service": "NEON GRID NETWORK", "timestamp": utcnow().isoformat()}

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
                resp = JSONResponse({"ok": True, "approved": True, "is_admin": True, "user_id": user.id})
                resp.set_cookie("token", token, httponly=True, samesite="lax", max_age=86400*30, path="/")
                return resp
            return {"ok": True, "approved": False, "message": "Awaiting admin approval"}
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
            resp = JSONResponse({"ok": True, "approved": True, "is_admin": True, "user_id": user_id})
            resp.set_cookie("token", token, httponly=True, samesite="lax", max_age=86400*30, path="/")
            return resp
        return {"ok": True, "approved": False, "message": "Awaiting admin approval"}

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
            resp = JSONResponse({"ok": True, "user_id": user.id, "username": user.username, "is_admin": user.is_admin})
            resp.set_cookie("token", token, httponly=True, samesite="lax", max_age=86400*30, path="/")
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
        resp = JSONResponse({"ok": True, "user_id": user["id"], "username": user["username"], "is_admin": user["is_admin"]})
        resp.set_cookie("token", token, httponly=True, samesite="lax", max_age=86400*30, path="/")
        return resp

@app.post("/api/auth/logout")
def logout(token: str = Cookie(default=None)):
    revoke_token(token)
    resp = JSONResponse({"ok": True})
    resp.delete_cookie("token", path="/")
    return resp

@app.get("/api/auth/me")
def me(token: str = Cookie(default=None)):
    user = get_user_from_token(token)
    if not user:
        raise HTTPException(401, "Not authenticated")
    return {"id": user["id"], "username": user["username"], "is_admin": user["is_admin"], "is_approved": user["is_approved"]}

# ══════════════════════════════════════════════════════════════════════════════
#  POOLS ENDPOINTS
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/api/pools")
def list_pools(token: str = Cookie(default=None)):
    user = get_user_from_token(token)
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
async def assign_number(req: AssignRequest, token: str = Cookie(default=None)):
    user = get_user_from_token(token)
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
def my_assignment(token: str = Cookie(default=None)):
    user = get_user_from_token(token)
    if not user:
        raise HTTPException(401, "Not authenticated")
    assignment = get_current_assignment(user["id"])
    return {"assignment": assignment}

@app.post("/api/pools/release/{assignment_id}")
def release_number(assignment_id: int, token: str = Cookie(default=None)):
    user = get_user_from_token(token)
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
def create_pool(req: PoolCreate, token: str = Cookie(default=None)):
    user = get_user_from_token(token)
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
def update_pool(pool_id: int, req: PoolCreate, token: str = Cookie(default=None)):
    user = get_user_from_token(token)
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
def delete_pool(pool_id: int, token: str = Cookie(default=None)):
    user = get_user_from_token(token)
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
async def upload_numbers(pool_id: int, file: UploadFile = File(...), token: str = Cookie(default=None)):
    user = get_user_from_token(token)
    if not user or not user["is_admin"]:
        raise HTTPException(403, "Admin only")
    
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
    
    if SessionLocal:
        with SessionLocal() as db:
            bad_set = {b.number for b in db.query(BadNumber).all()}
            filtered = [n for n in numbers if n not in bad_set]
            skipped_bad = len(numbers) - len(filtered)

            cooldown_dups = await get_cooldown_duplicates(filtered)
            filtered = [n for n in filtered if n not in cooldown_dups]
            skipped_cooldown = len(cooldown_dups)

            # Check globally across ALL pools (unique constraint is table-wide, not per-pool)
            if filtered:
                existing = {row.number for row in db.query(ActiveNumber.number).filter(
                    ActiveNumber.number.in_(filtered)
                ).all()}
            else:
                existing = set()
            new_numbers = [n for n in filtered if n not in existing]
            duplicates = len(filtered) - len(new_numbers)

            # Insert with per-row conflict handling to survive any edge-case duplicates
            added = 0
            for num in new_numbers:
                try:
                    db.add(ActiveNumber(pool_id=pool_id, number=num))
                    db.flush()
                    added += 1
                except Exception:
                    db.rollback()
                    duplicates += 1

            pool = db.query(Pool).filter(Pool.id == pool_id).first()
            if pool:
                pool.last_restocked = utcnow()
            try:
                db.commit()
            except Exception:
                db.rollback()
                raise HTTPException(500, "Database commit failed after upload")
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
def export_pool(pool_id: int, token: str = Cookie(default=None)):
    user = get_user_from_token(token)
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
def cut_numbers(pool_id: int, count: int, token: str = Cookie(default=None)):
    user = get_user_from_token(token)
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
def clear_pool(pool_id: int, token: str = Cookie(default=None)):
    user = get_user_from_token(token)
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
def pause_pool(pool_id: int, reason: str = "", token: str = Cookie(default=None)):
    user = get_user_from_token(token)
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
def resume_pool(pool_id: int, token: str = Cookie(default=None)):
    user = get_user_from_token(token)
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
def toggle_admin_only(pool_id: int, token: str = Cookie(default=None)):
    user = get_user_from_token(token)
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
def set_trick_text(pool_id: int, trick_text: str, token: str = Cookie(default=None)):
    user = get_user_from_token(token)
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
def get_pool_access(pool_id: int, token: str = Cookie(default=None)):
    user = get_user_from_token(token)
    if not user or not user["is_admin"]:
        raise HTTPException(403, "Admin only")
    
    return get_pool_access_users(pool_id)

@app.post("/api/admin/pools/{pool_id}/access/{user_id}")
def grant_pool_access_endpoint(pool_id: int, user_id: int, token: str = Cookie(default=None)):
    user = get_user_from_token(token)
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
def revoke_pool_access_endpoint(pool_id: int, user_id: int, token: str = Cookie(default=None)):
    user = get_user_from_token(token)
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
    
    otp_data = {
        "type": "otp",
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
def my_otps(token: str = Cookie(default=None)):
    user = get_user_from_token(token)
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
async def search_otp(number: str, token: str = Cookie(default=None)):
    user = get_user_from_token(token)
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
def save_numbers(req: SaveRequest, token: str = Cookie(default=None)):
    user = get_user_from_token(token)
    if not user:
        raise HTTPException(401, "Not authenticated")
    
    expires_at = utcnow() + timedelta(minutes=req.timer_minutes)
    saved = 0
    
    if SessionLocal:
        with SessionLocal() as db:
            # Auto-create pool if it doesn't exist
            pool = db.query(Pool).filter(Pool.name == req.pool_name).first()
            if not pool:
                pool = Pool(
                    name=req.pool_name,
                    country_code="unknown",
                    match_format="5+4",
                    otp_link="",
                    trick_text=""
                )
                db.add(pool)
                db.flush()
                log.info(f"Auto-created pool '{req.pool_name}' from saved numbers")

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
        # Auto-create pool in memory if it doesn't exist
        if not any(p["name"] == req.pool_name for p in pools.values()):
            pool_id = _counters["pool"]
            _counters["pool"] += 1
            pools[pool_id] = {
                "id": pool_id, "name": req.pool_name, "country_code": "unknown",
                "otp_group_id": None, "otp_link": "", "match_format": "5+4",
                "telegram_match_format": "", "uses_platform": 0,
                "is_paused": False, "pause_reason": "", "trick_text": "",
                "is_admin_only": False, "last_restocked": None
            }
            active_numbers[pool_id] = []

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
def list_saved(token: str = Cookie(default=None)):
    user = get_user_from_token(token)
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
def ready_numbers(token: str = Cookie(default=None)):
    user = get_user_from_token(token)
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
def update_saved(saved_id: int, timer_minutes: int, token: str = Cookie(default=None)):
    user = get_user_from_token(token)
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
def delete_saved(saved_id: int, token: str = Cookie(default=None)):
    user = get_user_from_token(token)
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

# ── Change the phone number on a ready saved entry ──────────────────────────

class ChangeNumberRequest(BaseModel):
    number: str

@app.put("/api/saved/{saved_id}/number")
def change_saved_number(saved_id: int, req: ChangeNumberRequest, token: str = Cookie(default=None)):
    user = get_user_from_token(token)
    if not user:
        raise HTTPException(401, "Not authenticated")
    new_number = req.number.strip()
    if not new_number:
        raise HTTPException(400, "Number cannot be empty")

    if SessionLocal:
        with SessionLocal() as db:
            saved = db.query(SavedNumber).filter(
                SavedNumber.id == saved_id,
                SavedNumber.user_id == user["id"]
            ).first()
            if not saved:
                raise HTTPException(404, "Not found")
            if saved.moved:
                pool = db.query(Pool).filter(Pool.name == saved.pool_name).first()
                if pool:
                    old_row = db.query(ActiveNumber).filter(
                        ActiveNumber.pool_id == pool.id,
                        ActiveNumber.number == saved.number
                    ).first()
                    if old_row:
                        clash = db.query(ActiveNumber).filter(ActiveNumber.number == new_number).first()
                        if not clash:
                            old_row.number = new_number
                        else:
                            db.delete(old_row)
            saved.number = new_number
            db.commit()
    else:
        for s in saved_numbers:
            if s["id"] == saved_id and s["user_id"] == user["id"]:
                old_num = s["number"]
                if s.get("moved"):
                    pool = next((p for p in pools.values() if p["name"] == s["pool_name"]), None)
                    if pool:
                        nums = active_numbers.get(pool["id"], [])
                        if old_num in nums and new_number not in nums:
                            nums[nums.index(old_num)] = new_number
                s["number"] = new_number
                return {"ok": True}
        raise HTTPException(404, "Not found")

    return {"ok": True}

# ── Move a ready saved entry to a different pool ─────────────────────────────

class ChangePoolRequest(BaseModel):
    pool_name: str

@app.put("/api/saved/{saved_id}/pool")
def change_saved_pool(saved_id: int, req: ChangePoolRequest, token: str = Cookie(default=None)):
    user = get_user_from_token(token)
    if not user:
        raise HTTPException(401, "Not authenticated")
    new_pool_name = req.pool_name.strip()
    if not new_pool_name:
        raise HTTPException(400, "Pool name cannot be empty")

    if SessionLocal:
        with SessionLocal() as db:
            new_pool = db.query(Pool).filter(Pool.name == new_pool_name).first()
            if not new_pool:
                raise HTTPException(404, f"Pool '{new_pool_name}' not found")
            saved = db.query(SavedNumber).filter(
                SavedNumber.id == saved_id,
                SavedNumber.user_id == user["id"]
            ).first()
            if not saved:
                raise HTTPException(404, "Saved entry not found")
            if saved.moved:
                old_pool = db.query(Pool).filter(Pool.name == saved.pool_name).first()
                if old_pool:
                    old_row = db.query(ActiveNumber).filter(
                        ActiveNumber.pool_id == old_pool.id,
                        ActiveNumber.number == saved.number
                    ).first()
                    if old_row:
                        clash = db.query(ActiveNumber).filter(
                            ActiveNumber.pool_id == new_pool.id,
                            ActiveNumber.number == saved.number
                        ).first()
                        if not clash:
                            old_row.pool_id = new_pool.id
                        else:
                            db.delete(old_row)
            saved.pool_name = new_pool_name
            db.commit()
    else:
        new_pool = next((p for p in pools.values() if p["name"] == new_pool_name), None)
        if not new_pool:
            raise HTTPException(404, f"Pool '{new_pool_name}' not found")
        for s in saved_numbers:
            if s["id"] == saved_id and s["user_id"] == user["id"]:
                if s.get("moved"):
                    old_pool = next((p for p in pools.values() if p["name"] == s["pool_name"]), None)
                    if old_pool:
                        old_nums = active_numbers.get(old_pool["id"], [])
                        new_nums = active_numbers.get(new_pool["id"], [])
                        if s["number"] in old_nums and s["number"] not in new_nums:
                            old_nums.remove(s["number"])
                            new_nums.append(s["number"])
                s["pool_name"] = new_pool_name
                return {"ok": True}
        raise HTTPException(404, "Saved entry not found")

    return {"ok": True}

# ══════════════════════════════════════════════════════════════════════════════
#  REVIEWS ENDPOINTS
# ══════════════════════════════════════════════════════════════════════════════

class ReviewRequest(BaseModel):
    number: str
    rating: int
    comment: str = ""
    mark_as_bad: bool = False

@app.post("/api/reviews")
def submit_review(req: ReviewRequest, token: str = Cookie(default=None)):
    user = get_user_from_token(token)
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
def stats(token: str = Cookie(default=None)):
    user = get_user_from_token(token)
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
def list_users(token: str = Cookie(default=None)):
    user = get_user_from_token(token)
    if not user or not user["is_admin"]:
        raise HTTPException(403, "Admin only")
    
    if SessionLocal:
        with SessionLocal() as db:
            users_list = db.query(User).order_by(User.created_at.desc()).all()
            return [{"id": u.id, "username": u.username, "is_admin": u.is_admin, "is_approved": u.is_approved, "is_blocked": u.is_blocked, "created_at": u.created_at.isoformat()} for u in users_list]
    else:
        return [{"id": u["id"], "username": u["username"], "is_admin": u["is_admin"], "is_approved": u["is_approved"], "is_blocked": u.get("is_blocked", False), "created_at": u.get("created_at", utcnow().isoformat())} for u in users.values()]

@app.post("/api/admin/users/{user_id}/approve")
async def approve_user_endpoint(user_id: int, token: str = Cookie(default=None)):
    user = get_user_from_token(token)
    if not user or not user["is_admin"]:
        raise HTTPException(403, "Admin only")
    approve_user(user_id)
    await send_to_user(user_id, {"type": "notification", "message": "✅ Your account has been approved!"})
    return {"ok": True}

@app.post("/api/admin/users/{user_id}/block")
async def block_user_endpoint(user_id: int, token: str = Cookie(default=None)):
    user = get_user_from_token(token)
    if not user or not user["is_admin"]:
        raise HTTPException(403, "Admin only")
    block_user(user_id)
    await send_to_user(user_id, {"type": "notification", "message": "🚫 Your account has been blocked."})
    return {"ok": True}

@app.post("/api/admin/users/{user_id}/unblock")
async def unblock_user_endpoint(user_id: int, token: str = Cookie(default=None)):
    user = get_user_from_token(token)
    if not user or not user["is_admin"]:
        raise HTTPException(403, "Admin only")
    unblock_user(user_id)
    await send_to_user(user_id, {"type": "notification", "message": "✅ Your account has been unblocked!"})
    return {"ok": True}

@app.get("/api/admin/bad-numbers")
def list_bad_numbers(token: str = Cookie(default=None)):
    user = get_user_from_token(token)
    if not user or not user["is_admin"]:
        raise HTTPException(403, "Admin only")
    
    if SessionLocal:
        with SessionLocal() as db:
            bad = db.query(BadNumber).order_by(BadNumber.created_at.desc()).all()
            return [{"number": b.number, "reason": b.reason, "created_at": b.created_at.isoformat()} for b in bad]
    else:
        return [{"number": num, "reason": data.get("reason", ""), "created_at": data.get("marked_at", "")} for num, data in bad_numbers.items()]

@app.delete("/api/admin/bad-numbers")
def remove_bad_number(number: str, token: str = Cookie(default=None)):
    user = get_user_from_token(token)
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
def list_reviews(token: str = Cookie(default=None)):
    user = get_user_from_token(token)
    if not user or not user["is_admin"]:
        raise HTTPException(403, "Admin only")
    
    if SessionLocal:
        with SessionLocal() as db:
            reviews_list = db.query(NumberReview).order_by(NumberReview.created_at.desc()).limit(100).all()
            return [{"id": r.id, "user_id": r.user_id, "number": r.number, "rating": r.rating, "comment": r.comment, "created_at": r.created_at.isoformat()} for r in reviews_list]
    else:
        return [{"id": r["id"], "user_id": r["user_id"], "number": r["number"], "rating": r["rating"], "comment": r["comment"], "created_at": r["created_at"]} for r in sorted(reviews, key=lambda x: x["created_at"], reverse=True)[:100]]

@app.post("/api/admin/broadcast")
async def broadcast_message(message: str, token: str = Cookie(default=None)):
    user = get_user_from_token(token)
    if not user or not user["is_admin"]:
        raise HTTPException(403, "Admin only")
    
    await broadcast_all({"type": "broadcast", "message": message})
    return {"ok": True}

@app.post("/api/admin/settings/approval")
def set_approval_mode_endpoint(enabled: bool, token: str = Cookie(default=None)):
    user = get_user_from_token(token)
    if not user or not user["is_admin"]:
        raise HTTPException(403, "Admin only")
    
    set_approval_mode(enabled)
    return {"ok": True, "mode": "on" if enabled else "off"}

@app.post("/api/admin/settings/otp-redirect")
def set_otp_redirect_mode_endpoint(mode: str, token: str = Cookie(default=None)):
    user = get_user_from_token(token)
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
def get_buttons(token: str = Cookie(default=None)):
    user = get_user_from_token(token)
    if not user:
        raise HTTPException(401, "Not authenticated")
    
    return _get_custom_buttons()

@app.post("/api/admin/buttons")
def add_button(label: str, url: str, token: str = Cookie(default=None)):
    user = get_user_from_token(token)
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
def delete_button(button_id: int, token: str = Cookie(default=None)):
    user = get_user_from_token(token)
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
