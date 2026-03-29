# -*- coding: utf-8 -*-
"""
NEON GRID NETWORK — COMPLETE PLATFORM BACKEND (REDESIGNED)
============================================================
Redesigned frontend: White & Blue theme
- Correct pending pool → ready pool logic with timers
- Save numbers page: ONE active pool showing, Change Number / Change Pool
- All admin features from telegram bot
- No earning/balance content
- WebSocket OTP delivery
- Monitor bot integration
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

try:
    from sqlalchemy import (create_engine, Column, Integer, String, Boolean, DateTime,
                            Text, ForeignKey, BigInteger, UniqueConstraint, select, func, text)
    from sqlalchemy.ext.declarative import declarative_base
    from sqlalchemy.orm import sessionmaker, Session, relationship
    from sqlalchemy.pool import QueuePool
    SQLALCHEMY_AVAILABLE = True
except ImportError:
    SQLALCHEMY_AVAILABLE = False

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
log = logging.getLogger("neongrid")

DATABASE_URL = os.environ.get("DATABASE_URL", "")
SHARED_SECRET = os.environ.get("SHARED_SECRET", "MonitorSecret2024")
MONITOR_BOT_URL = os.environ.get("MONITOR_BOT_URL", "").rstrip("/")
FRONTEND_URL = os.environ.get("FRONTEND_URL", "*")
PORT = int(os.environ.get("PORT", "8080"))

if DATABASE_URL and DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

PLATFORM_URL = os.environ.get("PLATFORM_URL", "http://198.135.52.238")
PLATFORM_USERNAME = os.environ.get("PLATFORM_USERNAME", "Frankhustle")
PLATFORM_PASSWORD = os.environ.get("PLATFORM_PASSWORD", "f11111")

PLATFORM_MONITOR_TTL = 600
PLATFORM_POLL_INTERVAL = 5
PLATFORM_STOCK_INTERVAL = 300
OTP_AUTO_DELETE_DELAY = 30

bot_settings = {"approval_mode": "on", "otp_redirect_mode": "pool"}
_compliance_counters: Dict[int, int] = {}

DEFAULT_LOCAL_PREFIX = "+234"
HARDCODED_OTP_GROUP = "https://t.me/earnplusz"
MONITOR_TELEGRAM = 0
MONITOR_PLATFORM = 1
MONITOR_BOTH = 2
COMPLIANCE_CODES_PER_CHECK = 5
COOLDOWN_CHECK_URL = "http://127.0.0.1:8003/check_numbers_exist"

if SQLALCHEMY_AVAILABLE and DATABASE_URL:
    engine = create_engine(
        DATABASE_URL, poolclass=QueuePool, pool_size=10,
        max_overflow=20, pool_pre_ping=True, echo=False
    )
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base = declarative_base()
else:
    engine = SessionLocal = None
    Base = None
    log.warning("No DATABASE_URL, running in memory-only mode")

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
                except:
                    pass
            Base.metadata.create_all(bind=engine)
            with SessionLocal() as db:
                if db.query(User).count() == 0:
                    admin = User(username="admin", password_hash=hash_password("admin123"),
                                 is_admin=True, is_approved=True)
                    db.add(admin)
                    db.commit()
                if db.query(Pool).count() == 0:
                    default_pools = [
                        {"name": "Nigeria", "country_code": "234", "otp_group_id": -1003388744078, "trick_text": "Best for WhatsApp"},
                        {"name": "USA", "country_code": "1", "otp_group_id": -1003388744078, "trick_text": "Best for Telegram"},
                        {"name": "United Kingdom", "country_code": "44", "otp_group_id": -1003388744078},
                    ]
                    for p in default_pools:
                        pool = Pool(name=p["name"], country_code=p["country_code"],
                                    otp_group_id=p["otp_group_id"], otp_link="",
                                    match_format="5+4", trick_text=p.get("trick_text", ""))
                        db.add(pool)
                    db.commit()
                if db.query(CustomButton).count() == 0:
                    db.add_all([
                        CustomButton(label="📢 Join Channel", url="https://t.me/earnplusz", position=0),
                        CustomButton(label="📱 Number Channel", url="https://t.me/Finalsearchbot", position=1),
                    ])
                    db.commit()
else:
    def init_db():
        log.warning("Using in-memory fallback")

    users = {1: {"id": 1, "username": "admin", "password_hash": hash_password("admin123"),
                 "is_admin": True, "is_approved": True, "is_blocked": False,
                 "created_at": datetime.now(timezone.utc).isoformat()}}
    sessions = {}
    pools = {
        1: {"id": 1, "name": "Nigeria", "country_code": "234", "otp_group_id": -1003388744078,
            "otp_link": "", "match_format": "5+4", "telegram_match_format": "", "uses_platform": 0,
            "is_paused": False, "pause_reason": "", "trick_text": "Best for WhatsApp", "is_admin_only": False, "last_restocked": None},
        2: {"id": 2, "name": "USA", "country_code": "1", "otp_group_id": -1003388744078,
            "otp_link": "", "match_format": "5+4", "telegram_match_format": "", "uses_platform": 0,
            "is_paused": False, "pause_reason": "", "trick_text": "Best for Telegram", "is_admin_only": False, "last_restocked": None},
        3: {"id": 3, "name": "United Kingdom", "country_code": "44", "otp_group_id": -1003388744078,
            "otp_link": "", "match_format": "5+4", "telegram_match_format": "", "uses_platform": 0,
            "is_paused": False, "pause_reason": "", "trick_text": "", "is_admin_only": False, "last_restocked": None},
    }
    active_numbers = {
        1: [f"+234{random.randint(7000000000, 7999999999)}" for _ in range(50)],
        2: [f"+1{random.randint(7000000000, 7999999999)}" for _ in range(30)],
        3: [f"+44{random.randint(7000000000, 7999999999)}" for _ in range(20)],
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
    feedbacks = []
    _counters = {"user": 2, "pool": 4, "assignment": 1, "otp": 1, "saved": 1, "review": 1, "feedback": 1, "button": 3}

def utcnow():
    return datetime.now(timezone.utc)

def create_token(user_id: int) -> str:
    raw = f"{user_id}:{secrets.token_hex(32)}:{time.time()}"
    return hashlib.sha256(raw.encode()).hexdigest() + secrets.token_hex(16)

def get_user_from_token(token: Optional[str]) -> Optional[Dict]:
    if not token:
        return None
    if SessionLocal:
        with SessionLocal() as db:
            now = utcnow()
            sess = db.query(UserSession).filter(UserSession.token == token, UserSession.expires_at > now).first()
            if not sess:
                return None
            user = db.query(User).filter(User.id == sess.user_id).first()
            if not user or user.is_blocked:
                return None
            return {"id": user.id, "username": user.username, "is_admin": user.is_admin, "is_approved": user.is_approved}
    else:
        sess = sessions.get(token)
        if not sess or datetime.fromisoformat(sess["expires_at"]) < utcnow():
            return None
        user = users.get(sess["user_id"])
        if not user or user.get("is_blocked"):
            return None
        return {"id": user["id"], "username": user["username"], "is_admin": user["is_admin"], "is_approved": user["is_approved"]}

def store_token(user_id: int, token: str):
    expires = utcnow() + timedelta(days=30)
    if SessionLocal:
        with SessionLocal() as db:
            db.add(UserSession(user_id=user_id, token=token, expires_at=expires))
            db.commit()
    else:
        sessions[token] = {"user_id": user_id, "expires_at": expires.isoformat()}

def is_admin(user_id: int) -> bool:
    if SessionLocal:
        with SessionLocal() as db:
            user = db.query(User).filter(User.id == user_id).first()
            return user.is_admin if user else False
    else:
        user = users.get(user_id)
        return user.get("is_admin", False) if user else False

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
            db.query(User).filter(User.id == user_id).update({"is_approved": False})
            db.commit()
    else:
        if user_id in users:
            users[user_id]["is_approved"] = False

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

def get_approval_mode() -> bool:
    return bot_settings.get("approval_mode") == "on"

def set_approval_mode(enabled: bool):
    bot_settings["approval_mode"] = "on" if enabled else "off"

def get_otp_redirect_mode() -> str:
    return bot_settings.get("otp_redirect_mode", "pool")

def set_otp_redirect_mode(mode: str):
    bot_settings["otp_redirect_mode"] = mode

def has_pool_access(pool_id: int, user_id: int) -> bool:
    if is_admin(user_id):
        return True
    if SessionLocal:
        with SessionLocal() as db:
            count = db.query(PoolAccess).filter(PoolAccess.pool_id == pool_id).count()
            if count == 0:
                return True
            return db.query(PoolAccess).filter(PoolAccess.pool_id == pool_id, PoolAccess.user_id == user_id).first() is not None
    else:
        restricted = pool_access.get(pool_id, set())
        if not restricted:
            return True
        return user_id in restricted

def grant_pool_access(pool_id: int, user_id: int):
    if SessionLocal:
        with SessionLocal() as db:
            existing = db.query(PoolAccess).filter(PoolAccess.pool_id == pool_id, PoolAccess.user_id == user_id).first()
            if not existing:
                db.add(PoolAccess(pool_id=pool_id, user_id=user_id))
                db.commit()
    else:
        pool_access.setdefault(pool_id, set()).add(user_id)

def revoke_pool_access(pool_id: int, user_id: int):
    if SessionLocal:
        with SessionLocal() as db:
            db.query(PoolAccess).filter(PoolAccess.pool_id == pool_id, PoolAccess.user_id == user_id).delete()
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
                    result.append({"user_id": a.user_id, "username": user.username})
    else:
        for uid in pool_access.get(pool_id, set()):
            u = users.get(uid)
            if u:
                result.append({"user_id": uid, "username": u.get("username", "")})
    return result

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
        bad_numbers[number] = {"number": number, "reason": reason, "marked_by": marked_by, "marked_at": utcnow().isoformat()}
        for pid, nums in active_numbers.items():
            if number in nums:
                nums.remove(number)

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
            assignment = Assignment(user_id=user_id, pool_id=pool_id, number=number)
            db.add(assignment)
            db.commit()
            db.refresh(assignment)
            return {
                "assignment_id": assignment.id, "number": number,
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
        assignment = {"id": _counters["assignment"], "user_id": user_id, "pool_id": pool_id,
                      "number": selected, "assigned_at": utcnow().isoformat(), "released_at": None, "feedback": ""}
        archived_numbers.append(assignment)
        _counters["assignment"] += 1
        return {
            "assignment_id": assignment["id"], "number": selected,
            "pool_name": pool.get("name", "Unknown"), "pool_code": pool.get("country_code", ""),
            "otp_link": pool.get("otp_link", ""), "otp_group_id": pool.get("otp_group_id"),
            "uses_platform": pool.get("uses_platform", 0), "match_format": pool.get("match_format", "5+4"),
            "telegram_match_format": pool.get("telegram_match_format", ""),
            "trick_text": pool.get("trick_text", ""), "pool_id": pool_id
        }

def release_assignment(user_id: int, assignment_id: int = None):
    if SessionLocal:
        with SessionLocal() as db:
            query = db.query(Assignment).filter(Assignment.user_id == user_id, Assignment.released_at == None)
            if assignment_id:
                query = query.filter(Assignment.id == assignment_id)
            query.update({"released_at": utcnow()})
            db.commit()
    else:
        for a in archived_numbers:
            if a["user_id"] == user_id and a.get("released_at") is None:
                if assignment_id is None or a["id"] == assignment_id:
                    a["released_at"] = utcnow().isoformat()

def get_current_assignment(user_id: int) -> Optional[Dict]:
    if SessionLocal:
        with SessionLocal() as db:
            assignment = db.query(Assignment).filter(Assignment.user_id == user_id, Assignment.released_at == None).order_by(Assignment.assigned_at.desc()).first()
            if assignment:
                pool = db.query(Pool).filter(Pool.id == assignment.pool_id).first()
                return {
                    "assignment_id": assignment.id, "number": assignment.number,
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
                    "assignment_id": a["id"], "number": a["number"],
                    "pool_name": pool.get("name", "Unknown"), "pool_id": a["pool_id"],
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
            return [{"id": b.id, "label": b.label, "url": b.url} for b in buttons]
    else:
        return custom_buttons

# ── Platform Monitor ──────────────────────────────────────────────────────────
_platform_token = None
_platform_token_expires = 0
_active_platform_tasks: Dict[int, asyncio.Task] = {}
_platform_stock_snapshot: Dict[str, int] = {}

async def _get_platform_token():
    global _platform_token, _platform_token_expires
    if _platform_token and time.time() < _platform_token_expires:
        return _platform_token
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(f"{PLATFORM_URL}/api/auth/login",
                                     json={"username": PLATFORM_USERNAME, "password": PLATFORM_PASSWORD}, timeout=10) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    _platform_token = data.get("token") or data.get("access_token")
                    _platform_token_expires = time.time() + 3600
                    return _platform_token
    except Exception as e:
        log.error(f"[Platform] Login failed: {e}")
    return None

async def _platform_monitor_number(user_id: int, number: str, match_format: str):
    token = await _get_platform_token()
    if not token:
        return
    clean = re.sub(r'\D', '', number)
    deadline = time.time() + PLATFORM_MONITOR_TTL
    while time.time() < deadline:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(f"{PLATFORM_URL}/api/otp?number={clean}",
                                        headers={"Authorization": f"Bearer {token}"}, timeout=10) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        otps = data if isinstance(data, list) else data.get("otps", [])
                        if otps:
                            otp_entry = otps[0]
                            otp = str(otp_entry.get("otp") or otp_entry.get("code") or "")
                            if otp:
                                await _deliver_otp(user_id, number, otp, "Platform OTP")
                                return
        except asyncio.CancelledError:
            return
        except Exception as e:
            log.error(f"[PlatformMonitor] Error: {e}")
        await asyncio.sleep(PLATFORM_POLL_INTERVAL)

async def _deliver_otp(user_id: int, number: str, otp: str, raw_message: str):
    otp_id = 0
    is_saved = False
    if SessionLocal:
        with SessionLocal() as db:
            saved_row = db.query(SavedNumber).filter(SavedNumber.user_id == user_id, SavedNumber.number == number, SavedNumber.moved == True).first()
            if saved_row:
                is_saved = True
            else:
                assignment = db.query(Assignment).filter(Assignment.user_id == user_id, Assignment.number == number, Assignment.released_at == None).first()
                otp_entry = OTPLog(assignment_id=assignment.id if assignment else None,
                                   user_id=user_id, number=number, otp_code=otp, raw_message=raw_message)
                db.add(otp_entry)
                db.commit()
                db.refresh(otp_entry)
                otp_id = otp_entry.id
    else:
        saved_row = next((s for s in saved_numbers if s["user_id"] == user_id and s["number"] == number and s.get("moved")), None)
        if saved_row:
            is_saved = True
        else:
            otp_id = _counters["otp"]
            _counters["otp"] += 1
            otp_logs.append({"id": otp_id, "user_id": user_id, "number": number,
                             "otp_code": otp, "raw_message": raw_message, "delivered_at": utcnow().isoformat()})

    if is_saved:
        await send_to_user(user_id, {"type": "saved_otp", "number": number, "otp": otp,
                                      "raw_message": raw_message, "delivered_at": utcnow().isoformat(),
                                      "auto_delete_seconds": OTP_AUTO_DELETE_DELAY})
    else:
        await send_to_user(user_id, {"type": "otp", "id": otp_id, "number": number, "otp": otp,
                                      "raw_message": raw_message, "delivered_at": utcnow().isoformat(),
                                      "auto_delete_seconds": OTP_AUTO_DELETE_DELAY})
    await broadcast_feed({"type": "feed_otp", "number": number, "otp": otp, "delivered_at": utcnow().isoformat()})

def start_platform_monitor(user_id: int, number: str, match_format: str):
    existing = _active_platform_tasks.get(user_id)
    if existing and not existing.done():
        existing.cancel()
    task = asyncio.create_task(_platform_monitor_number(user_id, number, match_format))
    _active_platform_tasks[user_id] = task

async def _platform_stock_watcher():
    await asyncio.sleep(30)
    while True:
        try:
            token = await _get_platform_token()
            if token:
                async with aiohttp.ClientSession() as session:
                    async with session.get(f"{PLATFORM_URL}/api/numbers?limit=1000",
                                            headers={"Authorization": f"Bearer {token}"}, timeout=15) as resp:
                        if resp.status == 200:
                            numbers_data = await resp.json()
                            country_counts = {}
                            for entry in (numbers_data if isinstance(numbers_data, list) else []):
                                country = entry.get("country", "Unknown")
                                country_counts[country] = country_counts.get(country, 0) + 1
                            _platform_stock_snapshot.update(country_counts)
        except Exception as e:
            log.error(f"[StockWatcher] {e}")
        await asyncio.sleep(PLATFORM_STOCK_INTERVAL)

# ── Monitor Bot ───────────────────────────────────────────────────────────────
async def request_monitor_bot(number: str, group_id: int, match_format: str, user_id: int) -> bool:
    if not MONITOR_BOT_URL:
        log.error("[Monitor] MONITOR_BOT_URL not set!")
        return False
    url = f"{MONITOR_BOT_URL}/monitor-request"
    payload = {"number": number, "group_id": group_id, "match_format": match_format,
                "user_id": user_id, "secret": SHARED_SECRET}
    for attempt in range(3):
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload, timeout=10) as resp:
                    if resp.status == 200:
                        return True
                    log.error(f"[Monitor] HTTP {resp.status}")
        except Exception as e:
            log.error(f"[Monitor] Failed: {e}")
            if attempt < 2:
                await asyncio.sleep(2)
    return False

async def request_search_otp(number: str, group_id: int, match_format: str, user_id: int) -> bool:
    if not MONITOR_BOT_URL:
        return False
    url = f"{MONITOR_BOT_URL}/search-otp-request"
    payload = {"number": number, "group_id": group_id, "match_format": match_format,
                "user_id": user_id, "secret": SHARED_SECRET}
    for attempt in range(3):
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload, timeout=10) as resp:
                    if resp.status == 200:
                        return True
        except Exception as e:
            log.error(f"[SearchOTP] Failed: {e}")
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
        log.error(f"Cooldown check failed: {e}")
    return []

async def compliance_record_otp_delivered(user_id: int):
    _compliance_counters[user_id] = _compliance_counters.get(user_id, 0) + 1
    if _compliance_counters[user_id] >= COMPLIANCE_CODES_PER_CHECK:
        _compliance_counters[user_id] = 0

# ── WebSocket Manager ─────────────────────────────────────────────────────────
user_connections = {}
feed_connections = []

async def connect_user(ws: WebSocket, user_id: int):
    await ws.accept()
    user_connections.setdefault(user_id, []).append(ws)

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
        except:
            dead.append(ws)
    for ws in dead:
        disconnect_user(ws, user_id)

async def broadcast_feed(data: dict):
    dead = []
    for ws in feed_connections:
        try:
            await ws.send_text(json.dumps(data))
        except:
            dead.append(ws)
    for ws in dead:
        disconnect_feed(ws)

async def broadcast_all(data: dict):
    for user_id, conns in list(user_connections.items()):
        for ws in conns:
            try:
                await ws.send_text(json.dumps(data))
            except:
                pass
    for ws in list(feed_connections):
        try:
            await ws.send_text(json.dumps(data))
        except:
            pass

# ── Saved Numbers Expiry ──────────────────────────────────────────────────────
async def process_expired_saved():
    """Move numbers whose timer expired from pending to ready pool."""
    if SessionLocal:
        with SessionLocal() as db:
            now = utcnow()
            expired = db.query(SavedNumber).filter(SavedNumber.expires_at <= now, SavedNumber.moved == False).all()
            newly_ready = []
            for sn in expired:
                sn.moved = True
                pool = db.query(Pool).filter(Pool.name == sn.pool_name).first()
                if pool and pool.otp_group_id:
                    newly_ready.append({
                        "number": sn.number, "user_id": sn.user_id,
                        "otp_group_id": pool.otp_group_id,
                        "match_format": pool.telegram_match_format or pool.match_format,
                        "uses_platform": pool.uses_platform
                    })
            if expired:
                db.commit()
                log.info(f"[Scheduler] {len(expired)} numbers moved to ready")
            for info in newly_ready:
                try:
                    await request_monitor_bot(number=info["number"], group_id=info["otp_group_id"],
                                               match_format=info["match_format"], user_id=info["user_id"])
                    if info["uses_platform"] in (1, 2):
                        start_platform_monitor(info["user_id"], info["number"], info["match_format"])
                    # Notify user
                    await send_to_user(info["user_id"], {"type": "number_ready", "number": info["number"],
                                                          "message": f"✅ Number {info['number']} is now READY!"})
                except Exception as e:
                    log.error(f"[Scheduler] Monitor trigger failed: {e}")
    else:
        now = utcnow()
        expired = [s for s in saved_numbers if not s.get("moved", False) and
                   datetime.fromisoformat(s["expires_at"]) <= now]
        for sn in expired:
            sn["moved"] = True
        if expired:
            log.info(f"[Scheduler] {len(expired)} numbers moved to ready (memory)")

async def scheduler():
    while True:
        await asyncio.sleep(30)
        try:
            await process_expired_saved()
        except Exception as e:
            log.error(f"[Scheduler] {e}")

# ═════════════════════════════════════════════════════════════════════════════
#  FRONTEND HTML — White & Blue Theme, Complete Features
# ═════════════════════════════════════════════════════════════════════════════

FRONTEND_HTML = r'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
<title>NEON GRID NETWORK</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700;800&family=JetBrains+Mono:wght@500;700&display=swap" rel="stylesheet">
<style>
*{margin:0;padding:0;box-sizing:border-box;-webkit-tap-highlight-color:transparent}
:root{
  --blue:#1a6dff;--blue2:#0051cc;--blue-light:#e8f0ff;--blue-mid:#c7d9ff;
  --white:#ffffff;--bg:#f0f4ff;--surface:#ffffff;--surface2:#f5f8ff;
  --text:#0a1628;--text2:#3d5a8a;--text3:#7a92b8;
  --green:#00c96a;--red:#ff3b5c;--amber:#f59e0b;
  --border:#d4e0ff;--shadow:0 2px 16px rgba(26,109,255,0.10);
  --radius:16px;--radius-lg:24px;--radius-xl:32px;
}
body{font-family:'Plus Jakarta Sans',sans-serif;background:var(--bg);color:var(--text);min-height:100vh;padding-bottom:80px}
.mono{font-family:'JetBrains Mono',monospace}

/* ── TOP BAR ── */
.topbar{background:var(--blue);padding:14px 20px;display:flex;justify-content:space-between;align-items:center;position:sticky;top:0;z-index:200;box-shadow:0 2px 12px rgba(26,109,255,0.3)}
.topbar-logo{color:#fff;font-weight:800;font-size:18px;letter-spacing:-0.5px}
.topbar-right{display:flex;align-items:center;gap:10px;color:rgba(255,255,255,0.9);font-size:13px}
.topbar-time{background:rgba(255,255,255,0.15);padding:4px 10px;border-radius:30px;font-size:12px;color:#fff}
.admin-tag{background:#ffd700;color:#0a1628;padding:3px 10px;border-radius:30px;font-size:10px;font-weight:700;letter-spacing:0.5px}
.online-dot{width:8px;height:8px;border-radius:50%;background:#00ff88;display:inline-block;margin-right:4px;animation:pulse 2s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:0.4}}

/* ── USER HEADER ── */
.user-header{background:var(--white);border-bottom:1px solid var(--border);padding:14px 20px;display:flex;justify-content:space-between;align-items:center}
.user-name{font-weight:700;font-size:17px;color:var(--blue)}
.user-id-tag{background:var(--blue-light);color:var(--blue2);padding:4px 12px;border-radius:20px;font-size:11px;font-weight:600}

/* ── CARDS ── */
.card{background:var(--white);border-radius:var(--radius-lg);padding:20px;margin:12px 16px;border:1px solid var(--border);box-shadow:var(--shadow)}
.card-blue{background:linear-gradient(135deg,var(--blue),var(--blue2));color:#fff;border:none;box-shadow:0 8px 32px rgba(26,109,255,0.25)}
.card-title{font-size:11px;font-weight:700;letter-spacing:1px;text-transform:uppercase;opacity:0.7;margin-bottom:6px}
.number-display{font-size:26px;font-weight:700;letter-spacing:1px;margin:8px 0;word-break:break-all;cursor:pointer}
.card-blue .number-display{color:#fff}
.number-display:not(.card-blue .number-display){color:var(--blue)}
.pool-tag{display:inline-flex;align-items:center;gap:6px;background:rgba(255,255,255,0.2);padding:5px 14px;border-radius:20px;font-size:12px;font-weight:600}
.pool-tag.dark{background:var(--blue-light);color:var(--blue2)}

/* ── SECTION HEADER ── */
.sec-header{padding:16px 20px 8px;font-size:12px;font-weight:700;letter-spacing:1px;text-transform:uppercase;color:var(--text3)}

/* ── BUTTONS ── */
.btn{display:inline-flex;align-items:center;justify-content:center;gap:6px;padding:11px 20px;border-radius:40px;font-size:13px;font-weight:600;cursor:pointer;border:none;transition:all 0.15s;font-family:inherit}
.btn:active{transform:scale(0.97)}
.btn-blue{background:var(--blue);color:#fff}
.btn-blue:hover{background:var(--blue2)}
.btn-outline{background:transparent;color:var(--blue);border:1.5px solid var(--blue)}
.btn-ghost{background:var(--blue-light);color:var(--blue2)}
.btn-red{background:#fff1f3;color:var(--red);border:1.5px solid #ffc8d0}
.btn-green{background:#e8fff4;color:var(--green);border:1.5px solid #b8f0d4}
.btn-sm{padding:7px 14px;font-size:12px}
.btn-xs{padding:5px 10px;font-size:11px}
.btn-full{width:100%}
.btn-row{display:flex;gap:8px;flex-wrap:wrap}

/* ── REGION LIST ── */
.region-item{background:var(--white);border:1px solid var(--border);border-radius:var(--radius);padding:14px 16px;margin:8px 16px;display:flex;justify-content:space-between;align-items:center;cursor:pointer;transition:all 0.15s}
.region-item:active{background:var(--blue-light);border-color:var(--blue)}
.region-name{font-weight:700;font-size:15px;color:var(--text)}
.region-code{font-size:12px;color:var(--text3);margin-top:2px}
.region-trick{font-size:11px;color:var(--amber);margin-top:3px}
.region-count{background:var(--blue-light);color:var(--blue);padding:4px 12px;border-radius:20px;font-size:12px;font-weight:700}
.region-paused{color:var(--red);font-size:12px;font-weight:600}

/* ── OTP CARD ── */
.otp-card{background:linear-gradient(135deg,var(--green),#00a855);border-radius:var(--radius-lg);padding:20px;margin:0 16px 12px;color:#fff;animation:slideUp 0.3s ease}
@keyframes slideUp{from{opacity:0;transform:translateY(16px)}to{opacity:1;transform:translateY(0)}}
.otp-label{font-size:11px;font-weight:700;letter-spacing:1px;opacity:0.8;margin-bottom:6px}
.otp-code{font-size:44px;font-weight:800;letter-spacing:6px;text-align:center;cursor:pointer;margin:10px 0;font-family:'JetBrains Mono',monospace}
.otp-countdown{font-size:12px;text-align:center;opacity:0.85}
.otp-message{font-size:11px;text-align:center;opacity:0.75;margin-top:6px;word-break:break-all}

/* ── SAVED / PENDING ITEMS ── */
.list-item{background:var(--white);border:1px solid var(--border);border-radius:var(--radius);padding:14px 16px;margin:6px 16px;display:flex;justify-content:space-between;align-items:center}
.list-item.column{flex-direction:column;align-items:stretch;gap:10px}
.item-title{font-weight:700;font-size:14px;color:var(--text)}
.item-sub{font-size:12px;color:var(--text3);margin-top:3px}
.badge{display:inline-block;padding:3px 10px;border-radius:20px;font-size:11px;font-weight:700}
.badge-green{background:#e6fff2;color:#00a855}
.badge-amber{background:#fff8e6;color:#d97706}
.badge-red{background:#fff1f3;color:var(--red)}
.badge-blue{background:var(--blue-light);color:var(--blue2)}
.badge-ready{background:linear-gradient(90deg,var(--blue),#00c96a);color:#fff}

/* ── FILTER ROW ── */
.filter-row{display:flex;gap:10px;padding:10px 16px;background:var(--white);border-bottom:1px solid var(--border)}
.filter-input{flex:1;border:1.5px solid var(--border);border-radius:30px;padding:10px 16px;font-size:14px;color:var(--text);outline:none;background:var(--bg);font-family:inherit}
.filter-input:focus{border-color:var(--blue);background:#fff}
.filter-input::placeholder{color:var(--text3)}

/* ── POOL PENDING CARD ── */
.pending-pool{background:var(--white);border:1px solid var(--border);border-radius:var(--radius-lg);margin:8px 16px;overflow:hidden}
.pending-pool-header{background:var(--blue-light);padding:12px 16px;display:flex;justify-content:space-between;align-items:center}
.pending-pool-name{font-weight:700;font-size:15px;color:var(--blue2)}
.pending-pool-count{font-size:12px;color:var(--text3)}
.pending-pool-body{padding:12px 16px}

/* ── READY NUMBER CARD ── */
.ready-card{background:var(--white);border:2px solid var(--blue);border-radius:var(--radius-xl);margin:8px 16px;overflow:hidden;box-shadow:0 4px 24px rgba(26,109,255,0.12)}
.ready-card-header{background:linear-gradient(135deg,var(--blue),var(--blue2));padding:14px 18px;display:flex;justify-content:space-between;align-items:center}
.ready-card-pool{font-weight:700;font-size:15px;color:#fff}
.ready-card-queue{font-size:12px;color:rgba(255,255,255,0.8)}
.ready-card-body{padding:16px 18px}
.ready-number{font-size:22px;font-weight:700;color:var(--blue);cursor:pointer;letter-spacing:0.5px;font-family:'JetBrains Mono',monospace}

/* ── HISTORY ── */
.history-item{background:var(--white);border:1px solid var(--border);border-radius:var(--radius);padding:14px 16px;margin:6px 16px}
.history-num{font-size:12px;color:var(--text3);margin-bottom:4px}
.history-otp{font-size:26px;font-weight:800;color:var(--green);cursor:pointer;font-family:'JetBrains Mono',monospace}
.history-time{font-size:11px;color:var(--text3);margin-top:4px}

/* ── ADMIN ── */
.admin-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(130px,1fr));gap:10px;padding:12px 16px}
.admin-card{background:var(--white);border:1.5px solid var(--border);border-radius:var(--radius-lg);padding:18px 12px;text-align:center;cursor:pointer;transition:all 0.15s}
.admin-card:active{background:var(--blue-light);border-color:var(--blue);transform:scale(0.97)}
.admin-card-icon{font-size:28px;margin-bottom:8px}
.admin-card-label{font-size:12px;font-weight:700;color:var(--text)}
.admin-panel{background:var(--white);border-radius:var(--radius-lg);margin:8px 16px;border:1px solid var(--border)}
.fg{margin-bottom:18px}
.fg label{display:block;font-size:11px;font-weight:700;letter-spacing:0.8px;text-transform:uppercase;color:var(--text3);margin-bottom:7px}
.fg input,.fg select,.fg textarea{width:100%;padding:12px 16px;border:1.5px solid var(--border);border-radius:12px;font-size:14px;color:var(--text);background:var(--bg);outline:none;font-family:inherit;transition:border 0.15s}
.fg input:focus,.fg select:focus,.fg textarea:focus{border-color:var(--blue);background:#fff}
.brow{display:flex;gap:10px;justify-content:flex-end;margin-top:18px}
.admin-section{padding:16px}

/* ── FORM PRESETS ── */
.preset-row{display:flex;gap:6px;flex-wrap:wrap;margin-top:8px}
.preset-btn{background:var(--blue-light);color:var(--blue2);border:1.5px solid var(--blue-mid);padding:6px 12px;border-radius:20px;font-size:12px;font-weight:600;cursor:pointer;font-family:inherit;transition:all 0.15s}
.preset-btn:active{background:var(--blue);color:#fff}

/* ── NAV ── */
.bottom-nav{position:fixed;bottom:0;left:0;right:0;background:var(--white);border-top:1px solid var(--border);display:flex;justify-content:space-around;padding:8px 0 16px;z-index:200;box-shadow:0 -4px 20px rgba(26,109,255,0.08)}
.nav-item{display:flex;flex-direction:column;align-items:center;gap:3px;cursor:pointer;padding:6px 16px;border-radius:16px;transition:all 0.15s}
.nav-item:active{background:var(--blue-light)}
.nav-icon{font-size:22px}
.nav-label{font-size:10px;font-weight:600;color:var(--text3)}
.nav-item.active .nav-label{color:var(--blue)}
.nav-item.active .nav-icon{filter:drop-shadow(0 0 6px rgba(26,109,255,0.4))}

/* ── PAGE ── */
.page{display:none;padding-bottom:20px}
.page.active{display:block}
.empty-state{text-align:center;padding:40px 20px;color:var(--text3)}
.empty-icon{font-size:48px;margin-bottom:12px}
.empty-text{font-size:14px}

/* ── TOAST ── */
.toast{position:fixed;bottom:90px;left:50%;transform:translateX(-50%);background:var(--text);color:#fff;padding:12px 24px;border-radius:40px;font-size:13px;font-weight:600;z-index:9999;max-width:90%;text-align:center;animation:toastAnim 2.5s ease forwards}
@keyframes toastAnim{0%{opacity:0;transform:translateX(-50%) translateY(10px)}10%{opacity:1;transform:translateX(-50%) translateY(0)}85%{opacity:1}100%{opacity:0;transform:translateX(-50%) translateY(-10px)}}

/* ── MODAL ── */
.modal{display:none;position:fixed;inset:0;background:rgba(10,22,40,0.55);backdrop-filter:blur(4px);z-index:1000;align-items:flex-end;justify-content:center}
.modal.show{display:flex}
.modal-sheet{background:var(--white);border-radius:28px 28px 0 0;width:100%;max-width:560px;max-height:90vh;overflow-y:auto;padding:8px 0 24px}
.modal-handle{width:40px;height:4px;border-radius:4px;background:var(--border);margin:10px auto 16px}
.modal-title{font-size:17px;font-weight:700;padding:0 20px 14px;border-bottom:1px solid var(--border);color:var(--text)}
.modal-body{padding:16px 20px}
.feedback-grid{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:4px}
.feedback-btn{background:var(--surface2);border:1.5px solid var(--border);padding:14px;border-radius:14px;font-size:13px;font-weight:600;cursor:pointer;color:var(--text);font-family:inherit;transition:all 0.15s}
.feedback-btn:active{transform:scale(0.97)}
.feedback-btn.bad{background:#fff1f3;color:var(--red);border-color:#ffc8d0}
.feedback-btn.good{background:#e6fff2;color:#00a855;border-color:#b8f0d4}

/* ── AUTH ── */
#authContainer{min-height:100vh;display:flex;align-items:center;justify-content:center;padding:20px;background:linear-gradient(160deg,#e8f0ff 0%,#f0f4ff 50%,#dce8ff 100%)}
.auth-card{background:#fff;border-radius:var(--radius-xl);padding:36px 28px;width:100%;max-width:360px;box-shadow:0 12px 60px rgba(26,109,255,0.15);border:1px solid var(--border)}
.auth-logo{text-align:center;margin-bottom:20px}
.auth-logo-icon{width:64px;height:64px;background:linear-gradient(135deg,var(--blue),var(--blue2));border-radius:20px;display:inline-flex;align-items:center;justify-content:center;font-size:32px;color:#fff;margin-bottom:12px}
.auth-title{font-size:22px;font-weight:800;color:var(--text);text-align:center;margin-bottom:4px}
.auth-sub{font-size:13px;color:var(--text3);text-align:center;margin-bottom:24px}
.auth-tabs{display:flex;background:var(--bg);border-radius:30px;padding:4px;margin-bottom:20px}
.auth-tab{flex:1;text-align:center;padding:10px;border-radius:26px;font-size:13px;font-weight:600;cursor:pointer;color:var(--text3);transition:all 0.2s}
.auth-tab.active{background:#fff;color:var(--blue);box-shadow:0 2px 8px rgba(26,109,255,0.12)}
.auth-input{width:100%;padding:14px 18px;border:1.5px solid var(--border);border-radius:14px;font-size:15px;margin-bottom:14px;outline:none;background:var(--bg);color:var(--text);font-family:inherit;transition:border 0.15s}
.auth-input:focus{border-color:var(--blue);background:#fff}
.auth-btn{width:100%;background:linear-gradient(135deg,var(--blue),var(--blue2));color:#fff;border:none;padding:16px;border-radius:14px;font-size:16px;font-weight:700;cursor:pointer;font-family:inherit;margin-top:4px;transition:all 0.15s}
.auth-btn:active{transform:scale(0.98)}
.auth-error{color:var(--red);font-size:12px;text-align:center;margin-top:8px;font-weight:600}

/* ── SPINNER ── */
.spinner{width:36px;height:36px;border:3px solid var(--blue-mid);border-top-color:var(--blue);border-radius:50%;animation:spin 0.8s linear infinite;margin:0 auto 12px}
@keyframes spin{to{transform:rotate(360deg)}}
.loading-text{text-align:center;padding:30px;color:var(--text3);font-size:14px}

/* ── DIVIDER ── */
.divider{height:1px;background:var(--border);margin:0 16px}
hr{border:none;border-top:1px solid var(--border)}
</style>
</head>
<body>

<!-- AUTH -->
<div id="authContainer">
  <div class="auth-card">
    <div class="auth-logo">
      <div class="auth-logo-icon">⚡</div>
      <div class="auth-title">NEON GRID</div>
      <div class="auth-sub">Number Management Platform</div>
    </div>
    <div class="auth-tabs">
      <div class="auth-tab active" id="tabLogin" onclick="switchAuthTab('login')">Login</div>
      <div class="auth-tab" id="tabRegister" onclick="switchAuthTab('register')">Register</div>
    </div>
    <div id="loginForm">
      <input type="text" id="loginUsername" class="auth-input" placeholder="Username" autocomplete="username">
      <input type="password" id="loginPassword" class="auth-input" placeholder="Password" autocomplete="current-password" onkeydown="if(event.key==='Enter')doLogin()">
      <button class="auth-btn" onclick="doLogin()">Sign In</button>
      <div id="loginError" class="auth-error"></div>
    </div>
    <div id="registerForm" style="display:none">
      <input type="text" id="regUsername" class="auth-input" placeholder="Username">
      <input type="password" id="regPassword" class="auth-input" placeholder="Password (min 6 chars)" onkeydown="if(event.key==='Enter')doRegister()">
      <button class="auth-btn" onclick="doRegister()">Create Account</button>
      <div id="regError" class="auth-error"></div>
    </div>
  </div>
</div>

<!-- APP -->
<div id="appContainer" style="display:none">

  <div class="topbar">
    <div class="topbar-logo">⚡ NEON GRID</div>
    <div class="topbar-right">
      <span class="topbar-time" id="currentTime">--:--</span>
      <span id="adminTagEl" style="display:none" class="admin-tag">ADMIN</span>
    </div>
  </div>

  <div class="user-header">
    <div>
      <div class="user-name" id="userName">--</div>
      <div style="font-size:12px;color:var(--text3);margin-top:2px"><span class="online-dot"></span><span id="userId">ID: --</span></div>
    </div>
    <button class="btn btn-ghost btn-sm" onclick="doLogout()">Logout</button>
  </div>

  <!-- HOME PAGE -->
  <div id="homePage" class="page active">
    <div class="sec-header">Quick Access</div>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;padding:0 16px">
      <div class="card" style="margin:0;cursor:pointer;text-align:center" onclick="navigateTo('numbers')">
        <div style="font-size:32px;margin-bottom:8px">📱</div>
        <div style="font-weight:700;font-size:14px;color:var(--blue)">Get Number</div>
        <div style="font-size:11px;color:var(--text3);margin-top:3px">Assign a new number</div>
      </div>
      <div class="card" style="margin:0;cursor:pointer;text-align:center" onclick="navigateTo('saved')">
        <div style="font-size:32px;margin-bottom:8px">💾</div>
        <div style="font-weight:700;font-size:14px;color:var(--blue)">Saved Numbers</div>
        <div style="font-size:11px;color:var(--text3);margin-top:3px">Pending & ready pools</div>
      </div>
      <div class="card" style="margin:0;cursor:pointer;text-align:center" onclick="navigateTo('history')">
        <div style="font-size:32px;margin-bottom:8px">📜</div>
        <div style="font-weight:700;font-size:14px;color:var(--blue)">OTP History</div>
        <div style="font-size:11px;color:var(--text3);margin-top:3px">View all OTPs received</div>
      </div>
      <div class="card" style="margin:0;cursor:pointer;text-align:center" onclick="navigateTo('numbers');setTimeout(()=>document.getElementById('prefixFilter').focus(),300)">
        <div style="font-size:32px;margin-bottom:8px">🔍</div>
        <div style="font-weight:700;font-size:14px;color:var(--blue)">Browse Pools</div>
        <div style="font-size:11px;color:var(--text3);margin-top:3px">Filter by prefix</div>
      </div>
    </div>

    <div class="sec-header" style="margin-top:8px">Custom Links</div>
    <div id="customButtonsList" style="padding:0 16px;display:flex;flex-wrap:wrap;gap:8px"></div>
  </div>

  <!-- NUMBERS PAGE -->
  <div id="numbersPage" class="page">
    <div class="card card-blue" style="margin-top:16px">
      <div class="card-title">YOUR ACTIVE NUMBER</div>
      <div class="number-display mono" id="currentNumber" onclick="copyNumber()" title="Tap to copy">—</div>
      <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px">
        <span class="pool-tag" id="currentRegionTag">No region selected</span>
        <div class="btn-row">
          <button class="btn btn-sm" style="background:rgba(255,255,255,0.2);color:#fff;border:none" onclick="copyNumber()">📋 Copy</button>
          <button class="btn btn-sm" style="background:rgba(255,255,255,0.2);color:#fff;border:none" onclick="changeNumber()">🔄 Change</button>
        </div>
      </div>
    </div>

    <div id="otpDisplayArea" style="display:none"></div>

    <div class="filter-row">
      <input type="text" id="prefixFilter" class="filter-input" placeholder="🔍 Filter by prefix (e.g. 8101)">
      <button class="btn btn-blue btn-sm" onclick="applyFilter()">Filter</button>
    </div>
    <div id="regionList">
      <div class="loading-text"><div class="spinner"></div>Loading regions...</div>
    </div>
  </div>

  <!-- SAVED PAGE -->
  <div id="savedPage" class="page">

    <!-- READY NUMBERS — ONLY ONE POOL VISIBLE AT A TIME -->
    <div id="readySection" style="display:none">
      <div class="sec-header" style="color:var(--green)">✅ READY NUMBERS</div>
      <div id="readyCard"><!-- rendered by JS --></div>
    </div>

    <!-- SAVE NUMBERS FORM -->
    <div class="sec-header">💾 SAVE NUMBERS</div>
    <div style="padding:0 16px 12px">
      <div class="fg">
        <label>Phone Numbers (one per line)</label>
        <textarea id="savedNumbersInput" rows="4" placeholder="e.g.&#10;2349157338416&#10;2349167577481" style="resize:vertical"></textarea>
      </div>
      <div class="fg">
        <label>Timer</label>
        <input type="text" id="timerInput" placeholder="e.g. 8h, 30m, 1d, 4h" value="30m">
        <div class="preset-row">
          <button class="preset-btn" onclick="setTimer('30m')">30m</button>
          <button class="preset-btn" onclick="setTimer('1h')">1h</button>
          <button class="preset-btn" onclick="setTimer('2h')">2h</button>
          <button class="preset-btn" onclick="setTimer('4h')">4h</button>
          <button class="preset-btn" onclick="setTimer('8h')">8h</button>
          <button class="preset-btn" onclick="setTimer('1d')">1d</button>
        </div>
      </div>
      <div class="fg">
        <label>Pool Name</label>
        <input type="text" id="poolNameInput" placeholder="e.g. Nigeria, uk, USA">
      </div>
      <button class="btn btn-blue btn-full" onclick="saveNumbers()">💾 Save Numbers</button>
    </div>

    <!-- PENDING POOLS -->
    <div id="pendingSection" style="display:none">
      <div class="sec-header">⏳ PENDING POOLS</div>
      <div id="pendingList"></div>
    </div>
  </div>

  <!-- HISTORY PAGE -->
  <div id="historyPage" class="page">
    <div class="sec-header">📜 OTP HISTORY</div>
    <div id="historyList">
      <div class="loading-text"><div class="spinner"></div>Loading...</div>
    </div>
  </div>

  <!-- ADMIN PAGE -->
  <div id="adminPage" class="page hidden" style="display:none">
    <div class="sec-header">🛠️ ADMIN PANEL</div>
    <div class="admin-grid">
      <div class="admin-card" onclick="adminView('stats')"><div class="admin-card-icon">📊</div><div class="admin-card-label">Stats</div></div>
      <div class="admin-card" onclick="openCreatePoolModal()"><div class="admin-card-icon">➕</div><div class="admin-card-label">Create Pool</div></div>
      <div class="admin-card" onclick="openUploadModal()"><div class="admin-card-icon">📁</div><div class="admin-card-label">Upload Numbers</div></div>
      <div class="admin-card" onclick="adminView('users')"><div class="admin-card-icon">👥</div><div class="admin-card-label">Users</div></div>
      <div class="admin-card" onclick="adminView('bad')"><div class="admin-card-icon">🚫</div><div class="admin-card-label">Bad Numbers</div></div>
      <div class="admin-card" onclick="adminView('reviews')"><div class="admin-card-icon">⭐</div><div class="admin-card-label">Reviews</div></div>
      <div class="admin-card" onclick="adminView('broadcast')"><div class="admin-card-icon">📢</div><div class="admin-card-label">Broadcast</div></div>
      <div class="admin-card" onclick="adminView('settings')"><div class="admin-card-icon">⚙️</div><div class="admin-card-label">Settings</div></div>
      <div class="admin-card" onclick="adminView('buttons')"><div class="admin-card-icon">🔗</div><div class="admin-card-label">Custom Buttons</div></div>
      <div class="admin-card" onclick="adminView('access')"><div class="admin-card-icon">🔒</div><div class="admin-card-label">Pool Access</div></div>
    </div>

    <div id="adminContent" class="admin-panel" style="display:none">
      <div id="adminContentInner" style="padding:16px"></div>
    </div>

    <div class="sec-header" style="margin-top:8px">📋 POOLS</div>
    <div id="poolsList"></div>
  </div>

  <div class="bottom-nav">
    <div class="nav-item active" data-page="home"><div class="nav-icon">🏠</div><div class="nav-label">Home</div></div>
    <div class="nav-item" data-page="numbers"><div class="nav-icon">📱</div><div class="nav-label">Numbers</div></div>
    <div class="nav-item" data-page="saved"><div class="nav-icon">💾</div><div class="nav-label">Saved</div></div>
    <div class="nav-item" data-page="history"><div class="nav-icon">📜</div><div class="nav-label">History</div></div>
    <div class="nav-item" data-page="admin" id="adminNavItem" style="display:none"><div class="nav-icon">⚙️</div><div class="nav-label">Admin</div></div>
  </div>
</div>

<!-- FEEDBACK MODAL -->
<div id="feedbackModal" class="modal">
  <div class="modal-sheet">
    <div class="modal-handle"></div>
    <div class="modal-title">Rate Your Number</div>
    <div class="modal-body">
      <div style="font-family:'JetBrains Mono',monospace;font-size:18px;font-weight:700;color:var(--blue);text-align:center;margin-bottom:16px" id="feedbackNumber"></div>
      <div class="feedback-grid">
        <button class="feedback-btn good" onclick="submitFeedback('worked')">✅ Worked</button>
        <button class="feedback-btn bad" onclick="submitFeedback('bad')">❌ Not Available</button>
        <button class="feedback-btn" onclick="submitFeedback('email')">📧 Email Only</button>
        <button class="feedback-btn" onclick="submitFeedback('other_devices')">📱 Other Devices</button>
        <button class="feedback-btn" onclick="submitFeedback('try_later')">⏳ Try Later</button>
        <button class="feedback-btn" onclick="showOtherFeedback()">📝 Other</button>
      </div>
      <div id="otherFeedbackDiv" style="display:none;margin-top:12px">
        <textarea id="otherFeedbackText" rows="2" class="fg" style="width:100%;padding:12px;border:1.5px solid var(--border);border-radius:12px;font-size:14px;font-family:inherit" placeholder="Describe the issue..."></textarea>
        <button class="btn btn-blue btn-full" style="margin-top:8px" onclick="submitFeedback('other')">Submit</button>
      </div>
    </div>
  </div>
</div>

<!-- CHANGE POOL MODAL (for saved/ready numbers) -->
<div id="changePoolModal" class="modal">
  <div class="modal-sheet">
    <div class="modal-handle"></div>
    <div class="modal-title">🌐 Switch Pool</div>
    <div class="modal-body">
      <div style="font-size:12px;color:var(--text3);margin-bottom:4px">Current Number</div>
      <div id="changePoolCurrentNum" class="mono" style="font-size:17px;font-weight:700;color:var(--blue);margin-bottom:16px"></div>
      <div class="fg">
        <label>Switch To Pool</label>
        <select id="changePoolSelect" style="width:100%;padding:12px;border:1.5px solid var(--border);border-radius:12px;background:var(--bg);color:var(--text);font-size:14px;outline:none;font-family:inherit"></select>
      </div>
      <div style="font-size:12px;color:var(--text3);margin-bottom:16px">You'll get the next available number from that pool</div>
      <div class="brow">
        <button class="btn btn-ghost" onclick="closeChangePoolModal()">Cancel</button>
        <button class="btn btn-blue" onclick="submitSwitchPool()">Switch Pool</button>
      </div>
    </div>
  </div>
</div>

<!-- CREATE/EDIT POOL MODAL -->
<div id="poolModal" class="modal">
  <div class="modal-sheet" style="border-radius:28px 28px 0 0;max-height:95vh">
    <div class="modal-handle"></div>
    <div class="modal-title" id="poolModalTitle">Create New Pool</div>
    <div class="modal-body">
      <div class="fg"><label>Pool Name *</label><input type="text" id="poolName" placeholder="e.g. Nigeria"></div>
      <div class="fg"><label>Country Code *</label><input type="text" id="poolCode" placeholder="e.g. 234"></div>
      <div class="fg"><label>OTP Group ID *</label><input type="text" id="poolGroupId" placeholder="e.g. -1001234567890"></div>
      <div class="fg"><label>OTP Link</label><input type="text" id="poolOtpLink" placeholder="https://t.me/your_channel"></div>
      <div class="fg"><label>Match Format</label><input type="text" id="poolMatchFormat" value="5+4" placeholder="e.g. 5+4"></div>
      <div class="fg"><label>Telegram Match Format</label><input type="text" id="poolTelegramMatchFormat" placeholder="Leave blank to use Match Format"></div>
      <div class="fg"><label>Monitoring Mode</label><select id="poolUsesPlatform"><option value="0">0 — Telegram Only 📱</option><option value="1">1 — Platform Only 🖥️</option><option value="2">2 — Both 📱+🖥️</option></select></div>
      <div class="fg"><label>Trick Text (hint for users)</label><textarea id="poolTrickText" rows="2" placeholder="Tips for using numbers..."></textarea></div>
      <div style="display:flex;gap:20px;margin-bottom:18px;flex-wrap:wrap">
        <label style="display:flex;align-items:center;gap:8px;font-size:14px;font-weight:600;cursor:pointer"><input type="checkbox" id="poolAdminOnly"> Admin Only</label>
        <label style="display:flex;align-items:center;gap:8px;font-size:14px;font-weight:600;cursor:pointer"><input type="checkbox" id="poolPaused" onchange="document.getElementById('pauseReasonDiv').style.display=this.checked?'block':'none'"> Paused</label>
      </div>
      <div id="pauseReasonDiv" style="display:none" class="fg"><label>Pause Reason</label><input type="text" id="poolPauseReason" placeholder="Reason for pausing"></div>
      <div class="brow">
        <button class="btn btn-ghost" onclick="closePoolModal()">Cancel</button>
        <button class="btn btn-blue" onclick="savePool()">Save Pool</button>
      </div>
    </div>
  </div>
</div>

<!-- UPLOAD MODAL -->
<div id="uploadModal" class="modal">
  <div class="modal-sheet">
    <div class="modal-handle"></div>
    <div class="modal-title">📁 Upload Numbers</div>
    <div class="modal-body">
      <div class="fg"><label>Select Pool</label><select id="uploadPoolSelect" style="width:100%;padding:12px;border:1.5px solid var(--border);border-radius:12px;background:var(--bg);color:var(--text);font-size:14px;outline:none"></select></div>
      <div class="fg"><label>Upload File (.txt or .csv)</label><input type="file" id="uploadFile" accept=".txt,.csv" style="padding:10px;border:1.5px solid var(--border);border-radius:12px;background:var(--bg)"></div>
      <div class="brow">
        <button class="btn btn-ghost" onclick="closeUploadModal()">Cancel</button>
        <button class="btn btn-blue" onclick="uploadNumbers()">Upload</button>
      </div>
      <div id="uploadResult" style="margin-top:12px"></div>
    </div>
  </div>
</div>

<script>
const API = window.location.origin;
let currentUser = null;
let currentAssignment = null;
let currentPoolId = null;
let ws = null;
let otpAutoTimer = null;
let currentFilter = '';
let allRegions = [];
let savedOtps = {};      // number -> {otp, raw_message}
let currentReadyPool = null;  // currently displayed pool name on save page
let changeTargetId = null;
let currentEditPoolId = null;

// ── UTILS ──────────────────────────────────────────────────────────────────
function showToast(msg) {
  const t = document.createElement('div');
  t.className = 'toast';
  t.textContent = msg;
  document.body.appendChild(t);
  setTimeout(() => t.remove(), 2600);
}
function fmt(n) {
  if (!n || n === '—') return n;
  return n.startsWith('+') ? n : '+' + n;
}
function escH(t) {
  if (!t) return '';
  return String(t).replace(/[&<>"']/g, m => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]));
}
function copyText(t) { navigator.clipboard.writeText(t).then(() => showToast('📋 Copied!')); }
function setTimer(v) { document.getElementById('timerInput').value = v; }
function parseTimer(s) {
  const m = s.match(/^(\d+)([smhd])$/i);
  if (m) {
    const n = parseInt(m[1]), u = m[2].toLowerCase();
    if (u === 's') return Math.max(1, Math.ceil(n / 60));
    if (u === 'h') return n * 60;
    if (u === 'd') return n * 1440;
    return n;
  }
  return parseInt(s) || 30;
}
function formatCountdown(sec) {
  if (sec <= 0) return '0s';
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  if (h > 0) return `${h}h ${m}m`;
  if (m > 0) return `${m}m ${s}s`;
  return `${s}s`;
}
function timeNow() {
  document.getElementById('currentTime').textContent = new Date().toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'});
}
setInterval(timeNow, 1000);
timeNow();

// ── AUTH ───────────────────────────────────────────────────────────────────
function switchAuthTab(tab) {
  document.getElementById('loginForm').style.display = tab === 'login' ? 'block' : 'none';
  document.getElementById('registerForm').style.display = tab === 'register' ? 'block' : 'none';
  document.getElementById('tabLogin').classList.toggle('active', tab === 'login');
  document.getElementById('tabRegister').classList.toggle('active', tab === 'register');
}

async function doLogin() {
  const username = document.getElementById('loginUsername').value.trim();
  const password = document.getElementById('loginPassword').value;
  const errEl = document.getElementById('loginError');
  errEl.textContent = '';
  try {
    const res = await fetch(`${API}/api/auth/login`, {
      method: 'POST', headers: {'Content-Type':'application/json'}, credentials: 'include',
      body: JSON.stringify({username, password})
    });
    const data = await res.json();
    if (res.ok) { checkAuth(); }
    else { errEl.textContent = data.detail || 'Login failed'; }
  } catch(e) { errEl.textContent = 'Network error — please try again'; }
}

async function doRegister() {
  const username = document.getElementById('regUsername').value.trim();
  const password = document.getElementById('regPassword').value;
  const errEl = document.getElementById('regError');
  errEl.textContent = '';
  if (password.length < 6) { errEl.textContent = 'Password must be at least 6 characters'; return; }
  try {
    const res = await fetch(`${API}/api/auth/register`, {
      method: 'POST', headers: {'Content-Type':'application/json'}, credentials: 'include',
      body: JSON.stringify({username, password})
    });
    const data = await res.json();
    if (res.ok) {
      if (data.approved) checkAuth();
      else errEl.textContent = '✅ Registered! Awaiting admin approval.';
    } else { errEl.textContent = data.detail || 'Registration failed'; }
  } catch(e) { errEl.textContent = 'Network error'; }
}

async function doLogout() {
  try { await fetch(`${API}/api/auth/logout`, {method:'POST', credentials:'include'}); } catch {}
  location.reload();
}

async function checkAuth() {
  try {
    const res = await fetch(`${API}/api/auth/me`, {credentials:'include'});
    if (res.ok) {
      currentUser = await res.json();
      document.getElementById('authContainer').style.display = 'none';
      document.getElementById('appContainer').style.display = 'block';
      document.getElementById('userName').textContent = currentUser.username;
      document.getElementById('userId').textContent = `ID: ${currentUser.id}`;
      if (currentUser.is_admin) {
        document.getElementById('adminTagEl').style.display = 'inline-block';
        document.getElementById('adminNavItem').style.display = 'flex';
        document.getElementById('adminPage').style.display = 'block';
        loadAdminPools();
      }
      connectWebSocket();
      loadRegions();
      loadCurrentAssignment();
      loadCustomButtons();
      loadSavedPage();
      return true;
    }
  } catch {}
  document.getElementById('authContainer').style.display = 'flex';
  document.getElementById('appContainer').style.display = 'none';
  return false;
}

// ── WEBSOCKET ──────────────────────────────────────────────────────────────
function connectWebSocket() {
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  ws = new WebSocket(`${proto}//${location.host}/ws/user/${currentUser.id}`);
  ws.onmessage = (e) => {
    const data = JSON.parse(e.data);
    if (data.type === 'otp') showOtpOnNumberPage(data);
    else if (data.type === 'saved_otp') showSavedOtp(data);
    else if (data.type === 'broadcast') showToast(`📢 ${data.message}`);
    else if (data.type === 'number_ready') { showToast(data.message); loadSavedPage(); }
    else if (data.type === 'notification') showToast(data.message);
  };
  ws.onclose = () => setTimeout(connectWebSocket, 5000);
}

function showOtpOnNumberPage(data) {
  const container = document.getElementById('otpDisplayArea');
  if (otpAutoTimer) clearTimeout(otpAutoTimer);
  container.innerHTML = `<div class="otp-card" style="margin:0 16px 12px">
    <div class="otp-label">🔑 OTP CODE</div>
    <div class="otp-code" onclick="copyText('${escH(data.otp)}')">${escH(data.otp)}</div>
    <div class="otp-countdown" id="otpCountdown">Auto-deletes in 30s</div>
    ${data.raw_message ? `<div class="otp-message">${escH(data.raw_message)}</div>` : ''}
  </div>`;
  container.style.display = 'block';
  let sec = 30;
  const interval = setInterval(() => {
    sec--;
    const el = document.getElementById('otpCountdown');
    if (el) el.textContent = `Auto-deletes in ${sec}s`;
    if (sec <= 0) { clearInterval(interval); container.style.display = 'none'; }
  }, 1000);
  otpAutoTimer = setTimeout(() => { clearInterval(interval); container.style.display = 'none'; }, 30000);
  loadHistory();
}

function showSavedOtp(data) {
  savedOtps[data.number] = {otp: data.otp, raw_message: data.raw_message || ''};
  showToast(`🔑 OTP for ${data.number}: ${data.otp}`);
  renderReadyNumbers();
  setTimeout(() => { delete savedOtps[data.number]; renderReadyNumbers(); }, 30000);
  loadHistory();
}

// ── NAVIGATION ─────────────────────────────────────────────────────────────
function navigateTo(page) {
  document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
  document.getElementById(`${page}Page`).classList.add('active');
  const navEl = document.querySelector(`.nav-item[data-page="${page}"]`);
  if (navEl) navEl.classList.add('active');
  if (page === 'numbers') loadRegions();
  if (page === 'saved') loadSavedPage();
  if (page === 'history') loadHistory();
  if (page === 'admin') loadAdminPools();
}
document.querySelectorAll('.nav-item').forEach(n => n.addEventListener('click', () => navigateTo(n.dataset.page)));

// ── CUSTOM BUTTONS ─────────────────────────────────────────────────────────
async function loadCustomButtons() {
  try {
    const res = await fetch(`${API}/api/buttons`, {credentials:'include'});
    if (!res.ok) return;
    const btns = await res.json();
    const container = document.getElementById('customButtonsList');
    if (!btns.length) { container.innerHTML = ''; return; }
    container.innerHTML = btns.map(b => `<a href="${escH(b.url)}" target="_blank" rel="noopener" class="btn btn-outline btn-sm">${escH(b.label)}</a>`).join('');
  } catch {}
}

// ── NUMBERS PAGE ───────────────────────────────────────────────────────────
async function loadRegions() {
  try {
    const res = await fetch(`${API}/api/pools`, {credentials:'include'});
    if (!res.ok) throw new Error();
    allRegions = await res.json();
    renderRegions();
  } catch(e) {
    document.getElementById('regionList').innerHTML = '<div class="loading-text">Failed to load regions. Check connection.</div>';
  }
}

function renderRegions() {
  const container = document.getElementById('regionList');
  let list = allRegions;
  if (currentFilter) {
    const f = currentFilter.toLowerCase();
    list = allRegions.filter(r => r.name.toLowerCase().includes(f) || r.country_code.includes(f));
  }
  if (!list.length) { container.innerHTML = '<div class="loading-text">No regions found</div>'; return; }
  container.innerHTML = list.map(r => `
    <div class="region-item" onclick="${r.is_paused ? `showToast('Paused: ${escH(r.pause_reason || 'Temporarily unavailable')}')` : `selectRegion(${r.id})`}">
      <div>
        <div class="region-name">${escH(r.name)}</div>
        <div class="region-code">+${escH(r.country_code)}</div>
        ${r.trick_text ? `<div class="region-trick">💡 ${escH(r.trick_text)}</div>` : ''}
      </div>
      <div>
        ${r.is_paused ? `<span class="region-paused">⏸ Paused</span>` : `<span class="region-count">${r.number_count}</span>`}
      </div>
    </div>`).join('');
}

function applyFilter() {
  currentFilter = document.getElementById('prefixFilter').value.trim();
  renderRegions();
}

async function selectRegion(poolId) {
  try {
    const res = await fetch(`${API}/api/pools/assign`, {
      method:'POST', headers:{'Content-Type':'application/json'}, credentials:'include',
      body: JSON.stringify({pool_id: poolId})
    });
    const data = await res.json();
    if (res.ok) {
      currentAssignment = data;
      currentPoolId = data.pool_id;
      document.getElementById('currentNumber').textContent = fmt(data.number);
      document.getElementById('currentRegionTag').textContent = data.pool_name;
      document.getElementById('otpDisplayArea').style.display = 'none';
      if (otpAutoTimer) clearTimeout(otpAutoTimer);
      showToast(`✅ Assigned: ${fmt(data.number)}`);
      loadRegions();
    } else { showToast(data.detail || 'Failed to assign number'); }
  } catch(e) { showToast('Network error'); }
}

async function loadCurrentAssignment() {
  try {
    const res = await fetch(`${API}/api/pools/my-assignment`, {credentials:'include'});
    const data = await res.json();
    if (data.assignment) {
      currentAssignment = data.assignment;
      currentPoolId = data.assignment.pool_id;
      document.getElementById('currentNumber').textContent = fmt(currentAssignment.number);
      document.getElementById('currentRegionTag').textContent = currentAssignment.pool_name;
    }
  } catch {}
}

function copyNumber() {
  if (currentAssignment) copyText(fmt(currentAssignment.number));
  else showToast('No active number');
}

function changeNumber() {
  if (!currentAssignment) { showToast('No active number to change'); return; }
  document.getElementById('feedbackNumber').textContent = fmt(currentAssignment.number);
  document.getElementById('otherFeedbackDiv').style.display = 'none';
  document.getElementById('otherFeedbackText').value = '';
  document.getElementById('feedbackModal').classList.add('show');
}

function showOtherFeedback() { document.getElementById('otherFeedbackDiv').style.display = 'block'; }

async function submitFeedback(type) {
  const comment = type === 'other' ? document.getElementById('otherFeedbackText').value : type;
  const markAsBad = type === 'bad';
  document.getElementById('feedbackModal').classList.remove('show');
  if (!currentAssignment) return;

  const savedAssignment = currentAssignment;
  const savedPoolId = currentPoolId;
  document.getElementById('currentNumber').textContent = '...';

  const releaseP = fetch(`${API}/api/pools/release/${savedAssignment.assignment_id}`, {method:'POST', credentials:'include'});
  fetch(`${API}/api/reviews`, {
    method:'POST', headers:{'Content-Type':'application/json'}, credentials:'include',
    body: JSON.stringify({number: savedAssignment.number, rating: markAsBad ? 1 : 4, comment, mark_as_bad: markAsBad})
  });
  await releaseP;

  if (savedPoolId) {
    const res = await fetch(`${API}/api/pools/assign`, {
      method:'POST', headers:{'Content-Type':'application/json'}, credentials:'include',
      body: JSON.stringify({pool_id: savedPoolId})
    });
    const data = await res.json();
    if (res.ok) {
      currentAssignment = data;
      document.getElementById('currentNumber').textContent = fmt(data.number);
      document.getElementById('currentRegionTag').textContent = data.pool_name;
      document.getElementById('otpDisplayArea').style.display = 'none';
      showToast(`✅ New number: ${fmt(data.number)}`);
    } else {
      showToast('No more numbers in this pool');
      currentAssignment = null; currentPoolId = null;
      document.getElementById('currentNumber').textContent = '—';
      document.getElementById('currentRegionTag').textContent = 'No region selected';
    }
    loadRegions();
  }
}

// ── SAVED PAGE ─────────────────────────────────────────────────────────────
async function loadSavedPage() {
  await Promise.all([loadReadyNumbers(), loadPendingNumbers()]);
}

async function loadReadyNumbers() {
  try {
    const res = await fetch(`${API}/api/saved/ready`, {credentials:'include'});
    const data = await res.json();
    const grouped = {};
    data.forEach(item => {
      if (!grouped[item.pool_name]) grouped[item.pool_name] = [];
      grouped[item.pool_name].push(item);
    });

    const poolNames = Object.keys(grouped);
    const section = document.getElementById('readySection');

    if (!poolNames.length) {
      section.style.display = 'none';
      document.getElementById('readyCard').innerHTML = '';
      currentReadyPool = null;
      return;
    }

    section.style.display = 'block';

    // If current pool is gone or not set, pick first
    if (!currentReadyPool || !grouped[currentReadyPool]) {
      currentReadyPool = poolNames[0];
    }

    renderReadyNumbers(grouped);
  } catch {}
}

function renderReadyNumbers(grouped) {
  // If no grouped passed, re-fetch
  if (!grouped) { loadReadyNumbers(); return; }

  const poolNames = Object.keys(grouped);
  if (!poolNames.length) {
    document.getElementById('readySection').style.display = 'none';
    document.getElementById('readyCard').innerHTML = '';
    return;
  }
  if (!currentReadyPool || !grouped[currentReadyPool]) {
    currentReadyPool = poolNames[0];
  }

  const items = grouped[currentReadyPool];
  const front = items[0];
  const qCount = items.length;
  const otpInfo = savedOtps[front.number];

  let otpHtml = '';
  if (otpInfo) {
    otpHtml = `<div style="background:linear-gradient(135deg,var(--green),#00a855);border-radius:14px;padding:14px;color:#fff;margin-top:12px;text-align:center">
      <div style="font-size:11px;opacity:0.8;margin-bottom:4px">🔑 OTP CODE</div>
      <div class="mono" style="font-size:32px;font-weight:800;cursor:pointer;letter-spacing:4px" onclick="copyText('${escH(otpInfo.otp)}')">${escH(otpInfo.otp)}</div>
      ${otpInfo.raw_message ? `<div style="font-size:11px;opacity:0.75;margin-top:6px;word-break:break-all">${escH(otpInfo.raw_message)}</div>` : ''}
    </div>`;
  }

  const html = `
    <div class="ready-card">
      <div class="ready-card-header">
        <div>
          <div class="ready-card-pool">📦 ${escH(currentReadyPool)}</div>
          <div class="ready-card-queue">${qCount} number${qCount!==1?'s':''} in queue</div>
        </div>
        <span style="background:rgba(255,255,255,0.25);padding:4px 12px;border-radius:20px;font-size:11px;font-weight:700;color:#fff">READY</span>
      </div>
      <div class="ready-card-body">
        <div style="font-size:11px;color:var(--text3);margin-bottom:6px">Slot 1 of ${qCount} — tap to copy</div>
        <div class="ready-number" onclick="copyText('${escH(front.number)}')">${escH(front.number)} 📋</div>
        <div class="btn-row" style="margin-top:12px">
          <button class="btn btn-blue btn-sm" onclick="doChangeNumber(${front.id}, '${escH(currentReadyPool).replace(/'/g,"\\'")}')">🔄 Change Number</button>
          <button class="btn btn-ghost btn-sm" onclick="openChangePoolModal(${front.id}, '${escH(front.number).replace(/'/g,"\\'")}', '${escH(currentReadyPool).replace(/'/g,"\\'")}')">🌐 Change Pool</button>
        </div>
        ${poolNames.length > 1 ? `<div style="margin-top:10px;font-size:12px;color:var(--text3)">Other pools: ${poolNames.filter(p=>p!==currentReadyPool).map(p=>`<span style="cursor:pointer;color:var(--blue);font-weight:600;margin-left:6px" onclick="switchReadyPool('${escH(p).replace(/'/g,"\\'")}')">${escH(p)} (${grouped[p].length})</span>`).join('')}</div>` : ''}
        ${otpHtml}
      </div>
    </div>`;
  document.getElementById('readyCard').innerHTML = html;
}

function switchReadyPool(poolName) {
  currentReadyPool = poolName;
  loadReadyNumbers();
}

async function doChangeNumber(id, poolName) {
  try {
    const res = await fetch(`${API}/api/saved/${id}/next-number`, {method:'POST', credentials:'include'});
    const data = await res.json();
    if (res.ok) {
      showToast(`✅ New number: ${data.number}`);
      delete savedOtps[data.number];
      await triggerSavedMonitor(data.number);
      loadReadyNumbers();
    } else { showToast(data.detail || 'No more numbers in this pool'); }
  } catch(e) { showToast('Network error'); }
}

async function triggerSavedMonitor(number) {
  try {
    const res = await fetch(`${API}/api/saved/trigger-monitor`, {
      method:'POST', headers:{'Content-Type':'application/json'}, credentials:'include',
      body: JSON.stringify({number})
    });
    if (res.ok) showToast('👁️ Monitoring started...');
  } catch {}
}

async function openChangePoolModal(id, number, currentPool) {
  changeTargetId = id;
  document.getElementById('changePoolCurrentNum').textContent = number;
  try {
    const res = await fetch(`${API}/api/saved/ready-pools`, {credentials:'include'});
    const pools = await res.json();
    const opts = pools.filter(p => p.pool_name !== currentPool && p.count > 0);
    if (!opts.length) { showToast('No other pools with available numbers'); return; }
    document.getElementById('changePoolSelect').innerHTML = opts.map(p => `<option value="${escH(p.pool_name)}">${escH(p.pool_name)} (${p.count} available)</option>`).join('');
    document.getElementById('changePoolModal').classList.add('show');
  } catch(e) { showToast('Failed to load pools'); }
}

function closeChangePoolModal() { document.getElementById('changePoolModal').classList.remove('show'); }

async function submitSwitchPool() {
  const newPool = document.getElementById('changePoolSelect').value;
  if (!newPool) { showToast('Select a pool'); return; }
  try {
    const res = await fetch(`${API}/api/saved/${changeTargetId}/switch-pool?new_pool_name=${encodeURIComponent(newPool)}`, {method:'POST', credentials:'include'});
    const data = await res.json();
    if (res.ok) {
      currentReadyPool = newPool;
      showToast(`✅ Switched to ${newPool}: ${data.number}`);
      closeChangePoolModal();
      await triggerSavedMonitor(data.number);
      loadReadyNumbers();
    } else { showToast(data.detail || 'Switch failed'); }
  } catch(e) { showToast('Network error'); }
}

async function loadPendingNumbers() {
  try {
    const res = await fetch(`${API}/api/saved`, {credentials:'include'});
    const data = await res.json();
    const pending = data.filter(item => !item.moved);
    const section = document.getElementById('pendingSection');
    const container = document.getElementById('pendingList');

    if (!pending.length) {
      section.style.display = 'none';
      container.innerHTML = '';
      return;
    }

    section.style.display = 'block';

    // Group by pool_name
    const grouped = {};
    pending.forEach(item => {
      if (!grouped[item.pool_name]) grouped[item.pool_name] = {items: [], minSec: Infinity, status: 'green'};
      grouped[item.pool_name].items.push(item);
      if (item.seconds_left < grouped[item.pool_name].minSec) {
        grouped[item.pool_name].minSec = item.seconds_left;
        grouped[item.pool_name].status = item.status;
      }
    });

    container.innerHTML = Object.entries(grouped).map(([poolName, g]) => {
      const count = g.items.length;
      const badgeClass = g.status === 'red' ? 'badge-red' : (g.status === 'yellow' ? 'badge-amber' : 'badge-green');
      const timeStr = g.status === 'expired' ? 'Expiring...' : formatCountdown(g.minSec);
      const ids = g.items.map(i => i.id);
      return `
        <div class="pending-pool">
          <div class="pending-pool-header">
            <div>
              <div class="pending-pool-name">📦 ${escH(poolName)}</div>
              <div class="pending-pool-count">${count} number${count!==1?'s':''} pending</div>
            </div>
            <div style="display:flex;align-items:center;gap:8px">
              <span class="badge ${badgeClass}">⏱ ${timeStr}</span>
              <button onclick="deleteSavedPool(${JSON.stringify(ids).replace(/"/g,'&quot;')})" style="background:none;border:none;cursor:pointer;font-size:18px" title="Delete pool">🗑️</button>
            </div>
          </div>
        </div>`;
    }).join('');

    // Start countdown updates
    startCountdownUpdates();
  } catch {}
}

let countdownInterval = null;
function startCountdownUpdates() {
  if (countdownInterval) clearInterval(countdownInterval);
  countdownInterval = setInterval(() => {
    // Just reload saved page every 60s
  }, 60000);
}

async function saveNumbers() {
  const raw = document.getElementById('savedNumbersInput').value;
  const numbers = raw.split('\n').map(l => l.trim()).filter(l => l);
  const timerRaw = document.getElementById('timerInput').value.trim();
  const timerMinutes = parseTimer(timerRaw);
  const poolName = document.getElementById('poolNameInput').value.trim() || 'Default Pool';
  if (!numbers.length) { showToast('Enter at least one number'); return; }
  try {
    const res = await fetch(`${API}/api/saved`, {
      method:'POST', headers:{'Content-Type':'application/json'}, credentials:'include',
      body: JSON.stringify({numbers, timer_minutes: timerMinutes, pool_name: poolName})
    });
    const data = await res.json();
    if (res.ok) {
      showToast(`✅ Saved ${data.saved} number${data.saved!==1?'s':''} to "${poolName}" pool`);
      document.getElementById('savedNumbersInput').value = '';
      loadSavedPage();
    } else { showToast(data.detail || 'Failed to save'); }
  } catch(e) { showToast('Network error'); }
}

async function deleteSavedPool(ids) {
  if (!confirm(`Delete all ${ids.length} number(s) in this pool?`)) return;
  for (const id of ids) {
    try { await fetch(`${API}/api/saved/${id}`, {method:'DELETE', credentials:'include'}); } catch {}
  }
  loadSavedPage();
}

// ── HISTORY ────────────────────────────────────────────────────────────────
async function loadHistory() {
  try {
    const res = await fetch(`${API}/api/otp/my`, {credentials:'include'});
    const data = await res.json();
    const container = document.getElementById('historyList');
    if (!data.length) {
      container.innerHTML = '<div class="empty-state"><div class="empty-icon">📭</div><div class="empty-text">No OTP history yet</div></div>';
      return;
    }
    container.innerHTML = data.map(item => `
      <div class="history-item">
        <div class="history-num">${escH(item.number)}</div>
        <div class="history-otp" onclick="copyText('${escH(item.otp_code)}')">${escH(item.otp_code)} 📋</div>
        <div class="history-time">${new Date(item.delivered_at).toLocaleString()}</div>
      </div>`).join('');
  } catch {}
}

// ── ADMIN ──────────────────────────────────────────────────────────────────
const ADMIN_SECTIONS = ['stats','users','bad','reviews','broadcast','settings','buttons','access'];

function adminView(section) {
  const content = document.getElementById('adminContent');
  const inner = document.getElementById('adminContentInner');
  content.style.display = 'block';

  if (section === 'stats') {
    fetch(`${API}/api/admin/stats`, {credentials:'include'}).then(r=>r.json()).then(s => {
      inner.innerHTML = `<div style="display:grid;grid-template-columns:1fr 1fr;gap:12px">
        ${[['👥 Users',s.total_users,'blue'],['⏳ Pending',s.pending_approval,'amber'],
           ['🌍 Pools',s.total_pools,'blue'],['📞 Numbers',s.total_numbers,'green'],
           ['🔑 OTPs',s.total_otps,'green'],['🚫 Bad',s.bad_numbers,'red'],
           ['💾 Saved',s.saved_numbers,'blue'],['🟢 Online',s.online_users,'green']
        ].map(([label,val,color])=>`<div style="background:var(--bg);border-radius:14px;padding:14px;border:1px solid var(--border)">
          <div style="font-size:12px;color:var(--text3)">${label}</div>
          <div style="font-size:24px;font-weight:800;color:var(--${color==='green'?'green':color==='red'?'red':'blue'})">${val}</div>
        </div>`).join('')}
      </div>`;
    });
  } else if (section === 'users') {
    fetch(`${API}/api/admin/users`, {credentials:'include'}).then(r=>r.json()).then(users => {
      inner.innerHTML = `<div style="font-weight:700;margin-bottom:12px;font-size:16px">👥 Users (${users.length})</div>` +
        (users.length ? users.map(u => {
          const status = u.is_admin ? '👑 Admin' : (u.is_blocked ? '🚫 Blocked' : (u.is_approved ? '✅ Active' : '⏳ Pending'));
          const approveBtn = (!u.is_approved && !u.is_blocked && !u.is_admin) ? `<button class="btn btn-green btn-xs" onclick="adminApprove(${u.id})">Approve</button>` : '';
          const blockBtn = u.is_admin ? '' : (u.is_blocked ? `<button class="btn btn-blue btn-xs" onclick="adminToggleUser(${u.id},false)">Unblock</button>` : `<button class="btn btn-red btn-xs" onclick="adminToggleUser(${u.id},true)">Block</button>`);
          const denyBtn = (!u.is_approved && !u.is_blocked && !u.is_admin) ? `<button class="btn btn-ghost btn-xs" onclick="adminDeny(${u.id})">Deny</button>` : '';
          return `<div class="list-item">
            <div><div class="item-title">${escH(u.username)}</div><div class="item-sub">ID: ${u.id} · ${status}</div></div>
            <div class="btn-row">${approveBtn}${denyBtn}${blockBtn}</div>
          </div>`;
        }).join('') : '<div class="empty-state">No users found</div>');
    });
  } else if (section === 'bad') {
    fetch(`${API}/api/admin/bad-numbers`, {credentials:'include'}).then(r=>r.json()).then(bad => {
      inner.innerHTML = `<div style="font-weight:700;margin-bottom:12px;font-size:16px">🚫 Bad Numbers (${bad.length})</div>` +
        (bad.length ? bad.map(b => `<div class="list-item">
          <div><div class="item-title mono">${escH(b.number)}</div><div class="item-sub">${escH(b.reason||'')}</div></div>
          <button class="btn btn-blue btn-xs" onclick="adminRemoveBad('${escH(b.number)}')">Remove</button>
        </div>`).join('') : '<div class="empty-state">No bad numbers 🎉</div>');
    });
  } else if (section === 'reviews') {
    fetch(`${API}/api/admin/reviews`, {credentials:'include'}).then(r=>r.json()).then(reviews => {
      inner.innerHTML = `<div style="font-weight:700;margin-bottom:12px;font-size:16px">⭐ Reviews (${reviews.length})</div>` +
        (reviews.length ? reviews.map(r => `<div class="list-item">
          <div><div class="item-title mono">${escH(r.number)}</div><div class="item-sub">${'⭐'.repeat(Math.max(0,r.rating))} · ${escH(r.comment||'No comment')}</div></div>
        </div>`).join('') : '<div class="empty-state">No reviews yet</div>');
    });
  } else if (section === 'broadcast') {
    inner.innerHTML = `<div style="font-weight:700;margin-bottom:12px;font-size:16px">📢 Broadcast Message</div>
      <div class="fg"><label>Message</label><textarea id="broadcastMsg" rows="4" placeholder="Type your message..."></textarea></div>
      <button class="btn btn-blue" onclick="sendBroadcast()">Send to All Users</button>`;
  } else if (section === 'settings') {
    fetch(`${API}/api/admin/stats`, {credentials:'include'}).then(() => {
      inner.innerHTML = `<div style="font-weight:700;margin-bottom:12px;font-size:16px">⚙️ Settings</div>
        <div class="fg"><label>Approval Mode</label><select id="approvalMode"><option value="on">ON — New users need approval</option><option value="off">OFF — All users can access</option></select></div>
        <div class="fg"><label>OTP Redirect</label><select id="otpRedirect"><option value="pool">Per-Pool Link</option><option value="hardcoded">Hardcoded: t.me/earnplusz</option></select></div>
        <button class="btn btn-blue" onclick="saveSettings()">Save Settings</button>`;
    });
  } else if (section === 'buttons') {
    loadAdminButtons();
  } else if (section === 'access') {
    loadPoolAccessAdmin();
  }
}

async function adminApprove(userId) {
  await fetch(`${API}/api/admin/users/${userId}/approve`, {method:'POST', credentials:'include'});
  showToast('✅ User approved'); adminView('users');
}
async function adminDeny(userId) {
  await fetch(`${API}/api/admin/users/${userId}/deny`, {method:'POST', credentials:'include'});
  showToast('User denied'); adminView('users');
}
async function adminToggleUser(userId, block) {
  const url = block ? `/api/admin/users/${userId}/block` : `/api/admin/users/${userId}/unblock`;
  await fetch(`${API}${url}`, {method:'POST', credentials:'include'});
  showToast(block ? '🚫 User blocked' : '✅ User unblocked'); adminView('users');
}
async function adminRemoveBad(number) {
  await fetch(`${API}/api/admin/bad-numbers?number=${encodeURIComponent(number)}`, {method:'DELETE', credentials:'include'});
  showToast('Removed from bad numbers'); adminView('bad');
}
async function sendBroadcast() {
  const msg = document.getElementById('broadcastMsg').value.trim();
  if (!msg) { showToast('Enter a message'); return; }
  const res = await fetch(`${API}/api/admin/broadcast?message=${encodeURIComponent(msg)}`, {method:'POST', credentials:'include'});
  if (res.ok) { showToast('📢 Broadcast sent!'); document.getElementById('broadcastMsg').value = ''; }
  else showToast('Broadcast failed');
}
async function saveSettings() {
  const approval = document.getElementById('approvalMode').value;
  const redirect = document.getElementById('otpRedirect').value;
  await fetch(`${API}/api/admin/settings/approval?enabled=${approval==='on'}`, {method:'POST', credentials:'include'});
  await fetch(`${API}/api/admin/settings/otp-redirect?mode=${redirect}`, {method:'POST', credentials:'include'});
  showToast('✅ Settings saved');
}

// Admin custom buttons
async function loadAdminButtons() {
  const inner = document.getElementById('adminContentInner');
  try {
    const res = await fetch(`${API}/api/buttons`, {credentials:'include'});
    const btns = await res.json();
    inner.innerHTML = `<div style="font-weight:700;margin-bottom:12px;font-size:16px">🔗 Custom Buttons</div>
      ${btns.map(b=>`<div class="list-item"><div><div class="item-title">${escH(b.label)}</div><div class="item-sub">${escH(b.url)}</div></div><button class="btn btn-red btn-xs" onclick="adminDeleteButton(${b.id})">Delete</button></div>`).join('')}
      <div style="margin-top:14px">
        <div class="fg"><label>Label</label><input type="text" id="newBtnLabel" placeholder="e.g. 📢 Join Channel"></div>
        <div class="fg"><label>URL</label><input type="text" id="newBtnUrl" placeholder="https://..."></div>
        <button class="btn btn-blue" onclick="adminAddButton()">Add Button</button>
      </div>`;
  } catch { inner.innerHTML = 'Failed to load'; }
}

async function adminAddButton() {
  const label = document.getElementById('newBtnLabel').value.trim();
  const url = document.getElementById('newBtnUrl').value.trim();
  if (!label || !url) { showToast('Fill in both fields'); return; }
  await fetch(`${API}/api/admin/buttons?label=${encodeURIComponent(label)}&url=${encodeURIComponent(url)}`, {method:'POST', credentials:'include'});
  showToast('Button added'); loadAdminButtons(); loadCustomButtons();
}
async function adminDeleteButton(id) {
  await fetch(`${API}/api/admin/buttons/${id}`, {method:'DELETE', credentials:'include'});
  showToast('Button deleted'); loadAdminButtons(); loadCustomButtons();
}

async function loadPoolAccessAdmin() {
  const inner = document.getElementById('adminContentInner');
  try {
    const poolsRes = await fetch(`${API}/api/pools`, {credentials:'include'});
    const pools = await poolsRes.json();
    inner.innerHTML = `<div style="font-weight:700;margin-bottom:12px;font-size:16px">🔒 Pool Access Control</div>
      <div class="fg"><label>Pool</label><select id="accessPoolSelect">${pools.map(p=>`<option value="${p.id}">${escH(p.name)}</option>`).join('')}</select></div>
      <div class="fg"><label>User ID</label><input type="number" id="accessUserId" placeholder="User ID"></div>
      <div class="btn-row">
        <button class="btn btn-blue btn-sm" onclick="adminGrantAccess()">Grant Access</button>
        <button class="btn btn-red btn-sm" onclick="adminRevokeAccess()">Revoke Access</button>
      </div>`;
  } catch { inner.innerHTML = 'Failed to load'; }
}

async function adminGrantAccess() {
  const poolId = document.getElementById('accessPoolSelect').value;
  const userId = document.getElementById('accessUserId').value;
  if (!poolId || !userId) { showToast('Fill all fields'); return; }
  const res = await fetch(`${API}/api/admin/pools/${poolId}/access/${userId}`, {method:'POST', credentials:'include'});
  if (res.ok) showToast('✅ Access granted');
  else showToast((await res.json()).detail || 'Failed');
}
async function adminRevokeAccess() {
  const poolId = document.getElementById('accessPoolSelect').value;
  const userId = document.getElementById('accessUserId').value;
  if (!poolId || !userId) { showToast('Fill all fields'); return; }
  const res = await fetch(`${API}/api/admin/pools/${poolId}/access/${userId}`, {method:'DELETE', credentials:'include'});
  if (res.ok) showToast('✅ Access revoked');
  else showToast((await res.json()).detail || 'Failed');
}

// ── POOL MANAGEMENT ────────────────────────────────────────────────────────
async function loadAdminPools() {
  try {
    const res = await fetch(`${API}/api/pools`, {credentials:'include'});
    if (!res.ok) throw new Error();
    const pools = await res.json();
    const container = document.getElementById('poolsList');
    if (!pools.length) { container.innerHTML = '<div class="loading-text">No pools — create one above</div>'; return; }
    container.innerHTML = pools.map(p => `
      <div class="list-item column" style="margin:6px 16px">
        <div style="display:flex;justify-content:space-between;align-items:flex-start">
          <div style="flex:1">
            <div class="item-title">${escH(p.name)} <span style="font-size:12px;color:var(--text3)">(+${escH(p.country_code)})</span>${p.is_admin_only?'<span class="badge badge-blue" style="margin-left:6px">🔒 Admin</span>':''}</div>
            <div class="item-sub">${p.number_count} numbers · Mode: ${p.uses_platform} · Format: ${escH(p.match_format)}${p.is_paused?` · <span style="color:var(--red)">⏸ Paused</span>`:''}</div>
            ${p.trick_text?`<div style="font-size:11px;color:var(--amber);margin-top:3px">💡 ${escH(p.trick_text)}</div>`:''}
          </div>
          <div class="btn-row">
            <button class="btn btn-blue btn-xs" onclick="openEditPoolModal(${p.id})">✏️</button>
            <button class="btn btn-red btn-xs" onclick="deletePool(${p.id},'${escH(p.name).replace(/'/g,"\\'")}')">🗑️</button>
          </div>
        </div>
        <div class="btn-row" style="flex-wrap:wrap">
          <button class="btn btn-ghost btn-xs" onclick="cutPoolNumbers(${p.id})">✂️ Cut</button>
          <button class="btn btn-ghost btn-xs" onclick="exportPool(${p.id})">📤 Export</button>
          <button class="btn btn-red btn-xs" onclick="clearPool(${p.id},'${escH(p.name).replace(/'/g,"\\'")}')">🧹 Clear</button>
          <button class="btn btn-ghost btn-xs" onclick="${p.is_paused?`resumePool(${p.id})`:`pausePool(${p.id})`}">${p.is_paused?'▶ Resume':'⏸ Pause'}</button>
          <button class="btn btn-ghost btn-xs" onclick="toggleAdminOnly(${p.id})">${p.is_admin_only?'🔓 Public':'🔒 Admin Only'}</button>
        </div>
      </div>`).join('');
  } catch(e) { document.getElementById('poolsList').innerHTML = '<div class="loading-text">Failed to load pools</div>'; }
}

function openCreatePoolModal() {
  currentEditPoolId = null;
  document.getElementById('poolModalTitle').textContent = 'Create New Pool';
  ['poolName','poolCode','poolGroupId','poolOtpLink','poolTrickText','poolPauseReason'].forEach(id => document.getElementById(id).value = '');
  document.getElementById('poolMatchFormat').value = '5+4';
  document.getElementById('poolTelegramMatchFormat').value = '';
  document.getElementById('poolUsesPlatform').value = '0';
  document.getElementById('poolAdminOnly').checked = false;
  document.getElementById('poolPaused').checked = false;
  document.getElementById('pauseReasonDiv').style.display = 'none';
  document.getElementById('poolModal').classList.add('show');
}

async function openEditPoolModal(poolId) {
  currentEditPoolId = poolId;
  try {
    const res = await fetch(`${API}/api/pools`, {credentials:'include'});
    const pools = await res.json();
    const pool = pools.find(p => p.id === poolId);
    if (!pool) return;
    document.getElementById('poolModalTitle').textContent = `Edit: ${pool.name}`;
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
    match_format: document.getElementById('poolMatchFormat').value.trim() || '5+4',
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
    const res = await fetch(`${API}${url}`, {method, headers:{'Content-Type':'application/json'}, credentials:'include', body:JSON.stringify(data)});
    if (res.ok) {
      showToast(currentEditPoolId ? '✅ Pool updated!' : '✅ Pool created!');
      closePoolModal(); loadAdminPools(); loadRegions();
    } else { const err = await res.json(); showToast(err.detail || 'Failed to save pool'); }
  } catch(e) { showToast('Network error'); }
}

function closePoolModal() { document.getElementById('poolModal').classList.remove('show'); }

async function cutPoolNumbers(poolId) {
  const count = prompt('How many numbers to cut from the top?');
  if (!count || isNaN(parseInt(count))) return;
  const res = await fetch(`${API}/api/admin/pools/${poolId}/cut?count=${parseInt(count)}`, {method:'POST', credentials:'include'});
  const data = await res.json();
  if (res.ok) { showToast(`✂️ Removed ${data.removed} numbers`); loadAdminPools(); }
  else showToast(data.detail || 'Cut failed');
}

function exportPool(poolId) { window.open(`${API}/api/admin/pools/${poolId}/export`, '_blank'); }

async function clearPool(poolId, poolName) {
  if (!confirm(`⚠️ Clear ALL numbers from "${poolName}"?`)) return;
  const res = await fetch(`${API}/api/admin/pools/${poolId}/clear`, {method:'POST', credentials:'include'});
  const data = await res.json();
  if (res.ok) { showToast(`🧹 Cleared ${data.deleted} numbers`); loadAdminPools(); }
  else showToast(data.detail || 'Clear failed');
}

async function pausePool(poolId) {
  const reason = prompt('Pause reason (optional):') || '';
  const res = await fetch(`${API}/api/admin/pools/${poolId}/pause?reason=${encodeURIComponent(reason)}`, {method:'POST', credentials:'include'});
  if (res.ok) { showToast('⏸ Pool paused'); loadAdminPools(); }
  else showToast('Failed');
}

async function resumePool(poolId) {
  const res = await fetch(`${API}/api/admin/pools/${poolId}/resume`, {method:'POST', credentials:'include'});
  if (res.ok) { showToast('▶ Pool resumed'); loadAdminPools(); }
  else showToast('Failed');
}

async function toggleAdminOnly(poolId) {
  const res = await fetch(`${API}/api/admin/pools/${poolId}/toggle-admin-only`, {method:'POST', credentials:'include'});
  const data = await res.json();
  if (res.ok) { showToast(data.is_admin_only ? '🔒 Now Admin Only' : '🔓 Now Public'); loadAdminPools(); }
  else showToast('Toggle failed');
}

async function deletePool(poolId, poolName) {
  if (!confirm(`⚠️ Delete pool "${poolName}" and all its numbers?`)) return;
  try {
    const res = await fetch(`${API}/api/admin/pools/${poolId}`, {method:'DELETE', credentials:'include'});
    if (res.ok) { showToast(`Pool "${poolName}" deleted`); loadAdminPools(); loadRegions(); }
    else showToast('Failed to delete pool');
  } catch(e) { showToast('Network error'); }
}

async function openUploadModal() {
  try {
    const res = await fetch(`${API}/api/pools`, {credentials:'include'});
    const pools = await res.json();
    document.getElementById('uploadPoolSelect').innerHTML = '<option value="">-- Select Pool --</option>' + pools.map(p=>`<option value="${p.id}">${escH(p.name)} (+${p.country_code}) — ${p.number_count} numbers</option>`).join('');
    document.getElementById('uploadResult').innerHTML = '';
    document.getElementById('uploadFile').value = '';
    document.getElementById('uploadModal').classList.add('show');
  } catch(e) { showToast('Failed to load pools'); }
}

function closeUploadModal() { document.getElementById('uploadModal').classList.remove('show'); }

async function uploadNumbers() {
  const poolId = document.getElementById('uploadPoolSelect').value;
  const fileInput = document.getElementById('uploadFile');
  if (!poolId) { showToast('Select a pool'); return; }
  if (!fileInput.files || !fileInput.files[0]) { showToast('Select a file'); return; }
  const formData = new FormData();
  formData.append('file', fileInput.files[0]);
  try {
    const res = await fetch(`${API}/api/admin/pools/${poolId}/upload`, {method:'POST', credentials:'include', body:formData});
    const data = await res.json();
    if (res.ok) {
      document.getElementById('uploadResult').innerHTML = `<div style="background:#e6fff2;border-radius:12px;padding:14px;border:1px solid #b8f0d4;margin-top:8px">
        ✅ Added: <strong>${data.added}</strong><br>
        🚫 Bad skipped: ${data.skipped_bad}<br>
        ⏳ Cooldown skipped: ${data.skipped_cooldown}<br>
        🔁 Duplicates: ${data.duplicates}
      </div>`;
      showToast(`${data.added} numbers uploaded!`); loadAdminPools();
    } else { showToast(data.detail || 'Upload failed'); }
  } catch(e) { showToast('Network error'); }
}

// ── INIT ───────────────────────────────────────────────────────────────────
// Close modals when clicking backdrop
['feedbackModal','changePoolModal','poolModal','uploadModal'].forEach(id => {
  document.getElementById(id).addEventListener('click', (e) => {
    if (e.target.id === id) document.getElementById(id).classList.remove('show');
  });
});

checkAuth();
</script>
</body>
</html>'''

# ═════════════════════════════════════════════════════════════════════════════
#  FASTAPI APP
# ═════════════════════════════════════════════════════════════════════════════

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    asyncio.create_task(scheduler())
    asyncio.create_task(_platform_stock_watcher())
    log.info("✅ NEON GRID started")
    yield

app = FastAPI(title="NEON GRID NETWORK", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[FRONTEND_URL] if FRONTEND_URL != "*" else ["*"],
    allow_credentials=True, allow_methods=["*"], allow_headers=["*"]
)

@app.get("/")
async def serve_frontend():
    return HTMLResponse(content=FRONTEND_HTML)

@app.get("/health")
def health():
    return {"status": "ok", "service": "NEON GRID NETWORK", "timestamp": utcnow().isoformat()}

# ── AUTH ──────────────────────────────────────────────────────────────────────
class RegisterRequest(BaseModel):
    username: str
    password: str

class LoginRequest(BaseModel):
    username: str
    password: str

@app.post("/api/auth/register")
async def register(req: RegisterRequest):
    username = req.username.strip()
    if len(username) < 3:
        raise HTTPException(400, "Username must be at least 3 characters")
    if len(req.password) < 6:
        raise HTTPException(400, "Password must be at least 6 characters")
    if SessionLocal:
        with SessionLocal() as db:
            if db.query(User).filter(User.username == username).first():
                raise HTTPException(400, "Username already taken")
            is_first = db.query(User).count() == 0
            user = User(username=username, password_hash=hash_password(req.password),
                        is_admin=is_first, is_approved=is_first)
            db.add(user)
            db.commit()
            db.refresh(user)
            if is_first:
                token = create_token(user.id)
                store_token(user.id, token)
                resp = JSONResponse({"ok": True, "approved": True, "is_admin": True, "user_id": user.id})
                resp.set_cookie("token", token, httponly=True, samesite="lax", max_age=86400*30, path="/")
                return resp
            return {"ok": True, "approved": False, "message": "Awaiting admin approval"}
    else:
        if any(u["username"] == username for u in users.values()):
            raise HTTPException(400, "Username already taken")
        is_first = len(users) == 1
        uid = _counters["user"]
        _counters["user"] += 1
        users[uid] = {"id": uid, "username": username, "password_hash": hash_password(req.password),
                      "is_admin": is_first, "is_approved": is_first, "is_blocked": False,
                      "created_at": utcnow().isoformat()}
        if is_first:
            token = create_token(uid)
            store_token(uid, token)
            resp = JSONResponse({"ok": True, "approved": True, "is_admin": True, "user_id": uid})
            resp.set_cookie("token", token, httponly=True, samesite="lax", max_age=86400*30, path="/")
            return resp
        return {"ok": True, "approved": False, "message": "Awaiting admin approval"}

@app.post("/api/auth/login")
async def login(req: LoginRequest):
    username = req.username.strip()
    if SessionLocal:
        with SessionLocal() as db:
            user = db.query(User).filter(User.username == username).first()
            if not user or not verify_password(req.password, user.password_hash):
                raise HTTPException(401, "Invalid username or password")
            if user.is_blocked:
                raise HTTPException(403, "Account blocked")
            if not user.is_approved:
                raise HTTPException(403, "Account pending approval")
            token = create_token(user.id)
            store_token(user.id, token)
            resp = JSONResponse({"ok": True, "user_id": user.id, "username": user.username, "is_admin": user.is_admin})
            resp.set_cookie("token", token, httponly=True, samesite="lax", max_age=86400*30, path="/")
            return resp
    else:
        user = next((u for u in users.values() if u["username"] == username), None)
        if not user or not verify_password(req.password, user["password_hash"]):
            raise HTTPException(401, "Invalid username or password")
        if user.get("is_blocked"):
            raise HTTPException(403, "Account blocked")
        if not user.get("is_approved"):
            raise HTTPException(403, "Account pending approval")
        token = create_token(user["id"])
        store_token(user["id"], token)
        resp = JSONResponse({"ok": True, "user_id": user["id"], "username": user["username"], "is_admin": user["is_admin"]})
        resp.set_cookie("token", token, httponly=True, samesite="lax", max_age=86400*30, path="/")
        return resp

@app.post("/api/auth/logout")
async def logout(token: str = Cookie(default=None)):
    if token and SessionLocal:
        with SessionLocal() as db:
            db.query(UserSession).filter(UserSession.token == token).delete()
            db.commit()
    elif token:
        sessions.pop(token, None)
    resp = JSONResponse({"ok": True})
    resp.delete_cookie("token")
    return resp

@app.get("/api/auth/me")
async def me(token: str = Cookie(default=None)):
    user = get_user_from_token(token)
    if not user:
        raise HTTPException(401, "Not authenticated")
    return user

# ── POOLS ─────────────────────────────────────────────────────────────────────
@app.get("/api/pools")
def list_pools(token: str = Cookie(default=None)):
    user = get_user_from_token(token)
    if not user:
        raise HTTPException(401, "Not authenticated")
    if SessionLocal:
        with SessionLocal() as db:
            query = db.query(Pool)
            if not user["is_admin"]:
                query = query.filter(Pool.is_admin_only == False)
            pools_list = query.order_by(Pool.name).all()
            result = []
            for p in pools_list:
                if not has_pool_access(p.id, user["id"]):
                    continue
                count = db.query(ActiveNumber).filter(ActiveNumber.pool_id == p.id).count()
                result.append({
                    "id": p.id, "name": p.name, "country_code": p.country_code,
                    "otp_group_id": p.otp_group_id, "otp_link": p.otp_link,
                    "match_format": p.match_format, "telegram_match_format": p.telegram_match_format,
                    "uses_platform": p.uses_platform, "is_paused": p.is_paused,
                    "pause_reason": p.pause_reason, "trick_text": p.trick_text,
                    "is_admin_only": p.is_admin_only, "number_count": count,
                    "last_restocked": p.last_restocked.isoformat() if p.last_restocked else None
                })
            return result
    else:
        result = []
        for p in pools.values():
            if not user["is_admin"] and p.get("is_admin_only"):
                continue
            if not has_pool_access(p["id"], user["id"]):
                continue
            result.append({**p, "number_count": len(active_numbers.get(p["id"], []))})
        return sorted(result, key=lambda x: x["name"])

@app.post("/api/pools/assign")
async def assign_number(pool_id: int, prefix: Optional[str] = None, token: str = Cookie(default=None)):
    user = get_user_from_token(token)
    if not user:
        raise HTTPException(401, "Not authenticated")
    if not has_pool_access(pool_id, user["id"]):
        raise HTTPException(403, "No access to this pool")

    # Release existing assignment first
    release_assignment(user["id"])

    result = assign_one_number(user["id"], pool_id, prefix)
    if not result:
        raise HTTPException(404, "No numbers available in this pool")

    # Trigger monitor bot
    if result.get("otp_group_id") and result.get("uses_platform", 0) in (0, 2):
        asyncio.create_task(request_monitor_bot(
            number=result["number"], group_id=result["otp_group_id"],
            match_format=result.get("telegram_match_format") or result.get("match_format", "5+4"),
            user_id=user["id"]
        ))
    if result.get("uses_platform", 0) in (1, 2):
        start_platform_monitor(user["id"], result["number"],
                               result.get("telegram_match_format") or result.get("match_format", "5+4"))
    return result

class AssignRequest(BaseModel):
    pool_id: int
    prefix: Optional[str] = None

@app.post("/api/pools/assign")
async def assign_number_body(req: AssignRequest, token: str = Cookie(default=None)):
    user = get_user_from_token(token)
    if not user:
        raise HTTPException(401, "Not authenticated")
    if not has_pool_access(req.pool_id, user["id"]):
        raise HTTPException(403, "No access to this pool")

    release_assignment(user["id"])
    result = assign_one_number(user["id"], req.pool_id, req.prefix)
    if not result:
        raise HTTPException(404, "No numbers available in this pool")

    if result.get("otp_group_id") and result.get("uses_platform", 0) in (0, 2):
        asyncio.create_task(request_monitor_bot(
            number=result["number"], group_id=result["otp_group_id"],
            match_format=result.get("telegram_match_format") or result.get("match_format", "5+4"),
            user_id=user["id"]
        ))
    if result.get("uses_platform", 0) in (1, 2):
        start_platform_monitor(user["id"], result["number"],
                               result.get("telegram_match_format") or result.get("match_format", "5+4"))
    return result

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

# ── ADMIN POOL MANAGEMENT ─────────────────────────────────────────────────────
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
        if any(p["name"] == req.name for p in pools.values()):
            raise HTTPException(400, "Pool name already exists")
        pid = _counters["pool"]
        _counters["pool"] += 1
        pools[pid] = {**req.dict(), "id": pid}
        active_numbers[pid] = []
        return {"ok": True, "id": pid}

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
def delete_pool_endpoint(pool_id: int, token: str = Cookie(default=None)):
    user = get_user_from_token(token)
    if not user or not user["is_admin"]:
        raise HTTPException(403, "Admin only")
    if SessionLocal:
        with SessionLocal() as db:
            db.query(ActiveNumber).filter(ActiveNumber.pool_id == pool_id).delete()
            db.query(Pool).filter(Pool.id == pool_id).delete()
            db.commit()
    else:
        pools.pop(pool_id, None)
        active_numbers.pop(pool_id, None)
    return {"ok": True}

@app.post("/api/admin/pools/{pool_id}/upload")
async def upload_numbers(pool_id: int, file: UploadFile = File(...), token: str = Cookie(default=None)):
    user = get_user_from_token(token)
    if not user or not user["is_admin"]:
        raise HTTPException(403, "Admin only")
    content = await file.read()
    lines = content.decode("utf-8", errors="ignore").splitlines()
    phone_re = re.compile(r'(\+?\d{6,15})')
    numbers = []
    for line in lines:
        m = phone_re.search(line.strip())
        if m:
            numbers.append(m.group(1))
    if not numbers:
        raise HTTPException(400, "No valid numbers found")

    added = 0; skipped_bad = 0; skipped_cooldown = 0; duplicates = 0

    if SessionLocal:
        with SessionLocal() as db:
            bad_set = {b.number for b in db.query(BadNumber).all()}
            filtered = [n for n in numbers if n not in bad_set]
            skipped_bad = len(numbers) - len(filtered)
            cooldown_dups = await get_cooldown_duplicates(filtered)
            filtered = [n for n in filtered if n not in cooldown_dups]
            skipped_cooldown = len(cooldown_dups)
            if filtered:
                now = utcnow()
                CHUNK = 500
                for i in range(0, len(filtered), CHUNK):
                    chunk = filtered[i:i+CHUNK]
                    try:
                        db.execute(text("INSERT INTO active_numbers (pool_id, number, created_at) VALUES (:pool_id, :number, :created_at) ON CONFLICT (number) DO NOTHING"),
                                   [{"pool_id": pool_id, "number": num, "created_at": now} for num in chunk])
                        db.commit()
                    except Exception as e:
                        db.rollback()
                in_pool = {row.number for row in db.query(ActiveNumber.number).filter(ActiveNumber.pool_id == pool_id, ActiveNumber.number.in_(filtered)).all()}
                added = len(in_pool)
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
        new_nums = [n for n in filtered if n not in existing]
        duplicates = len(filtered) - len(new_nums)
        active_numbers.setdefault(pool_id, []).extend(new_nums)
        if pool_id in pools:
            pools[pool_id]["last_restocked"] = utcnow().isoformat()
        added = len(new_nums)

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
    return Response(content="\n".join(numbers), media_type="text/plain",
                    headers={"Content-Disposition": f"attachment; filename=pool_{pool_id}.txt"})

@app.post("/api/admin/pools/{pool_id}/cut")
def cut_numbers(pool_id: int, count: int, token: str = Cookie(default=None)):
    user = get_user_from_token(token)
    if not user or not user["is_admin"]:
        raise HTTPException(403, "Admin only")
    if SessionLocal:
        with SessionLocal() as db:
            nums = db.query(ActiveNumber).filter(ActiveNumber.pool_id == pool_id).limit(count).all()
            removed = len(nums)
            for n in nums: db.delete(n)
            db.commit()
    else:
        nums = active_numbers.get(pool_id, [])
        removed = min(count, len(nums))
        active_numbers[pool_id] = nums[count:]
    return {"ok": True, "removed": removed}

@app.post("/api/admin/pools/{pool_id}/clear")
def clear_pool_endpoint(pool_id: int, token: str = Cookie(default=None)):
    user = get_user_from_token(token)
    if not user or not user["is_admin"]:
        raise HTTPException(403, "Admin only")
    if SessionLocal:
        with SessionLocal() as db:
            deleted = db.query(ActiveNumber).filter(ActiveNumber.pool_id == pool_id).delete()
            db.commit()
    else:
        deleted = len(active_numbers.get(pool_id, []))
        active_numbers[pool_id] = []
    return {"ok": True, "deleted": deleted}

@app.post("/api/admin/pools/{pool_id}/pause")
def pause_pool_endpoint(pool_id: int, reason: str = "", token: str = Cookie(default=None)):
    user = get_user_from_token(token)
    if not user or not user["is_admin"]:
        raise HTTPException(403, "Admin only")
    pool_name = f"Pool {pool_id}"
    if SessionLocal:
        with SessionLocal() as db:
            pool = db.query(Pool).filter(Pool.id == pool_id).first()
            if not pool: raise HTTPException(404, "Pool not found")
            pool.is_paused = True; pool.pause_reason = reason; pool_name = pool.name
            db.commit()
    else:
        if pool_id not in pools: raise HTTPException(404, "Pool not found")
        pools[pool_id]["is_paused"] = True; pools[pool_id]["pause_reason"] = reason
        pool_name = pools[pool_id]["name"]
    asyncio.create_task(broadcast_all({"type": "notification", "message": f"⏸ {pool_name} paused. {reason}"}))
    return {"ok": True}

@app.post("/api/admin/pools/{pool_id}/resume")
def resume_pool_endpoint(pool_id: int, token: str = Cookie(default=None)):
    user = get_user_from_token(token)
    if not user or not user["is_admin"]:
        raise HTTPException(403, "Admin only")
    pool_name = f"Pool {pool_id}"
    if SessionLocal:
        with SessionLocal() as db:
            pool = db.query(Pool).filter(Pool.id == pool_id).first()
            if not pool: raise HTTPException(404, "Pool not found")
            pool.is_paused = False; pool.pause_reason = ""; pool_name = pool.name
            db.commit()
    else:
        if pool_id not in pools: raise HTTPException(404, "Pool not found")
        pools[pool_id]["is_paused"] = False; pools[pool_id]["pause_reason"] = ""
        pool_name = pools[pool_id]["name"]
    asyncio.create_task(broadcast_all({"type": "notification", "message": f"▶ {pool_name} is now available!"}))
    return {"ok": True}

@app.post("/api/admin/pools/{pool_id}/toggle-admin-only")
def toggle_admin_only_endpoint(pool_id: int, token: str = Cookie(default=None)):
    user = get_user_from_token(token)
    if not user or not user["is_admin"]:
        raise HTTPException(403, "Admin only")
    new_val = False
    if SessionLocal:
        with SessionLocal() as db:
            pool = db.query(Pool).filter(Pool.id == pool_id).first()
            if not pool: raise HTTPException(404, "Pool not found")
            pool.is_admin_only = not pool.is_admin_only; new_val = pool.is_admin_only
            db.commit()
    else:
        if pool_id not in pools: raise HTTPException(404, "Pool not found")
        pools[pool_id]["is_admin_only"] = not pools[pool_id].get("is_admin_only", False)
        new_val = pools[pool_id]["is_admin_only"]
    return {"ok": True, "is_admin_only": new_val}

@app.post("/api/admin/pools/{pool_id}/trick")
def set_trick_text(pool_id: int, trick_text: str, token: str = Cookie(default=None)):
    user = get_user_from_token(token)
    if not user or not user["is_admin"]: raise HTTPException(403, "Admin only")
    if SessionLocal:
        with SessionLocal() as db:
            pool = db.query(Pool).filter(Pool.id == pool_id).first()
            if pool: pool.trick_text = trick_text; db.commit()
    else:
        if pool_id in pools: pools[pool_id]["trick_text"] = trick_text
    return {"ok": True}

# ── POOL ACCESS ───────────────────────────────────────────────────────────────
@app.get("/api/admin/pools/{pool_id}/access")
def get_pool_access(pool_id: int, token: str = Cookie(default=None)):
    user = get_user_from_token(token)
    if not user or not user["is_admin"]: raise HTTPException(403, "Admin only")
    return get_pool_access_users(pool_id)

@app.post("/api/admin/pools/{pool_id}/access/{user_id}")
def grant_access(pool_id: int, user_id: int, token: str = Cookie(default=None)):
    user = get_user_from_token(token)
    if not user or not user["is_admin"]: raise HTTPException(403, "Admin only")
    grant_pool_access(pool_id, user_id)
    return {"ok": True}

@app.delete("/api/admin/pools/{pool_id}/access/{user_id}")
def revoke_access(pool_id: int, user_id: int, token: str = Cookie(default=None)):
    user = get_user_from_token(token)
    if not user or not user["is_admin"]: raise HTTPException(403, "Admin only")
    revoke_pool_access(pool_id, user_id)
    return {"ok": True}

# ── OTP ───────────────────────────────────────────────────────────────────────
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
    log.info(f"[OTP] {payload.number} -> {payload.otp} for user {payload.user_id}")
    await _deliver_otp(payload.user_id, payload.number, payload.otp, payload.raw_message)
    await compliance_record_otp_delivered(payload.user_id)
    return {"ok": True}

@app.get("/api/otp/my")
def my_otps(limit: int = 50, token: str = Cookie(default=None)):
    user = get_user_from_token(token)
    if not user: raise HTTPException(401, "Not authenticated")
    if SessionLocal:
        with SessionLocal() as db:
            logs = db.query(OTPLog).filter(OTPLog.user_id == user["id"]).order_by(OTPLog.delivered_at.desc()).limit(limit).all()
            return [{"id": o.id, "number": o.number, "otp_code": o.otp_code, "raw_message": o.raw_message, "delivered_at": o.delivered_at.isoformat()} for o in logs]
    else:
        user_logs = [o for o in otp_logs if o["user_id"] == user["id"]]
        return sorted(user_logs, key=lambda x: x["delivered_at"], reverse=True)[:limit]

@app.post("/api/otp/search")
async def search_otp(number: str, token: str = Cookie(default=None)):
    user = get_user_from_token(token)
    if not user: raise HTTPException(401, "Not authenticated")
    if SessionLocal:
        with SessionLocal() as db:
            assignment = db.query(Assignment).filter(Assignment.user_id == user["id"], Assignment.number == number).order_by(Assignment.assigned_at.desc()).first()
            if assignment:
                pool = db.query(Pool).filter(Pool.id == assignment.pool_id).first()
                if pool and pool.otp_group_id:
                    await request_search_otp(number=number, group_id=pool.otp_group_id,
                                              match_format=pool.telegram_match_format or pool.match_format, user_id=user["id"])
                    return {"ok": True}
    raise HTTPException(404, "Number not found")

# ── SAVED NUMBERS ─────────────────────────────────────────────────────────────
class SaveRequest(BaseModel):
    numbers: List[str]
    timer_minutes: int
    pool_name: str

@app.post("/api/saved")
def save_numbers_endpoint(req: SaveRequest, token: str = Cookie(default=None)):
    user = get_user_from_token(token)
    if not user: raise HTTPException(401, "Not authenticated")
    expires_at = utcnow() + timedelta(minutes=req.timer_minutes)
    saved = 0
    if SessionLocal:
        with SessionLocal() as db:
            for number in req.numbers:
                number = number.strip()
                if not number: continue
                existing = db.query(SavedNumber).filter(SavedNumber.user_id == user["id"],
                                                         SavedNumber.number == number, SavedNumber.moved == False).first()
                if not existing:
                    db.add(SavedNumber(user_id=user["id"], number=number, pool_name=req.pool_name, expires_at=expires_at))
                    saved += 1
            db.commit()
    else:
        for number in req.numbers:
            number = number.strip()
            if not number: continue
            if any(s["user_id"] == user["id"] and s["number"] == number and not s.get("moved") for s in saved_numbers):
                continue
            saved_numbers.append({"id": _counters["saved"], "user_id": user["id"], "number": number,
                                   "country": "", "pool_name": req.pool_name, "expires_at": expires_at.isoformat(),
                                   "moved": False, "created_at": utcnow().isoformat()})
            _counters["saved"] += 1
            saved += 1
    return {"ok": True, "saved": saved, "expires_at": expires_at.isoformat()}

@app.get("/api/saved")
def list_saved(token: str = Cookie(default=None)):
    user = get_user_from_token(token)
    if not user: raise HTTPException(401, "Not authenticated")
    if SessionLocal:
        with SessionLocal() as db:
            saved = db.query(SavedNumber).filter(SavedNumber.user_id == user["id"]).order_by(SavedNumber.expires_at).all()
            now = utcnow()
            result = []
            for s in saved:
                seconds_left = max(0, int((s.expires_at - now).total_seconds()))
                if s.moved: status = "ready"
                elif seconds_left == 0: status = "expired"
                elif seconds_left < 600: status = "red"
                elif seconds_left < 3600: status = "yellow"
                else: status = "green"
                result.append({"id": s.id, "number": s.number, "country": s.country, "pool_name": s.pool_name,
                                "expires_at": s.expires_at.isoformat(), "seconds_left": seconds_left,
                                "status": status, "moved": s.moved})
            return result
    else:
        user_saved = [s for s in saved_numbers if s["user_id"] == user["id"]]
        now = utcnow()
        result = []
        for s in user_saved:
            expires = datetime.fromisoformat(s["expires_at"])
            seconds_left = max(0, int((expires - now).total_seconds()))
            if s.get("moved"): status = "ready"
            elif seconds_left == 0: status = "expired"
            elif seconds_left < 600: status = "red"
            elif seconds_left < 3600: status = "yellow"
            else: status = "green"
            result.append({"id": s["id"], "number": s["number"], "country": s.get("country",""), "pool_name": s["pool_name"],
                            "expires_at": s["expires_at"], "seconds_left": seconds_left, "status": status, "moved": s.get("moved", False)})
        return sorted(result, key=lambda x: x["expires_at"])

@app.get("/api/saved/ready")
def ready_numbers(token: str = Cookie(default=None)):
    user = get_user_from_token(token)
    if not user: raise HTTPException(401, "Not authenticated")
    if SessionLocal:
        with SessionLocal() as db:
            ready = db.query(SavedNumber).filter(SavedNumber.user_id == user["id"], SavedNumber.moved == True).order_by(SavedNumber.created_at.desc()).all()
            result = []
            for s in ready:
                pool = db.query(Pool).filter(Pool.name == s.pool_name).first()
                result.append({"id": s.id, "number": s.number, "country": s.country, "pool_name": s.pool_name,
                                "pool_id": pool.id if pool else None})
            return result
    else:
        ready = [s for s in saved_numbers if s["user_id"] == user["id"] and s.get("moved", False)]
        ready.sort(key=lambda x: x["created_at"], reverse=True)
        result = []
        for s in ready:
            pool = next((p for p in pools.values() if p["name"] == s["pool_name"]), None)
            result.append({"id": s["id"], "number": s["number"], "country": s.get("country",""),
                            "pool_name": s["pool_name"], "pool_id": pool["id"] if pool else None})
        return result

@app.get("/api/saved/ready-pools")
def ready_pools(token: str = Cookie(default=None)):
    user = get_user_from_token(token)
    if not user: raise HTTPException(401, "Not authenticated")
    if SessionLocal:
        with SessionLocal() as db:
            user_pool_names = list({s.pool_name for s in db.query(SavedNumber).filter(SavedNumber.user_id == user["id"], SavedNumber.moved == True).all()})
            result = []
            for pname in user_pool_names:
                pool = db.query(Pool).filter(Pool.name == pname).first()
                if pool:
                    count = db.query(ActiveNumber).filter(ActiveNumber.pool_id == pool.id).count()
                    result.append({"pool_name": pname, "pool_id": pool.id, "count": count})
            return result
    else:
        user_pool_names = list({s["pool_name"] for s in saved_numbers if s["user_id"] == user["id"] and s.get("moved", False)})
        result = []
        for pname in user_pool_names:
            pool = next((p for p in pools.values() if p["name"] == pname), None)
            if pool:
                count = len(active_numbers.get(pool["id"], []))
                result.append({"pool_name": pname, "pool_id": pool["id"], "count": count})
        return result

@app.delete("/api/saved/{saved_id}")
def delete_saved(saved_id: int, token: str = Cookie(default=None)):
    user = get_user_from_token(token)
    if not user: raise HTTPException(401, "Not authenticated")
    if SessionLocal:
        with SessionLocal() as db:
            saved = db.query(SavedNumber).filter(SavedNumber.id == saved_id, SavedNumber.user_id == user["id"]).first()
            if not saved: raise HTTPException(404, "Not found")
            db.delete(saved); db.commit()
    else:
        for i, s in enumerate(saved_numbers):
            if s["id"] == saved_id and s["user_id"] == user["id"]:
                saved_numbers.pop(i)
                return {"ok": True}
        raise HTTPException(404, "Not found")
    return {"ok": True}

@app.post("/api/saved/{saved_id}/next-number")
def ready_next_number(saved_id: int, token: str = Cookie(default=None)):
    user = get_user_from_token(token)
    if not user: raise HTTPException(401, "Not authenticated")
    if SessionLocal:
        with SessionLocal() as db:
            saved = db.query(SavedNumber).filter(SavedNumber.id == saved_id, SavedNumber.user_id == user["id"], SavedNumber.moved == True).first()
            if not saved: raise HTTPException(404, "Ready number not found")
            pool = db.query(Pool).filter(Pool.name == saved.pool_name).first()
            if not pool: raise HTTPException(404, "Pool not found")
            next_num_row = db.query(ActiveNumber).filter(ActiveNumber.pool_id == pool.id, ActiveNumber.number != saved.number).order_by(ActiveNumber.id.desc()).first()
            if not next_num_row: raise HTTPException(404, "No more numbers available in this pool")
            old_number = saved.number
            new_number = next_num_row.number
            db.delete(next_num_row)
            if not db.query(ActiveNumber).filter(ActiveNumber.number == old_number).first():
                db.add(ActiveNumber(pool_id=pool.id, number=old_number))
            saved.number = new_number
            db.commit()
            return {"ok": True, "number": new_number, "pool_name": saved.pool_name}
    else:
        for s in saved_numbers:
            if s["id"] == saved_id and s["user_id"] == user["id"] and s.get("moved", False):
                pool = next((p for p in pools.values() if p["name"] == s["pool_name"]), None)
                if not pool: raise HTTPException(404, "Pool not found")
                pid = pool["id"]
                nums = [n for n in active_numbers.get(pid, []) if n != s["number"]]
                if not nums: raise HTTPException(404, "No more numbers available")
                old_number = s["number"]
                new_number = nums[-1]
                nums.pop()
                if old_number not in nums: nums.append(old_number)
                active_numbers[pid] = nums
                s["number"] = new_number
                return {"ok": True, "number": new_number, "pool_name": s["pool_name"]}
        raise HTTPException(404, "Ready number not found")

@app.post("/api/saved/{saved_id}/switch-pool")
def ready_switch_pool(saved_id: int, new_pool_name: str, token: str = Cookie(default=None)):
    user = get_user_from_token(token)
    if not user: raise HTTPException(401, "Not authenticated")
    new_pool_name = new_pool_name.strip()
    if SessionLocal:
        with SessionLocal() as db:
            saved = db.query(SavedNumber).filter(SavedNumber.id == saved_id, SavedNumber.user_id == user["id"], SavedNumber.moved == True).first()
            if not saved: raise HTTPException(404, "Ready number not found")
            new_pool = db.query(Pool).filter(Pool.name == new_pool_name).first()
            if not new_pool: raise HTTPException(404, "Pool not found")
            next_num_row = db.query(ActiveNumber).filter(ActiveNumber.pool_id == new_pool.id).order_by(ActiveNumber.id.desc()).first()
            if not next_num_row: raise HTTPException(404, "No numbers available in that pool")
            old_pool = db.query(Pool).filter(Pool.name == saved.pool_name).first()
            old_number = saved.number
            new_number = next_num_row.number
            db.delete(next_num_row)
            if old_pool and not db.query(ActiveNumber).filter(ActiveNumber.number == old_number).first():
                db.add(ActiveNumber(pool_id=old_pool.id, number=old_number))
            saved.number = new_number; saved.pool_name = new_pool_name
            db.commit()
            return {"ok": True, "number": new_number, "pool_name": new_pool_name}
    else:
        new_pool = next((p for p in pools.values() if p["name"] == new_pool_name), None)
        if not new_pool: raise HTTPException(404, "Pool not found")
        pid_new = new_pool["id"]
        new_nums = active_numbers.get(pid_new, [])
        if not new_nums: raise HTTPException(404, "No numbers available")
        for s in saved_numbers:
            if s["id"] == saved_id and s["user_id"] == user["id"] and s.get("moved", False):
                old_pool = next((p for p in pools.values() if p["name"] == s["pool_name"]), None)
                old_number = s["number"]
                new_number = new_nums[-1]
                new_nums.pop(); active_numbers[pid_new] = new_nums
                if old_pool:
                    old_nums = active_numbers.get(old_pool["id"], [])
                    if old_number not in old_nums: old_nums.append(old_number)
                s["number"] = new_number; s["pool_name"] = new_pool_name
                return {"ok": True, "number": new_number, "pool_name": new_pool_name}
        raise HTTPException(404, "Ready number not found")

class TriggerMonitorRequest(BaseModel):
    number: str

@app.post("/api/saved/trigger-monitor")
async def trigger_saved_monitor(req: TriggerMonitorRequest, token: str = Cookie(default=None)):
    user = get_user_from_token(token)
    if not user: raise HTTPException(401, "Not authenticated")
    number = req.number.strip()
    pool_info = None
    if SessionLocal:
        with SessionLocal() as db:
            saved = db.query(SavedNumber).filter(SavedNumber.user_id == user["id"], SavedNumber.number == number).order_by(SavedNumber.created_at.desc()).first()
            if saved:
                pool = db.query(Pool).filter(Pool.name == saved.pool_name).first()
                if pool:
                    pool_info = {"otp_group_id": pool.otp_group_id, "match_format": pool.telegram_match_format or pool.match_format, "uses_platform": pool.uses_platform}
    else:
        saved = next((s for s in saved_numbers if s["user_id"] == user["id"] and s["number"] == number), None)
        if saved:
            pool = next((p for p in pools.values() if p["name"] == saved["pool_name"]), None)
            if pool:
                pool_info = {"otp_group_id": pool.get("otp_group_id"), "match_format": pool.get("telegram_match_format") or pool.get("match_format","5+4"), "uses_platform": pool.get("uses_platform", 0)}
    if not pool_info: raise HTTPException(404, "Could not find pool for this number")
    if not pool_info["otp_group_id"]: raise HTTPException(400, "Pool has no OTP group configured")
    ok = await request_monitor_bot(number=number, group_id=pool_info["otp_group_id"],
                                    match_format=pool_info["match_format"], user_id=user["id"])
    if pool_info["uses_platform"] in (1, 2):
        start_platform_monitor(user["id"], number, pool_info["match_format"])
    return {"ok": ok, "message": f"Monitoring started for {number}"}

# ── REVIEWS ───────────────────────────────────────────────────────────────────
class ReviewRequest(BaseModel):
    number: str
    rating: int
    comment: str = ""
    mark_as_bad: bool = False

@app.post("/api/reviews")
def submit_review(req: ReviewRequest, token: str = Cookie(default=None)):
    user = get_user_from_token(token)
    if not user: raise HTTPException(401, "Not authenticated")
    if not (1 <= req.rating <= 5): raise HTTPException(400, "Rating must be 1-5")
    if SessionLocal:
        with SessionLocal() as db:
            db.add(NumberReview(user_id=user["id"], number=req.number, rating=req.rating, comment=req.comment))
            if req.mark_as_bad or req.rating == 1:
                if not db.query(BadNumber).filter(BadNumber.number == req.number).first():
                    db.add(BadNumber(number=req.number, reason=req.comment or "Flagged by user", flagged_by=user["id"]))
                db.query(ActiveNumber).filter(ActiveNumber.number == req.number).delete()
            assignment = db.query(Assignment).filter(Assignment.user_id == user["id"], Assignment.number == req.number, Assignment.released_at == None).first()
            if assignment:
                assignment.released_at = utcnow()
                assignment.feedback = req.comment or ("bad" if req.mark_as_bad else "ok")
            db.commit()
    else:
        reviews.append({"id": _counters["review"], "user_id": user["id"], "number": req.number,
                         "rating": req.rating, "comment": req.comment, "created_at": utcnow().isoformat()})
        _counters["review"] += 1
        if req.mark_as_bad or req.rating == 1:
            add_bad_number(req.number, user["id"], req.comment or "Flagged by user")
    return {"ok": True}

# ── ADMIN ENDPOINTS ───────────────────────────────────────────────────────────
@app.get("/api/admin/stats")
def admin_stats(token: str = Cookie(default=None)):
    user = get_user_from_token(token)
    if not user or not user["is_admin"]: raise HTTPException(403, "Admin only")
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
                "online_users": sum(len(c) for c in user_connections.values())
            }
    else:
        return {
            "total_users": len(users),
            "pending_approval": sum(1 for u in users.values() if not u["is_approved"] and not u.get("is_blocked")),
            "total_pools": len(pools),
            "total_numbers": sum(len(n) for n in active_numbers.values()),
            "total_otps": len(otp_logs),
            "total_assignments": len(archived_numbers),
            "bad_numbers": len(bad_numbers),
            "saved_numbers": len([s for s in saved_numbers if not s.get("moved")]),
            "online_users": len(user_connections)
        }

@app.get("/api/admin/users")
def list_users(token: str = Cookie(default=None)):
    user = get_user_from_token(token)
    if not user or not user["is_admin"]: raise HTTPException(403, "Admin only")
    if SessionLocal:
        with SessionLocal() as db:
            ul = db.query(User).order_by(User.created_at.desc()).all()
            return [{"id": u.id, "username": u.username, "is_admin": u.is_admin,
                     "is_approved": u.is_approved, "is_blocked": u.is_blocked,
                     "created_at": u.created_at.isoformat()} for u in ul]
    else:
        return [{"id": u["id"], "username": u["username"], "is_admin": u["is_admin"],
                 "is_approved": u["is_approved"], "is_blocked": u.get("is_blocked", False),
                 "created_at": u.get("created_at", utcnow().isoformat())} for u in users.values()]

@app.post("/api/admin/users/{user_id}/approve")
async def approve_user_endpoint(user_id: int, token: str = Cookie(default=None)):
    user = get_user_from_token(token)
    if not user or not user["is_admin"]: raise HTTPException(403, "Admin only")
    approve_user(user_id)
    await send_to_user(user_id, {"type": "notification", "message": "✅ Your account has been approved!"})
    return {"ok": True}

@app.post("/api/admin/users/{user_id}/deny")
async def deny_user_endpoint(user_id: int, token: str = Cookie(default=None)):
    user = get_user_from_token(token)
    if not user or not user["is_admin"]: raise HTTPException(403, "Admin only")
    deny_user(user_id)
    return {"ok": True}

@app.post("/api/admin/users/{user_id}/block")
async def block_user_endpoint(user_id: int, token: str = Cookie(default=None)):
    user = get_user_from_token(token)
    if not user or not user["is_admin"]: raise HTTPException(403, "Admin only")
    block_user(user_id)
    await send_to_user(user_id, {"type": "notification", "message": "🚫 Your account has been blocked."})
    return {"ok": True}

@app.post("/api/admin/users/{user_id}/unblock")
async def unblock_user_endpoint(user_id: int, token: str = Cookie(default=None)):
    user = get_user_from_token(token)
    if not user or not user["is_admin"]: raise HTTPException(403, "Admin only")
    unblock_user(user_id)
    await send_to_user(user_id, {"type": "notification", "message": "✅ Your account has been unblocked!"})
    return {"ok": True}

@app.get("/api/admin/bad-numbers")
def list_bad_numbers(token: str = Cookie(default=None)):
    user = get_user_from_token(token)
    if not user or not user["is_admin"]: raise HTTPException(403, "Admin only")
    if SessionLocal:
        with SessionLocal() as db:
            bad = db.query(BadNumber).order_by(BadNumber.created_at.desc()).all()
            return [{"number": b.number, "reason": b.reason, "created_at": b.created_at.isoformat()} for b in bad]
    else:
        return [{"number": num, "reason": data.get("reason",""), "created_at": data.get("marked_at","")} for num, data in bad_numbers.items()]

@app.delete("/api/admin/bad-numbers")
def remove_bad_number(number: str, token: str = Cookie(default=None)):
    user = get_user_from_token(token)
    if not user or not user["is_admin"]: raise HTTPException(403, "Admin only")
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
    if not user or not user["is_admin"]: raise HTTPException(403, "Admin only")
    if SessionLocal:
        with SessionLocal() as db:
            rev = db.query(NumberReview).order_by(NumberReview.created_at.desc()).limit(100).all()
            return [{"id": r.id, "user_id": r.user_id, "number": r.number, "rating": r.rating, "comment": r.comment, "created_at": r.created_at.isoformat()} for r in rev]
    else:
        return sorted([{"id": r["id"], "user_id": r["user_id"], "number": r["number"], "rating": r["rating"], "comment": r["comment"], "created_at": r["created_at"]} for r in reviews], key=lambda x: x["created_at"], reverse=True)[:100]

@app.post("/api/admin/broadcast")
async def broadcast_message(message: str, token: str = Cookie(default=None)):
    user = get_user_from_token(token)
    if not user or not user["is_admin"]: raise HTTPException(403, "Admin only")
    await broadcast_all({"type": "broadcast", "message": message})
    return {"ok": True}

@app.post("/api/admin/settings/approval")
def set_approval_mode_endpoint(enabled: bool, token: str = Cookie(default=None)):
    user = get_user_from_token(token)
    if not user or not user["is_admin"]: raise HTTPException(403, "Admin only")
    set_approval_mode(enabled)
    return {"ok": True}

@app.post("/api/admin/settings/otp-redirect")
def set_otp_redirect_endpoint(mode: str, token: str = Cookie(default=None)):
    user = get_user_from_token(token)
    if not user or not user["is_admin"]: raise HTTPException(403, "Admin only")
    if mode not in ["pool", "hardcoded"]: raise HTTPException(400, "Invalid mode")
    set_otp_redirect_mode(mode)
    return {"ok": True}

# ── CUSTOM BUTTONS ────────────────────────────────────────────────────────────
@app.get("/api/buttons")
def get_buttons(token: str = Cookie(default=None)):
    user = get_user_from_token(token)
    if not user: raise HTTPException(401, "Not authenticated")
    return _get_custom_buttons()

@app.post("/api/admin/buttons")
def add_button(label: str, url: str, token: str = Cookie(default=None)):
    user = get_user_from_token(token)
    if not user or not user["is_admin"]: raise HTTPException(403, "Admin only")
    if SessionLocal:
        with SessionLocal() as db:
            max_pos = db.query(func.max(CustomButton.position)).scalar() or 0
            db.add(CustomButton(label=label, url=url, position=max_pos + 1))
            db.commit()
    else:
        custom_buttons.append({"id": _counters["button"], "label": label, "url": url, "position": len(custom_buttons)})
        _counters["button"] += 1
    return {"ok": True}

@app.delete("/api/admin/buttons/{button_id}")
def delete_button(button_id: int, token: str = Cookie(default=None)):
    user = get_user_from_token(token)
    if not user or not user["is_admin"]: raise HTTPException(403, "Admin only")
    if SessionLocal:
        with SessionLocal() as db:
            db.query(CustomButton).filter(CustomButton.id == button_id).delete()
            db.commit()
    else:
        for i, b in enumerate(custom_buttons):
            if b["id"] == button_id:
                custom_buttons.pop(i)
                return {"ok": True}
    return {"ok": True}

# ── WEBSOCKETS ────────────────────────────────────────────────────────────────
@app.websocket("/ws/user/{user_id}")
async def ws_user(websocket: WebSocket, user_id: int):
    await connect_user(websocket, user_id)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        disconnect_user(websocket, user_id)

@app.websocket("/ws/feed")
async def ws_feed(websocket: WebSocket):
    await connect_feed(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        disconnect_feed(websocket)

if __name__ == "__main__":
    uvicorn.run("backend_new:app", host="0.0.0.0", port=PORT, reload=False, log_level="info")
