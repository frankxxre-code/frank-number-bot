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
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware
from fastapi import (FastAPI, WebSocket, WebSocketDisconnect,
                     Depends, HTTPException, Cookie, Header,
                     UploadFile, File, BackgroundTasks, Form, Query, Request)
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
                    # Ensure existing admin account is always approved and has admin rights
                    existing_admin = db.query(User).filter(User.is_admin == True).first()
                    if existing_admin and not existing_admin.is_approved:
                        existing_admin.is_approved = True
                        db.commit()
                        log.info(f"Fixed admin approval: {existing_admin.username}")
                    # Also ensure 'admin' user specifically is always approved
                    admin_user = db.query(User).filter(User.username == "admin").first()
                    if admin_user:
                        if not admin_user.is_approved or not admin_user.is_admin:
                            admin_user.is_approved = True
                            admin_user.is_admin = True
                            db.commit()
                            log.info("Ensured admin user is approved and has admin rights")

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

def get_user_from_token(token: str, x_token: str = None):
    # Accept token from cookie OR X-Token header (for Railway HTTPS compatibility)
    effective_token = token or x_token
    if not effective_token:
        return None
    token = effective_token
    
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

# CORS must be added FIRST (innermost), ProxyHeaders SECOND (outermost)
# In FastAPI/Starlette, last added = outermost = runs first on incoming requests
app.add_middleware(
    CORSMiddleware,
    allow_origins=[FRONTEND_URL] if FRONTEND_URL != "*" else ["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
# ProxyHeadersMiddleware added LAST so it runs FIRST — correctly unwraps Railway HTTPS headers
app.add_middleware(ProxyHeadersMiddleware, trusted_hosts="*")

# ══════════════════════════════════════════════════════════════════════════════
#  FRONTEND - EMBEDDED HTML (Complete - White/Blue Theme, Full Feature Parity)
# ══════════════════════════════════════════════════════════════════════════════

FRONTEND_HTML = '''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no, viewport-fit=cover">
    <title>NEON GRID NETWORK</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
        :root {
            --primary: #0a84ff;
            --primary-dark: #0060d0;
            --success: #10b981;
            --danger: #ef4444;
            --warning: #f59e0b;
            --bg: #f0f4f8;
            --surface: #ffffff;
            --surface2: #f7fafc;
            --border: #e2e8f0;
            --text: #1a202c;
            --text-muted: #64748b;
            --text-light: #94a3b8;
            --shadow: 0 2px 12px rgba(0,0,0,0.08);
            --shadow-lg: 0 8px 32px rgba(10,132,255,0.12);
        }
        body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: var(--bg); min-height: 100vh; padding-bottom: 75px; color: var(--text); }
        /* STATUS BAR */
        .status-bar { background: var(--primary); padding: 10px 16px 8px; color: white; font-size: 13px; font-weight: 500; display: flex; justify-content: space-between; position: sticky; top: 0; z-index: 100; box-shadow: 0 2px 8px rgba(10,132,255,0.3); }
        /* HEADER */
        .header { background: var(--surface); padding: 14px 16px; border-bottom: 1px solid var(--border); box-shadow: var(--shadow); }
        .user-info { display: flex; justify-content: space-between; align-items: center; }
        .user-name { font-size: 18px; font-weight: 700; color: var(--primary); }
        .user-id { font-size: 11px; color: var(--text-muted); background: var(--surface2); padding: 3px 10px; border-radius: 20px; border: 1px solid var(--border); }
        /* CARDS */
        .card { background: var(--surface); border-radius: 20px; padding: 20px; margin: 12px 16px; box-shadow: var(--shadow); border: 1px solid var(--border); }
        .card-blue { background: linear-gradient(135deg, var(--primary), #5e2aff); color: white; box-shadow: var(--shadow-lg); border: none; }
        .card-blue .label { color: rgba(255,255,255,0.8); }
        .card-green { background: linear-gradient(135deg, var(--success), #059669); color: white; border: none; }
        /* NUMBER CARD */
        .number-card { background: var(--surface); border-radius: 20px; margin: 12px 16px; padding: 20px; border: 2px solid var(--primary); box-shadow: var(--shadow-lg); }
        .number-label { font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: 1px; color: var(--text-muted); margin-bottom: 8px; }
        .number-value { font-size: 26px; font-weight: 800; font-family: monospace; color: var(--primary); word-break: break-all; margin-bottom: 14px; cursor: pointer; }
        .region-badge { display: inline-block; background: #e0f0ff; color: var(--primary); padding: 4px 12px; border-radius: 30px; font-size: 12px; font-weight: 600; }
        /* OTP CARD */
        .otp-card { background: linear-gradient(135deg, #10b981, #059669); border-radius: 20px; padding: 20px; margin: 12px 16px; color: white; animation: slideIn 0.3s ease; box-shadow: 0 6px 20px rgba(16,185,129,0.3); }
        @keyframes slideIn { from { opacity: 0; transform: translateY(16px); } to { opacity: 1; transform: translateY(0); } }
        .otp-code { font-size: 48px; font-weight: 900; font-family: monospace; letter-spacing: 6px; text-align: center; margin: 12px 0; cursor: pointer; }
        .otp-timer { text-align: center; font-size: 12px; opacity: 0.85; }
        .otp-message { font-size: 11px; opacity: 0.8; text-align: center; margin-top: 6px; word-break: break-all; }
        /* REGION LIST */
        .region-list { padding: 4px 16px; }
        .region-item { background: var(--surface); border-radius: 16px; padding: 14px 16px; margin-bottom: 8px; display: flex; justify-content: space-between; align-items: center; border: 1px solid var(--border); cursor: pointer; transition: all 0.15s; box-shadow: var(--shadow); }
        .region-item:active { background: #e8f4ff; border-color: var(--primary); transform: scale(0.99); }
        .region-name { font-weight: 600; color: var(--text); font-size: 15px; }
        .region-sub { font-size: 11px; color: var(--text-muted); margin-top: 2px; }
        .region-count { background: #e0f0ff; color: var(--primary); padding: 4px 12px; border-radius: 30px; font-size: 12px; font-weight: 700; }
        .region-count.low { background: #fff3cd; color: #d97706; }
        .region-count.paused { background: #fde8e8; color: var(--danger); }
        /* FILTER ROW */
        .filter-row { display: flex; gap: 10px; padding: 10px 16px; background: var(--surface); margin: 6px 16px; border-radius: 50px; border: 1px solid var(--border); box-shadow: var(--shadow); }
        .filter-input { flex: 1; border: none; outline: none; font-size: 14px; background: transparent; color: var(--text); }
        .filter-input::placeholder { color: var(--text-light); }
        .filter-btn { background: var(--primary); color: white; border: none; padding: 8px 18px; border-radius: 40px; font-size: 13px; font-weight: 600; cursor: pointer; transition: all 0.15s; white-space: nowrap; }
        .filter-btn:active { background: var(--primary-dark); transform: scale(0.97); }
        .filter-btn.secondary { background: var(--surface2); color: var(--text-muted); border: 1px solid var(--border); }
        /* SAVED / HISTORY ITEMS */
        .list-item { background: var(--surface); border-radius: 16px; padding: 14px 16px; margin-bottom: 8px; display: flex; justify-content: space-between; align-items: center; border: 1px solid var(--border); box-shadow: var(--shadow); }
        .list-item.column { flex-direction: column; align-items: stretch; gap: 10px; }
        .item-number { font-family: monospace; font-weight: 700; color: var(--primary); font-size: 15px; }
        .item-sub { font-size: 11px; color: var(--text-muted); margin-top: 2px; }
        .timer-badge { padding: 4px 10px; border-radius: 30px; font-size: 11px; font-weight: 700; white-space: nowrap; }
        .badge-green { background: #dcfce7; color: #16a34a; }
        .badge-yellow { background: #fef9c3; color: #a16207; }
        .badge-red { background: #fde8e8; color: var(--danger); }
        .badge-blue { background: #e0f0ff; color: var(--primary); }
        .badge-gray { background: var(--surface2); color: var(--text-muted); }
        /* HISTORY */
        .history-otp { font-size: 28px; font-weight: 800; font-family: monospace; color: var(--success); margin: 6px 0; cursor: pointer; }
        /* BOTTOM NAV */
        .bottom-nav { position: fixed; bottom: 0; left: 0; right: 0; background: var(--surface); border-top: 1px solid var(--border); display: flex; justify-content: space-around; padding: 8px 8px 20px; z-index: 100; box-shadow: 0 -2px 12px rgba(0,0,0,0.06); }
        .nav-item { display: flex; flex-direction: column; align-items: center; gap: 3px; cursor: pointer; padding: 6px 14px; border-radius: 40px; transition: all 0.15s; }
        .nav-item:active { background: #e8f4ff; }
        .nav-icon { font-size: 22px; }
        .nav-label { font-size: 10px; font-weight: 600; color: var(--text-muted); }
        .nav-item.active .nav-label { color: var(--primary); }
        .nav-item.active .nav-icon { filter: none; }
        /* PAGES */
        .page { display: none; padding-bottom: 20px; }
        .page.active { display: block; }
        /* SECTION TITLE */
        .section-title { font-size: 11px; font-weight: 700; color: var(--text-muted); padding: 16px 16px 8px; letter-spacing: 1px; text-transform: uppercase; }
        /* TOAST */
        .toast { position: fixed; bottom: 90px; left: 50%; transform: translateX(-50%); background: #1e293b; color: white; padding: 10px 22px; border-radius: 50px; font-size: 13px; z-index: 2000; max-width: 88%; text-align: center; animation: fadeInOut 2.2s ease forwards; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
        @keyframes fadeInOut { 0% { opacity: 0; transform: translateX(-50%) translateY(16px); } 12% { opacity: 1; transform: translateX(-50%) translateY(0); } 80% { opacity: 1; } 100% { opacity: 0; transform: translateX(-50%) translateY(-10px); } }
        /* LOADING */
        .loading { text-align: center; padding: 36px 16px; color: var(--text-muted); font-size: 14px; }
        .spinner { width: 36px; height: 36px; border: 3px solid #e0f0ff; border-top-color: var(--primary); border-radius: 50%; animation: spin 0.8s linear infinite; margin: 0 auto 10px; }
        @keyframes spin { to { transform: rotate(360deg); } }
        /* MODAL */
        .modal { display: none; position: fixed; inset: 0; background: rgba(0,0,0,0.4); backdrop-filter: blur(4px); z-index: 1000; align-items: flex-end; justify-content: center; }
        .modal.show { display: flex; }
        .modal-content { background: var(--surface); border-radius: 28px 28px 0 0; max-height: 90vh; overflow-y: auto; width: 100%; max-width: 540px; padding-bottom: 24px; }
        .modal-header { padding: 18px 20px 14px; font-weight: 700; font-size: 17px; color: var(--text); border-bottom: 1px solid var(--border); display: flex; justify-content: space-between; align-items: center; }
        .modal-close { font-size: 22px; cursor: pointer; color: var(--text-muted); background: none; border: none; }
        /* AUTH */
        .auth-container { min-height: 100vh; display: flex; align-items: center; justify-content: center; padding: 20px; background: linear-gradient(135deg, #e8f4ff 0%, #f0f4f8 100%); }
        .auth-card { background: var(--surface); border-radius: 32px; padding: 36px 28px; width: 100%; max-width: 360px; box-shadow: 0 20px 60px rgba(10,132,255,0.15); border: 1px solid var(--border); }
        .auth-logo { text-align: center; font-size: 52px; margin-bottom: 8px; }
        .auth-title { text-align: center; font-size: 22px; font-weight: 800; color: var(--primary); margin-bottom: 24px; }
        .auth-tab-row { display: flex; gap: 8px; margin-bottom: 20px; background: var(--surface2); padding: 4px; border-radius: 50px; }
        .auth-tab { flex: 1; padding: 10px; border-radius: 50px; border: none; font-size: 14px; font-weight: 600; cursor: pointer; background: transparent; color: var(--text-muted); transition: all 0.2s; }
        .auth-tab.active { background: var(--primary); color: white; box-shadow: 0 2px 8px rgba(10,132,255,0.3); }
        .auth-input { width: 100%; padding: 14px 16px; border: 1.5px solid var(--border); border-radius: 14px; font-size: 15px; margin-bottom: 12px; outline: none; background: var(--surface); color: var(--text); transition: border 0.2s; }
        .auth-input:focus { border-color: var(--primary); }
        .auth-btn { width: 100%; background: var(--primary); color: white; border: none; padding: 15px; border-radius: 14px; font-size: 16px; font-weight: 700; cursor: pointer; transition: all 0.2s; margin-top: 4px; }
        .auth-btn:active { background: var(--primary-dark); transform: scale(0.98); }
        .error-msg { color: var(--danger); font-size: 12px; margin-top: 8px; text-align: center; }
        /* ADMIN */
        .admin-badge { background: linear-gradient(135deg, #f59e0b, #d97706); color: white; padding: 3px 10px; border-radius: 20px; font-size: 10px; font-weight: 700; }
        .admin-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px; padding: 8px 16px; }
        .admin-card { background: var(--surface); border-radius: 16px; padding: 14px 10px; cursor: pointer; border: 1px solid var(--border); text-align: center; transition: all 0.15s; box-shadow: var(--shadow); }
        .admin-card:active { background: #e8f4ff; border-color: var(--primary); transform: scale(0.97); }
        .admin-icon { font-size: 26px; margin-bottom: 6px; }
        .admin-label { font-size: 11px; font-weight: 600; color: var(--text); }
        /* BUTTONS */
        .btn { padding: 8px 16px; border-radius: 30px; font-size: 12px; font-weight: 600; cursor: pointer; border: none; transition: all 0.15s; display: inline-flex; align-items: center; gap: 4px; }
        .btn:active { transform: scale(0.96); }
        .btn-primary { background: var(--primary); color: white; }
        .btn-danger { background: var(--danger); color: white; }
        .btn-success { background: var(--success); color: white; }
        .btn-secondary { background: var(--surface2); color: var(--text); border: 1px solid var(--border); }
        .btn-sm { padding: 6px 12px; font-size: 11px; }
        /* FORM GROUP */
        .fg { margin-bottom: 16px; }
        .fg label { display: block; font-size: 11px; font-weight: 700; color: var(--text-muted); margin-bottom: 6px; text-transform: uppercase; letter-spacing: 0.5px; }
        .fg input, .fg select, .fg textarea { width: 100%; padding: 12px 14px; border: 1.5px solid var(--border); border-radius: 12px; background: var(--surface); color: var(--text); outline: none; font-size: 14px; transition: border 0.2s; }
        .fg input:focus, .fg select:focus, .fg textarea:focus { border-color: var(--primary); }
        .fg textarea { resize: vertical; min-height: 70px; }
        .brow { display: flex; gap: 10px; justify-content: flex-end; margin-top: 16px; }
        /* READY CARD */
        .ready-pool-card { background: var(--surface); border-radius: 20px; margin: 0 16px 12px; padding: 18px; border: 2px solid var(--success); box-shadow: 0 4px 16px rgba(16,185,129,0.12); }
        .ready-pool-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 10px; }
        .ready-pool-name { font-weight: 700; color: var(--success); font-size: 14px; }
        .ready-queue { font-size: 11px; color: var(--text-muted); }
        .ready-number { font-family: monospace; font-size: 22px; font-weight: 800; color: var(--text); margin: 8px 0; cursor: pointer; }
        /* CHIP */
        .chip { display: inline-flex; align-items: center; gap: 4px; background: var(--surface2); border: 1px solid var(--border); border-radius: 30px; padding: 4px 10px; font-size: 11px; font-weight: 600; color: var(--text-muted); }
        /* SETTINGS TOGGLE */
        .toggle-row { display: flex; justify-content: space-between; align-items: center; padding: 12px 0; border-bottom: 1px solid var(--border); }
        .toggle-row:last-child { border-bottom: none; }
        .toggle-label { font-size: 14px; font-weight: 600; color: var(--text); }
        .toggle-desc { font-size: 11px; color: var(--text-muted); margin-top: 2px; }
        /* DIVIDER */
        .divider { height: 1px; background: var(--border); margin: 12px 16px; }
        /* FEEDBACK GRID */
        .feedback-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; padding: 16px 20px; }
        .feedback-btn { background: var(--surface2); border: 1.5px solid var(--border); padding: 14px 10px; border-radius: 14px; font-size: 13px; font-weight: 600; cursor: pointer; color: var(--text); transition: all 0.15s; text-align: center; }
        .feedback-btn:active { transform: scale(0.96); }
        .feedback-btn.good { background: #dcfce7; border-color: #86efac; color: #16a34a; }
        .feedback-btn.bad { background: #fde8e8; border-color: #fca5a5; color: var(--danger); }
        /* POOL ACCESS */
        .access-user { display: flex; justify-content: space-between; align-items: center; padding: 10px 0; border-bottom: 1px solid var(--border); }
        .hidden { display: none; }
    </style>
</head>
<body>

<!-- AUTH SCREEN -->
<div id="authContainer" style="display:flex;">
    <div class="auth-container">
        <div class="auth-card">
            <div class="auth-logo">⚡</div>
            <div class="auth-title">NEON GRID</div>
            <div class="auth-tab-row">
                <button class="auth-tab active" id="tabLogin" onclick="switchTab('login')">Login</button>
                <button class="auth-tab" id="tabRegister" onclick="switchTab('register')">Register</button>
            </div>
            <div id="loginForm">
                <input type="text" id="loginUsername" class="auth-input" placeholder="Username" autocomplete="username">
                <input type="password" id="loginPassword" class="auth-input" placeholder="Password" autocomplete="current-password">
                <button class="auth-btn" onclick="doLogin()">Login</button>
                <div id="loginError" class="error-msg"></div>
            </div>
            <div id="registerForm" style="display:none;">
                <input type="text" id="regUsername" class="auth-input" placeholder="Username" autocomplete="username">
                <input type="password" id="regPassword" class="auth-input" placeholder="Password (min 6 chars)" autocomplete="new-password">
                <button class="auth-btn" onclick="doRegister()">Create Account</button>
                <div id="regError" class="error-msg"></div>
            </div>
        </div>
    </div>
</div>

<!-- MAIN APP -->
<div id="appContainer" style="display:none;">
    <div class="status-bar">
        <span id="currentTime">--:--</span>
        <span style="font-weight:700;">⚡ NEON GRID</span>
        <span id="wsStatus">●</span>
    </div>
    <div class="header">
        <div class="user-info">
            <div>
                <div class="user-name" id="userName">User</div>
                <div class="user-id" id="userId">ID: --</div>
            </div>
            <div style="display:flex;align-items:center;gap:8px;">
                <div id="adminBadge" style="display:none;"><span class="admin-badge">ADMIN</span></div>
                <button class="btn btn-secondary btn-sm" onclick="doLogout()">Logout</button>
            </div>
        </div>
    </div>

    <!-- HOME PAGE -->
    <div id="homePage" class="page active">
        <div class="section-title">QUICK ACTIONS</div>
        <div style="padding: 0 16px; display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-bottom: 12px;">
            <div class="card" style="cursor:pointer;text-align:center;padding:16px;" onclick="navigateTo('numbers')">
                <div style="font-size:32px;margin-bottom:6px;">📱</div>
                <div style="font-weight:700;color:var(--primary);font-size:14px;">Get Number</div>
                <div style="font-size:11px;color:var(--text-muted);margin-top:2px;">Assign & monitor</div>
            </div>
            <div class="card" style="cursor:pointer;text-align:center;padding:16px;" onclick="navigateTo('saved')">
                <div style="font-size:32px;margin-bottom:6px;">💾</div>
                <div style="font-weight:700;color:var(--primary);font-size:14px;">Saved Numbers</div>
                <div style="font-size:11px;color:var(--text-muted);margin-top:2px;">Timer pools & ready</div>
            </div>
            <div class="card" style="cursor:pointer;text-align:center;padding:16px;" onclick="navigateTo('history')">
                <div style="font-size:32px;margin-bottom:6px;">📜</div>
                <div style="font-weight:700;color:var(--primary);font-size:14px;">OTP History</div>
                <div style="font-size:11px;color:var(--text-muted);margin-top:2px;">All received codes</div>
            </div>
            <div class="card" style="cursor:pointer;text-align:center;padding:16px;" onclick="doSearchOTP()">
                <div style="font-size:32px;margin-bottom:6px;">🔍</div>
                <div style="font-weight:700;color:var(--primary);font-size:14px;">Search OTP</div>
                <div style="font-size:11px;color:var(--text-muted);margin-top:2px;">Look up by number</div>
            </div>
        </div>
        <div class="section-title">CURRENT STATUS</div>
        <div class="card" id="homeStatusCard">
            <div class="number-label">ACTIVE NUMBER</div>
            <div id="homeNumber" style="font-size:20px;font-weight:800;font-family:monospace;color:var(--primary);margin:4px 0 8px;">—</div>
            <div id="homeRegion" style="font-size:12px;color:var(--text-muted);">No region selected</div>
        </div>
        <div id="homeOtpDisplay"></div>
    </div>

    <!-- NUMBERS PAGE -->
    <div id="numbersPage" class="page">
        <div class="number-card" id="currentNumberCard">
            <div class="number-label">YOUR ACTIVE NUMBER</div>
            <div class="number-value" id="currentNumber" onclick="copyNumber()" title="Tap to copy">—</div>
            <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px;">
                <span class="region-badge" id="currentRegion">No region selected</span>
                <div style="display:flex;gap:8px;">
                    <button class="btn btn-primary btn-sm" onclick="changeNumber()">🔄 Change</button>
                    <button class="btn btn-secondary btn-sm" onclick="copyNumber()">📋 Copy</button>
                    <button class="btn btn-secondary btn-sm" onclick="doSearchOTP()">🔍 OTP</button>
                </div>
            </div>
        </div>
        <div id="otpDisplay"></div>
        <div class="section-title">SELECT REGION</div>
        <div class="filter-row">
            <input type="text" id="prefixFilter" class="filter-input" placeholder="Filter by prefix (e.g. 8101)">
            <button class="filter-btn" onclick="applyFilter()">Filter</button>
            <button class="filter-btn secondary" onclick="clearFilter()">Clear</button>
        </div>
        <div id="regionList" class="region-list">
            <div class="loading"><div class="spinner"></div>Loading regions...</div>
        </div>
    </div>

    <!-- SAVED PAGE -->
    <div id="savedPage" class="page">
        <!-- READY NUMBERS — top priority -->
        <div id="readySectionWrapper" style="display:none;">
            <div class="section-title" style="color:var(--success);">✅ READY NUMBERS</div>
            <div id="readyList"></div>
        </div>

        <!-- SAVE FORM -->
        <div class="section-title">💾 SAVE NUMBERS</div>
        <div style="padding:0 16px 12px;">
            <div class="fg"><label>Phone Numbers (one per line)</label><textarea id="savedNumbersInput" placeholder="+2348012345678&#10;+2348098765432" rows="3"></textarea></div>
            <div class="fg">
                <label>Timer</label>
                <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;">
                    <input type="text" id="timerInput" placeholder="e.g. 30m, 2h, 1d, 2s" value="30m" style="flex:1;min-width:120px;padding:12px 14px;border:1.5px solid var(--border);border-radius:12px;outline:none;font-size:14px;">
                    <button class="filter-btn secondary" style="padding:12px 14px;" onclick="setTimer('30m')">30m</button>
                    <button class="filter-btn secondary" style="padding:12px 14px;" onclick="setTimer('2h')">2h</button>
                    <button class="filter-btn secondary" style="padding:12px 14px;" onclick="setTimer('1d')">1d</button>
                    <button class="filter-btn secondary" style="padding:12px 14px;" onclick="setTimer('2s')">2s</button>
                </div>
            </div>
            <div class="fg"><label>Pool Label</label><input type="text" id="poolNameInput" placeholder="e.g. UK WhatsApp, Nigeria IG"></div>
            <button class="filter-btn" style="width:100%;padding:14px;" onclick="saveNumbers()">💾 Save Numbers</button>
        </div>

        <!-- PENDING POOLS -->
        <div class="section-title" id="pendingSection" style="display:none;">⏳ PENDING POOLS</div>
        <div id="savedList" style="padding:0 16px;"></div>
    </div>

    <!-- HISTORY PAGE -->
    <div id="historyPage" class="page">
        <div class="section-title">📜 OTP HISTORY</div>
        <div id="historyList" style="padding:0 16px;"></div>
    </div>

    <!-- ADMIN PAGE -->
    <div id="adminPage" class="page hidden">
        <div class="section-title">🛠️ ADMIN PANEL</div>
        <div class="admin-grid">
            <div class="admin-card" onclick="adminSection('stats')"><div class="admin-icon">📊</div><div class="admin-label">Stats</div></div>
            <div class="admin-card" onclick="openPoolModal()"><div class="admin-icon">➕</div><div class="admin-label">New Pool</div></div>
            <div class="admin-card" onclick="openUploadModal()"><div class="admin-icon">📁</div><div class="admin-label">Upload</div></div>
            <div class="admin-card" onclick="adminSection('users')"><div class="admin-icon">👥</div><div class="admin-label">Users</div></div>
            <div class="admin-card" onclick="adminSection('pools')"><div class="admin-icon">🌍</div><div class="admin-label">Pools</div></div>
            <div class="admin-card" onclick="adminSection('bad')"><div class="admin-icon">🚫</div><div class="admin-label">Bad Nums</div></div>
            <div class="admin-card" onclick="adminSection('settings')"><div class="admin-icon">⚙️</div><div class="admin-label">Settings</div></div>
            <div class="admin-card" onclick="showBroadcast()"><div class="admin-icon">📢</div><div class="admin-label">Broadcast</div></div>
            <div class="admin-card" onclick="adminSection('access')"><div class="admin-icon">🔑</div><div class="admin-label">Pool Access</div></div>
        </div>
        <div id="adminContentArea" style="padding:0 16px;margin-top:8px;"></div>
    </div>

    <!-- BOTTOM NAV -->
    <div class="bottom-nav">
        <div class="nav-item active" data-page="home" onclick="navigateTo('home')"><div class="nav-icon">🏠</div><div class="nav-label">Home</div></div>
        <div class="nav-item" data-page="numbers" onclick="navigateTo('numbers')"><div class="nav-icon">📱</div><div class="nav-label">Numbers</div></div>
        <div class="nav-item" data-page="saved" onclick="navigateTo('saved')"><div class="nav-icon">💾</div><div class="nav-label">Saved</div></div>
        <div class="nav-item" data-page="history" onclick="navigateTo('history')"><div class="nav-icon">📜</div><div class="nav-label">History</div></div>
        <div class="nav-item hidden" id="adminNavItem" data-page="admin" onclick="navigateTo('admin')"><div class="nav-icon">⚙️</div><div class="nav-label">Admin</div></div>
    </div>
</div>

<!-- FEEDBACK MODAL -->
<div id="feedbackModal" class="modal">
    <div class="modal-content">
        <div class="modal-header">
            Rate Your Number
            <button class="modal-close" onclick="closeModal('feedbackModal')">✕</button>
        </div>
        <div id="feedbackNumber" style="font-family:monospace;font-size:18px;font-weight:700;text-align:center;padding:14px 20px 0;color:var(--primary);"></div>
        <div class="feedback-grid">
            <button class="feedback-btn good" onclick="submitFeedback('worked')">✅ Worked</button>
            <button class="feedback-btn bad" onclick="submitFeedback('bad')">❌ Not Available</button>
            <button class="feedback-btn" onclick="submitFeedback('email')">📧 Email Only</button>
            <button class="feedback-btn" onclick="submitFeedback('other_devices')">📱 Other Devices</button>
            <button class="feedback-btn" onclick="submitFeedback('try_later')">⏳ Try Later</button>
            <button class="feedback-btn" onclick="showOtherFeedback()">📝 Other Issue</button>
        </div>
        <div id="otherFeedbackDiv" style="display:none;padding:0 20px 16px;">
            <div class="fg"><label>Describe the issue</label><textarea id="otherFeedbackText" placeholder="What went wrong?"></textarea></div>
            <button class="btn btn-primary" style="width:100%;" onclick="submitFeedback('other')">Submit Feedback</button>
        </div>
    </div>
</div>

<!-- POOL MODAL -->
<div id="poolModal" class="modal">
    <div class="modal-content">
        <div class="modal-header">
            <span id="poolModalTitle">Create Pool</span>
            <button class="modal-close" onclick="closeModal('poolModal')">✕</button>
        </div>
        <div style="padding:16px 20px;">
            <div class="fg"><label>Pool Name *</label><input type="text" id="pmName" placeholder="e.g. Nigeria WS"></div>
            <div class="fg"><label>Country Code *</label><input type="text" id="pmCode" placeholder="e.g. 234"></div>
            <div class="fg"><label>OTP Group ID</label><input type="text" id="pmGroupId" placeholder="-1001234567890"></div>
            <div class="fg"><label>OTP Link</label><input type="text" id="pmOtpLink" placeholder="https://t.me/your_channel"></div>
            <div class="fg"><label>Match Format *</label><input type="text" id="pmMatchFmt" value="5+4" placeholder="e.g. 5+4"></div>
            <div class="fg"><label>Telegram Match Format</label><input type="text" id="pmTgMatchFmt" placeholder="Leave blank = same as above"></div>
            <div class="fg"><label>Monitoring Mode</label>
                <select id="pmUsesPlatform">
                    <option value="0">0 — Telegram Only 📱</option>
                    <option value="1">1 — Platform Only 🖥️</option>
                    <option value="2">2 — Both 📱🖥️</option>
                </select>
            </div>
            <div class="fg"><label>Trick Text / Guide</label><textarea id="pmTrickText" placeholder="Usage tips shown to users..."></textarea></div>
            <div style="display:flex;gap:20px;margin-bottom:12px;font-size:14px;">
                <label style="display:flex;align-items:center;gap:6px;"><input type="checkbox" id="pmAdminOnly"> Admin Only 🔒</label>
                <label style="display:flex;align-items:center;gap:6px;"><input type="checkbox" id="pmPaused" onchange="document.getElementById('pmPauseReasonRow').style.display=this.checked?'block':'none'"> Paused ⏸</label>
            </div>
            <div id="pmPauseReasonRow" style="display:none;" class="fg"><label>Pause Reason</label><input type="text" id="pmPauseReason" placeholder="Why is this pool paused?"></div>
            <div class="brow">
                <button class="btn btn-secondary" onclick="closeModal('poolModal')">Cancel</button>
                <button class="btn btn-primary" onclick="savePool()">Save Pool</button>
            </div>
        </div>
    </div>
</div>

<!-- UPLOAD MODAL -->
<div id="uploadModal" class="modal">
    <div class="modal-content">
        <div class="modal-header">
            Upload Numbers
            <button class="modal-close" onclick="closeModal('uploadModal')">✕</button>
        </div>
        <div style="padding:16px 20px;">
            <div class="fg"><label>Select Pool</label><select id="uploadPoolSelect"></select></div>
            <div class="fg"><label>File (.txt or .csv)</label><input type="file" id="uploadFile" accept=".txt,.csv" style="padding:10px;border-radius:12px;border:1.5px solid var(--border);width:100%;"></div>
            <div class="brow">
                <button class="btn btn-secondary" onclick="closeModal('uploadModal')">Cancel</button>
                <button class="btn btn-primary" onclick="uploadNumbers()">Upload</button>
            </div>
            <div id="uploadResult" style="margin-top:12px;"></div>
        </div>
    </div>
</div>

<!-- CHANGE POOL MODAL (for ready numbers) -->
<div id="changePoolModal" class="modal">
    <div class="modal-content">
        <div class="modal-header">
            🌐 Switch Pool
            <button class="modal-close" onclick="closeModal('changePoolModal')">✕</button>
        </div>
        <div style="padding:16px 20px;">
            <div style="font-size:12px;color:var(--text-muted);margin-bottom:4px;">CURRENT NUMBER</div>
            <div id="cpCurrentNum" style="font-family:monospace;font-size:18px;font-weight:700;color:var(--primary);margin-bottom:16px;"></div>
            <div class="fg"><label>Switch to Pool</label><select id="cpPoolSelect"></select></div>
            <div style="font-size:11px;color:var(--text-muted);margin-bottom:12px;">You\'ll get the next available number from the selected pool.</div>
            <div class="brow">
                <button class="btn btn-secondary" onclick="closeModal('changePoolModal')">Cancel</button>
                <button class="btn btn-primary" onclick="submitSwitchPool()">Switch</button>
            </div>
        </div>
    </div>
</div>

<!-- BROADCAST MODAL -->
<div id="broadcastModal" class="modal">
    <div class="modal-content">
        <div class="modal-header">
            📢 Broadcast
            <button class="modal-close" onclick="closeModal('broadcastModal')">✕</button>
        </div>
        <div style="padding:16px 20px;">
            <div class="fg"><label>Message</label><textarea id="broadcastMsg" rows="4" placeholder="Message to send to all users..."></textarea></div>
            <div class="brow">
                <button class="btn btn-secondary" onclick="closeModal('broadcastModal')">Cancel</button>
                <button class="btn btn-primary" onclick="sendBroadcast()">Send to All</button>
            </div>
        </div>
    </div>
</div>

<!-- SEARCH OTP MODAL -->
<div id="searchOtpModal" class="modal">
    <div class="modal-content">
        <div class="modal-header">
            🔍 Search OTP
            <button class="modal-close" onclick="closeModal('searchOtpModal')">✕</button>
        </div>
        <div style="padding:16px 20px;">
            <div class="fg"><label>Phone Number</label><input type="text" id="searchOtpNumber" placeholder="+2348012345678"></div>
            <div style="font-size:12px;color:var(--text-muted);margin-bottom:12px;">Enter any number previously assigned to you. The bot will scan for an OTP and deliver it.</div>
            <div class="brow">
                <button class="btn btn-secondary" onclick="closeModal('searchOtpModal')">Cancel</button>
                <button class="btn btn-primary" onclick="submitSearchOTP()">Search</button>
            </div>
        </div>
    </div>
</div>

<!-- POOL ACCESS MODAL -->
<div id="poolAccessModal" class="modal">
    <div class="modal-content">
        <div class="modal-header">
            🔑 Pool Access: <span id="paPoolName"></span>
            <button class="modal-close" onclick="closeModal('poolAccessModal')">✕</button>
        </div>
        <div style="padding:16px 20px;">
            <div style="font-size:12px;color:var(--text-muted);margin-bottom:12px;" id="paRestrictedNote"></div>
            <div id="paUserList"></div>
            <div class="fg" style="margin-top:14px;"><label>Add User by ID</label>
                <div style="display:flex;gap:8px;">
                    <input type="number" id="paNewUserId" placeholder="User ID" style="flex:1;padding:12px 14px;border:1.5px solid var(--border);border-radius:12px;outline:none;font-size:14px;">
                    <button class="btn btn-primary" onclick="grantAccess()">Grant</button>
                </div>
            </div>
        </div>
    </div>
</div>

<script>
const API = window.location.origin;
let currentUser = null;
let currentAssignment = null;
let currentPoolId = null;
let ws = null;
let wsRetries = 0;
let allRegions = [];
let savedOtps = {};
let currentFilter = '';
let cpSavedId = null;
let paCurrentPoolId = null;
let editingPoolId = null;

// ═══ TIME ═══
function tick() { document.getElementById('currentTime').textContent = new Date().toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'}); }
setInterval(tick, 1000); tick();

// ═══ UTILS ═══
function showToast(msg, dur=2200) {
    document.querySelectorAll('.toast').forEach(t=>t.remove());
    const t = document.createElement('div'); t.className='toast'; t.textContent=msg;
    document.body.appendChild(t); setTimeout(()=>t.remove(), dur);
}
function esc(s) { if(!s) return ''; return s.replace(/[&<>"]/g, m=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[m])); }
function fmt(n) { if(!n||n==='—') return n; return n.startsWith('+')?n:'+'+n; }
function closeModal(id) { document.getElementById(id).classList.remove('show'); }
function openModal(id) { document.getElementById(id).classList.add('show'); }
function setTimer(v) { document.getElementById('timerInput').value=v; }
// Token helpers - store in both cookie and localStorage for Railway HTTPS compatibility
function getToken() {
    // Try localStorage first, then cookie
    const ls = localStorage.getItem('ngn_token');
    if(ls) return ls;
    const m = document.cookie.match(/(?:^|; )token=([^;]*)/);
    return m ? decodeURIComponent(m[1]) : null;
}
function setToken(t) {
    localStorage.setItem('ngn_token', t);
    document.cookie = `token=${encodeURIComponent(t)};path=/;max-age=${86400*30};samesite=lax`;
}
function clearToken() {
    localStorage.removeItem('ngn_token');
    document.cookie = 'token=;path=/;max-age=0';
}

async function api(path, opts={}) {
    const token = getToken();
    const headers = {...(opts.headers||{})};
    if(token) headers['X-Token'] = token;
    const r = await fetch(API+path, {credentials:'include', ...opts, headers});
    const data = await r.json().catch(()=>({}));
    if(!r.ok) throw new Error(data.detail || `HTTP ${r.status}`);
    return data;
}

// ═══ AUTH ═══
function switchTab(tab) {
    document.getElementById('loginForm').style.display = tab==='login'?'block':'none';
    document.getElementById('registerForm').style.display = tab==='register'?'block':'none';
    document.getElementById('tabLogin').className = 'auth-tab'+(tab==='login'?' active':'');
    document.getElementById('tabRegister').className = 'auth-tab'+(tab==='register'?' active':'');
}
async function doLogin() {
    const u = document.getElementById('loginUsername').value.trim();
    const p = document.getElementById('loginPassword').value;
    const errEl = document.getElementById('loginError');
    errEl.textContent='';
    if(!u){errEl.textContent='Enter username';return;}
    if(!p){errEl.textContent='Enter password';return;}
    try {
        const data = await api('/api/auth/login', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({username:u,password:p})});
        if(data.token) setToken(data.token);
        await checkAuth();
    } catch(e) { errEl.textContent=e.message; }
}
async function doRegister() {
    const u = document.getElementById('regUsername').value.trim();
    const p = document.getElementById('regPassword').value;
    const errEl = document.getElementById('regError');
    errEl.textContent='';
    if(!u){errEl.textContent='Enter username';return;}
    if(p.length<6){errEl.textContent='Password must be at least 6 characters.';return;}
    try {
        const data = await api('/api/auth/register', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({username:u,password:p})});
        if(data.token) setToken(data.token);
        if(data.approved) { await checkAuth(); }
        else { errEl.textContent='Account created — awaiting admin approval.'; }
    } catch(e) { errEl.textContent=e.message; }
}
async function doLogout() {
    await fetch(API+'/api/auth/logout', {method:'POST', credentials:'include', headers:{'X-Token':getToken()||''}}).catch(()=>{});
    clearToken();
    currentUser=null; currentAssignment=null;
    document.getElementById('appContainer').style.display='none';
    document.getElementById('authContainer').style.display='flex';
    if(ws){ws.close();ws=null;}
}
async function checkAuth() {
    try {
        currentUser = await api('/api/auth/me');
        document.getElementById('authContainer').style.display='none';
        document.getElementById('appContainer').style.display='block';
        document.getElementById('userName').textContent = currentUser.username;
        document.getElementById('userId').textContent = 'ID: '+currentUser.id;
        if(currentUser.is_admin) {
            document.getElementById('adminBadge').style.display='inline-block';
            document.getElementById('adminNavItem').classList.remove('hidden');
            document.getElementById('adminPage').classList.remove('hidden');
        }
        connectWS();
        loadRegions();
        loadCurrentAssignment();
        loadSavedNumbers();
        loadHistory();
        return true;
    } catch(e) {
        document.getElementById('authContainer').style.display='flex';
        document.getElementById('appContainer').style.display='none';
        return false;
    }
}

// ═══ WEBSOCKET ═══
function connectWS() {
    const proto = location.protocol==='https:'?'wss:':'ws:';
    const tok = getToken();
    ws = new WebSocket(`${proto}//${location.host}/ws/user/${currentUser.id}${tok?'?token='+encodeURIComponent(tok):''}`);
    ws.onopen = () => { document.getElementById('wsStatus').textContent='🟢'; wsRetries=0; };
    ws.onmessage = (e) => {
        const d = JSON.parse(e.data);
        if(d.type==='otp') displayOTP(d, false);
        else if(d.type==='saved_otp') displaySavedOTP(d);
        else if(d.type==='broadcast'||d.type==='notification') showToast('📢 '+d.message, 4000);
    };
    ws.onclose = () => {
        document.getElementById('wsStatus').textContent='🔴';
        wsRetries = Math.min(wsRetries+1, 6);
        setTimeout(connectWS, Math.min(1000*wsRetries, 30000));
    };
    ws.onerror = () => ws.close();
}

// ═══ OTP DISPLAY ═══
function displayOTP(data, isSaved=false) {
    const containerId = isSaved ? null : (document.getElementById('numbersPage').classList.contains('active') ? 'otpDisplay' : 'homeOtpDisplay');
    const html = `<div class="otp-card">
        <div style="text-align:center;font-size:11px;opacity:0.85;">🔑 OTP RECEIVED</div>
        <div class="otp-code" onclick="copyText('${esc(data.otp)}')">${esc(data.otp)}</div>
        <div class="otp-timer" id="ocd_${data.id}">Auto-delete in 30s — tap code to copy</div>
        ${data.raw_message?`<div class="otp-message">${esc(data.raw_message)}</div>`:''}
    </div>`;
    if(containerId) {
        const el = document.getElementById(containerId);
        el.innerHTML = html; el.style.display='block';
        let s=30;
        const t=setInterval(()=>{
            s--; const cd=document.getElementById('ocd_'+data.id);
            if(cd) cd.textContent=`Auto-delete in ${s}s — tap code to copy`;
            if(s<=0){clearInterval(t); el.style.display='none';}
        },1000);
    }
    loadHistory();
}
function displaySavedOTP(data) {
    savedOtps[data.number] = {otp:data.otp, raw_message:data.raw_message||''};
    showToast(`🔑 OTP for ${data.number}: ${data.otp}`, 5000);
    loadReadyNumbers();
    setTimeout(()=>{delete savedOtps[data.number]; loadReadyNumbers();}, 30000);
    loadHistory();
}
function copyText(t) { navigator.clipboard.writeText(t).catch(()=>{}); showToast('📋 Copied!'); }

// ═══ NAVIGATION ═══
function navigateTo(page) {
    document.querySelectorAll('.page').forEach(p=>p.classList.remove('active'));
    document.getElementById(page+'Page').classList.add('active');
    document.querySelectorAll('.nav-item').forEach(n=>n.classList.remove('active'));
    const navEl = document.querySelector(`.nav-item[data-page="${page}"]`);
    if(navEl) navEl.classList.add('active');
    if(page==='numbers') loadRegions();
    if(page==='saved') loadSavedNumbers();
    if(page==='history') loadHistory();
}

// ═══ REGIONS ═══
async function loadRegions() {
    try {
        allRegions = await api('/api/pools');
        renderRegions();
    } catch(e) { document.getElementById('regionList').innerHTML='<div class="loading">Failed to load regions</div>'; }
}
function renderRegions() {
    const container = document.getElementById('regionList');
    let data = allRegions;
    if(currentFilter) data = data.filter(r=>r.name.toLowerCase().includes(currentFilter.toLowerCase())||r.country_code.includes(currentFilter));
    if(!data.length){container.innerHTML='<div class="loading">No regions available</div>';return;}
    container.innerHTML = data.map(r => {
        const low = r.number_count>0 && r.number_count<50;
        const badge = r.is_paused
            ? `<span class="region-count paused">⏸ Paused</span>`
            : `<span class="region-count${low?' low':''}">${r.number_count}${low?' ⚡':''}</span>`;
        const mode = r.uses_platform===1?'🖥️':r.uses_platform===2?'📱🖥️':'📱';
        const restricted = r.is_restricted?'🔐':'';
        const adminOnly = r.is_admin_only?'🔒':'';
        return `<div class="region-item" onclick="selectRegion(${r.id})">
            <div>
                <div class="region-name">${esc(r.name)} ${adminOnly}${restricted}</div>
                <div class="region-sub">+${r.country_code} · ${mode}${r.trick_text?` · 💡 ${esc(r.trick_text)}`:''}</div>
            </div>
            ${badge}
        </div>`;
    }).join('');
}
function applyFilter() { currentFilter=document.getElementById('prefixFilter').value.trim(); renderRegions(); }
function clearFilter() { currentFilter=''; document.getElementById('prefixFilter').value=''; renderRegions(); }

async function selectRegion(poolId) {
    const region = allRegions.find(r=>r.id===poolId);
    if(region?.is_paused){showToast(`⏸ ${region.pause_reason||'Region temporarily unavailable'}`);return;}
    try {
        const data = await api('/api/pools/assign', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({pool_id:poolId})});
        currentAssignment = data; currentPoolId = data.pool_id;
        document.getElementById('currentNumber').textContent = fmt(data.number);
        document.getElementById('currentRegion').textContent = data.pool_name;
        document.getElementById('homeNumber').textContent = fmt(data.number);
        document.getElementById('homeRegion').textContent = data.pool_name;
        document.getElementById('otpDisplay').style.display='none';
        showToast(`✅ Number assigned: ${fmt(data.number)}`);
        if(data.trick_text) showToast(`💡 ${data.trick_text}`, 4000);
        loadRegions();
    } catch(e) { showToast(e.message); }
}

async function loadCurrentAssignment() {
    try {
        const data = await api('/api/pools/my-assignment');
        if(data.assignment) {
            currentAssignment = data.assignment; currentPoolId = data.assignment.pool_id;
            document.getElementById('currentNumber').textContent = fmt(currentAssignment.number);
            document.getElementById('currentRegion').textContent = currentAssignment.pool_name;
            document.getElementById('homeNumber').textContent = fmt(currentAssignment.number);
            document.getElementById('homeRegion').textContent = currentAssignment.pool_name;
        }
    } catch(e){}
}

// ═══ NUMBER ACTIONS ═══
async function changeNumber() {
    if(!currentAssignment){showToast('No active number');return;}
    document.getElementById('feedbackNumber').textContent = fmt(currentAssignment.number);
    document.getElementById('otherFeedbackDiv').style.display='none';
    document.getElementById('otherFeedbackText').value='';
    openModal('feedbackModal');
}
function copyNumber() {
    if(currentAssignment) copyText(fmt(currentAssignment.number));
    else showToast('No number to copy');
}
function showOtherFeedback() { document.getElementById('otherFeedbackDiv').style.display='block'; }

async function submitFeedback(type) {
    if(!currentAssignment) return;
    const savedAsgn = currentAssignment;
    const savedPoolId = currentPoolId;
    closeModal('feedbackModal');
    document.getElementById('currentNumber').textContent='...';
    const comment = type==='other' ? document.getElementById('otherFeedbackText').value : type;
    const markBad = type==='bad';
    try {
        await api(`/api/pools/release/${savedAsgn.assignment_id}`, {method:'POST'});
        fetch(API+'/api/reviews', {method:'POST', credentials:'include', headers:{'Content-Type':'application/json'}, body:JSON.stringify({number:savedAsgn.number, rating:markBad?1:4, comment:comment, mark_as_bad:markBad})});
        if(savedPoolId) {
            const data = await api('/api/pools/assign', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({pool_id:savedPoolId})});
            currentAssignment=data; currentPoolId=data.pool_id;
            document.getElementById('currentNumber').textContent=fmt(data.number);
            document.getElementById('currentRegion').textContent=data.pool_name;
            document.getElementById('homeNumber').textContent=fmt(data.number);
            document.getElementById('homeRegion').textContent=data.pool_name;
            document.getElementById('otpDisplay').style.display='none';
            document.getElementById('homeOtpDisplay').innerHTML='';
            showToast(`✅ New number: ${fmt(data.number)}`);
        } else {
            currentAssignment=null; currentPoolId=null;
            document.getElementById('currentNumber').textContent='—';
            document.getElementById('currentRegion').textContent='No region selected';
        }
    } catch(e) { showToast(e.message); document.getElementById('currentNumber').textContent='—'; }
    loadRegions();
}

// ═══ SEARCH OTP ═══
function doSearchOTP() {
    if(currentAssignment) document.getElementById('searchOtpNumber').value = currentAssignment.number;
    openModal('searchOtpModal');
}
async function submitSearchOTP() {
    const num = document.getElementById('searchOtpNumber').value.trim();
    if(!num){showToast('Enter a number');return;}
    closeModal('searchOtpModal');
    try {
        const data = await api('/api/otp/search?number='+encodeURIComponent(num), {method:'POST'});
        showToast('👁️ Monitoring started — OTP will arrive shortly');
    } catch(e) { showToast(e.message); }
}

// ═══ SAVED NUMBERS ═══
function parseTimer(s) {
    const m = s.match(/^(\d+)([smhd])$/i);
    if(m) { const n=parseInt(m[1]),u=m[2].toLowerCase(); if(u==='s') return Math.max(1,Math.ceil(n/60)); if(u==='h') return n*60; if(u==='d') return n*1440; }
    const n=parseInt(s); return isNaN(n)?30:n;
}
async function saveNumbers() {
    const nums = document.getElementById('savedNumbersInput').value.split('\n').map(l=>l.trim()).filter(Boolean);
    const timerMins = parseTimer(document.getElementById('timerInput').value.trim()||'30m');
    const poolName = document.getElementById('poolNameInput').value.trim()||'Default';
    if(!nums.length){showToast('Enter at least one number');return;}
    try {
        const data = await api('/api/saved', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({numbers:nums, timer_minutes:timerMins, pool_name:poolName})});
        showToast(`💾 Saved ${data.saved} numbers`);
        document.getElementById('savedNumbersInput').value='';
        loadSavedNumbers();
    } catch(e) { showToast(e.message); }
}

async function loadSavedNumbers() {
    try {
        const data = await api('/api/saved');
        const pending = data.filter(i=>!i.moved);
        const savedList = document.getElementById('savedList');
        const pendingSection = document.getElementById('pendingSection');
        if(!pending.length) { pendingSection.style.display='none'; savedList.innerHTML=''; }
        else {
            pendingSection.style.display='block';
            // Group by pool_name
            const grouped = {};
            pending.forEach(i=>{if(!grouped[i.pool_name])grouped[i.pool_name]={items:[],minSec:Infinity,status:'green'};grouped[i.pool_name].items.push(i);if(i.seconds_left<grouped[i.pool_name].minSec){grouped[i.pool_name].minSec=i.seconds_left;grouped[i.pool_name].status=i.status;}});
            savedList.innerHTML = Object.entries(grouped).map(([pname,g])=>{
                const ids=g.items.map(i=>i.id);
                const count=g.items.length;
                const minSec=g.minSec;
                const m=Math.floor(minSec/60), s=minSec%60;
                const timeStr=`${m}m ${s}s`;
                const bc=g.status==='yellow'?'badge-yellow':g.status==='red'?'badge-red':g.status==='expired'?'badge-red':'badge-green';
                return `<div class="list-item">
                    <div>
                        <div class="item-number">📦 ${esc(pname)}</div>
                        <div class="item-sub">${count} number${count!==1?'s':''} • timer running</div>
                    </div>
                    <div style="display:flex;align-items:center;gap:8px;">
                        <span class="timer-badge ${bc}">${g.status==='expired'?'Expiring':timeStr}</span>
                        <button class="btn btn-danger btn-sm" onclick="deleteSavedPool(${JSON.stringify(ids)})">🗑️</button>
                    </div>
                </div>`;
            }).join('');
        }
    } catch(e){}
    loadReadyNumbers();
}

async function deleteSavedPool(ids) {
    if(!confirm(`Delete all ${ids.length} number(s)?`)) return;
    for(const id of ids) await fetch(API+'/api/saved/'+id, {method:'DELETE', credentials:'include'}).catch(()=>{});
    loadSavedNumbers();
}

async function loadReadyNumbers() {
    try {
        const data = await api('/api/saved/ready');
        const wrapper = document.getElementById('readySectionWrapper');
        const container = document.getElementById('readyList');
        if(!data.length){wrapper.style.display='none';container.innerHTML='';return;}
        wrapper.style.display='block';
        // Group by pool_name — each pool is an independent queue
        const grouped={};
        data.forEach(i=>{if(!grouped[i.pool_name])grouped[i.pool_name]=[];grouped[i.pool_name].push(i);});
        container.innerHTML = Object.entries(grouped).map(([pname,items])=>{
            const active=items[0];
            const qCount=items.length;
            const otpInfo=savedOtps[active.number];
            const otpHtml=otpInfo?`<div class="otp-card" style="margin:8px 0 0;padding:12px 16px;">
                <div style="font-size:11px;">🔑 OTP CODE</div>
                <div class="otp-code" style="font-size:28px;cursor:pointer;" onclick="copyText('${esc(otpInfo.otp)}')">${esc(otpInfo.otp)} 📋</div>
                ${otpInfo.raw_message?`<div class="otp-message">${esc(otpInfo.raw_message)}</div>`:''}
            </div>`:'';
            return `<div class="ready-pool-card">
                <div class="ready-pool-header">
                    <span class="ready-pool-name">📦 ${esc(pname)}</span>
                    <span class="ready-queue">${qCount} in queue</span>
                </div>
                <div class="ready-number" onclick="copyText('${active.number}');showToast('📋 Copied!')" title="Tap to copy">
                    ${esc(active.number)} <span style="font-size:14px;">📋</span>
                </div>
                <div style="font-size:11px;color:var(--text-muted);margin-bottom:10px;">Slot 1 of ${qCount} — tap number to copy</div>
                <div style="display:flex;gap:8px;flex-wrap:wrap;">
                    <button class="btn btn-secondary btn-sm" onclick="doNextNumber(${active.id},'${pname.replace(/'/g,"\\'")}')">🔄 Change Number</button>
                    <button class="btn btn-secondary btn-sm" onclick="openChangePool(${active.id},'${active.number.replace(/'/g,"\\'")}','${pname.replace(/'/g,"\\'")}')">🌐 Change Pool</button>
                    <button class="btn btn-primary btn-sm" onclick="triggerSavedMonitor('${active.number.replace(/'/g,"\\'")}')">👁️ Monitor</button>
                    <button class="btn btn-danger btn-sm" onclick="deleteReady(${active.id})">🗑️</button>
                </div>
                ${otpHtml}
            </div>`;
        }).join('');
    } catch(e){}
}

async function doNextNumber(id, poolName) {
    try {
        const data = await api(`/api/saved/${id}/next-number`, {method:'POST'});
        showToast(`🔄 New number: ${data.number}`);
        await triggerSavedMonitor(data.number);
        loadReadyNumbers();
    } catch(e) { showToast(e.message); }
}

async function triggerSavedMonitor(number) {
    try {
        await api('/api/saved/trigger-monitor', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({number})});
        showToast('👁️ Monitoring started...');
    } catch(e) { /* silent */ }
}

async function openChangePool(id, number, currentPool) {
    cpSavedId=id;
    document.getElementById('cpCurrentNum').textContent=number;
    try {
        const pools = await api('/api/saved/ready-pools');
        const opts = pools.filter(p=>p.pool_name!==currentPool && p.count>0);
        if(!opts.length){showToast('No other pools with available numbers');return;}
        document.getElementById('cpPoolSelect').innerHTML = opts.map(p=>`<option value="${esc(p.pool_name)}">${esc(p.pool_name)} (${p.count} available)</option>`).join('');
        openModal('changePoolModal');
    } catch(e){showToast(e.message);}
}

async function submitSwitchPool() {
    const newPool = document.getElementById('cpPoolSelect').value;
    if(!newPool||!cpSavedId) return;
    closeModal('changePoolModal');
    try {
        const data = await api(`/api/saved/${cpSavedId}/switch-pool?new_pool_name=${encodeURIComponent(newPool)}`, {method:'POST'});
        showToast(`🌐 Switched to ${newPool}: ${data.number}`);
        await triggerSavedMonitor(data.number);
        loadReadyNumbers();
    } catch(e){showToast(e.message);}
}

async function deleteReady(id) {
    await fetch(API+'/api/saved/'+id, {method:'DELETE', credentials:'include'});
    loadReadyNumbers();
}

// ═══ HISTORY ═══
async function loadHistory() {
    try {
        const data = await api('/api/otp/my');
        const container = document.getElementById('historyList');
        if(!data.length){container.innerHTML='<div class="loading">No OTP history yet</div>';return;}
        container.innerHTML = data.map(item=>`<div class="list-item column">
            <div style="display:flex;justify-content:space-between;align-items:center;">
                <div>
                    <div class="item-number" style="font-size:13px;">${esc(item.number)}</div>
                    <div class="item-sub">${new Date(item.delivered_at).toLocaleString()}</div>
                </div>
                <button class="btn btn-secondary btn-sm" onclick="copyText('${esc(item.otp_code)}')">📋</button>
            </div>
            <div class="history-otp" onclick="copyText('${esc(item.otp_code)}')">${esc(item.otp_code)} 📋</div>
            ${item.raw_message?`<div style="font-size:11px;color:var(--text-muted);">${esc(item.raw_message)}</div>`:''}
        </div>`).join('');
    } catch(e){}
}

// ═══ ADMIN ═══
async function adminSection(section) {
    const area = document.getElementById('adminContentArea');
    area.innerHTML = '<div class="loading"><div class="spinner"></div>Loading...</div>';
    try {
        if(section==='stats') await renderAdminStats(area);
        else if(section==='users') await renderAdminUsers(area);
        else if(section==='pools') await renderAdminPools(area);
        else if(section==='bad') await renderBadNumbers(area);
        else if(section==='settings') await renderSettings(area);
        else if(section==='access') await renderPoolAccessMenu(area);
    } catch(e) { area.innerHTML=`<div class="loading" style="color:var(--danger);">Error: ${esc(e.message)}</div>`; }
}

async function renderAdminStats(area) {
    const s = await api('/api/admin/stats');
    area.innerHTML = `<div class="card">
        <div class="number-label" style="margin-bottom:12px;">SYSTEM OVERVIEW</div>
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;font-size:14px;">
            <div><div style="color:var(--text-muted);font-size:11px;">USERS</div><div style="font-size:24px;font-weight:800;color:var(--primary);">${s.total_users}</div></div>
            <div><div style="color:var(--text-muted);font-size:11px;">PENDING</div><div style="font-size:24px;font-weight:800;color:var(--warning);">${s.pending_approval}</div></div>
            <div><div style="color:var(--text-muted);font-size:11px;">POOLS</div><div style="font-size:24px;font-weight:800;color:var(--primary);">${s.total_pools}</div></div>
            <div><div style="color:var(--text-muted);font-size:11px;">NUMBERS</div><div style="font-size:24px;font-weight:800;color:var(--success);">${s.total_numbers.toLocaleString()}</div></div>
            <div><div style="color:var(--text-muted);font-size:11px;">OTPs SENT</div><div style="font-size:24px;font-weight:800;color:var(--success);">${s.total_otps}</div></div>
            <div><div style="color:var(--text-muted);font-size:11px;">BAD NUMBERS</div><div style="font-size:24px;font-weight:800;color:var(--danger);">${s.bad_numbers}</div></div>
            <div><div style="color:var(--text-muted);font-size:11px;">SAVED (PENDING)</div><div style="font-size:24px;font-weight:800;color:var(--primary);">${s.saved_numbers}</div></div>
            <div><div style="color:var(--text-muted);font-size:11px;">ONLINE</div><div style="font-size:24px;font-weight:800;color:var(--success);">${s.online_users}</div></div>
        </div>
    </div>`;
}

async function renderAdminUsers(area) {
    const users = await api('/api/admin/users');
    if(!users.length){area.innerHTML='<div class="loading">No users</div>';return;}
    area.innerHTML = users.map(u=>{
        const status = u.is_admin?'👑 Admin':u.is_blocked?'🚫 Blocked':u.is_approved?'✅ Approved':'⏳ Pending';
        const approveBtn = (!u.is_approved&&!u.is_blocked&&!u.is_admin)?`<button class="btn btn-success btn-sm" onclick="approveUser(${u.id})">Approve</button>`:'';
        const blockBtn = !u.is_admin?(u.is_blocked?`<button class="btn btn-primary btn-sm" onclick="toggleBlock(${u.id},false)">Unblock</button>`:`<button class="btn btn-danger btn-sm" onclick="toggleBlock(${u.id},true)">Block</button>`):'';
        return `<div class="list-item">
            <div><div class="item-number" style="font-size:14px;">${esc(u.username)}</div><div class="item-sub">ID: ${u.id} · ${status}</div></div>
            <div style="display:flex;gap:6px;">${approveBtn}${blockBtn}</div>
        </div>`;
    }).join('');
}

async function approveUser(id) {
    await api('/api/admin/users/'+id+'/approve', {method:'POST'});
    showToast('✅ User approved'); adminSection('users');
}
async function toggleBlock(id, block) {
    await api('/api/admin/users/'+id+'/'+(block?'block':'unblock'), {method:'POST'});
    showToast(block?'🚫 User blocked':'✅ User unblocked'); adminSection('users');
}

async function renderAdminPools(area) {
    const pools = await api('/api/pools');
    if(!pools.length){area.innerHTML='<div class="loading">No pools — create one above</div>';return;}
    area.innerHTML = pools.map(p=>`<div class="list-item column">
        <div style="display:flex;justify-content:space-between;align-items:flex-start;">
            <div>
                <div class="item-number">${esc(p.name)} (+${p.country_code}) ${p.is_admin_only?'🔒':''}</div>
                <div class="item-sub">${p.number_count.toLocaleString()} numbers · Mode: ${p.uses_platform} · Format: ${p.match_format}${p.is_paused?' · <span style="color:var(--danger);">⏸ Paused</span>':''}</div>
                ${p.trick_text?`<div style="font-size:11px;color:var(--warning);margin-top:3px;">💡 ${esc(p.trick_text)}</div>`:''}
            </div>
            <div style="display:flex;gap:4px;">
                <button class="btn btn-primary btn-sm" onclick="openEditPool(${p.id})">✏️</button>
                <button class="btn btn-danger btn-sm" onclick="confirmDeletePool(${p.id},'${esc(p.name).replace(/'/g,"\\'")}')">🗑️</button>
            </div>
        </div>
        <div style="display:flex;gap:6px;flex-wrap:wrap;">
            <button class="btn btn-secondary btn-sm" onclick="cutPool(${p.id})">✂️ Cut</button>
            <button class="btn btn-secondary btn-sm" onclick="clearPool(${p.id},'${esc(p.name).replace(/'/g,"\\'")}')">🧹 Clear</button>
            <button class="btn btn-secondary btn-sm" onclick="exportPool(${p.id})">📤 Export</button>
            <button class="btn btn-secondary btn-sm" onclick="${p.is_paused?`resumePool(${p.id})`:`pausePool(${p.id})`}">${p.is_paused?'▶ Resume':'⏸ Pause'}</button>
            <button class="btn btn-secondary btn-sm" onclick="toggleAdminOnly(${p.id})">${p.is_admin_only?'🔓 Public':'🔒 Admin Only'}</button>
            <button class="btn btn-secondary btn-sm" onclick="openPoolAccessModal(${p.id},'${esc(p.name).replace(/'/g,"\\'")}')">🔑 Access</button>
        </div>
    </div>`).join('');
}

async function cutPool(id) {
    const n=prompt('How many numbers to cut?');
    if(!n||isNaN(parseInt(n))) return;
    try {
        const d=await api('/api/admin/pools/'+id+'/cut?count='+parseInt(n), {method:'POST'});
        showToast(`✂️ Removed ${d.removed} numbers`); adminSection('pools');
    } catch(e){showToast(e.message);}
}
async function clearPool(id, name) {
    if(!confirm(`⚠️ Clear ALL numbers from "${name}"?`)) return;
    const d=await api('/api/admin/pools/'+id+'/clear', {method:'POST'});
    showToast(`🧹 Cleared ${d.deleted} numbers`); adminSection('pools');
}
function exportPool(id) { window.open(API+'/api/admin/pools/'+id+'/export', '_blank'); }
async function pausePool(id) {
    const r=prompt('Pause reason (optional):')||'';
    await api('/api/admin/pools/'+id+'/pause?reason='+encodeURIComponent(r), {method:'POST'});
    showToast('⏸ Paused'); adminSection('pools');
}
async function resumePool(id) {
    await api('/api/admin/pools/'+id+'/resume', {method:'POST'});
    showToast('▶ Resumed'); adminSection('pools');
}
async function toggleAdminOnly(id) {
    const d=await api('/api/admin/pools/'+id+'/toggle-admin-only', {method:'POST'});
    showToast(d.is_admin_only?'🔒 Admin Only':'🔓 Public'); adminSection('pools');
}
async function confirmDeletePool(id, name) {
    if(!confirm(`⚠️ Delete pool "${name}" and all its numbers?`)) return;
    await api('/api/admin/pools/'+id, {method:'DELETE'});
    showToast('Pool deleted'); adminSection('pools');
}

async function renderBadNumbers(area) {
    const bad = await api('/api/admin/bad-numbers');
    if(!bad.length){area.innerHTML='<div class="loading">No bad numbers 🎉</div>';return;}
    area.innerHTML = bad.map(b=>`<div class="list-item">
        <div><div class="item-number" style="font-size:13px;">${esc(b.number)}</div><div class="item-sub">${esc(b.reason||'')}</div></div>
        <button class="btn btn-primary btn-sm" onclick="removeBad('${b.number.replace(/'/g,"\\'")}')">Remove</button>
    </div>`).join('');
}
async function removeBad(number) {
    await api('/api/admin/bad-numbers?number='+encodeURIComponent(number), {method:'DELETE'});
    showToast('Removed'); adminSection('bad');
}

async function renderSettings(area) {
    area.innerHTML = `<div class="card">
        <div class="number-label" style="margin-bottom:14px;">BOT SETTINGS</div>
        <div class="fg"><label>Approval Mode</label>
            <select id="settApproval" onchange="saveSetting('approval',this.value==='on')">
                <option value="on">ON — New users need approval</option>
                <option value="off">OFF — All users can access freely</option>
            </select>
        </div>
        <div class="fg"><label>OTP Redirect Mode</label>
            <select id="settOtpRedirect" onchange="saveSetting('otp_redirect',null,this.value)">
                <option value="pool">Per-Pool Link</option>
                <option value="hardcoded">Hardcoded: https://t.me/earnplusz</option>
            </select>
        </div>
    </div>`;
}
async function saveSetting(type, boolVal, strVal) {
    if(type==='approval') await api('/api/admin/settings/approval?enabled='+(boolVal?'true':'false'), {method:'POST'});
    if(type==='otp_redirect') await api('/api/admin/settings/otp-redirect?mode='+strVal, {method:'POST'});
    showToast('✅ Setting saved');
}

async function renderPoolAccessMenu(area) {
    const pools = await api('/api/pools');
    if(!pools.length){area.innerHTML='<div class="loading">No pools</div>';return;}
    area.innerHTML = `<div class="card">
        <div class="number-label" style="margin-bottom:12px;">SELECT POOL TO MANAGE ACCESS</div>
        ${pools.map(p=>`<div class="list-item" style="cursor:pointer;" onclick="openPoolAccessModal(${p.id},'${esc(p.name).replace(/'/g,"\\'")}')">
            <div><div class="item-number" style="font-size:14px;">${esc(p.name)}</div><div class="item-sub">${p.is_restricted?'🔐 Restricted':'🌐 Open access'}</div></div>
            <span style="font-size:20px;">›</span>
        </div>`).join('')}
    </div>`;
}

async function openPoolAccessModal(poolId, poolName) {
    paCurrentPoolId=poolId;
    document.getElementById('paPoolName').textContent=poolName;
    document.getElementById('paNewUserId').value='';
    try {
        const users = await api('/api/admin/pools/'+poolId+'/access');
        const noteEl = document.getElementById('paRestrictedNote');
        noteEl.textContent = users.length ? `🔐 Restricted — only listed users can access this pool.` : `🌐 Open — all approved users can access this pool. Add users below to restrict it.`;
        const listEl = document.getElementById('paUserList');
        if(!users.length){listEl.innerHTML='<div style="color:var(--text-muted);font-size:13px;margin-bottom:12px;">No users added yet.</div>';return;}
        listEl.innerHTML = users.map(u=>`<div class="access-user">
            <div><div style="font-weight:600;font-size:14px;">${esc(u.username||u.first_name||'User '+u.user_id)}</div><div style="font-size:11px;color:var(--text-muted);">ID: ${u.user_id}</div></div>
            <button class="btn btn-danger btn-sm" onclick="revokeAccess(${u.user_id})">Revoke</button>
        </div>`).join('');
    } catch(e){}
    openModal('poolAccessModal');
}
async function grantAccess() {
    const uid=parseInt(document.getElementById('paNewUserId').value);
    if(!uid||!paCurrentPoolId) return;
    try {
        await api('/api/admin/pools/'+paCurrentPoolId+'/access/'+uid, {method:'POST'});
        showToast('✅ Access granted'); openPoolAccessModal(paCurrentPoolId, document.getElementById('paPoolName').textContent);
    } catch(e){showToast(e.message);}
}
async function revokeAccess(uid) {
    if(!paCurrentPoolId) return;
    await api('/api/admin/pools/'+paCurrentPoolId+'/access/'+uid, {method:'DELETE'});
    showToast('Access revoked'); openPoolAccessModal(paCurrentPoolId, document.getElementById('paPoolName').textContent);
}

function showBroadcast() { openModal('broadcastModal'); }
async function sendBroadcast() {
    const msg=document.getElementById('broadcastMsg').value.trim();
    if(!msg){showToast('Enter a message');return;}
    await api('/api/admin/broadcast?message='+encodeURIComponent(msg), {method:'POST'});
    showToast('📢 Broadcast sent!'); closeModal('broadcastModal'); document.getElementById('broadcastMsg').value='';
}

// ═══ POOL MODAL ═══
function openPoolModal(id=null) {
    editingPoolId=id;
    document.getElementById('poolModalTitle').textContent = id?'Edit Pool':'Create Pool';
    if(!id) {
        ['pmName','pmCode','pmGroupId','pmOtpLink','pmTgMatchFmt','pmTrickText','pmPauseReason'].forEach(f=>document.getElementById(f).value='');
        document.getElementById('pmMatchFmt').value='5+4';
        document.getElementById('pmUsesPlatform').value='0';
        document.getElementById('pmAdminOnly').checked=false;
        document.getElementById('pmPaused').checked=false;
        document.getElementById('pmPauseReasonRow').style.display='none';
    }
    openModal('poolModal');
}
async function openEditPool(id) {
    try {
        const pools=await api('/api/pools');
        const p=pools.find(pp=>pp.id===id);
        if(!p) return;
        editingPoolId=id;
        document.getElementById('poolModalTitle').textContent='Edit Pool: '+p.name;
        document.getElementById('pmName').value=p.name||'';
        document.getElementById('pmCode').value=p.country_code||'';
        document.getElementById('pmGroupId').value=p.otp_group_id||'';
        document.getElementById('pmOtpLink').value=p.otp_link||'';
        document.getElementById('pmMatchFmt').value=p.match_format||'5+4';
        document.getElementById('pmTgMatchFmt').value=p.telegram_match_format||'';
        document.getElementById('pmUsesPlatform').value=p.uses_platform||0;
        document.getElementById('pmTrickText').value=p.trick_text||'';
        document.getElementById('pmAdminOnly').checked=!!p.is_admin_only;
        document.getElementById('pmPaused').checked=!!p.is_paused;
        document.getElementById('pmPauseReason').value=p.pause_reason||'';
        document.getElementById('pmPauseReasonRow').style.display=p.is_paused?'block':'none';
        openModal('poolModal');
    } catch(e){showToast(e.message);}
}
async function savePool() {
    const d={
        name:document.getElementById('pmName').value.trim(),
        country_code:document.getElementById('pmCode').value.trim(),
        otp_group_id:parseInt(document.getElementById('pmGroupId').value)||null,
        otp_link:document.getElementById('pmOtpLink').value.trim(),
        match_format:document.getElementById('pmMatchFmt').value.trim(),
        telegram_match_format:document.getElementById('pmTgMatchFmt').value.trim(),
        uses_platform:parseInt(document.getElementById('pmUsesPlatform').value),
        trick_text:document.getElementById('pmTrickText').value.trim(),
        is_admin_only:document.getElementById('pmAdminOnly').checked,
        is_paused:document.getElementById('pmPaused').checked,
        pause_reason:document.getElementById('pmPauseReason').value.trim()
    };
    if(!d.name||!d.country_code){showToast('Name and country code are required');return;}
    try {
        if(editingPoolId) await api('/api/admin/pools/'+editingPoolId, {method:'PUT', headers:{'Content-Type':'application/json'}, body:JSON.stringify(d)});
        else await api('/api/admin/pools', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(d)});
        showToast(editingPoolId?'✅ Pool updated!':'✅ Pool created!');
        closeModal('poolModal'); loadRegions(); adminSection('pools');
    } catch(e){showToast(e.message);}
}

// ═══ UPLOAD MODAL ═══
async function openUploadModal() {
    const pools=await api('/api/pools');
    document.getElementById('uploadPoolSelect').innerHTML='<option value="">-- Select Pool --</option>'+pools.map(p=>`<option value="${p.id}">${esc(p.name)} (+${p.country_code}) — ${p.number_count} nums</option>`).join('');
    document.getElementById('uploadFile').value='';
    document.getElementById('uploadResult').innerHTML='';
    openModal('uploadModal');
}
async function uploadNumbers() {
    const pid=document.getElementById('uploadPoolSelect').value;
    const fi=document.getElementById('uploadFile');
    if(!pid){showToast('Select a pool');return;}
    if(!fi.files||!fi.files[0]){showToast('Select a file');return;}
    const fd=new FormData(); fd.append('file',fi.files[0]);
    try {
        const d=await api('/api/admin/pools/'+pid+'/upload', {method:'POST', body:fd});
        document.getElementById('uploadResult').innerHTML=`<div style="background:#dcfce7;padding:12px;border-radius:12px;font-size:13px;">✅ Added: <b>${d.added}</b><br>🚫 Bad skipped: <b>${d.skipped_bad}</b><br>⏳ Cooldown: <b>${d.skipped_cooldown}</b><br>🔁 Duplicates: <b>${d.duplicates}</b></div>`;
        showToast(`${d.added} numbers uploaded!`); loadRegions(); adminSection('pools');
    } catch(e){showToast(e.message);}
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
                resp = JSONResponse({"ok": True, "token": token, "approved": True, "is_admin": True, "user_id": user.id})
                resp.set_cookie("token", token, httponly=False, samesite="lax", secure=False, max_age=86400*30, path="/")
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
            resp = JSONResponse({"ok": True, "token": token, "approved": True, "is_admin": True, "user_id": user_id})
            resp.set_cookie("token", token, httponly=False, samesite="lax", secure=False, max_age=86400*30, path="/")
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
            if not user.is_approved and not user.is_admin:
                raise HTTPException(403, "Account pending approval")
            token = create_token(user.id)
            resp = JSONResponse({"ok": True, "token": token, "user_id": user.id, "username": user.username, "is_admin": user.is_admin})
            resp.set_cookie("token", token, httponly=False, samesite="lax", secure=False, max_age=86400*30, path="/")
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
        if not user.get("is_approved") and not user.get("is_admin"):
            raise HTTPException(403, "Account pending approval")
        token = create_token(user["id"])
        resp = JSONResponse({"ok": True, "token": token, "user_id": user["id"], "username": user["username"], "is_admin": user["is_admin"]})
        resp.set_cookie("token", token, httponly=False, samesite="lax", secure=False, max_age=86400*30, path="/")
        return resp

@app.post("/api/auth/logout")
def logout(token: str = Cookie(default=None), x_token: str = Header(default=None, alias="X-Token")):
    token = token or x_token
    revoke_token(token)
    resp = JSONResponse({"ok": True})
    resp.delete_cookie("token", path="/")
    return resp

@app.get("/api/auth/me")
def me(token: str = Cookie(default=None), x_token: str = Header(default=None, alias="X-Token")):
    user = get_user_from_token(token, x_token)
    if not user:
        raise HTTPException(401, "Not authenticated")
    return {"id": user["id"], "username": user["username"], "is_admin": user["is_admin"], "is_approved": user["is_approved"]}

# ══════════════════════════════════════════════════════════════════════════════
#  POOLS ENDPOINTS
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/api/pools")
def list_pools(token: str = Cookie(default=None), x_token: str = Header(default=None, alias="X-Token")):
    user = get_user_from_token(token, x_token)
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
                    "is_restricted": pool_is_restricted(p.id),
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
                "is_restricted": pool_is_restricted(pid),
                "last_restocked": p.get("last_restocked")
            })
        return result

class AssignRequest(BaseModel):
    pool_id: int
    prefix: Optional[str] = None

@app.post("/api/pools/assign")
async def assign_number(req: AssignRequest, token: str = Cookie(default=None), x_token: str = Header(default=None, alias="X-Token")):
    user = get_user_from_token(token, x_token)
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
def my_assignment(token: str = Cookie(default=None), x_token: str = Header(default=None, alias="X-Token")):
    user = get_user_from_token(token, x_token)
    if not user:
        raise HTTPException(401, "Not authenticated")
    assignment = get_current_assignment(user["id"])
    return {"assignment": assignment}

@app.post("/api/pools/release/{assignment_id}")
def release_number(assignment_id: int, token: str = Cookie(default=None), x_token: str = Header(default=None, alias="X-Token")):
    user = get_user_from_token(token, x_token)
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
def create_pool(req: PoolCreate, token: str = Cookie(default=None), x_token: str = Header(default=None, alias="X-Token")):
    user = get_user_from_token(token, x_token)
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
def update_pool(pool_id: int, req: PoolCreate, token: str = Cookie(default=None), x_token: str = Header(default=None, alias="X-Token")):
    user = get_user_from_token(token, x_token)
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
def delete_pool(pool_id: int, token: str = Cookie(default=None), x_token: str = Header(default=None, alias="X-Token")):
    user = get_user_from_token(token, x_token)
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
async def upload_numbers(pool_id: int, file: UploadFile = File(...), token: str = Cookie(default=None), x_token: str = Header(default=None, alias="X-Token")):
    user = get_user_from_token(token, x_token)
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
def export_pool(pool_id: int, token: str = Cookie(default=None), x_token: str = Header(default=None, alias="X-Token")):
    user = get_user_from_token(token, x_token)
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
def cut_numbers(pool_id: int, count: int, token: str = Cookie(default=None), x_token: str = Header(default=None, alias="X-Token")):
    user = get_user_from_token(token, x_token)
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
def clear_pool(pool_id: int, token: str = Cookie(default=None), x_token: str = Header(default=None, alias="X-Token")):
    user = get_user_from_token(token, x_token)
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
def pause_pool(pool_id: int, reason: str = "", token: str = Cookie(default=None), x_token: str = Header(default=None, alias="X-Token")):
    user = get_user_from_token(token, x_token)
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
def resume_pool(pool_id: int, token: str = Cookie(default=None), x_token: str = Header(default=None, alias="X-Token")):
    user = get_user_from_token(token, x_token)
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
def toggle_admin_only(pool_id: int, token: str = Cookie(default=None), x_token: str = Header(default=None, alias="X-Token")):
    user = get_user_from_token(token, x_token)
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
def set_trick_text(pool_id: int, trick_text: str, token: str = Cookie(default=None), x_token: str = Header(default=None, alias="X-Token")):
    user = get_user_from_token(token, x_token)
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
def get_pool_access(pool_id: int, token: str = Cookie(default=None), x_token: str = Header(default=None, alias="X-Token")):
    user = get_user_from_token(token, x_token)
    if not user or not user["is_admin"]:
        raise HTTPException(403, "Admin only")
    
    return get_pool_access_users(pool_id)

@app.post("/api/admin/pools/{pool_id}/access/{user_id}")
def grant_pool_access_endpoint(pool_id: int, user_id: int, token: str = Cookie(default=None), x_token: str = Header(default=None, alias="X-Token")):
    user = get_user_from_token(token, x_token)
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
def revoke_pool_access_endpoint(pool_id: int, user_id: int, token: str = Cookie(default=None), x_token: str = Header(default=None, alias="X-Token")):
    user = get_user_from_token(token, x_token)
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
def my_otps(token: str = Cookie(default=None), x_token: str = Header(default=None, alias="X-Token")):
    user = get_user_from_token(token, x_token)
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
async def search_otp(number: str, token: str = Cookie(default=None), x_token: str = Header(default=None, alias="X-Token")):
    user = get_user_from_token(token, x_token)
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
def save_numbers(req: SaveRequest, token: str = Cookie(default=None), x_token: str = Header(default=None, alias="X-Token")):
    user = get_user_from_token(token, x_token)
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
def list_saved(token: str = Cookie(default=None), x_token: str = Header(default=None, alias="X-Token")):
    user = get_user_from_token(token, x_token)
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
def ready_numbers(token: str = Cookie(default=None), x_token: str = Header(default=None, alias="X-Token")):
    user = get_user_from_token(token, x_token)
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
def update_saved(saved_id: int, timer_minutes: int, token: str = Cookie(default=None), x_token: str = Header(default=None, alias="X-Token")):
    user = get_user_from_token(token, x_token)
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
def delete_saved(saved_id: int, token: str = Cookie(default=None), x_token: str = Header(default=None, alias="X-Token")):
    user = get_user_from_token(token, x_token)
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
def submit_review(req: ReviewRequest, token: str = Cookie(default=None), x_token: str = Header(default=None, alias="X-Token")):
    user = get_user_from_token(token, x_token)
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
def stats(token: str = Cookie(default=None), x_token: str = Header(default=None, alias="X-Token")):
    user = get_user_from_token(token, x_token)
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
def list_users(token: str = Cookie(default=None), x_token: str = Header(default=None, alias="X-Token")):
    user = get_user_from_token(token, x_token)
    if not user or not user["is_admin"]:
        raise HTTPException(403, "Admin only")
    
    if SessionLocal:
        with SessionLocal() as db:
            users_list = db.query(User).order_by(User.created_at.desc()).all()
            return [{"id": u.id, "username": u.username, "is_admin": u.is_admin, "is_approved": u.is_approved, "is_blocked": u.is_blocked, "created_at": u.created_at.isoformat()} for u in users_list]
    else:
        return [{"id": u["id"], "username": u["username"], "is_admin": u["is_admin"], "is_approved": u["is_approved"], "is_blocked": u.get("is_blocked", False), "created_at": u.get("created_at", utcnow().isoformat())} for u in users.values()]

@app.post("/api/admin/users/{user_id}/approve")
async def approve_user_endpoint(user_id: int, token: str = Cookie(default=None), x_token: str = Header(default=None, alias="X-Token")):
    user = get_user_from_token(token, x_token)
    if not user or not user["is_admin"]:
        raise HTTPException(403, "Admin only")
    approve_user(user_id)
    await send_to_user(user_id, {"type": "notification", "message": "✅ Your account has been approved!"})
    return {"ok": True}

@app.post("/api/admin/users/{user_id}/block")
async def block_user_endpoint(user_id: int, token: str = Cookie(default=None), x_token: str = Header(default=None, alias="X-Token")):
    user = get_user_from_token(token, x_token)
    if not user or not user["is_admin"]:
        raise HTTPException(403, "Admin only")
    block_user(user_id)
    await send_to_user(user_id, {"type": "notification", "message": "🚫 Your account has been blocked."})
    return {"ok": True}

@app.post("/api/admin/users/{user_id}/unblock")
async def unblock_user_endpoint(user_id: int, token: str = Cookie(default=None), x_token: str = Header(default=None, alias="X-Token")):
    user = get_user_from_token(token, x_token)
    if not user or not user["is_admin"]:
        raise HTTPException(403, "Admin only")
    unblock_user(user_id)
    await send_to_user(user_id, {"type": "notification", "message": "✅ Your account has been unblocked!"})
    return {"ok": True}

@app.get("/api/admin/bad-numbers")
def list_bad_numbers(token: str = Cookie(default=None), x_token: str = Header(default=None, alias="X-Token")):
    user = get_user_from_token(token, x_token)
    if not user or not user["is_admin"]:
        raise HTTPException(403, "Admin only")
    
    if SessionLocal:
        with SessionLocal() as db:
            bad = db.query(BadNumber).order_by(BadNumber.created_at.desc()).all()
            return [{"number": b.number, "reason": b.reason, "created_at": b.created_at.isoformat()} for b in bad]
    else:
        return [{"number": num, "reason": data.get("reason", ""), "created_at": data.get("marked_at", "")} for num, data in bad_numbers.items()]

@app.delete("/api/admin/bad-numbers")
def remove_bad_number(number: str, token: str = Cookie(default=None), x_token: str = Header(default=None, alias="X-Token")):
    user = get_user_from_token(token, x_token)
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
def list_reviews(token: str = Cookie(default=None), x_token: str = Header(default=None, alias="X-Token")):
    user = get_user_from_token(token, x_token)
    if not user or not user["is_admin"]:
        raise HTTPException(403, "Admin only")
    
    if SessionLocal:
        with SessionLocal() as db:
            reviews_list = db.query(NumberReview).order_by(NumberReview.created_at.desc()).limit(100).all()
            return [{"id": r.id, "user_id": r.user_id, "number": r.number, "rating": r.rating, "comment": r.comment, "created_at": r.created_at.isoformat()} for r in reviews_list]
    else:
        return [{"id": r["id"], "user_id": r["user_id"], "number": r["number"], "rating": r["rating"], "comment": r["comment"], "created_at": r["created_at"]} for r in sorted(reviews, key=lambda x: x["created_at"], reverse=True)[:100]]

@app.post("/api/admin/broadcast")
async def broadcast_message(message: str, token: str = Cookie(default=None), x_token: str = Header(default=None, alias="X-Token")):
    user = get_user_from_token(token, x_token)
    if not user or not user["is_admin"]:
        raise HTTPException(403, "Admin only")
    
    await broadcast_all({"type": "broadcast", "message": message})
    return {"ok": True}

@app.post("/api/admin/settings/approval")
def set_approval_mode_endpoint(enabled: bool, token: str = Cookie(default=None), x_token: str = Header(default=None, alias="X-Token")):
    user = get_user_from_token(token, x_token)
    if not user or not user["is_admin"]:
        raise HTTPException(403, "Admin only")
    
    set_approval_mode(enabled)
    return {"ok": True, "mode": "on" if enabled else "off"}

@app.post("/api/admin/settings/otp-redirect")
def set_otp_redirect_mode_endpoint(mode: str, token: str = Cookie(default=None), x_token: str = Header(default=None, alias="X-Token")):
    user = get_user_from_token(token, x_token)
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
def get_buttons(token: str = Cookie(default=None), x_token: str = Header(default=None, alias="X-Token")):
    user = get_user_from_token(token, x_token)
    if not user:
        raise HTTPException(401, "Not authenticated")
    
    return _get_custom_buttons()

@app.post("/api/admin/buttons")
def add_button(label: str, url: str, token: str = Cookie(default=None), x_token: str = Header(default=None, alias="X-Token")):
    user = get_user_from_token(token, x_token)
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
def delete_button(button_id: int, token: str = Cookie(default=None), x_token: str = Header(default=None, alias="X-Token")):
    user = get_user_from_token(token, x_token)
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
async def websocket_user(websocket: WebSocket, user_id: int, token: str = None):
    # Accept token as query param for WS authentication
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
#  MONITOR RESULT — numberbot POSTs here when OTP is found (same as /api/otp/monitor-result)
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
    # Reuse existing logic
    from fastapi import Request
    class _FakePayload:
        def __init__(self, p): self.__dict__ = p.__dict__
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
async def deny_user_endpoint(user_id: int, token: str = Cookie(default=None), x_token: str = Header(default=None, alias="X-Token")):
    user = get_user_from_token(token, x_token)
    if not user or not user["is_admin"]:
        raise HTTPException(403, "Admin only")
    deny_user(user_id)
    await send_to_user(user_id, {"type": "notification", "message": "❌ Your access has been denied."})
    return {"ok": True}


# ══════════════════════════════════════════════════════════════════════════════
#  ADMIN — BLOCK ALL USERS
# ══════════════════════════════════════════════════════════════════════════════

@app.post("/api/admin/block-all")
async def block_all_users(token: str = Cookie(default=None), x_token: str = Header(default=None, alias="X-Token")):
    user = get_user_from_token(token, x_token)
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
#  ADMIN — SEARCH USER BY ID
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/api/admin/users/{user_id}/info")
def user_info(user_id: int, token: str = Cookie(default=None), x_token: str = Header(default=None, alias="X-Token")):
    user = get_user_from_token(token, x_token)
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
async def platform_files(token: str = Cookie(default=None)):
    """Fetch all numbers from platform API, grouped by country, return as JSON list of files."""
    user = get_user_from_token(token)
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
