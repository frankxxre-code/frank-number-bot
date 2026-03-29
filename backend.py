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
                        log.info(f"[Monitor] ✅ MONITOR_REQUEST sent for {number} user={user_id} group_id={group_id} match_format={match_format}")
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
            # Trigger monitoring for in-memory mode too
            pool = next((p for p in pools.values() if p["name"] == sn["pool_name"]), None)
            if pool and pool.get("otp_group_id"):
                try:
                    asyncio.create_task(request_monitor_bot(
                        number=sn["number"],
                        group_id=pool["otp_group_id"],
                        match_format=pool.get("telegram_match_format") or pool.get("match_format", "5+4"),
                        user_id=sn["user_id"]
                    ))
                    if pool.get("uses_platform", 0) in (1, 2):
                        start_platform_monitor(sn["user_id"], sn["number"], pool.get("telegram_match_format") or pool.get("match_format", "5+4"))
                except Exception as e:
                    log.error(f"[Scheduler] Monitor trigger failed for {sn['number']}: {e}")

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
    <title>NumBot Platform</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
        :root {
            --blue: #1a6bff;
            --blue-dark: #1255cc;
            --blue-light: #e8f0ff;
            --blue-mid: #4d8dff;
            --white: #ffffff;
            --gray-50: #f8faff;
            --gray-100: #eef2ff;
            --gray-200: #dde6ff;
            --gray-400: #8fa8d4;
            --gray-500: #5a78a8;
            --gray-700: #2a3f6e;
            --gray-900: #0d1f42;
            --green: #00c48c;
            --red: #ff4d6a;
            --yellow: #ffb020;
            --shadow-sm: 0 2px 8px rgba(26,107,255,0.08);
            --shadow-md: 0 4px 20px rgba(26,107,255,0.14);
            --shadow-lg: 0 8px 32px rgba(26,107,255,0.2);
            --radius: 16px;
            --radius-sm: 10px;
            --radius-lg: 24px;
            --radius-xl: 32px;
        }
        body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: var(--gray-50); min-height: 100vh; padding-bottom: 80px; color: var(--gray-900); }

        /* ── AUTH ── */
        .auth-wrap { min-height: 100vh; display: flex; align-items: center; justify-content: center; background: linear-gradient(145deg, #1a6bff 0%, #0d3d99 60%, #07205e 100%); padding: 20px; }
        .auth-card { background: white; border-radius: var(--radius-xl); padding: 40px 32px; width: 100%; max-width: 360px; box-shadow: 0 24px 60px rgba(0,0,0,0.25); }
        .auth-logo { text-align: center; margin-bottom: 8px; }
        .auth-logo svg { width: 52px; height: 52px; }
        .auth-title { text-align: center; font-size: 22px; font-weight: 800; color: var(--blue); margin-bottom: 4px; }
        .auth-sub { text-align: center; font-size: 13px; color: var(--gray-400); margin-bottom: 28px; }
        .tab-row { display: flex; background: var(--gray-100); border-radius: 50px; padding: 4px; margin-bottom: 24px; }
        .tab-btn { flex: 1; padding: 10px; border: none; border-radius: 50px; font-size: 14px; font-weight: 600; cursor: pointer; transition: all .2s; background: transparent; color: var(--gray-500); }
        .tab-btn.active { background: var(--blue); color: white; box-shadow: 0 4px 12px rgba(26,107,255,0.3); }
        .auth-input { width: 100%; padding: 14px 18px; border: 1.5px solid var(--gray-200); border-radius: 50px; font-size: 15px; outline: none; background: var(--gray-50); color: var(--gray-900); margin-bottom: 14px; transition: border-color .2s; }
        .auth-input:focus { border-color: var(--blue); background: white; }
        .auth-btn { width: 100%; background: var(--blue); color: white; border: none; padding: 15px; border-radius: 50px; font-size: 15px; font-weight: 700; cursor: pointer; transition: all .2s; box-shadow: 0 6px 20px rgba(26,107,255,0.35); margin-top: 6px; }
        .auth-btn:active { transform: scale(0.97); background: var(--blue-dark); }
        .error-msg { color: var(--red); font-size: 12px; margin-top: 8px; text-align: center; }

        /* ── HEADER ── */
        .top-bar { background: var(--blue); padding: 16px 20px 14px; display: flex; justify-content: space-between; align-items: center; position: sticky; top: 0; z-index: 100; box-shadow: 0 2px 12px rgba(26,107,255,0.25); }
        .top-bar-left { display: flex; align-items: center; gap: 10px; }
        .top-avatar { width: 38px; height: 38px; background: rgba(255,255,255,0.25); border-radius: 50%; display: flex; align-items: center; justify-content: font-size:18px; justify-content: center; font-size: 18px; }
        .top-user { font-size: 16px; font-weight: 700; color: white; }
        .top-id { font-size: 11px; color: rgba(255,255,255,0.7); }
        .top-right { display: flex; align-items: center; gap: 8px; }
        .conn-dot { width: 8px; height: 8px; border-radius: 50%; background: #00e676; box-shadow: 0 0 6px #00e676; }
        .conn-dot.off { background: #ff5252; box-shadow: 0 0 6px #ff5252; }
        .admin-badge { background: rgba(255,255,255,0.25); color: white; padding: 3px 10px; border-radius: 20px; font-size: 10px; font-weight: 700; letter-spacing: .5px; }
        .time-label { font-size: 12px; color: rgba(255,255,255,0.85); font-weight: 500; }

        /* ── NAV ── */
        .bottom-nav { position: fixed; bottom: 0; left: 0; right: 0; background: white; display: flex; justify-content: space-around; padding: 10px 8px 20px; border-top: 1px solid var(--gray-200); z-index: 100; box-shadow: 0 -4px 20px rgba(0,0,0,0.06); }
        .nav-item { display: flex; flex-direction: column; align-items: center; gap: 3px; cursor: pointer; padding: 6px 16px; border-radius: 14px; transition: all .2s; flex: 1; }
        .nav-item:active { background: var(--blue-light); }
        .nav-item.active { background: var(--blue-light); }
        .nav-icon { font-size: 22px; line-height: 1; }
        .nav-label { font-size: 10px; font-weight: 600; color: var(--gray-400); letter-spacing: .3px; }
        .nav-item.active .nav-label { color: var(--blue); }

        /* ── PAGES ── */
        .page { display: none; padding-bottom: 16px; }
        .page.active { display: block; }

        /* ── SECTION TITLE ── */
        .section-title { font-size: 11px; font-weight: 700; color: var(--gray-400); padding: 20px 20px 10px; text-transform: uppercase; letter-spacing: 1.2px; }

        /* ── CARDS ── */
        .card { background: white; border-radius: var(--radius-lg); margin: 0 16px 12px; padding: 20px; box-shadow: var(--shadow-sm); border: 1px solid var(--gray-200); }
        .card-blue { background: linear-gradient(135deg, var(--blue) 0%, #0d3d99 100%); color: white; border: none; }
        .card-blue .label { color: rgba(255,255,255,0.75); }

        /* ── NUMBER DISPLAY ── */
        .number-label { font-size: 11px; font-weight: 700; color: var(--gray-400); text-transform: uppercase; letter-spacing: 1px; margin-bottom: 10px; }
        .number-value { font-size: 28px; font-weight: 800; font-family: 'SF Mono', 'Fira Code', monospace; color: var(--blue); margin-bottom: 14px; cursor: pointer; transition: opacity .15s; word-break: break-all; }
        .number-value:active { opacity: 0.7; }
        .number-value.dark { color: white; }
        .region-pill { display: inline-flex; align-items: center; gap: 6px; background: var(--blue-light); padding: 6px 14px; border-radius: 50px; font-size: 12px; font-weight: 600; color: var(--blue); }
        .btn-row { display: flex; gap: 10px; margin-top: 16px; }
        .btn { padding: 11px 20px; border-radius: 50px; font-size: 13px; font-weight: 600; cursor: pointer; border: none; transition: all .2s; }
        .btn-sm { padding: 8px 16px; font-size: 12px; }
        .btn-primary { background: var(--blue); color: white; box-shadow: 0 4px 12px rgba(26,107,255,0.3); }
        .btn-primary:active { background: var(--blue-dark); transform: scale(0.97); }
        .btn-secondary { background: var(--gray-100); color: var(--gray-700); }
        .btn-secondary:active { background: var(--gray-200); transform: scale(0.97); }
        .btn-danger { background: rgba(255,77,106,0.1); color: var(--red); }
        .btn-danger:active { background: rgba(255,77,106,0.2); }
        .btn-green { background: rgba(0,196,140,0.1); color: var(--green); }

        /* ── OTP CARD ── */
        .otp-card { background: linear-gradient(135deg, #00c48c, #00966d); border-radius: var(--radius-lg); margin: 0 16px 12px; padding: 20px; color: white; animation: slideUp .3s ease; box-shadow: 0 8px 24px rgba(0,196,140,0.3); }
        @keyframes slideUp { from { opacity:0; transform:translateY(16px); } to { opacity:1; transform:translateY(0); } }
        .otp-header { font-size: 11px; font-weight: 700; letter-spacing: 1px; opacity: .8; margin-bottom: 8px; text-transform: uppercase; }
        .otp-code { font-size: 48px; font-weight: 900; font-family: monospace; letter-spacing: 6px; text-align: center; cursor: pointer; margin: 8px 0; }
        .otp-code:active { opacity: .7; }
        .otp-timer { text-align: center; font-size: 12px; opacity: .8; }
        .otp-msg { font-size: 11px; opacity: .75; text-align: center; margin-top: 6px; word-break: break-all; }

        /* ── REGION LIST ── */
        .filter-bar { display: flex; align-items: center; gap: 10px; background: white; margin: 8px 16px; padding: 12px 16px; border-radius: 50px; border: 1.5px solid var(--gray-200); box-shadow: var(--shadow-sm); }
        .filter-bar:focus-within { border-color: var(--blue); }
        .filter-input { flex: 1; border: none; outline: none; font-size: 14px; background: transparent; color: var(--gray-900); }
        .filter-input::placeholder { color: var(--gray-400); }
        .filter-btn { background: var(--blue); color: white; border: none; padding: 8px 18px; border-radius: 50px; font-size: 13px; font-weight: 600; cursor: pointer; }
        .filter-btn:active { background: var(--blue-dark); }

        .pool-item { display: flex; justify-content: space-between; align-items: center; background: white; margin: 0 16px 8px; padding: 16px; border-radius: var(--radius); border: 1.5px solid var(--gray-200); cursor: pointer; transition: all .2s; box-shadow: var(--shadow-sm); }
        .pool-item:active { background: var(--blue-light); border-color: var(--blue); transform: scale(0.99); }
        .pool-item.paused { opacity: .5; cursor: not-allowed; }
        .pool-name { font-size: 15px; font-weight: 700; color: var(--gray-900); }
        .pool-code { font-size: 12px; color: var(--gray-400); margin-top: 2px; }
        .pool-tip { font-size: 11px; color: var(--yellow); margin-top: 3px; font-weight: 500; }
        .pool-count { background: var(--blue-light); padding: 5px 12px; border-radius: 50px; font-size: 12px; font-weight: 700; color: var(--blue); }
        .pool-paused { font-size: 11px; color: var(--red); font-weight: 600; }

        /* ── SAVED PAGE ── */
        .saved-item { display: flex; justify-content: space-between; align-items: center; background: white; margin: 0 16px 8px; padding: 16px; border-radius: var(--radius); border: 1.5px solid var(--gray-200); box-shadow: var(--shadow-sm); }
        .saved-num { font-family: monospace; font-size: 15px; font-weight: 700; color: var(--blue); cursor: pointer; }
        .saved-num:active { opacity: .7; }
        .timer-badge { padding: 5px 12px; border-radius: 50px; font-size: 11px; font-weight: 700; }
        .timer-green { background: rgba(0,196,140,0.1); color: var(--green); }
        .timer-yellow { background: rgba(255,176,32,0.12); color: var(--yellow); }
        .timer-red { background: rgba(255,77,106,0.1); color: var(--red); }
        .timer-ready { background: rgba(26,107,255,0.1); color: var(--blue); }

        .ready-card { background: white; margin: 0 16px 12px; border-radius: var(--radius-lg); border: 2px solid var(--blue); box-shadow: var(--shadow-md); overflow: hidden; }
        .ready-card-head { background: var(--blue); padding: 12px 16px; display: flex; justify-content: space-between; align-items: center; }
        .ready-pool-name { font-size: 13px; font-weight: 700; color: white; }
        .ready-queue { font-size: 11px; color: rgba(255,255,255,0.8); }
        .ready-card-body { padding: 16px; }
        .ready-number { font-family: monospace; font-size: 22px; font-weight: 800; color: var(--blue); cursor: pointer; margin-bottom: 12px; }
        .ready-number:active { opacity: .7; }
        .ready-actions { display: flex; gap: 8px; flex-wrap: wrap; }
        .ready-otp { background: linear-gradient(135deg, #00c48c, #00966d); border-radius: var(--radius-sm); padding: 10px 14px; margin-top: 10px; }
        .ready-otp-label { font-size: 10px; font-weight: 700; color: rgba(255,255,255,0.8); text-transform: uppercase; letter-spacing: .8px; }
        .ready-otp-code { font-size: 28px; font-weight: 900; font-family: monospace; color: white; cursor: pointer; margin: 4px 0; }
        .ready-otp-msg { font-size: 10px; color: rgba(255,255,255,0.75); word-break: break-all; }

        /* ── HISTORY ── */
        .history-item { background: white; margin: 0 16px 8px; padding: 16px; border-radius: var(--radius); border: 1.5px solid var(--gray-200); box-shadow: var(--shadow-sm); }
        .history-num { font-family: monospace; font-size: 12px; color: var(--gray-400); margin-bottom: 4px; }
        .history-otp { font-size: 26px; font-weight: 800; font-family: monospace; color: var(--green); cursor: pointer; }
        .history-otp:active { opacity: .7; }
        .history-time { font-size: 11px; color: var(--gray-400); margin-top: 4px; }

        /* ── ADMIN ── */
        .admin-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px; padding: 8px 16px; }
        .admin-card { background: white; border-radius: var(--radius); padding: 16px 12px; cursor: pointer; border: 1.5px solid var(--gray-200); text-align: center; transition: all .2s; box-shadow: var(--shadow-sm); }
        .admin-card:active { background: var(--blue-light); border-color: var(--blue); transform: scale(0.97); }
        .admin-icon { font-size: 26px; margin-bottom: 6px; }
        .admin-label { font-size: 11px; font-weight: 700; color: var(--gray-700); }

        /* ── MODALS ── */
        .modal { display: none; position: fixed; top: 0; left: 0; right: 0; bottom: 0; background: rgba(13,31,66,0.6); backdrop-filter: blur(4px); z-index: 1000; align-items: flex-end; justify-content: center; }
        .modal.show { display: flex; }
        .modal-content { background: white; border-radius: var(--radius-xl) var(--radius-xl) 0 0; max-height: 90vh; overflow-y: auto; width: 100%; max-width: 600px; padding-bottom: 20px; animation: slideUp .25s ease; }
        .modal-handle { width: 40px; height: 4px; background: var(--gray-200); border-radius: 4px; margin: 12px auto 0; }
        .modal-header { padding: 16px 20px; font-weight: 800; font-size: 18px; color: var(--gray-900); border-bottom: 1px solid var(--gray-200); }
        .modal-body { padding: 20px; }
        .fg { margin-bottom: 18px; }
        .fg label { display: block; font-size: 11px; font-weight: 700; color: var(--gray-500); margin-bottom: 8px; text-transform: uppercase; letter-spacing: .8px; }
        .fg input, .fg select, .fg textarea { width: 100%; padding: 13px 16px; border: 1.5px solid var(--gray-200); border-radius: var(--radius); background: var(--gray-50); color: var(--gray-900); outline: none; font-size: 14px; transition: border-color .2s; }
        .fg input:focus, .fg select:focus, .fg textarea:focus { border-color: var(--blue); background: white; }
        .modal-footer { display: flex; gap: 10px; justify-content: flex-end; padding: 0 20px; margin-top: 10px; }

        /* ── FEEDBACK MODAL ── */
        .feedback-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; padding: 20px; }
        .feedback-btn { padding: 14px; border-radius: var(--radius); font-size: 13px; font-weight: 600; cursor: pointer; border: 1.5px solid var(--gray-200); background: var(--gray-50); color: var(--gray-700); transition: all .2s; text-align: center; }
        .feedback-btn:active { transform: scale(0.97); }
        .feedback-btn.bad { border-color: var(--red); background: rgba(255,77,106,0.06); color: var(--red); }
        .feedback-btn.good { border-color: var(--green); background: rgba(0,196,140,0.06); color: var(--green); }

        /* ── SAVE FORM ── */
        .save-form { background: white; margin: 0 16px 12px; border-radius: var(--radius-lg); padding: 20px; border: 1.5px solid var(--gray-200); box-shadow: var(--shadow-sm); }
        .save-form textarea { width: 100%; border: 1.5px solid var(--gray-200); border-radius: var(--radius); padding: 12px; font-size: 14px; resize: vertical; min-height: 80px; outline: none; background: var(--gray-50); color: var(--gray-900); }
        .save-form textarea:focus { border-color: var(--blue); }
        .preset-row { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 8px; }
        .preset-btn { padding: 7px 14px; border-radius: 50px; font-size: 12px; font-weight: 600; border: 1.5px solid var(--gray-200); background: var(--gray-50); color: var(--gray-700); cursor: pointer; transition: all .2s; }
        .preset-btn:active { background: var(--blue-light); border-color: var(--blue); color: var(--blue); }
        .save-row { display: flex; gap: 10px; margin-top: 12px; align-items: center; }
        .save-row input { flex: 1; border: 1.5px solid var(--gray-200); border-radius: 50px; padding: 11px 16px; font-size: 13px; outline: none; background: var(--gray-50); color: var(--gray-900); }
        .save-row input:focus { border-color: var(--blue); }

        /* ── MISC ── */
        .loading { text-align: center; padding: 40px 20px; color: var(--gray-400); }
        .spinner { width: 36px; height: 36px; border: 3px solid var(--gray-200); border-top-color: var(--blue); border-radius: 50%; animation: spin .7s linear infinite; margin: 0 auto 12px; }
        @keyframes spin { to { transform: rotate(360deg); } }
        .empty-state { text-align: center; padding: 48px 20px; color: var(--gray-400); }
        .empty-icon { font-size: 48px; margin-bottom: 12px; }
        .empty-text { font-size: 14px; font-weight: 500; }
        .toast { position: fixed; bottom: 100px; left: 50%; transform: translateX(-50%); background: var(--gray-900); color: white; padding: 11px 22px; border-radius: 50px; font-size: 13px; font-weight: 500; z-index: 9999; max-width: 90%; text-align: center; animation: toastAnim 2.5s ease forwards; white-space: nowrap; }
        @keyframes toastAnim { 0%{opacity:0;transform:translateX(-50%) translateY(12px)} 12%{opacity:1;transform:translateX(-50%) translateY(0)} 80%{opacity:1} 100%{opacity:0;transform:translateX(-50%) translateY(-8px)} }
        .badge-green { display: inline-block; background: rgba(0,196,140,0.1); color: var(--green); padding: 3px 10px; border-radius: 50px; font-size: 11px; font-weight: 700; }
        .badge-red { display: inline-block; background: rgba(255,77,106,0.1); color: var(--red); padding: 3px 10px; border-radius: 50px; font-size: 11px; font-weight: 700; }
        .divider { height: 1px; background: var(--gray-200); margin: 4px 16px; }
        .hidden { display: none !important; }
    </style>
</head>
<body>

<!-- AUTH -->
<div id="authContainer">
    <div class="auth-wrap">
        <div class="auth-card">
            <div class="auth-logo">
                <svg viewBox="0 0 52 52" fill="none"><circle cx="26" cy="26" r="26" fill="#1a6bff"/><rect x="13" y="17" width="26" height="18" rx="4" fill="white" opacity=".2"/><rect x="16" y="20" width="20" height="12" rx="2" fill="white"/><circle cx="26" cy="26" r="3" fill="#1a6bff"/></svg>
            </div>
            <div class="auth-title">NumBot</div>
            <div class="auth-sub">Number management platform</div>
            <div class="tab-row">
                <button class="tab-btn active" id="authLoginTab" onclick="switchTab('login')">Login</button>
                <button class="tab-btn" id="authRegisterTab" onclick="switchTab('register')">Register</button>
            </div>
            <div id="loginForm">
                <input type="text" id="loginUsername" class="auth-input" placeholder="Username" autocomplete="username">
                <input type="password" id="loginPassword" class="auth-input" placeholder="Password" autocomplete="current-password">
                <button class="auth-btn" onclick="doLogin()">Sign In</button>
                <div id="loginError" class="error-msg"></div>
            </div>
            <div id="registerForm" style="display:none;">
                <input type="text" id="regUsername" class="auth-input" placeholder="Choose username" autocomplete="username">
                <input type="password" id="regPassword" class="auth-input" placeholder="Password (min 6 chars)" autocomplete="new-password">
                <button class="auth-btn" onclick="doRegister()">Create Account</button>
                <div id="regError" class="error-msg"></div>
            </div>
        </div>
    </div>
</div>

<!-- APP -->
<div id="appContainer" style="display:none;">
    <div class="top-bar">
        <div class="top-bar-left">
            <div class="top-avatar">👤</div>
            <div>
                <div class="top-user" id="userName">User</div>
                <div class="top-id" id="userId">ID: --</div>
            </div>
        </div>
        <div class="top-right">
            <span class="time-label" id="currentTime">--:--</span>
            <div class="conn-dot" id="connDot"></div>
            <div id="adminBadge" style="display:none;" class="admin-badge">ADMIN</div>
        </div>
    </div>

    <!-- HOME PAGE -->
    <div id="homePage" class="page active">
        <div class="section-title">Platform</div>
        <div class="card card-blue" style="margin-top:8px;">
            <div class="number-label" style="color:rgba(255,255,255,0.7);">WELCOME BACK</div>
            <div style="font-size:26px;font-weight:800;color:white;" id="homeUserName">User</div>
            <div style="font-size:13px;color:rgba(255,255,255,0.75);margin-top:4px;">NumBot Number Platform</div>
        </div>
        <div class="section-title">Quick Links</div>
        <div id="customButtonsList" style="padding: 0 16px;"></div>
    </div>

    <!-- NUMBERS PAGE -->
    <div id="numbersPage" class="page">
        <div class="card" style="margin-top:16px;">
            <div class="number-label">YOUR ACTIVE NUMBER</div>
            <div class="number-value" id="currentNumber" onclick="copyNumber()" title="Tap to copy">—</div>
            <div style="display:flex;justify-content:space-between;align-items:center;">
                <div class="region-pill" id="currentRegion">No region selected</div>
                <div class="btn-row" style="margin:0;">
                    <button class="btn btn-sm btn-secondary" onclick="changeNumber()">🔄 Change</button>
                    <button class="btn btn-sm btn-primary" onclick="copyNumber()">📋 Copy</button>
                </div>
            </div>
        </div>
        <div id="otpDisplay" style="display:none;"></div>
        <div class="section-title">SELECT REGION</div>
        <div class="filter-bar">
            <input type="text" id="prefixFilter" class="filter-input" placeholder="Search by name or prefix…">
            <button class="filter-btn" onclick="applyFilter()">Go</button>
        </div>
        <div id="regionList"><div class="loading"><div class="spinner"></div>Loading regions…</div></div>
    </div>

    <!-- SAVED PAGE -->
    <div id="savedPage" class="page">
        <!-- READY NUMBERS (shown first, only when they exist) -->
        <div id="readySection" style="display:none;">
            <div class="section-title">✅ READY NUMBERS</div>
            <div id="readyList"></div>
        </div>

        <!-- SAVE FORM -->
        <div class="section-title">💾 SAVE NUMBERS</div>
        <div class="save-form">
            <textarea id="savedNumbersInput" placeholder="Enter numbers, one per line…"></textarea>
            <div style="margin-top:12px;">
                <div style="font-size:11px;font-weight:700;color:var(--gray-500);text-transform:uppercase;letter-spacing:.8px;margin-bottom:8px;">Timer</div>
                <div style="display:flex;gap:8px;align-items:center;">
                    <input type="text" id="timerInput" value="30m" placeholder="30m, 2h, 1d…" style="flex:1;border:1.5px solid var(--gray-200);border-radius:50px;padding:10px 14px;font-size:13px;outline:none;background:var(--gray-50);color:var(--gray-900);">
                </div>
                <div class="preset-row">
                    <button class="preset-btn" onclick="setTimerPreset('30m')">30m</button>
                    <button class="preset-btn" onclick="setTimerPreset('2h')">2h</button>
                    <button class="preset-btn" onclick="setTimerPreset('1d')">1d</button>
                    <button class="preset-btn" onclick="setTimerPreset('2s')">2s</button>
                </div>
            </div>
            <div class="save-row">
                <input type="text" id="poolNameInput" placeholder="Pool label (e.g. Nigeria)">
                <button class="btn btn-primary" onclick="saveNumbers()">Save</button>
            </div>
        </div>

        <!-- SAVED POOLS — pool count cards only, NO pool list shown here -->
        <div id="savedPoolsSection" style="display:none;">
            <div class="section-title">⏳ WAITING POOLS</div>
            <div id="savedList"></div>
        </div>
    </div>

    <!-- HISTORY PAGE -->
    <div id="historyPage" class="page">
        <div class="section-title">📜 OTP HISTORY</div>
        <div id="historyList"><div class="empty-state"><div class="empty-icon">📭</div><div class="empty-text">No OTP history yet</div></div></div>
    </div>

    <!-- ADMIN PAGE -->
    <div id="adminPage" class="page hidden">
        <div class="section-title">🛠 ADMIN PANEL</div>
        <div class="admin-grid">
            <div class="admin-card" onclick="loadAdminStats()"><div class="admin-icon">📊</div><div class="admin-label">Stats</div></div>
            <div class="admin-card" onclick="openCreatePoolModal()"><div class="admin-icon">➕</div><div class="admin-label">New Pool</div></div>
            <div class="admin-card" onclick="openUploadModal()"><div class="admin-icon">📁</div><div class="admin-label">Upload</div></div>
            <div class="admin-card" onclick="loadUsersList()"><div class="admin-icon">👥</div><div class="admin-label">Users</div></div>
            <div class="admin-card" onclick="loadBadNumbers()"><div class="admin-icon">🚫</div><div class="admin-label">Bad Nums</div></div>
            <div class="admin-card" onclick="showBroadcast()"><div class="admin-icon">📢</div><div class="admin-label">Broadcast</div></div>
            <div class="admin-card" onclick="showSettings()"><div class="admin-icon">⚙️</div><div class="admin-label">Settings</div></div>
            <div class="admin-card" onclick="loadReviews()"><div class="admin-icon">📝</div><div class="admin-label">Reviews</div></div>
        </div>
        <div id="adminStatsDiv" class="card" style="display:none;margin-top:12px;"></div>
        <div id="adminUsersDiv" style="display:none;padding:0 16px;"></div>
        <div id="adminBadDiv" style="display:none;padding:0 16px;"></div>
        <div id="adminReviewsDiv" style="display:none;padding:0 16px;"></div>
        <div id="adminBroadcastDiv" class="card" style="display:none;margin-top:12px;">
            <textarea id="broadcastMsg" rows="3" placeholder="Message to broadcast…" style="width:100%;padding:12px;border:1.5px solid var(--gray-200);border-radius:var(--radius);background:var(--gray-50);font-size:14px;resize:none;outline:none;"></textarea>
            <button class="btn btn-primary" style="margin-top:10px;width:100%;" onclick="sendBroadcast()">Send Broadcast</button>
        </div>
        <div id="adminSettingsDiv" class="card" style="display:none;margin-top:12px;">
            <div class="fg"><label>Approval Mode</label><select id="approvalMode"><option value="on">ON - New users need approval</option><option value="off">OFF - Auto-approve all</option></select></div>
            <div class="fg"><label>OTP Redirect</label><select id="otpRedirect"><option value="pool">Per-Pool Link</option><option value="hardcoded">Hardcoded Link</option></select></div>
            <button class="btn btn-primary" onclick="saveSettings()">Save Settings</button>
        </div>
        <div id="poolsList" style="padding:0 16px;margin-top:4px;"></div>
    </div>

    <!-- BOTTOM NAV -->
    <div class="bottom-nav">
        <div class="nav-item active" data-page="home"><div class="nav-icon">🏠</div><div class="nav-label">Home</div></div>
        <div class="nav-item" data-page="numbers"><div class="nav-icon">📱</div><div class="nav-label">Numbers</div></div>
        <div class="nav-item" data-page="saved"><div class="nav-icon">💾</div><div class="nav-label">Saved</div></div>
        <div class="nav-item" data-page="history"><div class="nav-icon">📜</div><div class="nav-label">History</div></div>
        <div class="nav-item hidden" data-page="admin" id="adminNavItem"><div class="nav-icon">⚙️</div><div class="nav-label">Admin</div></div>
    </div>
</div>

<!-- MODALS -->
<!-- Feedback modal -->
<div id="feedbackModal" class="modal">
    <div class="modal-content">
        <div class="modal-handle"></div>
        <div class="modal-header">Rate Your Number</div>
        <div style="font-family:monospace;font-size:18px;text-align:center;padding:16px 20px 0;color:var(--blue);font-weight:700;" id="feedbackNumber"></div>
        <div class="feedback-grid">
            <button class="feedback-btn good" onclick="submitFeedback('worked')">✅ Worked</button>
            <button class="feedback-btn bad" onclick="submitFeedback('bad')">❌ Not Available</button>
            <button class="feedback-btn" onclick="submitFeedback('email')">📧 Email Only</button>
            <button class="feedback-btn" onclick="submitFeedback('other_devices')">📱 Other Devices</button>
            <button class="feedback-btn" onclick="submitFeedback('try_later')">⏳ Try Later</button>
            <button class="feedback-btn" onclick="showOtherFeedback()">📝 Other</button>
        </div>
        <div id="otherFeedbackDiv" style="display:none;padding:0 20px 20px;">
            <textarea id="otherFeedbackText" rows="2" placeholder="Describe the issue…" style="width:100%;padding:12px;border:1.5px solid var(--gray-200);border-radius:var(--radius);font-size:14px;outline:none;resize:none;"></textarea>
            <button class="btn btn-primary" style="width:100%;margin-top:10px;" onclick="submitFeedback('other')">Submit</button>
        </div>
    </div>
</div>

<!-- Change Pool modal (hidden, only shown when button clicked) -->
<div id="changePoolModal" class="modal">
    <div class="modal-content">
        <div class="modal-handle"></div>
        <div class="modal-header">🌐 Switch Pool</div>
        <div class="modal-body">
            <div style="font-size:11px;font-weight:700;color:var(--gray-500);text-transform:uppercase;letter-spacing:.8px;margin-bottom:6px;">Current Number</div>
            <div id="changePoolCurrentNum" style="font-family:monospace;font-size:18px;color:var(--blue);font-weight:700;margin-bottom:20px;"></div>
            <div class="fg">
                <label>Switch To Pool</label>
                <select id="changePoolSelect" style="width:100%;"></select>
            </div>
            <div style="font-size:12px;color:var(--gray-400);margin-top:6px;">You will get the next available number from that pool.</div>
        </div>
        <div class="modal-footer">
            <button class="btn btn-secondary" onclick="document.getElementById('changePoolModal').classList.remove('show')">Cancel</button>
            <button class="btn btn-primary" onclick="submitSwitchPool()">Switch Pool</button>
        </div>
    </div>
</div>

<!-- Pool create/edit modal -->
<div id="poolModal" class="modal">
    <div class="modal-content">
        <div class="modal-handle"></div>
        <div class="modal-header" id="poolModalTitle">Create Pool</div>
        <div class="modal-body">
            <div class="fg"><label>Pool Name *</label><input type="text" id="poolName" placeholder="e.g. Nigeria"></div>
            <div class="fg"><label>Country Code *</label><input type="text" id="poolCode" placeholder="e.g. 234"></div>
            <div class="fg"><label>OTP Group ID *</label><input type="text" id="poolGroupId" placeholder="e.g. -1001234567890"></div>
            <div class="fg"><label>OTP Link</label><input type="text" id="poolOtpLink" placeholder="https://t.me/your_channel"></div>
            <div class="fg"><label>Match Format *</label><input type="text" id="poolMatchFormat" value="5+4" placeholder="e.g. 5+4"></div>
            <div class="fg"><label>Telegram Match Format</label><input type="text" id="poolTelegramMatchFormat" placeholder="Leave blank to use above"></div>
            <div class="fg"><label>Monitoring Mode</label><select id="poolUsesPlatform"><option value="0">0 - Telegram Only</option><option value="1">1 - Platform Only</option><option value="2">2 - Both</option></select></div>
            <div class="fg"><label>Trick Text</label><textarea id="poolTrickText" rows="2" placeholder="Tips for users…"></textarea></div>
            <div style="display:flex;gap:20px;margin:12px 0;">
                <label style="display:flex;align-items:center;gap:8px;font-size:14px;cursor:pointer;"><input type="checkbox" id="poolAdminOnly"> Admin Only</label>
                <label style="display:flex;align-items:center;gap:8px;font-size:14px;cursor:pointer;"><input type="checkbox" id="poolPaused" onchange="document.getElementById('pauseReasonDiv').style.display=this.checked?'block':'none'"> Paused</label>
            </div>
            <div id="pauseReasonDiv" style="display:none;" class="fg"><label>Pause Reason</label><input type="text" id="poolPauseReason" placeholder="Reason for pausing"></div>
        </div>
        <div class="modal-footer">
            <button class="btn btn-secondary" onclick="closePoolModal()">Cancel</button>
            <button class="btn btn-primary" onclick="savePool()">Save Pool</button>
        </div>
    </div>
</div>

<!-- Upload modal -->
<div id="uploadModal" class="modal">
    <div class="modal-content">
        <div class="modal-handle"></div>
        <div class="modal-header">Upload Numbers</div>
        <div class="modal-body">
            <div class="fg"><label>Select Pool</label><select id="uploadPoolSelect"></select></div>
            <div class="fg"><label>Upload File (.txt or .csv)</label><input type="file" id="uploadFile" accept=".txt,.csv"></div>
        </div>
        <div class="modal-footer">
            <button class="btn btn-secondary" onclick="closeUploadModal()">Cancel</button>
            <button class="btn btn-primary" onclick="uploadNumbers()">Upload</button>
        </div>
        <div id="uploadResult" style="padding:0 20px 10px;font-size:13px;color:var(--gray-500);"></div>
    </div>
</div>

<script>
const API_BASE = window.location.origin;
let currentUser = null;
let currentAssignment = null;
let currentPoolId = null;
let ws = null;
let otpTimer = null;
let currentFilter = null;
let allRegions = [];
const savedOtps = {};

// ── UTILS ──
function showToast(msg) {
    const t = document.createElement('div'); t.className = 'toast'; t.textContent = msg;
    document.body.appendChild(t); setTimeout(() => t.remove(), 2600);
}
function formatTime() { document.getElementById('currentTime').textContent = new Date().toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'}); }
setInterval(formatTime, 1000); formatTime();
function copyText(t) { navigator.clipboard.writeText(t).then(() => showToast('📋 Copied!')).catch(() => { const ta = document.createElement('textarea'); ta.value = t; document.body.appendChild(ta); ta.select(); document.execCommand('copy'); ta.remove(); showToast('📋 Copied!'); }); }
function fmt(n) { if (!n || n === '—') return n; return n.startsWith('+') ? n : '+' + n; }
function setTimerPreset(v) { document.getElementById('timerInput').value = v; }
function escapeHtml(t) { if (!t) return ''; return String(t).replace(/[&<>"']/g, m => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m])); }
function switchTab(tab) {
    document.getElementById('loginForm').style.display = tab==='login'?'block':'none';
    document.getElementById('registerForm').style.display = tab==='register'?'block':'none';
    document.getElementById('authLoginTab').classList.toggle('active', tab==='login');
    document.getElementById('authRegisterTab').classList.toggle('active', tab==='register');
}

// ── AUTH ──
async function checkAuth() {
    try {
        const res = await fetch(`${API_BASE}/api/auth/me`, {credentials:'include'});
        if (res.ok) {
            currentUser = await res.json();
            document.getElementById('authContainer').style.display = 'none';
            document.getElementById('appContainer').style.display = 'block';
            document.getElementById('userName').textContent = currentUser.username;
            document.getElementById('homeUserName').textContent = currentUser.username;
            document.getElementById('userId').textContent = `ID: ${currentUser.id}`;
            if (currentUser.is_admin) {
                document.getElementById('adminBadge').style.display = 'inline-block';
                document.getElementById('adminNavItem').classList.remove('hidden');
                document.getElementById('adminPage').classList.remove('hidden');
                loadAdminPools();
            }
            connectWebSocket();
            loadRegions();
            loadCurrentAssignment();
            loadSavedNumbers();
            loadHistory();
            loadCustomButtons();
            return true;
        }
    } catch(e) {}
    document.getElementById('authContainer').style.display = 'flex';
    document.getElementById('appContainer').style.display = 'none';
    return false;
}

async function doLogin() {
    const username = document.getElementById('loginUsername').value.trim();
    const password = document.getElementById('loginPassword').value;
    const err = document.getElementById('loginError');
    err.textContent = '';
    if (!username || !password) { err.textContent = 'Enter username and password'; return; }
    try {
        const res = await fetch(`${API_BASE}/api/auth/login`, {method:'POST',headers:{'Content-Type':'application/json'},credentials:'include',body:JSON.stringify({username,password})});
        const data = await res.json();
        if (res.ok) { checkAuth(); }
        else { err.textContent = data.detail || 'Login failed'; }
    } catch(e) { err.textContent = 'Network error'; }
}

async function doRegister() {
    const username = document.getElementById('regUsername').value.trim();
    const password = document.getElementById('regPassword').value;
    const err = document.getElementById('regError');
    err.textContent = '';
    if (password.length < 6) { err.textContent = 'Password must be at least 6 characters'; return; }
    try {
        const res = await fetch(`${API_BASE}/api/auth/register`, {method:'POST',headers:{'Content-Type':'application/json'},credentials:'include',body:JSON.stringify({username,password})});
        const data = await res.json();
        if (res.ok) {
            if (data.approved) { checkAuth(); }
            else { err.textContent = '✅ Account created! Awaiting admin approval.'; }
        } else { err.textContent = data.detail || 'Registration failed'; }
    } catch(e) { err.textContent = 'Network error'; }
}

// ── WEBSOCKET ──
function connectWebSocket() {
    const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
    ws = new WebSocket(`${protocol}//${location.host}/ws/user/${currentUser.id}`);
    ws.onopen = () => { document.getElementById('connDot').classList.remove('off'); };
    ws.onmessage = (e) => {
        const data = JSON.parse(e.data);
        if (data.type === 'otp') displayOTP(data);
        else if (data.type === 'saved_otp') displaySavedOTP(data);
        else if (data.type === 'broadcast') showToast('📢 ' + data.message);
        else if (data.type === 'notification') showToast(data.message);
    };
    ws.onclose = () => { document.getElementById('connDot').classList.add('off'); setTimeout(connectWebSocket, 5000); };
    ws.onerror = () => { document.getElementById('connDot').classList.add('off'); };
}

function displayOTP(data) {
    const div = document.getElementById('otpDisplay');
    if (otpTimer) clearTimeout(otpTimer);
    div.innerHTML = `<div class="otp-card">
        <div class="otp-header">🔑 OTP CODE</div>
        <div class="otp-code" onclick="copyText('${escapeHtml(data.otp)}')">${escapeHtml(data.otp)}</div>
        <div class="otp-timer" id="otpCountdown">Auto-deletes in 30s</div>
        <div class="otp-msg">${escapeHtml(data.raw_message || '')}</div>
    </div>`;
    div.style.display = 'block';
    let secs = 30;
    const interval = setInterval(() => {
        secs--;
        const cd = document.getElementById('otpCountdown');
        if (cd) cd.textContent = `Auto-deletes in ${secs}s`;
        if (secs <= 0) { clearInterval(interval); div.style.display = 'none'; }
    }, 1000);
    otpTimer = setTimeout(() => { clearInterval(interval); div.style.display = 'none'; }, 30000);
    loadHistory();
}

function displaySavedOTP(data) {
    savedOtps[data.number] = {otp: data.otp, raw_message: data.raw_message || ''};
    showToast(`🔑 OTP ${data.number}: ${data.otp}`);
    loadReadyNumbers();
    loadHistory();
    setTimeout(() => { delete savedOtps[data.number]; loadReadyNumbers(); }, 30000);
}

// ── NAVIGATION ──
function navigateTo(page) {
    document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
    document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
    document.getElementById(page + 'Page').classList.add('active');
    document.querySelector(`.nav-item[data-page="${page}"]`)?.classList.add('active');
    if (page === 'saved') { loadSavedNumbers(); }
    if (page === 'history') { loadHistory(); }
    if (page === 'numbers') { loadCurrentAssignment(); }
}
document.querySelectorAll('.nav-item').forEach(i => i.addEventListener('click', () => navigateTo(i.dataset.page)));

// ── REGIONS ──
async function loadRegions() {
    try {
        const res = await fetch(`${API_BASE}/api/pools`, {credentials:'include'});
        if (!res.ok) throw new Error();
        allRegions = await res.json();
        renderRegions();
    } catch(e) { document.getElementById('regionList').innerHTML = '<div class="empty-state"><div class="empty-icon">⚠️</div><div class="empty-text">Failed to load regions</div></div>'; }
}

function renderRegions() {
    const container = document.getElementById('regionList');
    let list = allRegions;
    if (currentFilter) list = allRegions.filter(r => r.name.toLowerCase().includes(currentFilter.toLowerCase()) || r.country_code.includes(currentFilter));
    if (!list.length) { container.innerHTML = '<div class="empty-state"><div class="empty-icon">🔍</div><div class="empty-text">No regions found</div></div>'; return; }
    container.innerHTML = list.map(r => `
        <div class="pool-item ${r.is_paused ? 'paused' : ''}" onclick="selectRegion(${r.id})">
            <div>
                <div class="pool-name">${escapeHtml(r.name)}</div>
                <div class="pool-code">+${escapeHtml(r.country_code)}</div>
                ${r.trick_text ? `<div class="pool-tip">💡 ${escapeHtml(r.trick_text)}</div>` : ''}
            </div>
            <div>${r.is_paused
                ? `<span class="pool-paused">⏸ Paused</span>`
                : `<span class="pool-count">${r.number_count}</span>`}
            </div>
        </div>`).join('');
}

function applyFilter() { currentFilter = document.getElementById('prefixFilter').value.trim(); renderRegions(); }

async function selectRegion(poolId) {
    const region = allRegions.find(r => r.id === poolId);
    if (region && region.is_paused) { showToast(`⏸ Region paused: ${region.pause_reason || 'Temporarily unavailable'}`); return; }
    try {
        const res = await fetch(`${API_BASE}/api/pools/assign`, {method:'POST',headers:{'Content-Type':'application/json'},credentials:'include',body:JSON.stringify({pool_id: poolId})});
        const data = await res.json();
        if (res.ok) {
            currentAssignment = data;
            currentPoolId = data.pool_id;
            document.getElementById('currentNumber').textContent = fmt(data.number);
            document.getElementById('currentNumber').onclick = () => copyText(fmt(data.number));
            document.getElementById('currentRegion').textContent = data.pool_name;
            document.getElementById('otpDisplay').style.display = 'none';
            if (otpTimer) clearTimeout(otpTimer);
            showToast(`✅ Assigned: ${fmt(data.number)}`);
            loadRegions();
        } else { showToast(data.detail || 'Failed to assign number'); }
    } catch(e) { showToast('Network error'); }
}

async function loadCurrentAssignment() {
    try {
        const res = await fetch(`${API_BASE}/api/pools/my-assignment`, {credentials:'include'});
        const data = await res.json();
        if (data.assignment) {
            currentAssignment = data.assignment;
            currentPoolId = data.assignment.pool_id;
            document.getElementById('currentNumber').textContent = fmt(currentAssignment.number);
            document.getElementById('currentNumber').onclick = () => copyText(fmt(currentAssignment.number));
            document.getElementById('currentRegion').textContent = currentAssignment.pool_name;
        }
    } catch(e) {}
}

function copyNumber() { if (currentAssignment) copyText(fmt(currentAssignment.number)); else showToast('No number to copy'); }

function changeNumber() {
    if (!currentAssignment) { showToast('No active number to change'); return; }
    document.getElementById('feedbackNumber').textContent = fmt(currentAssignment.number);
    document.getElementById('feedbackModal').classList.add('show');
}

async function submitFeedback(type) {
    const comment = type === 'other' ? document.getElementById('otherFeedbackText').value : type;
    const markAsBad = type === 'bad';
    document.getElementById('feedbackModal').classList.remove('show');
    document.getElementById('otherFeedbackDiv').style.display = 'none';
    document.getElementById('otherFeedbackText').value = '';
    if (!currentAssignment) return;
    const savedAssignment = currentAssignment;
    const savedPoolId = currentPoolId;
    document.getElementById('currentNumber').textContent = '…';
    const releasePromise = fetch(`${API_BASE}/api/pools/release/${savedAssignment.assignment_id}`, {method:'POST',credentials:'include'});
    fetch(`${API_BASE}/api/reviews`, {method:'POST',headers:{'Content-Type':'application/json'},credentials:'include',body:JSON.stringify({number:savedAssignment.number,rating:markAsBad?1:4,comment,mark_as_bad:markAsBad})});
    await releasePromise;
    if (savedPoolId) {
        const res = await fetch(`${API_BASE}/api/pools/assign`, {method:'POST',headers:{'Content-Type':'application/json'},credentials:'include',body:JSON.stringify({pool_id:savedPoolId})});
        const data = await res.json();
        if (res.ok) {
            currentAssignment = data;
            document.getElementById('currentNumber').textContent = fmt(data.number);
            document.getElementById('currentNumber').onclick = () => copyText(fmt(data.number));
            document.getElementById('currentRegion').textContent = data.pool_name;
            document.getElementById('otpDisplay').style.display = 'none';
            if (otpTimer) clearTimeout(otpTimer);
            showToast(`✅ New number: ${fmt(data.number)}`);
        } else {
            showToast('No more numbers — select another region');
            currentAssignment = null; currentPoolId = null;
            document.getElementById('currentNumber').textContent = '—';
            document.getElementById('currentRegion').textContent = 'No region selected';
        }
    }
    loadRegions();
}

function showOtherFeedback() { document.getElementById('otherFeedbackDiv').style.display = 'block'; }

// ── TIMER PARSER ──
function parseTimer(t) {
    const m = t.match(/^(\d+)([smhd])$/i);
    if (m) {
        const n = parseInt(m[1]), u = m[2].toLowerCase();
        if (u==='s') return Math.max(1, Math.ceil(n/60));
        if (u==='h') return n*60;
        if (u==='d') return n*1440;
        return n;
    }
    const n = parseInt(t); return isNaN(n) ? 30 : n;
}

// ── SAVE NUMBERS ──
async function saveNumbers() {
    const numbers = document.getElementById('savedNumbersInput').value.split('\\n').map(l=>l.trim()).filter(l=>l);
    const timerMinutes = parseTimer(document.getElementById('timerInput').value.trim());
    const poolName = document.getElementById('poolNameInput').value.trim() || 'Default Pool';
    if (!numbers.length) { showToast('Enter at least one number'); return; }
    try {
        const res = await fetch(`${API_BASE}/api/saved`, {method:'POST',headers:{'Content-Type':'application/json'},credentials:'include',body:JSON.stringify({numbers,timer_minutes:timerMinutes,pool_name:poolName})});
        const data = await res.json();
        if (res.ok) { showToast(`✅ Saved ${data.saved} numbers`); document.getElementById('savedNumbersInput').value = ''; loadSavedNumbers(); }
        else { showToast(data.detail || 'Failed to save'); }
    } catch(e) { showToast('Network error'); }
}

async function loadSavedNumbers() {
    try {
        const res = await fetch(`${API_BASE}/api/saved`, {credentials:'include'});
        const data = await res.json();
        // Waiting pools (timer still running) are background only — never shown on page
        // Only ready numbers (moved=true) are shown, handled by loadReadyNumbers below
        document.getElementById('savedPoolsSection').style.display = 'none';
        document.getElementById('savedList').innerHTML = '';
    } catch(e) {}
    loadReadyNumbers();
}

async function deleteSavedPool(ids) {
    if (!confirm(`Delete all ${ids.length} number(s) in this pool?`)) return;
    for (const id of ids) await fetch(`${API_BASE}/api/saved/${id}`, {method:'DELETE',credentials:'include'});
    loadSavedNumbers();
}

async function loadReadyNumbers() {
    try {
        const res = await fetch(`${API_BASE}/api/saved/ready`, {credentials:'include'});
        const data = await res.json();
        const container = document.getElementById('readyList');
        const section = document.getElementById('readySection');
        if (!data.length) { section.style.display='none'; container.innerHTML=''; return; }
        section.style.display = 'block';

        // Group by pool_name — each pool has one active slot (front of queue)
        const grouped = {};
        data.forEach(item => {
            if (!grouped[item.pool_name]) grouped[item.pool_name] = [];
            grouped[item.pool_name].push(item);
        });

        container.innerHTML = Object.entries(grouped).map(([poolName, items]) => {
            const active = items[0];
            const count = items.length;
            const otpInfo = savedOtps[active.number];
            const otpHtml = otpInfo ? `<div class="ready-otp">
                <div class="ready-otp-label">🔑 OTP CODE</div>
                <div class="ready-otp-code" onclick="copyText('${escapeHtml(otpInfo.otp)}')">${escapeHtml(otpInfo.otp)} 📋</div>
                ${otpInfo.raw_message ? `<div class="ready-otp-msg">${escapeHtml(otpInfo.raw_message)}</div>` : ''}
            </div>` : '';
            return `<div class="ready-card">
                <div class="ready-card-head">
                    <div class="ready-pool-name">📦 ${escapeHtml(poolName)}</div>
                    <div class="ready-queue">${count} in queue</div>
                </div>
                <div class="ready-card-body">
                    <div class="ready-number" onclick="copyText('${escapeHtml(active.number)}');showToast('📋 Copied!')">${escapeHtml(active.number)} 📋</div>
                    <div class="ready-actions">
                        <button class="btn btn-sm btn-secondary" onclick="doNextNumber(${active.id}, '${escapeHtml(poolName)}')">🔄 Change Number</button>
                        <button class="btn btn-sm btn-secondary" onclick="openSwitchPool(${active.id}, '${escapeHtml(active.number)}', '${escapeHtml(poolName)}')">🌐 Change Pool</button>
                    </div>
                    ${otpHtml}
                </div>
            </div>`;
        }).join('');
    } catch(e) {}
}

let changeTargetId = null;

async function doNextNumber(id, poolName) {
    try {
        const res = await fetch(`${API_BASE}/api/saved/${id}/next-number`, {method:'POST',credentials:'include'});
        const data = await res.json();
        if (res.ok) {
            showToast(`✅ New number: ${data.number}`);
            // Trigger monitor for newly assigned number — lookup pool info from main page
            await triggerSavedMonitor(data.number);
            loadReadyNumbers();
        } else { showToast(data.detail || 'No more numbers in this pool'); }
    } catch(e) { showToast('Network error'); }
}

async function triggerSavedMonitor(number) {
    try {
        const res = await fetch(`${API_BASE}/api/saved/trigger-monitor`, {method:'POST',headers:{'Content-Type':'application/json'},credentials:'include',body:JSON.stringify({number})});
        if (res.ok) showToast('👁️ Monitoring started…');
    } catch(e) {}
}

async function openSwitchPool(id, number, currentPool) {
    changeTargetId = id;
    document.getElementById('changePoolCurrentNum').textContent = number;
    const select = document.getElementById('changePoolSelect');
    select.innerHTML = '<option>Loading pools…</option>';
    document.getElementById('changePoolModal').classList.add('show');
    try {
        // Only show OTHER ready pools — pools that also have numbers ready
        const res = await fetch(`${API_BASE}/api/saved/ready-pools`, {credentials:'include'});
        const readyPools = await res.json();
        const options = readyPools.filter(p => p.pool_name !== currentPool && p.count > 0);
        if (!options.length) {
            select.innerHTML = '<option disabled>No other ready pools available</option>';
            return;
        }
        select.innerHTML = options.map(p => `<option value="${escapeHtml(p.pool_name)}">${escapeHtml(p.pool_name)} (${p.count} available)</option>`).join('');
    } catch(e) { select.innerHTML = '<option disabled>Failed to load pools</option>'; }
}

async function submitSwitchPool() {
    const newPool = document.getElementById('changePoolSelect').value;
    if (!newPool) { showToast('Select a pool'); return; }
    try {
        const res = await fetch(`${API_BASE}/api/saved/${changeTargetId}/switch-pool?new_pool_name=${encodeURIComponent(newPool)}`, {method:'POST',credentials:'include'});
        const data = await res.json();
        if (res.ok) {
            showToast(`✅ Switched to ${newPool}: ${data.number}`);
            document.getElementById('changePoolModal').classList.remove('show');
            await triggerSavedMonitor(data.number);
            loadReadyNumbers();
        } else { showToast(data.detail || 'Switch failed'); }
    } catch(e) { showToast('Network error'); }
}

async function deleteSaved(id) { await fetch(`${API_BASE}/api/saved/${id}`,{method:'DELETE',credentials:'include'}); loadSavedNumbers(); }

// ── HISTORY ──
async function loadHistory() {
    try {
        const res = await fetch(`${API_BASE}/api/otp/my`, {credentials:'include'});
        const data = await res.json();
        const container = document.getElementById('historyList');
        if (!data.length) { container.innerHTML = '<div class="empty-state"><div class="empty-icon">📭</div><div class="empty-text">No OTP history yet</div></div>'; return; }
        container.innerHTML = data.map(item => `<div class="history-item">
            <div class="history-num">${escapeHtml(item.number)}</div>
            <div class="history-otp" onclick="copyText('${escapeHtml(item.otp_code)}')">${escapeHtml(item.otp_code)} 📋</div>
            <div class="history-time">${new Date(item.delivered_at).toLocaleString()}</div>
        </div>`).join('');
    } catch(e) {}
}

// ── CUSTOM BUTTONS ──
async function loadCustomButtons() {
    try {
        const res = await fetch(`${API_BASE}/api/buttons`, {credentials:'include'});
        const buttons = await res.json();
        const container = document.getElementById('customButtonsList');
        if (!buttons.length) { container.innerHTML = ''; return; }
        container.innerHTML = buttons.map(b => `<a href="${escapeHtml(b.url)}" target="_blank" style="display:flex;align-items:center;justify-content:space-between;background:white;padding:14px 16px;border-radius:var(--radius);border:1.5px solid var(--gray-200);margin-bottom:10px;text-decoration:none;box-shadow:var(--shadow-sm);transition:all .2s;">
            <span style="font-size:14px;font-weight:600;color:var(--gray-900);">${escapeHtml(b.label)}</span>
            <span style="color:var(--blue);">→</span>
        </a>`).join('');
    } catch(e) {}
}

// ── ADMIN ──
let currentEditPoolId = null;

function openCreatePoolModal() {
    currentEditPoolId = null;
    document.getElementById('poolModalTitle').textContent = 'Create New Pool';
    ['poolName','poolCode','poolGroupId','poolOtpLink','poolTelegramMatchFormat','poolTrickText','poolPauseReason'].forEach(id => document.getElementById(id).value = '');
    document.getElementById('poolMatchFormat').value = '5+4';
    document.getElementById('poolUsesPlatform').value = '0';
    document.getElementById('poolAdminOnly').checked = false;
    document.getElementById('poolPaused').checked = false;
    document.getElementById('pauseReasonDiv').style.display = 'none';
    document.getElementById('poolModal').classList.add('show');
}

function closePoolModal() { document.getElementById('poolModal').classList.remove('show'); }

async function savePool() {
    const body = {
        name: document.getElementById('poolName').value.trim(),
        country_code: document.getElementById('poolCode').value.trim(),
        otp_group_id: parseInt(document.getElementById('poolGroupId').value) || null,
        otp_link: document.getElementById('poolOtpLink').value.trim(),
        match_format: document.getElementById('poolMatchFormat').value.trim() || '5+4',
        telegram_match_format: document.getElementById('poolTelegramMatchFormat').value.trim(),
        uses_platform: parseInt(document.getElementById('poolUsesPlatform').value),
        trick_text: document.getElementById('poolTrickText').value.trim(),
        is_admin_only: document.getElementById('poolAdminOnly').checked,
        is_paused: document.getElementById('poolPaused').checked,
        pause_reason: document.getElementById('poolPauseReason').value.trim()
    };
    if (!body.name || !body.country_code) { showToast('Name and country code are required'); return; }
    const url = currentEditPoolId ? `${API_BASE}/api/admin/pools/${currentEditPoolId}` : `${API_BASE}/api/admin/pools`;
    const method = currentEditPoolId ? 'PUT' : 'POST';
    try {
        const res = await fetch(url, {method,headers:{'Content-Type':'application/json'},credentials:'include',body:JSON.stringify(body)});
        const data = await res.json();
        if (res.ok) { showToast(currentEditPoolId ? '✅ Pool updated' : '✅ Pool created'); closePoolModal(); loadAdminPools(); loadRegions(); }
        else { showToast(data.detail || 'Failed to save pool'); }
    } catch(e) { showToast('Network error'); }
}

async function loadAdminPools() {
    try {
        const res = await fetch(`${API_BASE}/api/pools`, {credentials:'include'});
        const pools = await res.json();
        const container = document.getElementById('poolsList');
        container.innerHTML = pools.map(p => `
            <div class="card" style="margin-bottom:10px;">
                <div style="display:flex;justify-content:space-between;align-items:flex-start;">
                    <div>
                        <div style="font-size:15px;font-weight:700;color:var(--gray-900);">${escapeHtml(p.name)}</div>
                        <div style="font-size:12px;color:var(--gray-400);margin-top:2px;">+${escapeHtml(p.country_code)} · ${p.number_count} numbers · Format: ${escapeHtml(p.match_format)}</div>
                        ${p.is_paused?`<div style="font-size:11px;color:var(--red);margin-top:3px;">⏸ Paused: ${escapeHtml(p.pause_reason||'')}</div>`:''}
                    </div>
                    <span class="pool-count">${p.number_count}</span>
                </div>
                <div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:12px;">
                    <button class="btn btn-sm btn-secondary" onclick="editPool(${p.id})">✏️ Edit</button>
                    <button class="btn btn-sm btn-secondary" onclick="openUploadForPool(${p.id})">📁 Upload</button>
                    ${p.is_paused
                        ? `<button class="btn btn-sm btn-green" onclick="resumePool(${p.id})">▶ Resume</button>`
                        : `<button class="btn btn-sm btn-danger" onclick="pausePool(${p.id})">⏸ Pause</button>`}
                    <button class="btn btn-sm btn-danger" onclick="deletePool(${p.id})">🗑</button>
                </div>
            </div>`).join('');
    } catch(e) {}
}

async function editPool(poolId) {
    try {
        const res = await fetch(`${API_BASE}/api/pools`, {credentials:'include'});
        const pools = await res.json();
        const p = pools.find(x => x.id === poolId);
        if (!p) return;
        currentEditPoolId = poolId;
        document.getElementById('poolModalTitle').textContent = 'Edit Pool';
        document.getElementById('poolName').value = p.name;
        document.getElementById('poolCode').value = p.country_code;
        document.getElementById('poolGroupId').value = p.otp_group_id || '';
        document.getElementById('poolOtpLink').value = p.otp_link || '';
        document.getElementById('poolMatchFormat').value = p.match_format || '5+4';
        document.getElementById('poolTelegramMatchFormat').value = p.telegram_match_format || '';
        document.getElementById('poolUsesPlatform').value = p.uses_platform || 0;
        document.getElementById('poolTrickText').value = p.trick_text || '';
        document.getElementById('poolAdminOnly').checked = !!p.is_admin_only;
        document.getElementById('poolPaused').checked = !!p.is_paused;
        document.getElementById('pauseReasonDiv').style.display = p.is_paused ? 'block' : 'none';
        document.getElementById('poolPauseReason').value = p.pause_reason || '';
        document.getElementById('poolModal').classList.add('show');
    } catch(e) { showToast('Failed to load pool data'); }
}

async function deletePool(poolId) {
    if (!confirm('Delete this pool and all its numbers?')) return;
    try {
        const res = await fetch(`${API_BASE}/api/admin/pools/${poolId}`, {method:'DELETE',credentials:'include'});
        if (res.ok) { showToast('Pool deleted'); loadAdminPools(); loadRegions(); }
        else { const d = await res.json(); showToast(d.detail || 'Delete failed'); }
    } catch(e) { showToast('Network error'); }
}

async function pausePool(poolId) {
    const reason = prompt('Pause reason (optional):') || '';
    const res = await fetch(`${API_BASE}/api/admin/pools/${poolId}/pause?reason=${encodeURIComponent(reason)}`, {method:'POST',credentials:'include'});
    if (res.ok) { showToast('Pool paused'); loadAdminPools(); loadRegions(); }
}

async function resumePool(poolId) {
    const res = await fetch(`${API_BASE}/api/admin/pools/${poolId}/resume`, {method:'POST',credentials:'include'});
    if (res.ok) { showToast('Pool resumed'); loadAdminPools(); loadRegions(); }
}

function openUploadForPool(poolId) {
    document.getElementById('uploadPoolSelect').innerHTML = `<option value="${poolId}">Pool ${poolId}</option>`;
    document.getElementById('uploadModal').classList.add('show');
}

function openUploadModal() {
    fetch(`${API_BASE}/api/pools`, {credentials:'include'}).then(r=>r.json()).then(pools => {
        document.getElementById('uploadPoolSelect').innerHTML = pools.map(p=>`<option value="${p.id}">${escapeHtml(p.name)}</option>`).join('');
    });
    document.getElementById('uploadModal').classList.add('show');
}

function closeUploadModal() { document.getElementById('uploadModal').classList.remove('show'); }

async function uploadNumbers() {
    const poolId = document.getElementById('uploadPoolSelect').value;
    const file = document.getElementById('uploadFile').files[0];
    if (!file) { showToast('Select a file first'); return; }
    const form = new FormData(); form.append('file', file);
    const resultDiv = document.getElementById('uploadResult');
    resultDiv.textContent = 'Uploading…';
    try {
        const res = await fetch(`${API_BASE}/api/admin/pools/${poolId}/upload`, {method:'POST',credentials:'include',body:form});
        const data = await res.json();
        if (res.ok) { resultDiv.textContent = `✅ Added ${data.added}, skipped bad: ${data.skipped_bad}, cooldown: ${data.skipped_cooldown}, duplicates: ${data.duplicates}`; loadAdminPools(); loadRegions(); }
        else { resultDiv.textContent = data.detail || 'Upload failed'; }
    } catch(e) { resultDiv.textContent = 'Network error'; }
}

async function loadAdminStats() {
    const div = document.getElementById('adminStatsDiv');
    div.style.display = 'block';
    div.innerHTML = '<div class="loading"><div class="spinner"></div>Loading…</div>';
    try {
        const res = await fetch(`${API_BASE}/api/admin/stats`, {credentials:'include'});
        const d = await res.json();
        div.innerHTML = `<div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;">
            ${Object.entries(d).map(([k,v])=>`<div style="text-align:center;"><div style="font-size:22px;font-weight:800;color:var(--blue);">${v}</div><div style="font-size:11px;color:var(--gray-400);text-transform:uppercase;letter-spacing:.5px;">${k.replace(/_/g,' ')}</div></div>`).join('')}
        </div>`;
    } catch(e) { div.innerHTML = 'Failed to load stats'; }
}

async function loadUsersList() {
    const div = document.getElementById('adminUsersDiv');
    div.style.display = 'block';
    div.innerHTML = '<div class="loading"><div class="spinner"></div>Loading…</div>';
    try {
        const res = await fetch(`${API_BASE}/api/admin/users`, {credentials:'include'});
        const users = await res.json();
        div.innerHTML = users.map(u => `<div class="card" style="margin-bottom:8px;">
            <div style="display:flex;justify-content:space-between;align-items:center;">
                <div>
                    <div style="font-weight:700;color:var(--gray-900);">${escapeHtml(u.username)}</div>
                    <div style="font-size:11px;color:var(--gray-400);margin-top:2px;">ID:${u.id} · ${u.is_admin?'Admin':'User'}</div>
                </div>
                <div style="display:flex;gap:6px;align-items:center;">
                    ${u.is_approved?'<span class="badge-green">Active</span>':'<span class="badge-red">Pending</span>'}
                </div>
            </div>
            <div style="display:flex;gap:8px;margin-top:10px;flex-wrap:wrap;">
                ${!u.is_approved?`<button class="btn btn-sm btn-green" onclick="approveUser(${u.id})">✅ Approve</button>`:''}
                ${u.is_blocked?`<button class="btn btn-sm btn-secondary" onclick="unblockUser(${u.id})">🔓 Unblock</button>`:`<button class="btn btn-sm btn-danger" onclick="blockUser(${u.id})">🚫 Block</button>`}
            </div>
        </div>`).join('');
    } catch(e) { div.innerHTML = 'Failed to load users'; }
}

async function approveUser(id) { await fetch(`${API_BASE}/api/admin/users/${id}/approve`,{method:'POST',credentials:'include'}); loadUsersList(); showToast('User approved'); }
async function blockUser(id) { await fetch(`${API_BASE}/api/admin/users/${id}/block`,{method:'POST',credentials:'include'}); loadUsersList(); showToast('User blocked'); }
async function unblockUser(id) { await fetch(`${API_BASE}/api/admin/users/${id}/unblock`,{method:'POST',credentials:'include'}); loadUsersList(); showToast('User unblocked'); }

async function loadBadNumbers() {
    const div = document.getElementById('adminBadDiv');
    div.style.display = 'block';
    div.innerHTML = '<div class="loading"><div class="spinner"></div>Loading…</div>';
    try {
        const res = await fetch(`${API_BASE}/api/admin/bad-numbers`, {credentials:'include'});
        const nums = await res.json();
        if (!nums.length) { div.innerHTML = '<div class="empty-state"><div class="empty-icon">✅</div><div class="empty-text">No bad numbers</div></div>'; return; }
        div.innerHTML = nums.map(n=>`<div class="saved-item">
            <div><div class="saved-num">${escapeHtml(n.number)}</div><div style="font-size:11px;color:var(--gray-400);margin-top:2px;">${escapeHtml(n.reason)}</div></div>
            <button class="btn btn-sm btn-secondary" onclick="removeBadNum('${escapeHtml(n.number)}')">Remove</button>
        </div>`).join('');
    } catch(e) { div.innerHTML = 'Failed'; }
}

async function removeBadNum(number) { await fetch(`${API_BASE}/api/admin/bad-numbers?number=${encodeURIComponent(number)}`,{method:'DELETE',credentials:'include'}); loadBadNumbers(); }

async function loadReviews() {
    const div = document.getElementById('adminReviewsDiv');
    div.style.display = 'block';
    div.innerHTML = '<div class="loading"><div class="spinner"></div>Loading…</div>';
    try {
        const res = await fetch(`${API_BASE}/api/admin/reviews`, {credentials:'include'});
        const reviews = await res.json();
        if (!reviews.length) { div.innerHTML = '<div class="empty-state"><div class="empty-icon">📭</div><div class="empty-text">No reviews yet</div></div>'; return; }
        div.innerHTML = reviews.map(r=>`<div class="card" style="margin-bottom:8px;">
            <div style="font-family:monospace;font-weight:700;color:var(--blue);">${escapeHtml(r.number)}</div>
            <div style="font-size:13px;margin-top:4px;">Rating: ${'⭐'.repeat(r.rating)}</div>
            ${r.comment?`<div style="font-size:12px;color:var(--gray-500);margin-top:4px;">${escapeHtml(r.comment)}</div>`:''}
        </div>`).join('');
    } catch(e) { div.innerHTML = 'Failed'; }
}

function showBroadcast() { document.getElementById('adminBroadcastDiv').style.display = 'block'; }
async function sendBroadcast() {
    const msg = document.getElementById('broadcastMsg').value.trim();
    if (!msg) { showToast('Enter a message'); return; }
    await fetch(`${API_BASE}/api/admin/broadcast?message=${encodeURIComponent(msg)}`,{method:'POST',credentials:'include'});
    showToast('Broadcast sent'); document.getElementById('broadcastMsg').value='';
}

function showSettings() { document.getElementById('adminSettingsDiv').style.display='block'; }
async function saveSettings() {
    const ap = document.getElementById('approvalMode').value;
    const otp = document.getElementById('otpRedirect').value;
    await fetch(`${API_BASE}/api/admin/settings/approval?enabled=${ap==='on'}`,{method:'POST',credentials:'include'});
    await fetch(`${API_BASE}/api/admin/settings/otp-redirect?mode=${otp}`,{method:'POST',credentials:'include'});
    showToast('Settings saved');
}

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

@app.post("/api/saved/{saved_id}/next-number")
def ready_next_number(saved_id: int, token: str = Cookie(default=None)):
    """Replace this ready number with the next available number from the same pool."""
    user = get_user_from_token(token)
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
            # Do NOT put old number back — consuming it so we don't rotate between same two numbers

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
                # Exclude current number to avoid getting same one back
                nums = [n for n in active_numbers.get(pid, []) if n != s["number"]]
                if not nums:
                    raise HTTPException(404, "No more numbers available in this pool")
                new_number = nums[-1]
                nums.pop()
                # Do NOT put old number back — don't rotate between same two numbers
                active_numbers[pid] = nums
                s["number"] = new_number
                return {"ok": True, "number": new_number, "pool_name": s["pool_name"]}
        raise HTTPException(404, "Ready number not found")

@app.post("/api/saved/{saved_id}/switch-pool")
def ready_switch_pool(saved_id: int, new_pool_name: str, token: str = Cookie(default=None)):
    """Move this ready number slot to a different pool, picking the next number from that pool."""
    user = get_user_from_token(token)
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
def list_ready_pools(token: str = Cookie(default=None)):
    """Return pools that have ready numbers (moved=True saved numbers), with their stock count."""
    user = get_user_from_token(token)
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
async def trigger_saved_monitor(req: TriggerMonitorRequest, token: str = Cookie(default=None)):
    """
    Trigger monitor bot for a saved/ready number.
    Looks up the pool from the number's saved_number record,
    gets group_id and match_format, sends monitor request.
    """
    user = get_user_from_token(token)
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
