# -*- coding: utf-8 -*-
"""
NEON GRID NETWORK — Complete Platform Backend
================================================
All features from numberbot.py converted to web platform:
- Pool management (create/edit/delete)
- Number assignments with prefix filter
- OTP monitoring via monitor bot
- Saved numbers with timer expiry
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
- Match formats for OTP extraction
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
                     APIRouter, Depends, HTTPException, Cookie,
                     UploadFile, File, BackgroundTasks, Form, Query)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, HTMLResponse, FileResponse
from pydantic import BaseModel

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

# Platform config
PLATFORM_URL = os.environ.get("PLATFORM_URL", "http://198.135.52.238")
PLATFORM_USERNAME = os.environ.get("PLATFORM_USERNAME", "Frankhustle")
PLATFORM_PASSWORD = os.environ.get("PLATFORM_PASSWORD", "f11111")

# Monitoring config
PLATFORM_MONITOR_TTL = 600
PLATFORM_POLL_INTERVAL = 5
PLATFORM_STOCK_INTERVAL = 300
OTP_AUTO_DELETE_DELAY = 30

# Default values
DEFAULT_LOCAL_PREFIX = "+234"
OTP_CHANNEL_LINK = "https://t.me/earnplusz"
NUMBER_CHANNEL_LINK = "https://t.me/Finalsearchbot"

# Monitoring modes
MONITOR_TELEGRAM = 0
MONITOR_PLATFORM = 1
MONITOR_BOTH = 2

log.info(f"Starting Frank Number Bot on port {PORT}")
log.info(f"Database URL: {'configured' if DATABASE_URL else 'NOT SET'}")
log.info(f"Monitor Bot URL: {MONITOR_BOT_URL or 'NOT SET'}")

# ══════════════════════════════════════════════════════════════════════════════
#  IN-MEMORY DATABASE (All features)
# ══════════════════════════════════════════════════════════════════════════════

# Core tables
users: Dict[int, Dict] = {}
sessions: Dict[str, int] = {}
pools: Dict[int, Dict] = {}
active_numbers: Dict[int, List[str]] = {}  # pool_id -> list of numbers
archived_numbers: List[Dict] = []  # assignment history
bad_numbers: Dict[str, Dict] = {}  # number -> {reason, flagged_by, flagged_at}
feedbacks: List[Dict] = []
custom_buttons: List[Dict] = []
blocked_users: set = set()
denied_users: set = set()
approved_users: set = set()
pending_notified: set = set()
agreement_users: Dict[int, bool] = {}
pool_access: Dict[int, set] = {}  # pool_id -> set of user_ids
otp_logs: List[Dict] = []
saved_numbers: List[Dict] = []
reviews: List[Dict] = []
broadcasts: List[Dict] = []

# Counters
user_counter = 1
pool_counter = 1
assignment_counter = 1
otp_counter = 1
saved_counter = 1
review_counter = 1
feedback_counter = 1
button_counter = 1

# Platform monitoring
_platform_token: Optional[str] = None
_active_platform_tasks: Dict[int, asyncio.Task] = {}
_platform_stock_snapshot: Dict[str, int] = {}

# WebSocket connections
user_connections: Dict[int, List[WebSocket]] = {}
feed_connections: List[WebSocket] = []

# OTP delivery tracking for compliance
_compliance_counters: Dict[int, int] = {}
COMPLIANCE_BRIDGE_GROUP_ID = -1003817159179
COMPLIANCE_CODES_PER_CHECK = 5

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
    sessions[token] = user_id
    return token

def revoke_token(token: str):
    if token in sessions:
        del sessions[token]

def get_user_from_token(token: str) -> Optional[Dict]:
    if not token or token not in sessions:
        return None
    user_id = sessions[token]
    return users.get(user_id)

def is_admin(user_id: int) -> bool:
    user = users.get(user_id)
    return user and user.get("is_admin", False)

def is_blocked(user_id: int) -> bool:
    return user_id in blocked_users

def is_denied(user_id: int) -> bool:
    return user_id in denied_users

def is_approved(user_id: int) -> bool:
    return user_id in approved_users

def block_user(user_id: int):
    blocked_users.add(user_id)
    if user_id in approved_users:
        approved_users.remove(user_id)

def unblock_user(user_id: int):
    blocked_users.discard(user_id)

def deny_user(user_id: int):
    denied_users.add(user_id)
    if user_id in approved_users:
        approved_users.remove(user_id)

def undeny_user(user_id: int):
    denied_users.discard(user_id)

def approve_user(user_id: int):
    approved_users.add(user_id)
    denied_users.discard(user_id)
    blocked_users.discard(user_id)

def can_use_bot(user_id: int) -> bool:
    if is_admin(user_id):
        return True
    if is_blocked(user_id):
        return False
    if is_denied(user_id):
        return False
    return is_approved(user_id)

def get_approval_mode() -> bool:
    # Always require approval unless admin
    return True

def has_pool_access(pool_id: int, user_id: int) -> bool:
    if is_admin(user_id):
        return True
    restricted = pool_access.get(pool_id, set())
    if not restricted:
        return True
    return user_id in restricted

def grant_pool_access(pool_id: int, user_id: int):
    if pool_id not in pool_access:
        pool_access[pool_id] = set()
    pool_access[pool_id].add(user_id)

def revoke_pool_access(pool_id: int, user_id: int):
    if pool_id in pool_access:
        pool_access[pool_id].discard(user_id)

def get_pool_access_users(pool_id: int) -> List[Dict]:
    users_in_pool = pool_access.get(pool_id, set())
    result = []
    for uid in users_in_pool:
        u = users.get(uid)
        if u:
            result.append({
                "user_id": uid,
                "username": u.get("username", ""),
                "first_name": u.get("username", "")
            })
    return result

def normalize_number(raw: str) -> str:
    s = re.sub(r'[^\d+]', '', raw)
    if s.startswith('+'):
        return s
    if s.startswith('0'):
        return DEFAULT_LOCAL_PREFIX + s[1:]
    return '+' + s

def get_remaining_count(pool_id: int) -> int:
    return len(active_numbers.get(pool_id, []))

def add_bad_number(number: str, marked_by: int, reason: str = "marked as bad"):
    bad_numbers[number] = {
        "number": number,
        "reason": reason,
        "marked_by": marked_by,
        "marked_at": utcnow().isoformat()
    }
    # Remove from active numbers
    for pool_id, numbers in active_numbers.items():
        if number in numbers:
            numbers.remove(number)

def save_feedback(number: str, user_id: int, feedback: str):
    global feedback_counter
    feedbacks.append({
        "id": feedback_counter,
        "number": number,
        "user_id": user_id,
        "feedback": feedback,
        "created_at": utcnow().isoformat()
    })
    feedback_counter += 1

def assign_one_number(user_id: int, pool_id: int, prefix: Optional[str] = None) -> Optional[Dict]:
    global assignment_counter
    
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
        selected = numbers[-1]  # Last in list (newest)
    
    if not selected:
        return None
    
    # Remove from active
    numbers.remove(selected)
    active_numbers[pool_id] = numbers
    
    # Create assignment
    pool = pools.get(pool_id, {})
    assignment = {
        "id": assignment_counter,
        "user_id": user_id,
        "pool_id": pool_id,
        "number": selected,
        "assigned_at": utcnow().isoformat(),
        "released_at": None,
        "feedback": ""
    }
    archived_numbers.append(assignment)
    assignment_counter += 1
    
    return {
        "assignment_id": assignment["id"],
        "number": selected,
        "pool_name": pool.get("name", "Unknown"),
        "pool_code": pool.get("country_code", ""),
        "otp_link": pool.get("otp_link", ""),
        "uses_platform": pool.get("uses_platform", 0),
        "match_format": pool.get("match_format", "5+4"),
        "telegram_match_format": pool.get("telegram_match_format", ""),
        "trick_text": pool.get("trick_text", "")
    }

def release_assignment(user_id: int, assignment_id: int = None):
    for a in archived_numbers:
        if a["user_id"] == user_id and a.get("released_at") is None:
            if assignment_id is None or a["id"] == assignment_id:
                a["released_at"] = utcnow().isoformat()
                return a
    return None

def get_current_assignment(user_id: int) -> Optional[Dict]:
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
                "trick_text": pool.get("trick_text", ""),
                "otp_group_id": pool.get("otp_group_id"),
                "match_format": pool.get("match_format", "5+4"),
                "telegram_match_format": pool.get("telegram_match_format", "")
            }
    return None

# ══════════════════════════════════════════════════════════════════════════════
#  PLATFORM MONITORING
# ══════════════════════════════════════════════════════════════════════════════

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
                        _platform_token = None
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
                        otp_entry = {
                            "id": otp_counter,
                            "user_id": user_id,
                            "number": assigned_number,
                            "otp_code": otp,
                            "raw_message": raw_message,
                            "delivered_at": utcnow().isoformat()
                        }
                        otp_logs.append(otp_entry)
                        
                        # Send to user via WebSocket
                        otp_data = {
                            "type": "otp",
                            "id": otp_counter,
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
                        _platform_token = None
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
                # Notify admin (send file with numbers)
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
                                if not lines:
                                    continue
                                
                                # Send to admin
                                await send_to_user(1, {
                                    "type": "stock_update",
                                    "country": country,
                                    "count": country_counts[country],
                                    "numbers": lines[:100]  # First 100 as preview
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
                    else:
                        body = await resp.text()
                        log.error(f"[SearchOTP] HTTP {resp.status}: {body[:100]}")
        except Exception as e:
            log.error(f"[SearchOTP] Post failed: {e}")
            if attempt < 2:
                await asyncio.sleep(2)
    return False

# ══════════════════════════════════════════════════════════════════════════════
#  COMPLIANCE
# ══════════════════════════════════════════════════════════════════════════════

async def _compliance_post_check(user_id: int):
    """Post a COMPLIANCE_CHECK to the bridge group"""
    await broadcast_to_compliance({
        "type": "COMPLIANCE_CHECK",
        "user_id": user_id
    })

async def compliance_record_otp_delivered(user_id: int):
    _compliance_counters[user_id] = _compliance_counters.get(user_id, 0) + 1
    count = _compliance_counters[user_id]
    
    if count >= COMPLIANCE_CODES_PER_CHECK:
        _compliance_counters[user_id] = 0
        await _compliance_post_check(user_id)

# ══════════════════════════════════════════════════════════════════════════════
#  WEBSOCKET MANAGER
# ══════════════════════════════════════════════════════════════════════════════

async def connect_user(ws: WebSocket, user_id: int):
    await ws.accept()
    if user_id not in user_connections:
        user_connections[user_id] = []
    user_connections[user_id].append(ws)
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

async def broadcast_to_compliance(data: dict):
    # In production, would send to Telegram bridge group
    pass

# ══════════════════════════════════════════════════════════════════════════════
#  SAVED NUMBERS EXPIRY PROCESSOR
# ══════════════════════════════════════════════════════════════════════════════

async def process_expired_saved():
    global saved_numbers
    now = utcnow()
    expired = [s for s in saved_numbers if not s.get("moved", False) and 
               datetime.fromisoformat(s["expires_at"]) <= now]
    
    for sn in expired:
        # Find or create pool
        pool = None
        for p in pools.values():
            if p["name"] == sn["pool_name"]:
                pool = p
                break
        
        if not pool:
            pool_id = pool_counter
            pool_counter += 1
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
        
        # Check if number is bad
        if sn["number"] not in bad_numbers:
            if sn["number"] not in active_numbers.get(pool["id"], []):
                active_numbers.setdefault(pool["id"], []).append(sn["number"])
        
        sn["moved"] = True
    
    if expired:
        log.info(f"[Scheduler] Moved {len(expired)} expired saved numbers to pools")

async def scheduler():
    while True:
        await asyncio.sleep(60)
        try:
            await process_expired_saved()
        except Exception as e:
            log.error(f"[Scheduler] {e}")

# ══════════════════════════════════════════════════════════════════════════════
#  INITIAL DATA
# ══════════════════════════════════════════════════════════════════════════════

def init_data():
    global user_counter, pool_counter
    
    # Create default admin
    if not users:
        users[1] = {
            "id": 1,
            "username": "admin",
            "password_hash": hash_password("admin123"),
            "is_admin": True,
            "is_approved": True,
            "is_blocked": False,
            "created_at": utcnow().isoformat()
        }
        approved_users.add(1)
        user_counter = 2
        log.info("✅ Default admin created: admin / admin123")
    
    # Create default pools
    if not pools:
        default_pools = [
            {"name": "Nigeria", "country_code": "234", "number_count": 1250, "trick_text": "Best for WhatsApp"},
            {"name": "USA", "country_code": "1", "number_count": 842, "trick_text": "Best for Telegram"},
            {"name": "United Kingdom", "country_code": "44", "number_count": 567},
            {"name": "Canada", "country_code": "1", "number_count": 321},
            {"name": "Australia", "country_code": "61", "number_count": 234},
        ]
        
        for i, p in enumerate(default_pools, 1):
            pool_id = i
            pools[pool_id] = {
                "id": pool_id,
                "name": p["name"],
                "country_code": p["country_code"],
                "otp_group_id": None,
                "otp_link": "https://t.me/earnplusz",
                "match_format": "5+4",
                "telegram_match_format": "",
                "uses_platform": 0,
                "is_paused": False,
                "pause_reason": "",
                "trick_text": p.get("trick_text", ""),
                "is_admin_only": False,
                "last_restocked": utcnow().isoformat(),
                "created_at": utcnow().isoformat()
            }
            active_numbers[pool_id] = []
            # Generate demo numbers
            for j in range(p.get("number_count", 100)):
                num = f"+{p['country_code']}{random.randint(7000000000, 7999999999)}"
                active_numbers[pool_id].append(num)
        
        pool_counter = len(pools) + 1
        log.info(f"✅ Created {len(pools)} default pools")
    
    # Create custom buttons
    if not custom_buttons:
        default_buttons = [
            {"label": "📢 Join Channel", "url": "https://t.me/earnplusz", "position": 0},
            {"label": "📱 Number Channel", "url": "https://t.me/Finalsearchbot", "position": 1},
        ]
        for btn in default_buttons:
            custom_buttons.append({
                "id": len(custom_buttons) + 1,
                "label": btn["label"],
                "url": btn["url"],
                "position": btn["position"]
            })

# ══════════════════════════════════════════════════════════════════════════════
#  FASTAPI APP
# ══════════════════════════════════════════════════════════════════════════════

@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("Starting Frank Number Bot...")
    init_data()
    asyncio.create_task(scheduler())
    asyncio.create_task(_platform_stock_watcher())
    log.info("✅ Scheduler and stock watcher started")
    yield
    log.info("Shutting down Frank Number Bot...")

app = FastAPI(title="NEON GRID NETWORK", lifespan=lifespan)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=[FRONTEND_URL] if FRONTEND_URL != "*" else ["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ══════════════════════════════════════════════════════════════════════════════
#  FRONTEND
# ══════════════════════════════════════════════════════════════════════════════

FRONTEND_HTML = '''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no, viewport-fit=cover">
    <title>NEON GRID NETWORK</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
        body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: linear-gradient(135deg, #f5f7ff 0%, #eef2ff 100%); min-height: 100vh; padding-bottom: 70px; color: #1e293b; }
        .status-bar { background: #0a84ff; padding: 12px 16px 8px; color: white; font-size: 14px; font-weight: 500; display: flex; justify-content: space-between; position: sticky; top: 0; z-index: 100; }
        .header { background: white; padding: 16px; border-bottom: 1px solid #e2e8f0; }
        .user-info { display: flex; justify-content: space-between; align-items: center; }
        .user-name { font-size: 20px; font-weight: 700; color: #0a84ff; }
        .user-id { font-size: 12px; color: #64748b; background: #f1f5f9; padding: 4px 10px; border-radius: 20px; }
        .balance-card { background: linear-gradient(135deg, #0a84ff 0%, #0066cc 100%); border-radius: 24px; padding: 20px; margin: 16px; color: white; box-shadow: 0 8px 24px rgba(10,132,255,0.25); }
        .balance-label { font-size: 14px; opacity: 0.9; margin-bottom: 8px; }
        .balance-amount { font-size: 42px; font-weight: 800; letter-spacing: -1px; margin-bottom: 8px; }
        .balance-sub { font-size: 12px; opacity: 0.8; }
        .withdraw-btn { background: rgba(255,255,255,0.2); border: 1px solid rgba(255,255,255,0.3); padding: 10px 20px; border-radius: 30px; color: white; font-weight: 600; font-size: 14px; margin-top: 12px; display: inline-block; cursor: pointer; }
        .section-title { font-size: 16px; font-weight: 600; color: #334155; padding: 16px 16px 8px; }
        .menu-grid { display: grid; grid-template-columns: repeat(2, 1fr); gap: 12px; padding: 8px 16px; }
        .menu-card { background: white; border-radius: 20px; padding: 16px; text-align: center; cursor: pointer; box-shadow: 0 2px 8px rgba(0,0,0,0.04); border: 1px solid #eef2ff; }
        .menu-card:active { transform: scale(0.97); background: #f8fafc; }
        .menu-icon { font-size: 32px; margin-bottom: 8px; }
        .menu-label { font-size: 13px; font-weight: 500; color: #334155; }
        .menu-desc { font-size: 11px; color: #94a3b8; margin-top: 4px; }
        .number-card { background: white; margin: 16px; border-radius: 24px; padding: 20px; box-shadow: 0 4px 12px rgba(0,0,0,0.05); border: 1px solid #e2e8f0; }
        .number-label { font-size: 12px; color: #64748b; text-transform: uppercase; letter-spacing: 1px; margin-bottom: 8px; }
        .number-value { font-size: 28px; font-weight: 700; font-family: monospace; color: #0a84ff; word-break: break-all; margin-bottom: 12px; }
        .region-badge { display: inline-block; background: #f1f5f9; padding: 4px 12px; border-radius: 20px; font-size: 12px; color: #475569; }
        .otp-card { background: linear-gradient(135deg, #10b981 0%, #059669 100%); margin: 16px; border-radius: 24px; padding: 20px; color: white; animation: slideIn 0.3s ease; }
        @keyframes slideIn { from { opacity: 0; transform: translateY(20px); } to { opacity: 1; transform: translateY(0); } }
        .otp-code { font-size: 48px; font-weight: 800; font-family: monospace; letter-spacing: 8px; text-align: center; margin: 16px 0; cursor: pointer; }
        .otp-timer { text-align: center; font-size: 14px; opacity: 0.9; }
        .region-list { padding: 8px 16px; }
        .region-item { background: white; border-radius: 16px; padding: 14px; margin-bottom: 10px; display: flex; justify-content: space-between; align-items: center; border: 1px solid #e2e8f0; cursor: pointer; }
        .region-item:active { background: #f8fafc; }
        .region-name { font-weight: 500; }
        .region-code { font-size: 12px; color: #64748b; }
        .region-count { background: #f1f5f9; padding: 4px 10px; border-radius: 20px; font-size: 12px; font-weight: 600; color: #0a84ff; }
        .filter-row { display: flex; gap: 10px; padding: 12px 16px; background: white; margin: 8px 16px; border-radius: 40px; border: 1px solid #e2e8f0; }
        .filter-input { flex: 1; border: none; outline: none; font-size: 14px; background: transparent; }
        .filter-btn { background: #0a84ff; color: white; border: none; padding: 6px 16px; border-radius: 30px; font-size: 12px; font-weight: 500; cursor: pointer; }
        .saved-list { padding: 8px 16px; }
        .saved-item { background: white; border-radius: 16px; padding: 14px; margin-bottom: 10px; display: flex; justify-content: space-between; align-items: center; border: 1px solid #e2e8f0; }
        .saved-number { font-family: monospace; font-weight: 600; color: #0a84ff; }
        .saved-timer { font-size: 12px; color: #64748b; }
        .timer-badge { padding: 4px 10px; border-radius: 20px; font-size: 11px; font-weight: 600; }
        .timer-green { background: #dcfce7; color: #166534; }
        .timer-yellow { background: #fef9c3; color: #854d0e; }
        .timer-red { background: #fee2e2; color: #991b1b; }
        .timer-ready { background: #dbeafe; color: #1e40af; }
        .history-item { background: white; border-radius: 16px; padding: 14px; margin-bottom: 10px; border: 1px solid #e2e8f0; }
        .history-number { font-family: monospace; font-size: 13px; color: #475569; }
        .history-otp { font-size: 20px; font-weight: 700; font-family: monospace; color: #10b981; margin: 8px 0; cursor: pointer; }
        .bottom-nav { position: fixed; bottom: 0; left: 0; right: 0; background: white; display: flex; justify-content: space-around; padding: 8px 16px 20px; box-shadow: 0 -4px 12px rgba(0,0,0,0.05); border-top: 1px solid #eef2ff; z-index: 100; }
        .nav-item { display: flex; flex-direction: column; align-items: center; gap: 4px; cursor: pointer; padding: 8px 12px; border-radius: 30px; }
        .nav-item:active { background: #f1f5f9; }
        .nav-icon { font-size: 24px; }
        .nav-label { font-size: 11px; font-weight: 500; color: #64748b; }
        .nav-item.active .nav-label { color: #0a84ff; }
        .page { display: none; padding-bottom: 20px; }
        .page.active { display: block; }
        .toast { position: fixed; bottom: 100px; left: 50%; transform: translateX(-50%); background: #1e293b; color: white; padding: 12px 20px; border-radius: 40px; font-size: 14px; z-index: 1000; max-width: 90%; text-align: center; animation: fadeInOut 2s ease; }
        @keyframes fadeInOut { 0% { opacity: 0; transform: translateX(-50%) translateY(20px); } 15% { opacity: 1; transform: translateX(-50%) translateY(0); } 85% { opacity: 1; } 100% { opacity: 0; transform: translateX(-50%) translateY(-20px); } }
        .loading { text-align: center; padding: 40px; color: #94a3b8; }
        .spinner { width: 40px; height: 40px; border: 3px solid #e2e8f0; border-top-color: #0a84ff; border-radius: 50%; animation: spin 0.8s linear infinite; margin: 0 auto 12px; }
        @keyframes spin { to { transform: rotate(360deg); } }
        .modal { display: none; position: fixed; top: 0; left: 0; right: 0; bottom: 0; background: rgba(0,0,0,0.5); z-index: 1000; align-items: flex-end; }
        .modal.show { display: flex; }
        .modal-content { background: white; border-radius: 28px 28px 0 0; max-height: 80vh; overflow-y: auto; width: 100%; animation: slideUp 0.3s ease; }
        @keyframes slideUp { from { transform: translateY(100%); } to { transform: translateY(0); } }
        .modal-header { padding: 20px; border-bottom: 1px solid #e2e8f0; font-weight: 600; font-size: 18px; }
        .feedback-grid { display: grid; grid-template-columns: repeat(2, 1fr); gap: 10px; margin-top: 16px; padding: 20px; }
        .feedback-btn { background: #f1f5f9; border: none; padding: 12px; border-radius: 40px; font-size: 14px; font-weight: 500; cursor: pointer; }
        .feedback-btn.bad { background: #fee2e2; color: #dc2626; }
        .feedback-btn.good { background: #dcfce7; color: #16a34a; }
        .auth-container { min-height: 100vh; display: flex; align-items: center; justify-content: center; padding: 20px; background: linear-gradient(135deg, #0a84ff 0%, #0066cc 100%); }
        .auth-card { background: white; border-radius: 32px; padding: 32px 24px; width: 100%; max-width: 320px; box-shadow: 0 20px 40px rgba(0,0,0,0.1); }
        .auth-logo { text-align: center; font-size: 48px; margin-bottom: 24px; }
        .auth-input { width: 100%; padding: 14px; border: 1px solid #e2e8f0; border-radius: 40px; font-size: 16px; margin-bottom: 12px; outline: none; }
        .auth-input:focus { border-color: #0a84ff; }
        .auth-btn { width: 100%; background: #0a84ff; color: white; border: none; padding: 14px; border-radius: 40px; font-size: 16px; font-weight: 600; margin-top: 8px; cursor: pointer; }
        .auth-switch { text-align: center; margin-top: 16px; color: #64748b; font-size: 13px; }
        .auth-switch span { color: #0a84ff; font-weight: 600; cursor: pointer; }
        .error-msg { color: #ef4444; font-size: 12px; margin-top: 8px; text-align: center; }
        .admin-badge { background: #f59e0b; color: white; padding: 2px 8px; border-radius: 20px; font-size: 10px; margin-left: 8px; }
    </style>
</head>
<body>

<div id="authContainer" style="display: flex;">
    <div class="auth-container">
        <div class="auth-card">
            <div class="auth-logo">⚡</div>
            <h2 style="text-align: center; margin-bottom: 24px; color: #0a84ff;">NEON GRID</h2>
            <div style="display: flex; gap: 10px; margin-bottom: 20px;">
                <button id="authLoginTab" class="auth-btn" style="background: #0a84ff; margin: 0;">Login</button>
                <button id="authRegisterTab" class="auth-btn" style="background: #e2e8f0; color: #334155; margin: 0;">Register</button>
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
            <div class="auth-switch">First user becomes admin automatically</div>
        </div>
    </div>
</div>

<div id="appContainer" style="display: none;">
    <div class="status-bar"><span id="currentTime">--:--</span><span>⚡ NEON GRID</span><span id="connectionStatus">●</span></div>
    <div class="header"><div class="user-info"><div><div class="user-name" id="userName">User</div><div class="user-id" id="userId">ID: --</div></div><div id="adminBadge" style="display: none;"><span class="admin-badge">ADMIN</span></div></div></div>

    <div id="homePage" class="page active">
        <div class="balance-card"><div class="balance-label">ACCOUNT BALANCE</div><div class="balance-amount" id="balanceAmount">0</div><div class="balance-sub">≈ ₱0.00 NGN</div><div class="withdraw-btn" onclick="showToast('Withdrawal feature coming soon', 'info')">Withdraw Now</div></div>
        <div class="section-title">ACCOUNT MANAGEMENT</div>
        <div class="menu-grid">
            <div class="menu-card" onclick="showToast('Withdrawal feature coming soon', 'info')"><div class="menu-icon">💰</div><div class="menu-label">Withdraw</div><div class="menu-desc">Convert points to cash</div></div>
            <div class="menu-card" onclick="showToast('Earnings details coming soon', 'info')"><div class="menu-icon">📊</div><div class="menu-label">Earnings Details</div><div class="menu-desc">View your earning records</div></div>
            <div class="menu-card" onclick="showToast('Withdrawal history coming soon', 'info')"><div class="menu-icon">📦</div><div class="menu-label">Withdrawal Orders</div><div class="menu-desc">Track your withdrawals</div></div>
            <div class="menu-card" onclick="showToast('Leaderboard coming soon', 'info')"><div class="menu-icon">🏆</div><div class="menu-label">Daily Lead Card</div><div class="menu-desc">Top performers</div></div>
        </div>
    </div>

    <div id="numbersPage" class="page">
        <div class="number-card" id="currentNumberCard"><div class="number-label">YOUR ACTIVE NUMBER</div><div class="number-value" id="currentNumber">—</div><div style="display: flex; justify-content: space-between; align-items: center;"><span class="region-badge" id="currentRegion">No region selected</span><div style="display: flex; gap: 10px;"><button class="withdraw-btn" style="background: #f1f5f9; color: #334155; padding: 6px 16px;" onclick="changeNumber()">🔄 Change</button><button class="withdraw-btn" style="background: #f1f5f9; color: #334155; padding: 6px 16px;" onclick="copyNumber()">📋 Copy</button></div></div></div>
        <div id="otpDisplay" style="display: none;"></div>
        <div class="section-title">SELECT REGION</div>
        <div class="filter-row"><input type="text" id="prefixFilter" class="filter-input" placeholder="Filter by prefix (e.g., 8101)"><button class="filter-btn" onclick="applyFilter()">Filter</button></div>
        <div id="regionList" class="region-list"><div class="loading"><div class="spinner"></div>Loading regions...</div></div>
    </div>

    <div id="savedPage" class="page">
        <div class="section-title">💾 SAVED NUMBERS</div>
        <div style="padding: 0 16px 16px;">
            <div class="filter-row" style="margin: 0 0 12px 0;"><textarea id="savedNumbersInput" class="filter-input" placeholder="Enter numbers (one per line)" style="height: 80px; resize: vertical;"></textarea></div>
            <div class="filter-row" style="margin: 0 0 12px 0;"><input type="number" id="timerMinutes" class="filter-input" placeholder="Timer (minutes)" value="30"><input type="text" id="poolNameInput" class="filter-input" placeholder="Pool name"><button class="filter-btn" onclick="saveNumbers()">Save</button></div>
        </div>
        <div id="savedList" class="saved-list"></div>
    </div>

    <div id="historyPage" class="page">
        <div class="section-title">📜 OTP HISTORY</div>
        <div id="historyList" class="saved-list"></div>
    </div>

    <div id="adminPage" class="page">
        <div class="section-title">🛠️ ADMIN PANEL</div>
        <div class="number-card"><div class="number-label">System Stats</div><div id="adminStats" style="font-size: 14px;"></div></div>
        <div class="number-card"><div class="number-label">Broadcast Message</div><textarea id="broadcastMsg" rows="3" style="width: 100%; padding: 12px; border-radius: 16px; border: 1px solid #e2e8f0; margin: 12px 0;"></textarea><button class="filter-btn" onclick="sendBroadcast()">📢 Send</button></div>
        <div class="section-title">Users</div><div id="adminUsersList" class="saved-list"></div>
        <div class="section-title">Bad Numbers</div><div id="adminBadList" class="saved-list"></div>
        <div class="section-title">Reviews</div><div id="adminReviewsList" class="saved-list"></div>
    </div>

    <div class="bottom-nav">
        <div class="nav-item active" data-page="home"><div class="nav-icon">🏠</div><div class="nav-label">Home</div></div>
        <div class="nav-item" data-page="numbers"><div class="nav-icon">📱</div><div class="nav-label">Numbers</div></div>
        <div class="nav-item" data-page="saved"><div class="nav-icon">💾</div><div class="nav-label">Saved</div></div>
        <div class="nav-item" data-page="history"><div class="nav-icon">📜</div><div class="nav-label">History</div></div>
        <div class="nav-item" data-page="admin" id="adminNavItem" style="display: none;"><div class="nav-icon">⚙️</div><div class="nav-label">Admin</div></div>
    </div>
</div>

<div id="feedbackModal" class="modal"><div class="modal-content"><div class="modal-header">Rate Your Number</div><div style="padding: 20px;"><div id="feedbackNumber" style="font-family: monospace; font-size: 18px; text-align: center; margin-bottom: 20px;"></div><div class="feedback-grid"><button class="feedback-btn good" onclick="submitFeedback('worked')">✅ Worked</button><button class="feedback-btn bad" onclick="submitFeedback('bad')">❌ Not Available</button><button class="feedback-btn" onclick="submitFeedback('email')">📧 Email Only</button><button class="feedback-btn" onclick="submitFeedback('other_devices')">📱 Other Devices</button><button class="feedback-btn" onclick="submitFeedback('try_later')">⏳ Try Later</button><button class="feedback-btn" onclick="showOtherFeedback()">📝 Other</button></div><div id="otherFeedbackDiv" style="display: none; margin-top: 16px;"><textarea id="otherFeedbackText" rows="2" placeholder="Describe the issue..." style="width: 100%; padding: 12px; border-radius: 16px; border: 1px solid #e2e8f0;"></textarea><button class="filter-btn" style="margin-top: 12px; width: 100%;" onclick="submitFeedback('other')">Submit</button></div></div></div></div>

<script>
const API_BASE = window.location.origin;
let currentUser = null;
let currentAssignment = null;
let ws = null;
let otpTimer = null;
let pendingFeedbackNumber = null;
let currentFilter = null;
let allRegions = [];

function showToast(msg, type) { const t = document.createElement('div'); t.className = 'toast'; t.textContent = msg; document.body.appendChild(t); setTimeout(() => t.remove(), 2000); }
function formatTime() { document.getElementById('currentTime').textContent = new Date().toLocaleTimeString([], { hour:'2-digit', minute:'2-digit' }); }
setInterval(formatTime, 1000); formatTime();
function copyText(t) { navigator.clipboard.writeText(t); showToast('Copied!', 'success'); }

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
                document.getElementById('adminBadge').style.display = 'block';
                document.getElementById('adminNavItem').style.display = 'flex';
                loadAdminData();
            }
            connectWebSocket();
            loadRegions();
            loadCurrentAssignment();
            loadSavedNumbers();
            loadHistory();
            return true;
        }
    } catch(e) { console.error('Auth check failed:', e); }
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
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            credentials: 'include',
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
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            credentials: 'include',
            body: JSON.stringify({ username, password })
        });
        const data = await res.json();
        if (res.ok) {
            if (data.approved) { checkAuth(); }
            else { errorEl.textContent = data.message || 'Awaiting admin approval'; }
        } else { errorEl.textContent = data.detail || 'Registration failed'; }
    } catch(e) { errorEl.textContent = 'Network error'; }
}

function connectWebSocket() {
    const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
    ws = new WebSocket(`${protocol}//${location.host}/ws/user/${currentUser.id}`);
    ws.onmessage = (e) => {
        const data = JSON.parse(e.data);
        if (data.type === 'otp') displayOTP(data);
        else if (data.type === 'broadcast') showToast(`📢 ${data.message}`, 'info');
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
    if (region.is_paused) { showToast(`Region paused: ${region.pause_reason || 'Temporarily unavailable'}`, 'error'); return; }
    try {
        const res = await fetch(`${API_BASE}/api/pools/assign`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            credentials: 'include',
            body: JSON.stringify({ pool_id: poolId })
        });
        const data = await res.json();
        if (res.ok) {
            currentAssignment = data;
            document.getElementById('currentNumber').textContent = data.number;
            document.getElementById('currentRegion').textContent = data.pool_name;
            showToast(`Number assigned: ${data.number}`, 'success');
            document.getElementById('otpDisplay').style.display = 'none';
            if (otpTimer) clearTimeout(otpTimer);
        } else { showToast(data.detail || 'Failed to assign number', 'error'); }
    } catch(e) { showToast('Network error', 'error'); }
}

async function loadCurrentAssignment() {
    try {
        const res = await fetch(`${API_BASE}/api/pools/my-assignment`, { credentials: 'include' });
        const data = await res.json();
        if (data.assignment) { currentAssignment = data.assignment; document.getElementById('currentNumber').textContent = currentAssignment.number; document.getElementById('currentRegion').textContent = currentAssignment.pool_name; }
    } catch(e) {}
}

function changeNumber() {
    if (!currentAssignment) { showToast('No active number to change', 'warning'); return; }
    pendingFeedbackNumber = currentAssignment.number;
    document.getElementById('feedbackNumber').textContent = currentAssignment.number;
    document.getElementById('feedbackModal').classList.add('show');
}

async function submitFeedback(type) {
    const comment = type === 'other' ? document.getElementById('otherFeedbackText').value : type;
    const markAsBad = type === 'bad';
    if (pendingFeedbackNumber) {
        await fetch(`${API_BASE}/api/reviews`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            credentials: 'include',
            body: JSON.stringify({ number: pendingFeedbackNumber, rating: markAsBad ? 1 : 4, comment: comment, mark_as_bad: markAsBad })
        });
        if (currentAssignment) { await fetch(`${API_BASE}/api/pools/release/${currentAssignment.assignment_id}`, { method: 'POST', credentials: 'include' }); }
        currentAssignment = null;
        document.getElementById('currentNumber').textContent = '—';
        document.getElementById('currentRegion').textContent = 'No region selected';
        showToast('Number released. Select a new region', 'success');
    }
    document.getElementById('feedbackModal').classList.remove('show');
    document.getElementById('otherFeedbackDiv').style.display = 'none';
    document.getElementById('otherFeedbackText').value = '';
    pendingFeedbackNumber = null;
    loadRegions();
}

function showOtherFeedback() { document.getElementById('otherFeedbackDiv').style.display = 'block'; }
function copyNumber() { if (currentAssignment) copyText(currentAssignment.number); else showToast('No number to copy', 'warning'); }

async function saveNumbers() {
    const numbers = document.getElementById('savedNumbersInput').value.split('\\n').map(l => l.trim()).filter(l => l);
    const timerMinutes = parseInt(document.getElementById('timerMinutes').value) || 30;
    const poolName = document.getElementById('poolNameInput').value.trim() || 'Default Pool';
    if (!numbers.length) { showToast('Enter at least one number', 'warning'); return; }
    try {
        const res = await fetch(`${API_BASE}/api/saved`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            credentials: 'include',
            body: JSON.stringify({ numbers, timer_minutes: timerMinutes, pool_name: poolName })
        });
        const data = await res.json();
        if (res.ok) { showToast(`Saved ${data.saved} numbers`, 'success'); document.getElementById('savedNumbersInput').value = ''; loadSavedNumbers(); }
        else { showToast(data.detail || 'Failed to save', 'error'); }
    } catch(e) { showToast('Network error', 'error'); }
}

async function loadSavedNumbers() {
    try {
        const res = await fetch(`${API_BASE}/api/saved`, { credentials: 'include' });
        const data = await res.json();
        const container = document.getElementById('savedList');
        if (!data.length) { container.innerHTML = '<div class="loading">No saved numbers</div>'; return; }
        container.innerHTML = data.map(item => {
            let cls = 'timer-green', time = `${Math.floor(item.seconds_left / 60)}m ${item.seconds_left % 60}s`;
            if (item.status === 'yellow') cls = 'timer-yellow';
            if (item.status === 'red') cls = 'timer-red';
            if (item.status === 'ready') { cls = 'timer-ready'; time = 'READY'; }
            return `<div class="saved-item"><div><div class="saved-number">${escapeHtml(item.number)}</div><div class="saved-timer">${escapeHtml(item.pool_name)}</div></div><div><span class="timer-badge ${cls}">${time}</span><button onclick="deleteSaved(${item.id})" style="background:none;border:none;font-size:20px;margin-left:8px;cursor:pointer;">🗑️</button></div></div>`;
        }).join('');
    } catch(e) {}
}

async function deleteSaved(id) { await fetch(`${API_BASE}/api/saved/${id}`, { method: 'DELETE', credentials: 'include' }); loadSavedNumbers(); }

async function loadHistory() {
    try {
        const res = await fetch(`${API_BASE}/api/otp/my`, { credentials: 'include' });
        const data = await res.json();
        const container = document.getElementById('historyList');
        if (!data.length) { container.innerHTML = '<div class="loading">No OTP history</div>'; return; }
        container.innerHTML = data.map(item => `<div class="history-item"><div class="history-number">${escapeHtml(item.number)}</div><div class="history-otp" onclick="copyText('${item.otp_code}')">${item.otp_code} 📋</div><div class="history-time">${new Date(item.delivered_at).toLocaleString()}</div></div>`).join('');
    } catch(e) {}
}

async function loadAdminData() {
    try {
        const res = await fetch(`${API_BASE}/api/admin/stats`, { credentials: 'include' });
        const stats = await res.json();
        document.getElementById('adminStats').innerHTML = `<div>📊 Users: ${stats.total_users}</div><div>⏳ Pending: ${stats.pending_approval}</div><div>🌍 Pools: ${stats.total_pools}</div><div>📞 Numbers: ${stats.total_numbers}</div><div>🔑 OTPs: ${stats.total_otps}</div><div>🚫 Bad: ${stats.bad_numbers}</div><div>💾 Saved: ${stats.saved_numbers}</div><div>🟢 Online: ${stats.online_users}</div>`;
        
        const usersRes = await fetch(`${API_BASE}/api/admin/users`, { credentials: 'include' });
        const users = await usersRes.json();
        document.getElementById('adminUsersList').innerHTML = users.map(u => `<div class="saved-item"><div><div class="saved-number">${escapeHtml(u.username)}</div><div class="saved-timer">ID: ${u.id}</div></div><div>${!u.is_approved ? `<button onclick="approveUser(${u.id})" style="background:#10b981;color:white;border:none;padding:6px 12px;border-radius:20px;">Approve</button>` : ''}${u.is_blocked ? `<button onclick="unblockUser(${u.id})" style="background:#f59e0b;border:none;padding:6px 12px;border-radius:20px;">Unblock</button>` : `<button onclick="blockUser(${u.id})" style="background:#ef4444;border:none;padding:6px 12px;border-radius:20px;">Block</button>`}</div></div>`).join('');
        
        const badRes = await fetch(`${API_BASE}/api/admin/bad-numbers`, { credentials: 'include' });
        const bad = await badRes.json();
        document.getElementById('adminBadList').innerHTML = bad.map(b => `<div class="saved-item"><div><div class="saved-number">${escapeHtml(b.number)}</div><div class="saved-timer">${b.reason}</div></div><div><button onclick="removeBadNumber('${b.number}')" style="background:#ef4444;color:white;border:none;padding:6px 12px;border-radius:20px;">Remove</button></div></div>`).join('');
        
        const reviewsRes = await fetch(`${API_BASE}/api/admin/reviews`, { credentials: 'include' });
        const reviews = await reviewsRes.json();
        document.getElementById('adminReviewsList').innerHTML = reviews.map(r => `<div class="saved-item"><div><div class="saved-number">${escapeHtml(r.number)}</div><div class="saved-timer">${r.comment || 'No comment'}</div></div><div>⭐ ${r.rating}</div></div>`).join('');
    } catch(e) {}
}

async function approveUser(id) { await fetch(`${API_BASE}/api/admin/users/${id}/approve`, { method: 'POST', credentials: 'include' }); loadAdminData(); }
async function blockUser(id) { await fetch(`${API_BASE}/api/admin/users/${id}/block`, { method: 'POST', credentials: 'include' }); loadAdminData(); }
async function unblockUser(id) { await fetch(`${API_BASE}/api/admin/users/${id}/unblock`, { method: 'POST', credentials: 'include' }); loadAdminData(); }
async function removeBadNumber(number) { await fetch(`${API_BASE}/api/admin/bad-numbers?number=${encodeURIComponent(number)}`, { method: 'DELETE', credentials: 'include' }); loadAdminData(); }
async function sendBroadcast() { const msg = document.getElementById('broadcastMsg').value; if (!msg) return; await fetch(`${API_BASE}/api/admin/broadcast?message=${encodeURIComponent(msg)}`, { method: 'POST', credentials: 'include' }); showToast('Broadcast sent!', 'success'); document.getElementById('broadcastMsg').value = ''; }

function navigateTo(page) {
    document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
    document.getElementById(`${page}Page`).classList.add('active');
    document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
    document.querySelector(`.nav-item[data-page="${page}"]`).classList.add('active');
    if (page === 'numbers') loadRegions();
    if (page === 'saved') loadSavedNumbers();
    if (page === 'history') loadHistory();
    if (page === 'admin' && currentUser?.is_admin) loadAdminData();
}

document.querySelectorAll('.nav-item').forEach(i => i.addEventListener('click', () => navigateTo(i.dataset.page)));
document.getElementById('authLoginTab').onclick = () => { document.getElementById('loginForm').style.display = 'block'; document.getElementById('registerForm').style.display = 'none'; document.getElementById('authLoginTab').style.background = '#0a84ff'; document.getElementById('authLoginTab').style.color = 'white'; document.getElementById('authRegisterTab').style.background = '#e2e8f0'; document.getElementById('authRegisterTab').style.color = '#334155'; };
document.getElementById('authRegisterTab').onclick = () => { document.getElementById('loginForm').style.display = 'none'; document.getElementById('registerForm').style.display = 'block'; document.getElementById('authRegisterTab').style.background = '#0a84ff'; document.getElementById('authRegisterTab').style.color = 'white'; document.getElementById('authLoginTab').style.background = '#e2e8f0'; document.getElementById('authLoginTab').style.color = '#334155'; };

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
    global user_counter
    
    username = req.username.strip()
    password = req.password
    
    if len(username) < 3:
        raise HTTPException(400, "Username must be at least 3 characters")
    if len(password) < 6:
        raise HTTPException(400, "Password must be at least 6 characters")
    
    # Check if username exists
    for u in users.values():
        if u["username"] == username:
            raise HTTPException(400, "Username already taken")
    
    is_first = len(users) == 0
    user_id = user_counter
    user_counter += 1
    
    users[user_id] = {
        "id": user_id,
        "username": username,
        "password_hash": hash_password(password),
        "is_admin": is_first,
        "is_approved": is_first,
        "is_blocked": False,
        "created_at": utcnow().isoformat()
    }
    
    if is_first:
        approved_users.add(user_id)
        token = create_token(user_id)
        resp = JSONResponse({"ok": True, "approved": True, "is_admin": True, "user_id": user_id})
        resp.set_cookie("token", token, httponly=True, samesite="lax", max_age=86400*30, path="/")
        return resp
    
    return {"ok": True, "approved": False, "message": "Awaiting admin approval"}

@app.post("/api/auth/login")
async def login(req: LoginRequest):
    username = req.username.strip()
    password = req.password
    
    user = None
    for u in users.values():
        if u["username"] == username:
            user = u
            break
    
    if not user:
        raise HTTPException(401, "Invalid username or password")
    
    if not verify_password(password, user["password_hash"]):
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
    resp.set_cookie("token", token, httponly=True, samesite="lax", max_age=86400*30, path="/")
    return resp

@app.post("/api/auth/logout")
def logout(token: str = Cookie(default=None)):
    if token:
        revoke_token(token)
    resp = JSONResponse({"ok": True})
    resp.delete_cookie("token", path="/")
    return resp

@app.get("/api/auth/me")
def me(token: str = Cookie(default=None)):
    user = get_user_from_token(token)
    if not user:
        raise HTTPException(401, "Not authenticated")
    return {
        "id": user["id"],
        "username": user["username"],
        "is_admin": user["is_admin"],
        "is_approved": user["is_approved"]
    }

# ══════════════════════════════════════════════════════════════════════════════
#  POOLS ENDPOINTS
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/api/pools")
def list_pools(token: str = Cookie(default=None)):
    user = get_user_from_token(token)
    if not user:
        raise HTTPException(401, "Not authenticated")
    
    result = []
    for pid, pool in pools.items():
        if not is_admin(user["id"]) and pool.get("is_admin_only", False):
            continue
        if not has_pool_access(pid, user["id"]):
            continue
        
        count = len(active_numbers.get(pid, []))
        restricted = len(pool_access.get(pid, set())) > 0
        
        result.append({
            "id": pid,
            "name": pool.get("name", ""),
            "country_code": pool.get("country_code", ""),
            "otp_link": pool.get("otp_link", ""),
            "match_format": pool.get("match_format", "5+4"),
            "telegram_match_format": pool.get("telegram_match_format", ""),
            "uses_platform": pool.get("uses_platform", 0),
            "is_paused": pool.get("is_paused", False),
            "pause_reason": pool.get("pause_reason", ""),
            "trick_text": pool.get("trick_text", ""),
            "is_admin_only": pool.get("is_admin_only", False),
            "number_count": count,
            "restricted": restricted,
            "last_restocked": pool.get("last_restocked")
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
    
    pool = pools.get(req.pool_id)
    if not pool:
        raise HTTPException(404, "Pool not found")
    
    if pool.get("is_paused"):
        raise HTTPException(400, f"Pool paused: {pool.get('pause_reason', 'No reason')}")
    
    if pool.get("is_admin_only") and not is_admin(user["id"]):
        raise HTTPException(403, "Admin only pool")
    
    if not has_pool_access(req.pool_id, user["id"]):
        raise HTTPException(403, "No access to this pool")
    
    # Release current assignment
    release_assignment(user["id"])
    
    # Assign new number
    assignment = assign_one_number(user["id"], req.pool_id, req.prefix)
    if not assignment:
        raise HTTPException(400, "No numbers available in this pool")
    
    # Send monitor request if otp_group_id exists
    if pool.get("otp_group_id"):
        match_format = assignment.get("telegram_match_format") or assignment.get("match_format", "5+4")
        await request_monitor_bot(
            number=assignment["number"],
            group_id=pool["otp_group_id"],
            match_format=match_format,
            user_id=user["id"]
        )
    
    # Start platform monitor if needed
    if pool.get("uses_platform") in (MONITOR_PLATFORM, MONITOR_BOTH):
        start_platform_monitor(user["id"], assignment["number"], assignment.get("match_format", "5+4"))
    
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
    global otp_counter
    
    if SHARED_SECRET and payload.secret != SHARED_SECRET:
        raise HTTPException(403, "Invalid secret")
    
    log.info(f"[OTP] Received: {payload.number} -> {payload.otp} for user {payload.user_id}")
    
    # Save OTP
    otp_entry = {
        "id": otp_counter,
        "user_id": payload.user_id,
        "number": payload.number,
        "otp_code": payload.otp,
        "raw_message": payload.raw_message,
        "delivered_at": utcnow().isoformat()
    }
    otp_logs.append(otp_entry)
    otp_counter += 1
    
    # Send to user
    otp_data = {
        "type": "otp",
        "id": otp_entry["id"],
        "number": payload.number,
        "otp": payload.otp,
        "raw_message": payload.raw_message,
        "delivered_at": otp_entry["delivered_at"],
        "auto_delete_seconds": OTP_AUTO_DELETE_DELAY
    }
    await send_to_user(payload.user_id, otp_data)
    await broadcast_feed({
        "type": "feed_otp",
        "number": payload.number,
        "otp": payload.otp,
        "delivered_at": otp_entry["delivered_at"]
    })
    
    # Compliance
    await compliance_record_otp_delivered(payload.user_id)
    
    return {"ok": True}

@app.get("/api/otp/my")
def my_otps(token: str = Cookie(default=None)):
    user = get_user_from_token(token)
    if not user:
        raise HTTPException(401, "Not authenticated")
    
    user_otps = [o for o in otp_logs if o["user_id"] == user["id"]]
    user_otps.sort(key=lambda x: x["delivered_at"], reverse=True)
    
    return [{
        "id": o["id"],
        "number": o["number"],
        "otp_code": o["otp_code"],
        "raw_message": o.get("raw_message", ""),
        "delivered_at": o["delivered_at"]
    } for o in user_otps[:50]]

# ══════════════════════════════════════════════════════════════════════════════
#  SAVED NUMBERS ENDPOINTS
# ══════════════════════════════════════════════════════════════════════════════

class SaveRequest(BaseModel):
    numbers: List[str]
    timer_minutes: int
    pool_name: str

@app.post("/api/saved")
def save_numbers(req: SaveRequest, token: str = Cookie(default=None)):
    global saved_counter
    
    user = get_user_from_token(token)
    if not user:
        raise HTTPException(401, "Not authenticated")
    
    expires_at = utcnow() + timedelta(minutes=req.timer_minutes)
    saved = 0
    skipped = 0
    
    for number in req.numbers:
        number = number.strip()
        if not number:
            continue
        
        # Check if already saved
        existing = [s for s in saved_numbers if s["user_id"] == user["id"] and s["number"] == number and not s.get("moved", False)]
        if existing:
            skipped += 1
            continue
        
        saved_numbers.append({
            "id": saved_counter,
            "user_id": user["id"],
            "number": number,
            "country": "",
            "pool_name": req.pool_name,
            "expires_at": expires_at.isoformat(),
            "moved": False,
            "created_at": utcnow().isoformat()
        })
        saved_counter += 1
        saved += 1
    
    return {"ok": True, "saved": saved, "skipped": skipped, "expires_at": expires_at.isoformat()}

@app.get("/api/saved")
def list_saved(token: str = Cookie(default=None)):
    user = get_user_from_token(token)
    if not user:
        raise HTTPException(401, "Not authenticated")
    
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
    
    ready = [s for s in saved_numbers if s["user_id"] == user["id"] and s.get("moved", False)]
    ready.sort(key=lambda x: x["created_at"], reverse=True)
    
    result = []
    for s in ready:
        pool = None
        for p in pools.values():
            if p["name"] == s["pool_name"]:
                pool = p
                break
        
        in_pool = False
        if pool:
            in_pool = s["number"] in active_numbers.get(pool["id"], [])
        
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
    
    for s in saved_numbers:
        if s["id"] == saved_id and s["user_id"] == user["id"]:
            s["expires_at"] = (utcnow() + timedelta(minutes=timer_minutes)).isoformat()
            s["moved"] = False
            return {"ok": True}
    
    raise HTTPException(404, "Not found")

@app.delete("/api/saved/{saved_id}")
def delete_saved(saved_id: int, token: str = Cookie(default=None)):
    user = get_user_from_token(token)
    if not user:
        raise HTTPException(401, "Not authenticated")
    
    for i, s in enumerate(saved_numbers):
        if s["id"] == saved_id and s["user_id"] == user["id"]:
            saved_numbers.pop(i)
            return {"ok": True}
    
    raise HTTPException(404, "Not found")

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
    global review_counter
    
    user = get_user_from_token(token)
    if not user:
        raise HTTPException(401, "Not authenticated")
    
    if not (1 <= req.rating <= 5):
        raise HTTPException(400, "Rating must be 1-5")
    
    reviews.append({
        "id": review_counter,
        "user_id": user["id"],
        "number": req.number,
        "rating": req.rating,
        "comment": req.comment,
        "created_at": utcnow().isoformat()
    })
    review_counter += 1
    
    if req.mark_as_bad or req.rating == 1:
        add_bad_number(req.number, user["id"], req.comment or "Flagged by user")
    
    # Release assignment if this is the active number
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
    if not user:
        raise HTTPException(401, "Not authenticated")
    if not user["is_admin"]:
        raise HTTPException(403, "Admin only")
    
    return {
        "total_users": len(users),
        "pending_approval": sum(1 for u in users.values() if not u["is_approved"] and not u["is_blocked"]),
        "total_pools": len(pools),
        "total_numbers": sum(len(n) for n in active_numbers.values()),
        "total_otps": len(otp_logs),
        "total_assignments": len(archived_numbers),
        "bad_numbers": len(bad_numbers),
        "saved_numbers": len([s for s in saved_numbers if not s.get("moved", False)]),
        "online_users": sum(len(conns) for conns in user_connections.values())
    }

@app.get("/api/admin/users")
def list_users(token: str = Cookie(default=None)):
    user = get_user_from_token(token)
    if not user:
        raise HTTPException(401, "Not authenticated")
    if not user["is_admin"]:
        raise HTTPException(403, "Admin only")
    
    return [
        {
            "id": u["id"],
            "username": u["username"],
            "is_admin": u["is_admin"],
            "is_approved": u["is_approved"],
            "is_blocked": u["is_blocked"],
            "created_at": u["created_at"]
        }
        for u in users.values()
    ]

@app.post("/api/admin/users/{user_id}/approve")
def approve_user_endpoint(user_id: int, token: str = Cookie(default=None)):
    admin = get_user_from_token(token)
    if not admin or not admin["is_admin"]:
        raise HTTPException(403, "Admin only")
    
    if user_id in users:
        approve_user(user_id)
    return {"ok": True}

@app.post("/api/admin/users/{user_id}/block")
def block_user_endpoint(user_id: int, token: str = Cookie(default=None)):
    admin = get_user_from_token(token)
    if not admin or not admin["is_admin"]:
        raise HTTPException(403, "Admin only")
    
    if user_id in users:
        block_user(user_id)
    return {"ok": True}

@app.post("/api/admin/users/{user_id}/unblock")
def unblock_user_endpoint(user_id: int, token: str = Cookie(default=None)):
    admin = get_user_from_token(token)
    if not admin or not admin["is_admin"]:
        raise HTTPException(403, "Admin only")
    
    if user_id in users:
        unblock_user(user_id)
        approve_user(user_id)
    return {"ok": True}

@app.get("/api/admin/bad-numbers")
def list_bad_numbers(token: str = Cookie(default=None)):
    user = get_user_from_token(token)
    if not user:
        raise HTTPException(401, "Not authenticated")
    if not user["is_admin"]:
        raise HTTPException(403, "Admin only")
    
    return [
        {"number": num, "reason": data.get("reason", ""), "created_at": data.get("marked_at", "")}
        for num, data in bad_numbers.items()
    ]

@app.delete("/api/admin/bad-numbers")
def remove_bad_number(number: str, token: str = Cookie(default=None)):
    user = get_user_from_token(token)
    if not user:
        raise HTTPException(401, "Not authenticated")
    if not user["is_admin"]:
        raise HTTPException(403, "Admin only")
    
    if number in bad_numbers:
        del bad_numbers[number]
    return {"ok": True}

@app.get("/api/admin/reviews")
def list_reviews(token: str = Cookie(default=None)):
    user = get_user_from_token(token)
    if not user:
        raise HTTPException(401, "Not authenticated")
    if not user["is_admin"]:
        raise HTTPException(403, "Admin only")
    
    reviews_sorted = sorted(reviews, key=lambda x: x["created_at"], reverse=True)
    return [{"id": r["id"], "user_id": r["user_id"], "number": r["number"], "rating": r["rating"], "comment": r["comment"], "created_at": r["created_at"]} for r in reviews_sorted[:100]]

@app.post("/api/admin/broadcast")
async def broadcast_message(message: str, token: str = Cookie(default=None)):
    user = get_user_from_token(token)
    if not user:
        raise HTTPException(401, "Not authenticated")
    if not user["is_admin"]:
        raise HTTPException(403, "Admin only")
    
    await broadcast_all({"type": "broadcast", "message": message})
    return {"ok": True}

# ══════════════════════════════════════════════════════════════════════════════
#  POOL MANAGEMENT ENDPOINTS (Admin)
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

@app.post("/api/pools")
def create_pool(req: PoolCreate, token: str = Cookie(default=None)):
    global pool_counter
    
    user = get_user_from_token(token)
    if not user:
        raise HTTPException(401, "Not authenticated")
    if not user["is_admin"]:
        raise HTTPException(403, "Admin only")
    
    # Check if name exists
    for p in pools.values():
        if p["name"] == req.name:
            raise HTTPException(400, "Pool name already exists")
    
    pool_id = pool_counter
    pool_counter += 1
    
    pools[pool_id] = {
        "id": pool_id,
        "name": req.name,
        "country_code": req.country_code,
        "otp_group_id": req.otp_group_id,
        "otp_link": req.otp_link or "",
        "match_format": req.match_format,
        "telegram_match_format": req.telegram_match_format or "",
        "uses_platform": req.uses_platform,
        "is_paused": req.is_paused,
        "pause_reason": req.pause_reason or "",
        "trick_text": req.trick_text or "",
        "is_admin_only": req.is_admin_only,
        "last_restocked": utcnow().isoformat() if not req.is_paused else None,
        "created_at": utcnow().isoformat()
    }
    active_numbers[pool_id] = []
    
    return {"ok": True, "id": pool_id}

@app.put("/api/pools/{pool_id}")
def update_pool(pool_id: int, req: PoolCreate, token: str = Cookie(default=None)):
    user = get_user_from_token(token)
    if not user:
        raise HTTPException(401, "Not authenticated")
    if not user["is_admin"]:
        raise HTTPException(403, "Admin only")
    
    if pool_id not in pools:
        raise HTTPException(404, "Pool not found")
    
    pool = pools[pool_id]
    pool.update({
        "name": req.name,
        "country_code": req.country_code,
        "otp_group_id": req.otp_group_id,
        "otp_link": req.otp_link or "",
        "match_format": req.match_format,
        "telegram_match_format": req.telegram_match_format or "",
        "uses_platform": req.uses_platform,
        "is_paused": req.is_paused,
        "pause_reason": req.pause_reason or "",
        "trick_text": req.trick_text or "",
        "is_admin_only": req.is_admin_only
    })
    
    return {"ok": True}

@app.delete("/api/pools/{pool_id}")
def delete_pool(pool_id: int, token: str = Cookie(default=None)):
    user = get_user_from_token(token)
    if not user:
        raise HTTPException(401, "Not authenticated")
    if not user["is_admin"]:
        raise HTTPException(403, "Admin only")
    
    if pool_id not in pools:
        raise HTTPException(404, "Pool not found")
    
    del pools[pool_id]
    if pool_id in active_numbers:
        del active_numbers[pool_id]
    if pool_id in pool_access:
        del pool_access[pool_id]
    
    return {"ok": True}

@app.post("/api/pools/{pool_id}/upload")
async def upload_numbers(pool_id: int, file: UploadFile = File(...), token: str = Cookie(default=None)):
    user = get_user_from_token(token)
    if not user:
        raise HTTPException(401, "Not authenticated")
    if not user["is_admin"]:
        raise HTTPException(403, "Admin only")
    
    if pool_id not in pools:
        raise HTTPException(404, "Pool not found")
    
    content = await file.read()
    lines = content.decode("utf-8", errors="ignore").splitlines()
    
    added = 0
    skipped_bad = 0
    skipped_dup = 0
    
    phone_re = re.compile(r'(\+?\d{6,15})')
    numbers_set = set(active_numbers.get(pool_id, []))
    
    for line in lines:
        m = phone_re.search(line.strip())
        if not m:
            continue
        
        number = m.group(1)
        
        if number in bad_numbers:
            skipped_bad += 1
            continue
        if number in numbers_set:
            skipped_dup += 1
            continue
        
        numbers_set.add(number)
        active_numbers.setdefault(pool_id, []).append(number)
        added += 1
    
    pools[pool_id]["last_restocked"] = utcnow().isoformat()
    
    return {
        "ok": True,
        "added": added,
        "skipped_bad": skipped_bad,
        "skipped_duplicate": skipped_dup
    }

@app.get("/api/pools/{pool_id}/export")
def export_pool(pool_id: int, token: str = Cookie(default=None)):
    user = get_user_from_token(token)
    if not user:
        raise HTTPException(401, "Not authenticated")
    if not user["is_admin"]:
        raise HTTPException(403, "Admin only")
    
    if pool_id not in pools:
        raise HTTPException(404, "Pool not found")
    
    numbers = active_numbers.get(pool_id, [])
    content = "\n".join(numbers)
    
    return Response(
        content=content,
        media_type="text/plain",
        headers={"Content-Disposition": f"attachment; filename=pool_{pool_id}.txt"}
    )

@app.post("/api/pools/{pool_id}/cut")
def cut_numbers(pool_id: int, count: int, token: str = Cookie(default=None)):
    user = get_user_from_token(token)
    if not user:
        raise HTTPException(401, "Not authenticated")
    if not user["is_admin"]:
        raise HTTPException(403, "Admin only")
    
    if pool_id not in pools:
        raise HTTPException(404, "Pool not found")
    
    numbers = active_numbers.get(pool_id, [])
    removed = numbers[:count]
    active_numbers[pool_id] = numbers[count:]
    
    return {"ok": True, "removed": len(removed)}

@app.get("/api/pools/{pool_id}/access")
def get_pool_access(pool_id: int, token: str = Cookie(default=None)):
    user = get_user_from_token(token)
    if not user:
        raise HTTPException(401, "Not authenticated")
    if not user["is_admin"]:
        raise HTTPException(403, "Admin only")
    
    return get_pool_access_users(pool_id)

@app.post("/api/pools/{pool_id}/access/{user_id}")
def grant_pool_access_endpoint(pool_id: int, user_id: int, token: str = Cookie(default=None)):
    user = get_user_from_token(token)
    if not user:
        raise HTTPException(401, "Not authenticated")
    if not user["is_admin"]:
        raise HTTPException(403, "Admin only")
    
    if pool_id not in pools:
        raise HTTPException(404, "Pool not found")
    if user_id not in users:
        raise HTTPException(404, "User not found")
    
    grant_pool_access(pool_id, user_id)
    return {"ok": True}

@app.delete("/api/pools/{pool_id}/access/{user_id}")
def revoke_pool_access_endpoint(pool_id: int, user_id: int, token: str = Cookie(default=None)):
    user = get_user_from_token(token)
    if not user:
        raise HTTPException(401, "Not authenticated")
    if not user["is_admin"]:
        raise HTTPException(403, "Admin only")
    
    revoke_pool_access(pool_id, user_id)
    return {"ok": True}

# ══════════════════════════════════════════════════════════════════════════════
#  CUSTOM BUTTONS ENDPOINTS
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/api/buttons")
def get_buttons(token: str = Cookie(default=None)):
    user = get_user_from_token(token)
    if not user:
        raise HTTPException(401, "Not authenticated")
    
    return [{"label": b["label"], "url": b["url"]} for b in custom_buttons]

@app.post("/api/buttons")
def add_button(label: str, url: str, token: str = Cookie(default=None)):
    global button_counter
    
    user = get_user_from_token(token)
    if not user:
        raise HTTPException(401, "Not authenticated")
    if not user["is_admin"]:
        raise HTTPException(403, "Admin only")
    
    custom_buttons.append({
        "id": button_counter,
        "label": label,
        "url": url,
        "position": len(custom_buttons)
    })
    button_counter += 1
    
    return {"ok": True}

@app.delete("/api/buttons/{button_id}")
def delete_button(button_id: int, token: str = Cookie(default=None)):
    user = get_user_from_token(token)
    if not user:
        raise HTTPException(401, "Not authenticated")
    if not user["is_admin"]:
        raise HTTPException(403, "Admin only")
    
    for i, b in enumerate(custom_buttons):
        if b["id"] == button_id:
            custom_buttons.pop(i)
            return {"ok": True}
    
    raise HTTPException(404, "Button not found")

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
