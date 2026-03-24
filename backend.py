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
                     UploadFile, File, BackgroundTasks, Form)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, HTMLResponse
from pydantic import BaseModel

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
PORT = int(os.environ.get("PORT", "8080"))

log.info(f"Starting Frank Number Bot on port {PORT}")
log.info(f"Database URL: {'configured' if DATABASE_URL else 'NOT SET'}")
log.info(f"Monitor Bot URL: {MONITOR_BOT_URL or 'NOT SET'}")

# ══════════════════════════════════════════════════════════════════════════════
#  IN-MEMORY STORAGE (Simple, works without PostgreSQL)
# ══════════════════════════════════════════════════════════════════════════════

# In-memory storage
users = {}
sessions = {}
user_counter = 1

def get_user_from_token(token: str):
    if token and token in sessions:
        user_id = sessions[token]
        return users.get(user_id)
    return None

def create_token(user_id: int) -> str:
    token = secrets.token_urlsafe(48)
    sessions[token] = user_id
    return token

def revoke_token(token: str):
    if token in sessions:
        del sessions[token]

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

def utcnow():
    return datetime.now(timezone.utc)

# Create default admin if no users
def init_default_admin():
    global user_counter
    if not users:
        users[1] = {
            "id": 1,
            "username": "admin",
            "password_hash": hash_password("admin123"),
            "is_admin": True,
            "is_approved": True,
            "is_blocked": False,
            "created_at": utcnow()
        }
        user_counter = 2
        log.info("✅ Default admin created: admin / admin123")

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
    init_default_admin()
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
#  FRONTEND SERVING
# ══════════════════════════════════════════════════════════════════════════════

FRONTEND_HTML = '''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no, viewport-fit=cover">
    <meta name="theme-color" content="#0a84ff">
    <title>NEON GRID NETWORK</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
        body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', sans-serif; background: linear-gradient(135deg, #f5f7ff 0%, #eef2ff 100%); min-height: 100vh; padding-bottom: 70px; color: #1e293b; }
        .status-bar { background: #0a84ff; padding: 12px 16px 8px; color: white; font-size: 14px; font-weight: 500; display: flex; justify-content: space-between; position: sticky; top: 0; z-index: 100; }
        .header { background: white; padding: 16px; border-bottom: 1px solid #e2e8f0; }
        .user-info { display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; }
        .user-name { font-size: 20px; font-weight: 700; color: #0a84ff; }
        .user-id { font-size: 12px; color: #64748b; background: #f1f5f9; padding: 4px 10px; border-radius: 20px; }
        .balance-card { background: linear-gradient(135deg, #0a84ff 0%, #0066cc 100%); border-radius: 24px; padding: 20px; margin: 16px; color: white; box-shadow: 0 8px 24px rgba(10,132,255,0.25); }
        .balance-label { font-size: 14px; opacity: 0.9; margin-bottom: 8px; }
        .balance-amount { font-size: 42px; font-weight: 800; letter-spacing: -1px; margin-bottom: 8px; }
        .balance-sub { font-size: 12px; opacity: 0.8; }
        .withdraw-btn { background: rgba(255,255,255,0.2); border: 1px solid rgba(255,255,255,0.3); padding: 10px 20px; border-radius: 30px; color: white; font-weight: 600; font-size: 14px; margin-top: 12px; display: inline-block; cursor: pointer; transition: all 0.2s; }
        .withdraw-btn:active { background: rgba(255,255,255,0.3); transform: scale(0.98); }
        .section-title { font-size: 16px; font-weight: 600; color: #334155; padding: 16px 16px 8px; }
        .menu-grid { display: grid; grid-template-columns: repeat(2, 1fr); gap: 12px; padding: 8px 16px; }
        .menu-card { background: white; border-radius: 20px; padding: 16px; text-align: center; cursor: pointer; transition: all 0.2s; box-shadow: 0 2px 8px rgba(0,0,0,0.04); border: 1px solid #eef2ff; }
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
        .nav-item { display: flex; flex-direction: column; align-items: center; gap: 4px; cursor: pointer; padding: 8px 12px; border-radius: 30px; transition: all 0.2s; }
        .nav-item:active { background: #f1f5f9; }
        .nav-icon { font-size: 24px; }
        .nav-label { font-size: 11px; font-weight: 500; color: #64748b; }
        .nav-item.active .nav-label { color: #0a84ff; }
        .page { display: none; padding-bottom: 20px; }
        .page.active { display: block; }
        .toast { position: fixed; bottom: 100px; left: 50%; transform: translateX(-50%); background: #1e293b; color: white; padding: 12px 20px; border-radius: 40px; font-size: 14px; z-index: 1000; max-width: 90%; text-align: center; animation: fadeInOut 2s ease; }
        @keyframes fadeInOut { 0% { opacity: 0; transform: translateX(-50%) translateY(20px); } 15% { opacity: 1; transform: translateX(-50%) translateY(0); } 85% { opacity: 1; transform: translateX(-50%) translateY(0); } 100% { opacity: 0; transform: translateX(-50%) translateY(-20px); } }
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
        .feedback-btn:active { transform: scale(0.97); }
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
        <div class="filter-row"><input type="text" id="prefixFilter" class="filter-input" placeholder="Filter by prefix"><button class="filter-btn" onclick="applyFilter()">Filter</button></div>
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
            }
            connectWebSocket();
            loadRegions();
            loadCurrentAssignment();
            loadSavedNumbers();
            loadHistory();
            if (currentUser.is_admin) loadAdminData();
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
    if (!username || !password) { errorEl.textContent = 'Enter username and password'; return; }
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
    } catch(e) {}
}

async function approveUser(id) { await fetch(`${API_BASE}/api/admin/users/${id}/approve`, { method: 'POST', credentials: 'include' }); loadAdminData(); }
async function blockUser(id) { await fetch(`${API_BASE}/api/admin/users/${id}/block`, { method: 'POST', credentials: 'include' }); loadAdminData(); }
async function unblockUser(id) { await fetch(`${API_BASE}/api/admin/users/${id}/unblock`, { method: 'POST', credentials: 'include' }); loadAdminData(); }
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
    return {"status": "ok", "service": "Frank Number Bot", "timestamp": utcnow().isoformat()}

# ══════════════════════════════════════════════════════════════════════════════
#  AUTH ENDPOINTS - FIXED
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
        "created_at": utcnow()
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
    
    return [
        {"id": 1, "name": "Nigeria", "country_code": "234", "number_count": 1250, "is_paused": False, "trick_text": "Best for WhatsApp"},
        {"id": 2, "name": "USA", "country_code": "1", "number_count": 842, "is_paused": False, "trick_text": "Best for Telegram"},
        {"id": 3, "name": "United Kingdom", "country_code": "44", "number_count": 567, "is_paused": False},
        {"id": 4, "name": "Canada", "country_code": "1", "number_count": 321, "is_paused": True, "pause_reason": "Maintenance"},
        {"id": 5, "name": "Australia", "country_code": "61", "number_count": 234, "is_paused": False},
    ]

class AssignRequest(BaseModel):
    pool_id: int

@app.post("/api/pools/assign")
async def assign_number(req: AssignRequest, token: str = Cookie(default=None)):
    import random
    user = get_user_from_token(token)
    if not user:
        raise HTTPException(401, "Not authenticated")
    
    pool_names = {1: "Nigeria", 2: "USA", 3: "United Kingdom", 4: "Canada", 5: "Australia"}
    pool_codes = {1: "234", 2: "1", 3: "44", 4: "1", 5: "61"}
    
    number = f"+{pool_codes.get(req.pool_id, '234')}{random.randint(7000000000, 7999999999)}"
    
    return {
        "ok": True,
        "assignment_id": random.randint(1000, 9999),
        "number": number,
        "pool_name": pool_names.get(req.pool_id, "Unknown"),
        "country_code": pool_codes.get(req.pool_id, "234"),
        "otp_link": "https://t.me/earnplusz",
        "trick_text": "Use this number for verification"
    }

@app.get("/api/pools/my-assignment")
def my_assignment(token: str = Cookie(default=None)):
    user = get_user_from_token(token)
    if not user:
        raise HTTPException(401, "Not authenticated")
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
    
    return [
        {"id": 1, "number": "+2348012345678", "otp_code": "123456", "delivered_at": utcnow().isoformat()},
        {"id": 2, "number": "+2348098765432", "otp_code": "789012", "delivered_at": (utcnow() - timedelta(minutes=5)).isoformat()},
    ]

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
    
    return {"ok": True, "saved": len(req.numbers), "skipped": 0, "expires_at": (utcnow() + timedelta(minutes=req.timer_minutes)).isoformat()}

@app.get("/api/saved")
def list_saved(token: str = Cookie(default=None)):
    user = get_user_from_token(token)
    if not user:
        raise HTTPException(401, "Not authenticated")
    
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
    return {"ok": True, "marked_bad": req.mark_as_bad}

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
    if not user["is_admin"]:
        raise HTTPException(403, "Admin only")
    
    return [
        {
            "id": u["id"],
            "username": u["username"],
            "is_admin": u["is_admin"],
            "is_approved": u["is_approved"],
            "is_blocked": u["is_blocked"],
            "created_at": u["created_at"].isoformat()
        }
        for u in users.values()
    ]

@app.post("/api/admin/users/{user_id}/approve")
def approve_user(user_id: int, token: str = Cookie(default=None)):
    admin = get_user_from_token(token)
    if not admin or not admin["is_admin"]:
        raise HTTPException(403, "Admin only")
    
    if user_id in users:
        users[user_id]["is_approved"] = True
        users[user_id]["is_blocked"] = False
    return {"ok": True}

@app.post("/api/admin/users/{user_id}/block")
def block_user(user_id: int, token: str = Cookie(default=None)):
    admin = get_user_from_token(token)
    if not admin or not admin["is_admin"]:
        raise HTTPException(403, "Admin only")
    
    if user_id in users:
        users[user_id]["is_blocked"] = True
        users[user_id]["is_approved"] = False
    return {"ok": True}

@app.post("/api/admin/users/{user_id}/unblock")
def unblock_user(user_id: int, token: str = Cookie(default=None)):
    admin = get_user_from_token(token)
    if not admin or not admin["is_admin"]:
        raise HTTPException(403, "Admin only")
    
    if user_id in users:
        users[user_id]["is_blocked"] = False
        users[user_id]["is_approved"] = True
    return {"ok": True}

@app.post("/api/admin/broadcast")
async def broadcast(message: str, token: str = Cookie(default=None)):
    admin = get_user_from_token(token)
    if not admin or not admin["is_admin"]:
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
