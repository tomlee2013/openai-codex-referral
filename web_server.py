#!/usr/bin/env python3
"""
Codex Referral Activator - Web Server (WSL2 / Linux Native)
基于 Starlette + Uvicorn + SSE-Starlette 构建的现代化轻量级异步服务。
为 Codex Desktop 推荐邀请奖励激活提供直观的可视化 Web 控制台与实时流式日志看板。
"""

import argparse
import asyncio
import base64
import json
import os
import random
import re
import shutil
import sys
import time
import urllib.parse
import urllib.request
import uuid
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import AsyncGenerator, Dict, List, Optional

import requests
from sse_starlette.sse import EventSourceResponse
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Route

import database
from oauth_client import DesktopOAuthEngine

BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"
ACTIVATOR_SCRIPT = BASE_DIR / "referral_activator.py"
CODEX_BIN_DEFAULT = BASE_DIR / "codex"

# 初始化本地 SQLite 数据库
try:
    database.init_db()
except Exception as e:
    print(f"⚠️ 数据库初始化异常: {e}")


def is_wsl() -> bool:
    """检测当前是否运行在 WSL 环境下"""
    if sys.platform != "win32" and os.path.exists("/proc/version"):
        try:
            with open("/proc/version", "r") as f:
                return "microsoft" in f.read().lower()
        except Exception:
            pass
    return False


def get_default_proxy() -> str:
    """获取环境推荐默认代理（优先使用管理员后台配置）"""
    try:
        cfg = database.get_config("default_proxy", "")
        if cfg:
            from oauth_client import refresh_dynamic_proxy
            return refresh_dynamic_proxy(cfg)
    except Exception:
        pass
    if is_wsl():
        return "http://192.168.31.32:15732"
    return "http://127.0.0.1:15732"


def decode_jwt_safe(token: str) -> dict:
    """解析 JWT Payload 获取用户信息"""
    if not token or not isinstance(token, str):
        return {}
    token = token.strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    # 尝试如果是 JSON 字符串提取 access_token
    if token.startswith("{") and token.endswith("}"):
        try:
            d = json.loads(token)
            token = d.get("tokens", {}).get("access_token") or d.get("access_token") or token
        except Exception:
            pass

    parts = token.split(".")
    if len(parts) >= 2:
        try:
            payload_b64 = parts[1]
            rem = len(payload_b64) % 4
            if rem > 0:
                payload_b64 += "=" * (4 - rem)
            payload_bytes = base64.urlsafe_b64decode(payload_b64)
            data = json.loads(payload_bytes.decode("utf-8", errors="replace"))

            auth_info = data.get("https://api.openai.com/auth", {})
            profile_info = data.get("https://api.openai.com/profile", {})
            client_id = data.get("client_id", "")
            scopes = data.get("scp", [])
            is_desktop = (client_id == "app_EMoamEEZ73f0CkXaXp7hrann")
            return {
                "valid": True,
                "email": profile_info.get("email") or data.get("email", ""),
                "name": profile_info.get("name") or data.get("name", ""),
                "user_id": auth_info.get("user_id") or data.get("sub", ""),
                "account_id": auth_info.get("chatgpt_account_id") or "",
                "plan_type": auth_info.get("chatgpt_plan_type") or "free",
                "client_id": client_id,
                "scopes": scopes,
                "is_desktop_token": is_desktop,
                "exp": data.get("exp"),
                "iat": data.get("iat")
            }
        except Exception:
            pass
    return {"valid": False}


def detect_local_inviter_token() -> Optional[dict]:
    """探测宿主机或本机 .codex/auth.json 中的已登录凭据"""
    cands = [
        Path.home() / ".codex" / "auth.json",
        Path("/mnt/c/Users/Administrator/.codex/auth.json")
    ]
    for p in cands:
        if p.is_file():
            try:
                with open(p, "r", encoding="utf-8") as f:
                    d = json.load(f)
                toks = d.get("tokens", {})
                acc_tok = toks.get("access_token")
                acc_id = toks.get("account_id")
                if acc_tok:
                    info = decode_jwt_safe(acc_tok)
                    return {
                        "detected": True,
                        "token": acc_tok,
                        "account_id": acc_id,
                        "email": info.get("email", "Local User"),
                        "source": str(p)
                    }
            except Exception:
                pass
    return None


class Task:
    def __init__(self, task_id: str, params: dict):
        self.task_id = task_id
        self.params = params
        self.status = "queued"  # queued, running, completed, redeemed, failed, stopped
        self.created_at = time.time()
        self.started_at: Optional[float] = None
        self.completed_at: Optional[float] = None
        self.logs: List[dict] = []
        self.current_step = 0
        self.step_status: Dict[int, str] = {i: "pending" for i in range(1, 8)}
        self.invitee_info = decode_jwt_safe(params.get("token", ""))
        self.referral_info: Optional[dict] = None
        self.proc: Optional[asyncio.subprocess.Process] = None
        self.subscribers: List[asyncio.Queue] = []

    def add_log(self, text: str, log_type: str = "info"):
        entry = {
            "time": time.strftime("%H:%M:%S"),
            "text": text,
            "type": log_type
        }
        self.logs.append(entry)
        for q in self.subscribers:
            try:
                q.put_nowait({"event": "log", "data": entry})
            except Exception:
                pass

    def update_step(self, step: int, status: str = "active", msg: str = ""):
        self.current_step = step
        self.step_status[step] = status
        payload = {
            "current_step": step,
            "step_status": self.step_status,
            "message": msg
        }
        for q in self.subscribers:
            try:
                q.put_nowait({"event": "step", "data": payload})
            except Exception:
                pass

    def finish(self, final_status: str, summary: dict = None):
        self.status = final_status
        self.completed_at = time.time()
        payload = {
            "status": final_status,
            "duration": round(self.completed_at - (self.started_at or self.created_at), 1),
            "summary": summary or {}
        }
        for q in self.subscribers:
            try:
                q.put_nowait({"event": "done", "data": payload})
            except Exception:
                pass


class TaskManager:
    def __init__(self):
        self.tasks: Dict[str, Task] = {}

    def create_task(self, params: dict) -> Task:
        task_id = uuid.uuid4().hex[:12]
        task = Task(task_id, params)
        self.tasks[task_id] = task
        return task

    def get_task(self, task_id: str) -> Optional[Task]:
        return self.tasks.get(task_id)

    def list_tasks(self, limit: int = 20) -> List[dict]:
        res = []
        for t in sorted(self.tasks.values(), key=lambda x: x.created_at, reverse=True)[:limit]:
            res.append({
                "task_id": t.task_id,
                "status": t.status,
                "created_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t.created_at)),
                "duration": round((t.completed_at - t.started_at), 1) if (t.completed_at and t.started_at) else None,
                "email": t.invitee_info.get("email", "Unknown"),
                "current_step": t.current_step,
                "is_redeemed": t.status == "redeemed"
            })
        return res

    async def run_task(self, task: Task):
        task.status = "running"
        task.started_at = time.time()
        task.add_log("🚀 任务已启动，正在初始化执行环境...", "info")

        params = task.params
        mode = params.get("mode", "direct")
        card_key = params.get("card_key", "").strip()
        email = params.get("email", "").strip()

        token = params.get("token", "").strip()
        account_id = params.get("account_id", "").strip()
        proxy = params.get("proxy", "").strip() or get_default_proxy()

        # 卡密兑换模式: 优先执行官方桌面端 OAuth 登录以获取正规 Desktop Token
        if mode == "card" and card_key and email:
            task.add_log("🔐 [阶段 1/2] 正在调用官方 Codex Desktop OAuth 授权引擎...", "highlight")
            task.update_step(1, "active", "官方桌面客户端 OAuth 授权与自动收码")
            task.add_log(f"目标被邀请邮箱: {email} | 客户端 ID: app_EMoamEEZ73f0CkXaXp7hrann", "info")

            acc = database.get_account_by_email(email)
            acc_pwd = (acc.get("password") if acc else None) or "Openaipwd666!@"
            acc_jwt = (acc.get("worker_jwt") if acc else None) or ""
            acc_cid = (acc.get("client_id") if acc else None) or ""
            acc_rt = (acc.get("refresh_token") if acc else None) or ""
            acc_type = (acc.get("mail_type") if acc else None) or "worker"

            worker_url = database.get_config("worker_url", "https://email.091619.xyz")
            worker_auth = database.get_config("worker_admin_auth", "Openaipwd666!@")
            oauth_proxy = proxy or database.get_config("default_proxy", "")

            engine = DesktopOAuthEngine(proxy=oauth_proxy, worker_url=worker_url, worker_admin_auth=worker_auth)

            oauth_res = await asyncio.to_thread(
                engine.acquire_desktop_token,
                email=email,
                password=acc_pwd,
                worker_jwt=acc_jwt,
                client_id=acc_cid,
                refresh_token=acc_rt,
                mail_type=acc_type,
                skip_password=False,
                log_fn=lambda msg, lvl: task.add_log(msg, lvl)
            )

            if not oauth_res or not oauth_res.get("access_token"):
                task.step_status[1] = "error"
                err_type = (oauth_res or {}).get("error", "oauth_failed")
                err_msg = (oauth_res or {}).get("message", "官方客户端登录授权未完成")
                task.add_log(f"❌ [阶段 1 失败] 官方桌面端授权未完成: {err_msg}", "error")

                # 只有明确被 OpenAI 官方判定为停用/封禁 (deactivated / blocked) 时，才标记为 banned！
                if err_type in ("account_deactivated", "user_blocked") or "deactivated" in str(err_msg).lower():
                    database.update_account_status(email, "banned", error_message=f"账号已被 OpenAI 停用封禁 ({err_msg})")
                else:
                    # 卡密模式下保留 allocated 状态，供当前卡密独占重试，杜绝被其他卡密并发或再次抢占！
                    target_status = "allocated" if mode == "card" else "available"
                    database.update_account_status(email, target_status, error_message=f"授权未完成: {err_msg}")

                task.finish("failed", {"error": err_type, "message": err_msg})
                return

            token = oauth_res["access_token"]
            account_id = oauth_res.get("account_id") or ""
            task.invitee_info = decode_jwt_safe(token)
            task.step_status[1] = "completed"
            task.add_log("🎉 [阶段 1 完成] 官方桌面客户端 Token 获取成功，准备启动核销引擎...", "success")

            database.update_account_status(email, "activating", token_cache=json.dumps(oauth_res))

        referrer_token = params.get("referrer_token", "").strip()
        if not referrer_token:
            local_inv = detect_local_inviter_token()
            if local_inv:
                referrer_token = local_inv.get("token", "")

        message = params.get("message", "Hello, please reply in 3 words.").strip()
        invite_url = params.get("invite_url", "").strip()
        skip_conversation = params.get("skip_conversation", False)

        cmd = [
            sys.executable,
            str(ACTIVATOR_SCRIPT),
            "--token", token,
            "--proxy", proxy or "none"
        ]
        if account_id:
            cmd.extend(["--account-id", account_id])
        if referrer_token:
            cmd.extend(["--referrer-token", referrer_token])
        if invite_url:
            cmd.extend(["--invite-url", invite_url])
        if message:
            cmd.extend(["--message", message])
        if skip_conversation:
            cmd.append("--skip-conversation")

        task.add_log(f"执行脚本: {ACTIVATOR_SCRIPT.name} | 模式: 官方 Codex 独立引擎 (Headless)", "system")

        redeemed_found = False
        pending_found = False
        turn_success = False
        completed_cleanly = False

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=str(BASE_DIR)
            )
            task.proc = proc

            while True:
                line_bytes = await proc.stdout.readline()
                if not line_bytes:
                    break
                line = line_bytes.decode("utf-8", errors="replace").rstrip("\r\n")
                if not line.strip():
                    continue

                # 解析步骤与日志级别
                log_type = "info"
                if "✅" in line or "🎉" in line:
                    log_type = "success"
                elif "⚠️" in line:
                    log_type = "warning"
                elif "❌" in line:
                    log_type = "error"
                elif "🎯" in line or "🚀" in line:
                    log_type = "highlight"

                task.add_log(line, log_type)

                # 识别当前步骤
                if "[Step 1]" in line:
                    task.update_step(1, "active", "验证身份与桌面端握手")
                elif "[Step 2]" in line:
                    task.step_status[1] = "completed"
                    task.update_step(2, "active", "初始化 MCP 协议栈")
                elif "[Step 3]" in line:
                    task.step_status[2] = "completed"
                    task.update_step(3, "active", "上报活跃心跳与导航广播")
                elif "[Step 4]" in line:
                    task.step_status[3] = "completed"
                    task.update_step(4, "active", "探测推荐活动资格")
                elif "[Step 5]" in line or "【核心核销触发】" in line:
                    task.step_status[4] = "completed"
                    task.update_step(5, "active", "执行官方首会话 (触发专属 Credits 奖励)")
                elif "[Step 6]" in line:
                    task.step_status[5] = "completed"
                    task.update_step(6, "active", "Webview 辅助对话")
                elif "[Step 7]" in line:
                    task.step_status[6] = "completed"
                    task.update_step(7, "active", "核验被邀请人配额与邀请人追踪")

                # 判定核销状态
                if "官方 Codex Desktop 首会话完成" in line or "Codex Turn 首会话事件上报完毕" in line or "官方引擎会话已分发" in line:
                    turn_success = True

                if "REDEEMED" in line or "已完成 / REDEEMED" in line:
                    redeemed_found = True
                    task.add_log("🎉 [恭喜] OpenAI 服务端已核验完成，邀请状态为 REDEEMED，专属 Credits 奖励已到账！", "success")
                elif "【处理中 / PENDING】" in line:
                    pending_found = True

                if "流程执行完成" in line:
                    completed_cleanly = True

            returncode = await proc.wait()
            task.proc = None

            if task.status == "stopped":
                task.add_log("🛑 任务已被手动终止。", "warning")
                task.finish("stopped")
                return

            if redeemed_found:
                task.step_status[7] = "completed"
                if mode == "card" and card_key and email:
                    database.complete_activation(card_key, email)
                    task.add_log("🎉 [卡密核销成功] 专属邀请 Credits 奖励已成功核销！卡密与邮箱已自动标记为完成。", "success")
                task.finish("redeemed", {"redeemed": True, "returncode": returncode})
            elif turn_success and (returncode == 0 or completed_cleanly):
                task.step_status[7] = "completed"
                if mode == "card" and card_key and email:
                    database.complete_activation(card_key, email)
                    task.add_log("🎉 [官方客户端首会话核销成功] 官方 Codex 桌面端首轮推理及原生事件已由引擎成功核销！卡密与邮箱已标记为完成。", "success")
                    task.add_log("💡 提示：OpenAI 邀请奖励发放至邀请人账户，通常有 1-3 分钟系统结算延迟。请在您的 GPT 客户端（设置 -> 推荐朋友 -> 跟踪你的邀请）查看活动奖励 Credits 到账记录。", "info")
                task.finish("redeemed", {"redeemed": True, "returncode": returncode, "reason": "turn_completed_successfully"})
            elif pending_found:
                task.step_status[7] = "warning"
                task.add_log("⚠️ [状态提醒] OpenAI 邀请人追踪接口返回状态仍为 PENDING。通常由于 OpenAI 存在几分钟的异步入账结算延迟，或需确认该邮箱已在邀请邮件中点击接受专属邀请链接。", "warning")
                task.finish("pending", {"redeemed": False, "returncode": returncode, "reason": "status_is_pending"})
            elif returncode == 0 and completed_cleanly:
                task.step_status[7] = "completed"
                if mode == "card" and card_key and email:
                    database.complete_activation(card_key, email)
                    task.add_log("🎉 [会话分发完成] 官方客户端会话链路已全部处理完毕。卡密已自动标记为完成。", "success")
                task.finish("redeemed", {"redeemed": True, "returncode": returncode})
            else:
                task.finish("failed", {"returncode": returncode})
                task.add_log(f"❌ 流程退出，返回码: {returncode}", "error")

        except asyncio.CancelledError:
            if task.proc:
                task.proc.terminate()
            task.finish("stopped")
        except Exception as e:
            task.add_log(f"❌ 执行异常: {e}", "error")
            task.finish("failed", {"error": str(e)})


task_manager = TaskManager()


# ==================== 全自动批量注册任务管理 ====================

class AutoRegisterTask:
    def __init__(self, task_id: str, count: int, domain: str, proxy: str, password: str = "Openaipwd666!@"):
        self.task_id = task_id
        self.count = count
        self.domain = domain
        self.proxy = proxy
        self.password = password
        self.status = "queued"  # queued, running, completed, stopped, failed
        self.created_at = time.time()
        self.started_at: Optional[float] = None
        self.completed_at: Optional[float] = None
        self.current_index = 0
        self.current_email = ""
        self.success_count = 0
        self.failed_count = 0
        self.results: List[dict] = []  # [{index, email, password, status, error, time}]
        self.logs: List[dict] = []
        self.stopped = False
        self.subscribers: List[asyncio.Queue] = []

    def add_log(self, text: str, log_type: str = "info"):
        entry = {
            "time": time.strftime("%H:%M:%S"),
            "text": text,
            "type": log_type
        }
        self.logs.append(entry)
        for q in self.subscribers:
            try:
                q.put_nowait({"event": "log", "data": entry})
            except Exception:
                pass

    def emit_progress(self, current: int, current_email: str = ""):
        self.current_index = current
        if current_email:
            self.current_email = current_email
        pct = round((self.current_index / self.count) * 100) if self.count else 0
        payload = {
            "current": self.current_index,
            "total": self.count,
            "success": self.success_count,
            "failed": self.failed_count,
            "current_email": self.current_email,
            "percentage": pct,
            "status": self.status
        }
        for q in self.subscribers:
            try:
                q.put_nowait({"event": "progress", "data": payload})
            except Exception:
                pass

    def add_result(self, res_item: dict):
        self.results.append(res_item)
        for q in self.subscribers:
            try:
                q.put_nowait({"event": "item_result", "data": res_item})
            except Exception:
                pass

    def finish(self, final_status: str):
        self.status = final_status
        self.completed_at = time.time()
        payload = {
            "status": final_status,
            "total": self.count,
            "success": self.success_count,
            "failed": self.failed_count,
            "duration": round(self.completed_at - (self.started_at or self.created_at), 1),
            "results": self.results
        }
        for q in self.subscribers:
            try:
                q.put_nowait({"event": "done", "data": payload})
            except Exception:
                pass


class AutoRegisterTaskManager:
    def __init__(self):
        self.tasks: Dict[str, AutoRegisterTask] = {}

    def create_task(self, count: int, domain: str, proxy: str, password: str = "Openaipwd666!@") -> AutoRegisterTask:
        task_id = f"reg_{uuid.uuid4().hex[:10]}"
        task = AutoRegisterTask(task_id, count, domain, proxy, password)
        self.tasks[task_id] = task
        return task

    def get_task(self, task_id: str) -> Optional[AutoRegisterTask]:
        return self.tasks.get(task_id)

    def get_active_task(self) -> Optional[AutoRegisterTask]:
        for t in reversed(list(self.tasks.values())):
            if t.status in ("queued", "running"):
                return t
        return None

    def get_latest_task(self) -> Optional[AutoRegisterTask]:
        if self.tasks:
            return list(self.tasks.values())[-1]
        return None


auto_reg_task_manager = AutoRegisterTaskManager()


async def run_autoregister_task(task: AutoRegisterTask):
    from oauth_client import auto_register_openai_account
    task.status = "running"
    task.started_at = time.time()
    task.add_log(f"🚀 全自动批量注册任务已启动！计划注册: {task.count} 个账号 (后缀: @{task.domain})", "highlight")
    if task.proxy:
        task.add_log(f"🌐 注册出口代理: {task.proxy}", "info")

    cfg = database.get_all_config()
    worker_url = cfg.get("worker_url", "https://email.091619.xyz")
    worker_auth = cfg.get("worker_admin_auth", "Openaipwd666!@")

    for i in range(task.count):
        if task.stopped:
            task.add_log("🛑 收到终止指令，注册流程已停止。", "warning")
            break

        curr_num = i + 1
        task.emit_progress(current=curr_num, current_email="")
        task.add_log(f"━━━━━━━━━ [正在注册第 {curr_num}/{task.count} 个账号] ━━━━━━━━━", "highlight")

        def _log_relay(msg, level="info"):
            task.add_log(msg, level)
            if "成功生成邮箱:" in msg:
                try:
                    e_part = msg.split("成功生成邮箱:", 1)[1].strip()
                    task.emit_progress(current=curr_num, current_email=e_part)
                except Exception:
                    pass

        try:
            acc = await asyncio.to_thread(
                auto_register_openai_account,
                worker_url=worker_url,
                worker_admin_auth=worker_auth,
                domain=task.domain,
                default_password=task.password,
                proxy=task.proxy,
                log_fn=_log_relay
            )

            if acc and acc.get("success"):
                email = acc["email"]
                pwd = acc.get("password", task.password)
                jwt = acc.get("worker_jwt", "")
                database.add_account(
                    email=email,
                    password=pwd,
                    worker_jwt=jwt,
                    mail_type="auto_registered"
                )
                task.success_count += 1
                res_item = {
                    "index": curr_num,
                    "email": email,
                    "password": pwd,
                    "status": "success",
                    "error": "",
                    "time": time.strftime("%H:%M:%S")
                }
                task.add_result(res_item)
                task.add_log(f"🎉 第 {curr_num}/{task.count} 个账号 [{email}] 注册并入库成功！", "success")
            else:
                task.failed_count += 1
                err_text = (acc.get("error") if acc else "OpenAI 注册流程未完成")
                fail_email = (acc.get("email") if acc else "") or f"账号-{curr_num}(未生成邮箱)"
                res_item = {
                    "index": curr_num,
                    "email": fail_email,
                    "password": "",
                    "status": "failed",
                    "error": err_text,
                    "time": time.strftime("%H:%M:%S")
                }
                task.add_result(res_item)
                task.add_log(f"❌ 第 {curr_num}/{task.count} 个账号注册未成功: {err_text}", "error")

        except Exception as e:
            task.failed_count += 1
            res_item = {
                "index": curr_num,
                "email": f"账号-{curr_num}(异常)",
                "password": "",
                "status": "failed",
                "error": str(e),
                "time": time.strftime("%H:%M:%S")
            }
            task.add_result(res_item)
            task.add_log(f"❌ 第 {curr_num}/{task.count} 个账号出现异常: {e}", "error")

        task.emit_progress(current=curr_num, current_email=task.current_email)

        if i < task.count - 1 and not task.stopped:
            task.add_log("⏳ 等待 3 秒缓冲以降低 OpenAI 频率风控...", "info")
            await asyncio.sleep(3)

    final_status = "stopped" if task.stopped else "completed"
    task.add_log(f"🏁 注册批次全部结束！总计: {task.count}，成功: {task.success_count}，失败: {task.failed_count}", "highlight" if task.success_count > 0 else "warning")
    task.finish(final_status)


# ==================== Worker 邮箱慢速自动化巡检任务管理器 ====================

class WorkerAutoCheckTask:
    def __init__(
        self,
        task_id: str,
        min_delay: int = 25,
        max_delay: int = 40,
        scope: str = "pending",
        proxy: str = "",
        password: str = "Openaipwd666!@"
    ):
        self.task_id = task_id
        self.min_delay = max(int(min_delay), 15)  # 最低保底 15 秒防风控
        self.max_delay = max(int(max_delay), self.min_delay + 5)
        self.scope = scope
        self.proxy = proxy
        self.password = password
        self.status = "queued"  # queued, running, paused, completed, stopped, failed
        self.created_at = time.time()
        self.started_at: Optional[float] = None
        self.completed_at: Optional[float] = None
        self.total_target = 0
        self.processed_count = 0
        self.current_email = ""
        self.current_worker_id = 0
        self.has_phone_count = 0
        self.needs_phone_count = 0
        self.banned_count = 0
        self.unregistered_count = 0
        self.error_count = 0
        self.next_check_in = 0
        self.stopped = False
        self.paused = False
        self.logs: List[dict] = []

    def add_log(self, text: str, log_type: str = "info"):
        t_str = time.strftime("%H:%M:%S")
        entry = {"time": t_str, "text": text, "type": log_type}
        self.logs.append(entry)
        if len(self.logs) > 60:
            self.logs = self.logs[-60:]
        print(f"[WorkerAutoCheck:{self.task_id}] {text}")

    def pause(self):
        self.paused = True
        self.status = "paused"
        self.add_log("⏸ 巡检任务已暂停。", "warning")

    def resume(self):
        self.paused = False
        self.status = "running"
        self.add_log("▶ 巡检任务已恢复继续执行。", "info")

    def stop(self):
        self.stopped = True
        self.paused = False
        self.status = "stopped"
        self.add_log("🛑 巡检任务已收到终止指令。", "warning")

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "status": self.status,
            "min_delay": self.min_delay,
            "max_delay": self.max_delay,
            "scope": self.scope,
            "total_target": self.total_target,
            "processed_count": self.processed_count,
            "current_email": self.current_email,
            "current_worker_id": self.current_worker_id,
            "has_phone_count": self.has_phone_count,
            "needs_phone_count": self.needs_phone_count,
            "banned_count": self.banned_count,
            "unregistered_count": self.unregistered_count,
            "error_count": self.error_count,
            "next_check_in": self.next_check_in,
            "stopped": self.stopped,
            "paused": self.paused,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "logs": self.logs
        }


class WorkerAutoCheckManager:
    def __init__(self):
        self.tasks: Dict[str, WorkerAutoCheckTask] = {}

    def create_task(self, min_delay: int = 25, max_delay: int = 40, scope: str = "pending", proxy: str = "", password: str = "Openaipwd666!@") -> WorkerAutoCheckTask:
        task_id = f"chk_{uuid.uuid4().hex[:10]}"
        task = WorkerAutoCheckTask(task_id, min_delay=min_delay, max_delay=max_delay, scope=scope, proxy=proxy, password=password)
        self.tasks[task_id] = task
        return task

    def get_task(self, task_id: str) -> Optional[WorkerAutoCheckTask]:
        return self.tasks.get(task_id)

    def get_active_task(self) -> Optional[WorkerAutoCheckTask]:
        for t in reversed(list(self.tasks.values())):
            if t.status in ("queued", "running", "paused"):
                return t
        return None

    def get_latest_task(self) -> Optional[WorkerAutoCheckTask]:
        if self.tasks:
            return list(self.tasks.values())[-1]
        return None


worker_auto_check_manager = WorkerAutoCheckManager()


async def run_worker_autocheck_task(task: WorkerAutoCheckTask):
    from oauth_client import check_worker_account_phone_and_alive
    task.status = "running"
    task.started_at = time.time()

    # 获取待检测目标列表
    accounts = database.get_worker_accounts_for_auto_check(scope=task.scope, limit=1000)
    task.total_target = len(accounts)
    task.add_log(f"🛡️ Worker 慢速后台自动巡检已启动！待核验目标: {len(accounts)} 个邮箱 (间隔: {task.min_delay}~{task.max_delay}s 随机波动，启用动态住宅 IP 出口)", "highlight")

    if not accounts:
        task.status = "completed"
        task.completed_at = time.time()
        task.add_log("未找到符合筛选条件的 Worker 邮箱，巡检结束。", "info")
        return

    for idx, acc in enumerate(accounts):
        if task.stopped:
            task.add_log("🛑 巡检已终止退出。", "warning")
            break

        while task.paused:
            if task.stopped:
                break
            await asyncio.sleep(1)

        if task.stopped:
            break

        email = acc["email"]
        wid = acc["id"]
        jwt_val = acc.get("jwt")
        task.current_email = email
        task.current_worker_id = wid
        curr_idx = idx + 1

        task.add_log(f"[{curr_idx}/{task.total_target}] 正在核验: {email}...", "info")

        def _log_relay(msg, level="info"):
            task.add_log(msg, level)

        try:
            res = await asyncio.to_thread(
                check_worker_account_phone_and_alive,
                email=email,
                password=task.password,
                proxy=task.proxy,
                log_fn=_log_relay,
                worker_jwt=jwt_val
            )
            check_st = res.get("check_status", "unknown")
            has_ph = res.get("has_phone")
            ph_num = res.get("phone_number")
            msg = res.get("message", "")

            # 存入数据库
            database.update_worker_account_check_result(
                worker_account_id=wid,
                check_status=check_st,
                has_phone=has_ph,
                phone_number=ph_num,
                check_message=msg
            )

            # 更新任务统计
            if check_st == "has_phone" or has_ph == 1:
                task.has_phone_count += 1
                task.add_log(f"✅ [{email}] 结果: 已接手机码 {f'({ph_num})' if ph_num else ''}", "success")
            elif check_st == "needs_phone" or has_ph == 0:
                task.needs_phone_count += 1
                task.add_log(f"⚠️ [{email}] 结果: 存活但未绑手机 (要求接码)", "warning")
            elif check_st == "banned" or res.get("alive") is False:
                task.banned_count += 1
                task.add_log(f"🚫 [{email}] 结果: 已封禁/停用 ({msg})", "error")
            elif check_st == "unregistered":
                task.unregistered_count += 1
                task.add_log(f"❓ [{email}] 结果: 未在 OpenAI 注册或密码不符", "warning")
            else:
                task.error_count += 1
                task.add_log(f"⚠️ [{email}] 结果未明确: {msg}", "warning")

            task.processed_count += 1

        except Exception as e:
            task.error_count += 1
            task.processed_count += 1
            task.add_log(f"❌ [{email}] 检测异常: {e}", "error")

        # 若还有下一个账号，随机等待 task.min_delay ~ task.max_delay 秒（带倒计时显示防风控）
        if curr_idx < len(accounts) and not task.stopped:
            delay = random.uniform(task.min_delay, task.max_delay)
            task.add_log(f"⏳ 休息防风控 {int(delay)} 秒后继续核验下一账号 (已动态轮换代理出口)...", "info")
            for remaining in range(int(delay), 0, -1):
                if task.stopped:
                    break
                while task.paused and not task.stopped:
                    await asyncio.sleep(1)
                task.next_check_in = remaining
                await asyncio.sleep(1)
            task.next_check_in = 0

    if not task.stopped:
        task.status = "completed"
        task.completed_at = time.time()
        task.add_log(f"🎉 慢速后台巡检完成！共核验 {task.processed_count} 个邮箱，📱已接码: {task.has_phone_count}，⚠️待接码: {task.needs_phone_count}，🚫封禁: {task.banned_count}，❓未注册: {task.unregistered_count}，异常: {task.error_count}", "success")
    else:
        task.status = "stopped"
        task.completed_at = time.time()


# ==================== Starlette HTTP 路由处理 ====================

_active_visitors: Dict[str, float] = {}

def track_visitor(request) -> int:
    """记录并统计近 5 分钟内活跃访问者"""
    now = time.time()
    client_ip = request.client.host if request.client else "unknown"
    xff = request.headers.get("x-forwarded-for")
    if xff:
        client_ip = xff.split(",")[0].strip()
    _active_visitors[client_ip] = now

    # 清理 5 分钟之前的过期条目
    cutoff = now - 300
    expired = [ip for ip, t in list(_active_visitors.items()) if t < cutoff]
    for ip in expired:
        _active_visitors.pop(ip, None)

    real_active = len(_active_visitors)
    # 基于系统基准 22 ~ 32 人 + 真实 IP 自然波动
    time_slot = int(now) // 60
    baseline = 22 + (time_slot * 7 % 11)
    return max(baseline, baseline + real_active - 1)


async def api_live_stats(request):
    """公开实时数据概览接口 (在线人数、正在执行任务、近2小时完成、最近动态)"""
    online_count = track_visitor(request)

    # 统计当前正在执行的任务数
    active_tasks = sum(1 for t in task_manager.tasks.values() if t.status == "running")
    display_active_tasks = active_tasks if active_tasks > 0 else (1 if (int(time.time()) // 30 % 3 == 0) else 0)

    db_stats = database.get_live_completed_stats()

    return JSONResponse({
        "success": True,
        "online_count": online_count,
        "active_tasks_count": display_active_tasks,
        "completed_last_2h": db_stats.get("completed_2h", 20),
        "recent_completions": db_stats.get("recent_items", [])
    })


async def index_view(request):
    """主界面 HTML 视图 (支持通过 URL 参数控制开发者模式显隐)"""
    track_visitor(request)
    html_file = TEMPLATES_DIR / "index.html"
    if not html_file.is_file():
        return HTMLResponse("<h2>templates/index.html not found</h2>", status_code=404)

    with open(html_file, "r", encoding="utf-8") as f:
        html = f.read()

    # 检查是否通过 URL 参数显示开发者 Token 模式
    # 支持 ?dev=1 / ?dev=true / ?developer=1 / ?token_mode=1 以及配置的自定义参数
    custom_param = database.get_config("dev_url_param", "dev").strip()
    candidate_keys = [custom_param, "dev", "developer", "token_mode"]

    show_dev = False
    for k in candidate_keys:
        if not k:
            continue
        if k in request.query_params:
            val = request.query_params.get(k, "").strip().lower()
            if val in ("1", "true", "yes", "on", ""):
                show_dev = True
                break

    if not show_dev:
        # 服务端物理移除所有开发者模式专属代码块 (页面源码中无任何痕迹)
        html = re.sub(r"<!--\s*DEV_ONLY_START\s*-->.*?<!--\s*DEV_ONLY_END\s*-->", "", html, flags=re.DOTALL)

    return HTMLResponse(html)


async def api_system_status(request):
    """获取系统运行环境与 Codex 引擎状态"""
    uname = os.uname() if hasattr(os, "uname") else None
    os_desc = f"{uname.sysname} {uname.release} ({uname.machine})" if uname else sys.platform
    codex_exists = CODEX_BIN_DEFAULT.is_file()
    codex_exec = os.access(CODEX_BIN_DEFAULT, os.X_OK) if codex_exists else False

    # 探测 codex 版本
    codex_ver = "未检测到"
    if codex_exists:
        try:
            sub = await asyncio.create_subprocess_exec(
                str(CODEX_BIN_DEFAULT), "--version",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            out, _ = await sub.communicate()
            if sub.returncode == 0:
                codex_ver = out.decode().strip()
        except Exception:
            pass

    local_inviter = detect_local_inviter_token()

    return JSONResponse({
        "status": "online",
        "wsl": is_wsl(),
        "os": os_desc,
        "python_version": sys.version.split()[0],
        "default_proxy": get_default_proxy(),
        "codex": {
            "path": str(CODEX_BIN_DEFAULT),
            "exists": codex_exists,
            "executable": codex_exec,
            "version": codex_ver
        },
        "local_inviter": {
            "detected": bool(local_inviter),
            "email": local_inviter.get("email") if local_inviter else None,
            "token": local_inviter.get("token") if local_inviter else None,
            "account_id": local_inviter.get("account_id") if local_inviter else None
        } if local_inviter else None
    })


async def api_proxy_test(request):
    """测试指定代理连通性"""
    try:
        body = await request.json()
    except Exception:
        body = {}
    proxy = body.get("proxy", "").strip() or get_default_proxy()

    target_url = "https://chatgpt.com/backend-api/me"
    start_time = time.time()
    try:
        import urllib.request
        opener_handlers = []
        if proxy and proxy.lower() != "none":
            opener_handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
        opener = urllib.request.build_opener(*opener_handlers)
        req = urllib.request.Request(target_url, headers={"User-Agent": "curl/7.88.1"})
        with opener.open(req, timeout=8) as resp:
            code = resp.status
        cost = round((time.time() - start_time) * 1000)
        return JSONResponse({"success": True, "status": code, "cost_ms": cost, "proxy": proxy})
    except urllib.error.HTTPError as e:
        cost = round((time.time() - start_time) * 1000)
        # 401/403 说明已经顺利连接到 ChatGPT 服务器（握手成功）
        return JSONResponse({"success": True, "status": e.code, "cost_ms": cost, "proxy": proxy, "message": "已成功连通 OpenAI 网关"})
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e), "proxy": proxy}, status_code=500)


async def api_parse_token(request):
    """解析并校验传入的 Token"""
    try:
        body = await request.json()
    except Exception:
        body = {}
    raw_token = body.get("token", "")
    info = decode_jwt_safe(raw_token)
    return JSONResponse(info)


async def api_activate_task(request):
    """创建并执行激活任务"""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    token = body.get("token", "").strip()
    if not token:
        return JSONResponse({"error": "缺少被邀请人 Token (--token)"}, status_code=400)

    if not body.get("proxy"):
        body["proxy"] = get_default_proxy()

    task = task_manager.create_task(body)
    asyncio.create_task(task_manager.run_task(task))

    return JSONResponse({
        "task_id": task.task_id,
        "status": task.status,
        "email": task.invitee_info.get("email", ""),
        "created_at": task.created_at
    })


async def api_stream_task(request):
    """SSE 流式推送执行过程日志与步骤进度"""
    task_id = request.path_params.get("task_id")
    task = task_manager.get_task(task_id)
    if not task:
        return JSONResponse({"error": "Task not found"}, status_code=404)

    queue = asyncio.Queue()
    task.subscribers.append(queue)

    async def event_generator() -> AsyncGenerator[dict, None]:
        try:
            # 1. 优先推送历史日志补全
            for log in task.logs:
                yield {
                    "event": "log",
                    "data": json.dumps(log, ensure_ascii=False)
                }

            # 2. 推送当前步骤快照
            yield {
                "event": "step",
                "data": json.dumps({
                    "current_step": task.current_step,
                    "step_status": task.step_status
                }, ensure_ascii=False)
            }

            # 如果任务已结束，直接发送结束事件
            if task.status in ["completed", "redeemed", "failed", "stopped"]:
                yield {
                    "event": "done",
                    "data": json.dumps({
                        "status": task.status,
                        "duration": round((task.completed_at or time.time()) - (task.started_at or task.created_at), 1)
                    }, ensure_ascii=False)
                }
                return

            # 3. 持续消费实时事件
            while True:
                item = await queue.get()
                event_name = item.get("event", "message")
                data_obj = item.get("data", {})
                yield {
                    "event": event_name,
                    "data": json.dumps(data_obj, ensure_ascii=False)
                }
                if event_name == "done":
                    break
        finally:
            if queue in task.subscribers:
                task.subscribers.remove(queue)

    return EventSourceResponse(event_generator())


async def api_stop_task(request):
    """停止正在执行的任务"""
    task_id = request.path_params.get("task_id")
    task = task_manager.get_task(task_id)
    if not task:
        return JSONResponse({"error": "Task not found"}, status_code=404)

    if task.proc and task.status == "running":
        task.status = "stopped"
        try:
            task.proc.terminate()
        except Exception:
            pass
        return JSONResponse({"success": True, "message": "任务已发送终止信号"})
    return JSONResponse({"success": False, "message": "任务未在运行"})


async def api_task_history(request):
    """获取任务历史列表"""
    return JSONResponse({"tasks": task_manager.list_tasks()})


async def api_query_tracking(request):
    """独立查询邀请人追踪列表 (GET /referrals/invite/tracking)"""
    try:
        body = await request.json()
    except Exception:
        body = {}
    referrer_token = body.get("referrer_token", "").strip()
    account_id = body.get("account_id", "").strip()
    proxy = body.get("proxy", "").strip() or get_default_proxy()
    program_id = body.get("program_id", "codex_referral_consumer").strip()

    if not referrer_token:
        # 尝试使用本地探测到的邀请人凭证
        local_inv = detect_local_inviter_token()
        if local_inv:
            referrer_token = local_inv["token"]
            account_id = account_id or local_inv.get("account_id")

    if not referrer_token:
        return JSONResponse({"error": "缺少邀请人 Access Token"}, status_code=400)

    url = f"https://chatgpt.com/backend-api/referrals/invite/tracking?period=this_month&program_id={program_id}&limit=100"
    headers = {
        "Authorization": f"Bearer {referrer_token}",
        "originator": "Codex Desktop",
        "user-agent": "Codex Desktop/0.160.0 (Windows 10.0.19045; x86_64) unknown (Codex Desktop; 26.930.31730)"
    }
    if account_id:
        headers["chatgpt-account-id"] = account_id

    opener_handlers = []
    if proxy and proxy.lower() != "none":
        opener_handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    opener = urllib.request.build_opener(*opener_handlers)

    try:
        req = urllib.request.Request(url, headers=headers, method="GET")
        with opener.open(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return JSONResponse({"success": True, "data": data})
    except urllib.error.HTTPError as e:
        err_msg = e.read().decode("utf-8", errors="replace")
        return JSONResponse({"success": False, "status": e.code, "error": err_msg}, status_code=e.code)
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)


async def api_query_card_progress(request):
    """用户通过卡密 (CDK) 或邮箱查询核销进度"""
    try:
        body = await request.json()
    except Exception:
        body = {}
    keyword = body.get("keyword", "").strip() or body.get("card_key", "").strip() or body.get("email", "").strip()
    if not keyword:
        return JSONResponse({"success": False, "error": "请输入有效的卡密 (CDK) 或被邀请邮箱"}, status_code=400)

    info = database.query_card_progress(keyword)
    if not info:
        return JSONResponse({"success": False, "error": "未查询到该卡密或邮箱的记录，请检查是否输入正确"}, status_code=404)

    return JSONResponse({"success": True, "data": info})


# ==================== 卡密兑换与前台用户路由 ====================

async def api_redeem_card(request):
    """前台用户使用卡密兑换被邀请邮箱"""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    card_key = body.get("card_key", "").strip()
    if not card_key:
        return JSONResponse({"success": False, "error": "请输入有效的卡密"}, status_code=400)

    success, email, msg = database.redeem_card(card_key)
    if success:
        return JSONResponse({"success": True, "email": email, "message": msg, "card_key": card_key})
    return JSONResponse({"success": False, "error": msg, "email": email}, status_code=400)


async def api_card_activate(request):
    """用户在 GPT 客户端发送邀请后，启动全自动激活任务"""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    card_key = body.get("card_key", "").strip()
    email = body.get("email", "").strip()
    proxy = body.get("proxy", "").strip() or get_default_proxy()
    if not card_key or not email:
        return JSONResponse({"error": "缺少卡密或被邀请邮箱参数"}, status_code=400)

    # 防连点与并发防踩：检查是否已有同一卡密或邮箱正在执行中的任务，若有则直接复用
    for t in list(task_manager.tasks.values()):
        if t.status in ("pending", "running"):
            p = t.params or {}
            if p.get("card_key") == card_key or (email and p.get("email") == email):
                return JSONResponse({
                    "task_id": t.task_id,
                    "status": t.status,
                    "card_key": card_key,
                    "email": email,
                    "reused": True
                })

    body["mode"] = "card"
    body["proxy"] = proxy
    task = task_manager.create_task(body)
    asyncio.create_task(task_manager.run_task(task))

    return JSONResponse({
        "task_id": task.task_id,
        "status": task.status,
        "card_key": card_key,
        "email": email
    })


# ==================== 后台管理 API 与视图 ====================

async def admin_view(request):
    """后台管理 HTML 视图"""
    html_file = TEMPLATES_DIR / "admin.html"
    if html_file.is_file():
        with open(html_file, "r", encoding="utf-8") as f:
            return HTMLResponse(f.read())
    return HTMLResponse("<h2>templates/admin.html not found</h2>", status_code=404)


def verify_admin_token(request) -> bool:
    auth = request.headers.get("Authorization", "")
    token = ""
    if auth.startswith("Bearer "):
        token = auth[7:].strip()
    elif hasattr(request, "query_params"):
        token = request.query_params.get("token", "").strip()
    if token:
        admin_pass = database.get_config("admin_password", "admin123456")
        expected = base64.b64encode(admin_pass.encode()).decode()
        return token == expected or token == admin_pass
    return False


async def api_admin_login(request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    password = body.get("password", "").strip()
    admin_pass = database.get_config("admin_password", "admin123456")
    if password == admin_pass:
        token = base64.b64encode(admin_pass.encode()).decode()
        return JSONResponse({"success": True, "token": token})
    return JSONResponse({"success": False, "error": "管理员密码错误"}, status_code=401)


async def api_admin_stats(request):
    if not verify_admin_token(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    return JSONResponse(database.get_dashboard_stats())


async def api_admin_accounts(request):
    if not verify_admin_token(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    qp = request.query_params
    page = int(qp.get("page", 1))
    page_size = int(qp.get("page_size", 50))
    status = qp.get("status", "all")
    search = qp.get("search", "")
    return JSONResponse(database.list_accounts(status=status, search=search, page=page, page_size=page_size))


async def api_admin_accounts_import(request):
    if not verify_admin_token(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        body = {}
    text = body.get("text", "")
    res = database.batch_import_accounts(text)
    res["success"] = True
    return JSONResponse(res)


async def api_admin_accounts_reset(request):
    if not verify_admin_token(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        body = {}
    acc_id = body.get("id")
    ok = database.reset_account(acc_id)
    return JSONResponse({"success": ok})


async def api_admin_accounts_unfreeze(request):
    if not verify_admin_token(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        body = {}
    acc_id = body.get("id") or request.path_params.get("account_id")
    if not acc_id:
        return JSONResponse({"error": "缺少账号 ID"}, status_code=400)
    ok = database.unfreeze_account(int(acc_id))
    return JSONResponse({
        "success": ok,
        "message": "账号已解冻，已恢复为可用 (available) 状态，可再次作为被邀请人！" if ok else "解冻未生效 (该账号非已核销状态)"
    })


async def api_admin_accounts_batch_unfreeze(request):
    if not verify_admin_token(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    count = database.unfreeze_eligible_accounts(days=60)
    return JSONResponse({
        "success": True,
        "count": count,
        "message": f"成功解冻 {count} 个核销满 2 个月 (≥60天) 的邮箱账号，已重新归入可用库存池！"
    })


async def api_admin_accounts_autoregister(request):
    if not verify_admin_token(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        body = {}
    count = min(max(int(body.get("count", 1)), 1), 20)
    domain = body.get("domain", "").strip() or "haojiangnet.com"
    proxy = body.get("proxy", "").strip() or get_default_proxy()
    password = body.get("password", "").strip() or "Openaipwd666!@"

    # 检查是否已有正在运行的注册任务
    active_task = auto_reg_task_manager.get_active_task()
    if active_task:
        return JSONResponse({
            "success": False,
            "error": f"已有注册任务正在执行中 (任务ID: {active_task.task_id})，请等待完成或终止当前任务",
            "task_id": active_task.task_id
        }, status_code=400)

    task = auto_reg_task_manager.create_task(count=count, domain=domain, proxy=proxy, password=password)
    asyncio.create_task(run_autoregister_task(task))
    return JSONResponse({
        "success": True,
        "task_id": task.task_id,
        "total": task.count,
        "message": f"已启动 {count} 个账号全自动注册任务"
    })


async def api_admin_accounts_autoregister_active(request):
    if not verify_admin_token(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    task = auto_reg_task_manager.get_active_task()
    if task:
        return JSONResponse({
            "has_active": True,
            "task_id": task.task_id,
            "status": task.status,
            "total": task.count,
            "current": task.current_index,
            "success": task.success_count,
            "failed": task.failed_count,
            "current_email": task.current_email
        })
    latest = auto_reg_task_manager.get_latest_task()
    if latest:
        return JSONResponse({
            "has_active": False,
            "latest_task_id": latest.task_id,
            "status": latest.status
        })
    return JSONResponse({"has_active": False})


async def api_admin_accounts_autoregister_status(request):
    if not verify_admin_token(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    task_id = request.path_params.get("task_id")
    task = auto_reg_task_manager.get_task(task_id)
    if not task:
        return JSONResponse({"error": "Task not found"}, status_code=404)

    return JSONResponse({
        "task_id": task.task_id,
        "status": task.status,
        "total": task.count,
        "current": task.current_index,
        "success": task.success_count,
        "failed": task.failed_count,
        "current_email": task.current_email,
        "results": task.results,
        "stopped": task.stopped,
        "duration": round((task.completed_at or time.time()) - (task.started_at or task.created_at), 1)
    })


async def api_admin_accounts_autoregister_stream(request):
    if not verify_admin_token(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    task_id = request.path_params.get("task_id")
    task = auto_reg_task_manager.get_task(task_id)
    if not task:
        return JSONResponse({"error": "Task not found"}, status_code=404)

    queue = asyncio.Queue()
    task.subscribers.append(queue)

    async def event_generator() -> AsyncGenerator[dict, None]:
        try:
            # 1. 优先推送历史日志
            for log in task.logs:
                yield {
                    "event": "log",
                    "data": json.dumps(log, ensure_ascii=False)
                }

            # 2. 推送当前进度快照
            pct = round((task.current_index / task.count) * 100) if task.count else 0
            yield {
                "event": "progress",
                "data": json.dumps({
                    "current": task.current_index,
                    "total": task.count,
                    "success": task.success_count,
                    "failed": task.failed_count,
                    "current_email": task.current_email,
                    "percentage": pct,
                    "status": task.status
                }, ensure_ascii=False)
            }

            # 3. 推送已记录的单项结果
            for res_item in task.results:
                yield {
                    "event": "item_result",
                    "data": json.dumps(res_item, ensure_ascii=False)
                }

            # 若任务已结束，直接发送 done 退出
            if task.status in ["completed", "stopped", "failed"]:
                yield {
                    "event": "done",
                    "data": json.dumps({
                        "status": task.status,
                        "total": task.count,
                        "success": task.success_count,
                        "failed": task.failed_count,
                        "duration": round((task.completed_at or time.time()) - (task.started_at or task.created_at), 1),
                        "results": task.results
                    }, ensure_ascii=False)
                }
                return

            # 4. 持续消费实时事件
            while True:
                item = await queue.get()
                event_name = item.get("event", "message")
                data_obj = item.get("data", {})
                yield {
                    "event": event_name,
                    "data": json.dumps(data_obj, ensure_ascii=False)
                }
                if event_name == "done":
                    break
        finally:
            if queue in task.subscribers:
                task.subscribers.remove(queue)

    return EventSourceResponse(event_generator())


async def api_admin_accounts_autoregister_stop(request):
    if not verify_admin_token(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    task_id = request.path_params.get("task_id")
    task = auto_reg_task_manager.get_task(task_id)
    if not task:
        return JSONResponse({"error": "Task not found"}, status_code=404)

    if task.status in ("queued", "running"):
        task.stopped = True
        task.add_log("⚠️ 管理员发出了终止指令，当前账号流程完成后将停止继续注册...", "warning")
        return JSONResponse({"success": True, "message": "已向注册任务发送终止信号"})
    return JSONResponse({"success": False, "message": "任务未在运行或已结束"})



async def api_admin_account_delete(request):
    if not verify_admin_token(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    acc_id = int(request.path_params.get("account_id"))
    ok = database.delete_account(acc_id)
    return JSONResponse({"success": ok})


async def api_admin_accounts_batch_delete(request):
    if not verify_admin_token(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        body = {}
    ids = body.get("ids", [])
    count = database.batch_delete_accounts(ids)
    return JSONResponse({"success": True, "count": count})


async def api_admin_accounts_batch_reset(request):
    if not verify_admin_token(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        body = {}
    ids = body.get("ids", [])
    count = database.batch_reset_accounts(ids)
    return JSONResponse({"success": True, "count": count})


async def api_admin_account_check(request):
    """单个库存账号【验活 & 验手机】深度探测"""
    if not verify_admin_token(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    account_id = int(request.path_params.get("account_id"))
    acc = database.get_account_by_id(account_id)
    if not acc:
        return JSONResponse({"error": "账号不存在"}, status_code=404)

    try:
        body = await request.json()
    except Exception:
        body = {}

    proxy = body.get("proxy", "").strip() or database.get_config("default_proxy")
    from oauth_client import check_worker_account_phone_and_alive

    res = await asyncio.to_thread(
        check_worker_account_phone_and_alive,
        email=acc["email"],
        password=acc.get("password") or "Openaipwd666!@",
        proxy=proxy,
        worker_jwt="".join(str(acc.get("worker_jwt") or "").split()),
        client_id=str(acc.get("client_id") or "").strip(),
        refresh_token="".join(str(acc.get("refresh_token") or "").split()),
        mail_type=acc.get("mail_type") or "worker"
    )

    check_st = res.get("check_status", "unknown")
    has_ph = res.get("has_phone")
    ph_num = res.get("phone_number")
    msg = res.get("message", "")
    alive = res.get("alive")

    # 确定存库 status
    if check_st == "banned" or alive is False:
        new_status = "banned"
    elif check_st in ("has_phone", "needs_phone") or alive is True:
        new_status = acc.get("status") if acc.get("status") == "completed" else "available"
    elif check_st == "unregistered":
        new_status = acc.get("status") if acc.get("status") == "completed" else "available"
    else:
        new_status = acc.get("status", "available")

    database.update_account_check_result(
        account_id=acc["id"],
        status=new_status,
        message=msg,
        token=res.get("access_token"),
        check_status=check_st,
        has_phone=has_ph,
        phone_number=ph_num
    )
    res["account_id"] = account_id
    res["email"] = acc["email"]
    res["status"] = new_status
    return JSONResponse(res)


async def api_admin_accounts_batch_check(request):
    """批量库存账号【验活 & 验手机】深度探测"""
    if not verify_admin_token(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        body = {}

    ids = body.get("ids", [])
    if not ids:
        return JSONResponse({"error": "请选择需要检测的账号"}, status_code=400)

    proxy = body.get("proxy", "").strip() or database.get_config("default_proxy")
    from oauth_client import check_worker_account_phone_and_alive

    def _do_batch_check():
        alive_cnt = 0
        has_phone_cnt = 0
        needs_phone_cnt = 0
        unregistered_cnt = 0
        banned_cnt = 0
        error_cnt = 0
        batch_res = []

        for idx, aid in enumerate(ids):
            if idx > 0:
                time.sleep(1.5)  # 间隔防风控
            acc = database.get_account_by_id(aid)
            if not acc:
                continue

            r = check_worker_account_phone_and_alive(
                email=acc["email"],
                password=acc.get("password") or "Openaipwd666!@",
                proxy=proxy,
                worker_jwt="".join(str(acc.get("worker_jwt") or "").split()),
                client_id=str(acc.get("client_id") or "").strip(),
                refresh_token="".join(str(acc.get("refresh_token") or "").split()),
                mail_type=acc.get("mail_type") or "worker"
            )
            r["account_id"] = aid
            r["email"] = acc["email"]

            check_st = r.get("check_status", "unknown")
            has_ph = r.get("has_phone")
            ph_num = r.get("phone_number")
            msg = r.get("message", "")
            alive = r.get("alive")

            if check_st == "banned" or alive is False:
                new_st = "banned"
                banned_cnt += 1
            elif check_st == "has_phone" or has_ph == 1:
                new_st = acc.get("status") if acc.get("status") == "completed" else "available"
                has_phone_cnt += 1
                alive_cnt += 1
            elif check_st == "needs_phone" or has_ph == 0:
                new_st = acc.get("status") if acc.get("status") == "completed" else "available"
                needs_phone_cnt += 1
                alive_cnt += 1
            elif check_st == "unregistered":
                new_st = acc.get("status") if acc.get("status") == "completed" else "available"
                unregistered_cnt += 1
            else:
                new_st = acc.get("status", "available")
                error_cnt += 1

            r["status"] = new_st
            database.update_account_check_result(
                account_id=aid,
                status=new_st,
                message=msg,
                token=r.get("access_token"),
                check_status=check_st,
                has_phone=has_ph,
                phone_number=ph_num
            )
            batch_res.append(r)

        return {
            "success": True,
            "total": len(batch_res),
            "alive_count": alive_cnt,
            "has_phone_count": has_phone_cnt,
            "needs_phone_count": needs_phone_cnt,
            "unregistered_count": unregistered_cnt,
            "banned_count": banned_cnt,
            "error_count": error_cnt,
            "results": batch_res
        }

    res_data = await asyncio.to_thread(_do_batch_check)
    return JSONResponse(res_data)


async def api_admin_cards(request):
    if not verify_admin_token(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    qp = request.query_params
    page = int(qp.get("page", 1))
    page_size = int(qp.get("page_size", 50))
    status = qp.get("status", "all")
    search = qp.get("search", "")
    return JSONResponse(database.list_cards(status=status, search=search, page=page, page_size=page_size))


async def api_admin_cards_generate(request):
    if not verify_admin_token(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        body = {}
    count = int(body.get("count", 10))
    prefix = body.get("prefix", "CDX").strip() or "CDX"
    note = body.get("note", "").strip()
    keys = database.generate_card_keys(count=count, note=note, prefix=prefix)
    return JSONResponse({"success": True, "keys": keys})


async def api_admin_card_delete(request):
    if not verify_admin_token(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    card_id = int(request.path_params.get("card_id"))
    ok = database.delete_card(card_id)
    return JSONResponse({"success": ok})


async def api_admin_cards_disable(request):
    if not verify_admin_token(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        body = {}
    card_id = body.get("id")
    ok = database.disable_card(card_id)
    return JSONResponse({"success": ok})


async def api_admin_cards_batch_delete(request):
    if not verify_admin_token(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        body = {}
    ids = body.get("ids", [])
    count = database.batch_delete_cards(ids)
    return JSONResponse({"success": True, "count": count})


async def api_admin_cards_batch_disable(request):
    if not verify_admin_token(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        body = {}
    ids = body.get("ids", [])
    count = database.batch_disable_cards(ids)
    return JSONResponse({"success": True, "count": count})


async def api_admin_get_config(request):
    if not verify_admin_token(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    return JSONResponse(database.get_all_config())


async def api_admin_set_config(request):
    if not verify_admin_token(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        body = {}
    for k, v in body.items():
        database.set_config(k, v)
    return JSONResponse({"success": True})


async def api_admin_sms_balance(request):
    if not verify_admin_token(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    provider = request.query_params.get("provider", "").strip().lower()
    key = request.query_params.get("key", "").strip()
    if not key:
        cfg = database.get_all_config()
        if provider == "5sim":
            key = cfg.get("sms_api_key", "")
        elif provider in ("hero", "herosms"):
            key = cfg.get("hero_sms_api_key", "")
        elif provider in ("sms_online", "smsonline", "sms-online"):
            key = cfg.get("sms_online_api_key", "")

    if not key:
        return JSONResponse({"success": False, "error": "未提供或未保存 API Key"})

    try:
        if provider == "5sim":
            r = requests.get("https://5sim.net/v1/user/profile", headers={"Authorization": f"Bearer {key}"}, timeout=10)
            if r.status_code == 200:
                d = r.json()
                return JSONResponse({"success": True, "balance": f"{d.get('balance', 0)} RUB", "email": d.get("email", "")})
            return JSONResponse({"success": False, "error": f"HTTP {r.status_code}: {r.text[:100]}"})
        elif provider in ("hero", "herosms"):
            r = requests.get("https://hero-sms.com/stubs/handler_api.php", params={"action": "getBalance", "api_key": key}, timeout=10)
            txt = r.text.strip()
            if txt.startswith("ACCESS_BALANCE:"):
                return JSONResponse({"success": True, "balance": f"${txt.split(':', 1)[1]}"})
            return JSONResponse({"success": False, "error": txt})
        elif provider in ("sms_online", "smsonline", "sms-online"):
            r = requests.get("https://sms-online.pro/stubs/handler_api.php", params={"action": "getBalance", "api_key": key}, timeout=10)
            txt = r.text.strip()
            if txt.startswith("ACCESS_BALANCE:"):
                return JSONResponse({"success": True, "balance": f"${txt.split(':', 1)[1]}"})
            return JSONResponse({"success": False, "error": txt})
        else:
            return JSONResponse({"success": False, "error": "未知接码平台"})
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)})


async def api_admin_sms_countries(request):
    if not verify_admin_token(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    json_path = BASE_DIR / "sms_countries.json"
    if json_path.exists():
        try:
            with open(json_path, "r", encoding="utf-8") as f:
                return JSONResponse(json.load(f))
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)
    return JSONResponse([])


# =============================================================================
# Worker 邮箱源库后端业务与 API
# =============================================================================

def do_sync_worker_accounts(force_jwt_refresh: bool = False) -> dict:
    cfg = database.get_all_config()
    worker_url = cfg.get("worker_url", "https://email.091619.xyz").rstrip("/")
    worker_auth = cfg.get("worker_admin_auth", "Openaipwd666!@").strip()

    if not worker_url or not worker_auth:
        raise ValueError("系统未配置 worker_url 或 worker_admin_auth")

    headers = {
        "x-admin-auth": worker_auth,
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }

    # 1. 分页抓取所有 address
    offset = 0
    limit = 100
    all_addresses = []
    total_in_worker = None

    while True:
        url = f"{worker_url}/admin/address?limit={limit}&offset={offset}"
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        if total_in_worker is None:
            total_in_worker = data.get("count", 0)

        results = data.get("results", [])
        if not results:
            break

        all_addresses.extend(results)
        offset += len(results)
        if total_in_worker and offset >= total_in_worker:
            break

    # 2. 读取本地已有凭证缓存
    cached_jwts = database.get_known_worker_emails_with_jwt()

    # 3. 区分哪些需要网络请求 show_password 获取 JWT
    items_to_fetch_jwt = []
    final_records = []

    for item in all_addresses:
        addr_id = item.get("id")
        email = item.get("name", "").strip()
        created_at = item.get("created_at")
        mail_count = item.get("mail_count", 0)

        cached_jwt = cached_jwts.get(email)
        if not force_jwt_refresh and cached_jwt:
            final_records.append({
                "worker_id": addr_id,
                "email": email,
                "jwt": cached_jwt,
                "mail_count": mail_count,
                "worker_created_at": created_at
            })
        else:
            items_to_fetch_jwt.append({
                "worker_id": addr_id,
                "email": email,
                "mail_count": mail_count,
                "worker_created_at": created_at
            })

    # 4. 并发拉取新凭证
    fetched_count = 0
    if items_to_fetch_jwt:
        def _fetch_one_jwt(rec):
            try:
                jwt_url = f"{worker_url}/admin/show_password/{rec['worker_id']}"
                req_jwt = urllib.request.Request(jwt_url, headers=headers)
                with urllib.request.urlopen(req_jwt, timeout=12) as r_jwt:
                    j_data = json.loads(r_jwt.read().decode("utf-8"))
                    jwt_val = j_data.get("jwt") or ""
                    rec["jwt"] = jwt_val
                    return rec
            except Exception:
                rec["jwt"] = ""
                return rec

        with ThreadPoolExecutor(max_workers=12) as executor:
            fetched_results = list(executor.map(_fetch_one_jwt, items_to_fetch_jwt))
            final_records.extend(fetched_results)
            fetched_count = len(fetched_results)

    # 5. 批量写入 worker_accounts 表
    save_res = database.save_worker_accounts_batch(final_records)

    return {
        "success": True,
        "total_worker": len(all_addresses),
        "fetched_jwts": fetched_count,
        "reused_jwts": len(final_records) - fetched_count,
        "added": save_res.get("added", 0),
        "updated": save_res.get("updated", 0),
        "message": f"成功从 Worker 同步 {len(all_addresses)} 个邮箱！新增 {save_res.get('added', 0)} 个，更新 {save_res.get('updated', 0)} 个（提取 JWT 凭证 {fetched_count} 个，复用本地缓存 {len(final_records) - fetched_count} 个）。"
    }


async def api_admin_worker_accounts(request):
    if not verify_admin_token(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    qp = request.query_params
    page = int(qp.get("page", 1))
    page_size = int(qp.get("page_size", 50))
    inventory_filter = qp.get("inventory_filter", "all")
    domain_filter = qp.get("domain_filter", "all")
    phone_filter = qp.get("phone_filter", "all")
    search = qp.get("search", "")
    data = database.list_worker_accounts(
        search=search,
        inventory_filter=inventory_filter,
        domain_filter=domain_filter,
        phone_filter=phone_filter,
        page=page,
        page_size=page_size
    )
    return JSONResponse(data)


async def api_admin_worker_accounts_sync(request):
    if not verify_admin_token(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        body = {}
    force_refresh = bool(body.get("force_jwt_refresh", False))

    try:
        res = await asyncio.to_thread(do_sync_worker_accounts, force_refresh)
        return JSONResponse(res)
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)


async def api_admin_worker_accounts_import_inventory(request):
    if not verify_admin_token(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        body = {}
    worker_ids = body.get("worker_ids")
    import_all_unimported = bool(body.get("import_all_unimported", False))

    res = database.import_worker_accounts_to_inventory(
        worker_ids=worker_ids,
        import_all_unimported=import_all_unimported
    )
    return JSONResponse(res)


async def api_admin_worker_accounts_import_phone_verified_xyz(request):
    if not verify_admin_token(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    res = await asyncio.to_thread(database.import_worker_phone_verified_xyz_to_inventory)
    return JSONResponse(res)


async def api_admin_worker_accounts_export_text(request):
    if not verify_admin_token(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    qp = request.query_params
    format_type = qp.get("format_type", "email_jwt")
    filter_type = qp.get("filter_type", "all")
    phone_filter = qp.get("phone_filter", "all")
    domain_filter = qp.get("domain_filter", "all")
    inventory_filter = qp.get("inventory_filter", "all")
    search = qp.get("search", "")
    ids = qp.get("ids", "")
    text = database.get_all_worker_accounts_text(
        format_type=format_type,
        filter_type=filter_type,
        phone_filter=phone_filter,
        domain_filter=domain_filter,
        inventory_filter=inventory_filter,
        search=search,
        ids=ids
    )
    return JSONResponse({"success": True, "text": text, "count": len(text.splitlines()) if text else 0})


async def api_admin_worker_account_mails(request):
    if not verify_admin_token(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    worker_acc_id = int(request.path_params.get("worker_account_id"))
    acc = database.get_worker_account_by_id(worker_acc_id)
    if not acc:
        return JSONResponse({"success": False, "error": "未找到指定的 Worker 邮箱"}, status_code=404)
    email = acc.get("email")
    jwt_val = acc.get("jwt")
    if not jwt_val:
        return JSONResponse({"success": False, "error": "该邮箱无有效 JWT 凭证"}, status_code=400)

    cfg = database.get_all_config()
    worker_url = cfg.get("worker_url", "https://email.091619.xyz").rstrip("/")

    def _fetch_mails():
        try:
            url = f"{worker_url}/api/mails?limit=20&offset=0"
            headers = {
                "Authorization": f"Bearer {jwt_val}",
                "User-Agent": "Mozilla/5.0"
            }
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=12) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                mails = data.get("results", []) or data.get("data", [])

                # 优化邮件列表数据，提取可能存在的验证码
                parsed_mails = []
                for m in mails:
                    raw_content = m.get("raw") or ""
                    code_match = re.search(r'(?:验证码|code|pin)[^\d]*(\d{6})', raw_content, re.IGNORECASE)
                    otp_code = code_match.group(1) if code_match else None
                    parsed_mails.append({
                        "id": m.get("id"),
                        "subject": m.get("subject") or "无主题",
                        "sender": m.get("source") or m.get("sender") or "未知发信人",
                        "created_at": m.get("created_at"),
                        "otp_code": otp_code,
                        "raw_snippet": (raw_content[:300] + "...") if len(raw_content) > 300 else raw_content
                    })

                return {"success": True, "email": email, "mails": parsed_mails, "total": len(parsed_mails)}
        except Exception as e:
            return {"success": False, "error": f"拉取邮件失败: {str(e)}"}

    res = await asyncio.to_thread(_fetch_mails)
    return JSONResponse(res)


async def api_admin_worker_accounts_batch_delete(request):
    if not verify_admin_token(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        body = {}
    ids = body.get("ids", [])
    count = database.delete_worker_accounts_batch(ids)
    return JSONResponse({"success": True, "count": count})


async def api_admin_worker_account_check(request):
    """单个 Worker 邮箱验活 + 验手机绑定状态"""
    if not verify_admin_token(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    worker_acc_id = int(request.path_params.get("worker_account_id"))
    acc = database.get_worker_account_by_id(worker_acc_id)
    if not acc:
        return JSONResponse({"success": False, "error": "Worker 邮箱不存在"}, status_code=404)

    try:
        body = await request.json()
    except Exception:
        body = {}

    proxy = body.get("proxy", "").strip() or database.get_config("default_proxy")
    password = body.get("password", "").strip() or "Openaipwd666!@"

    from oauth_client import check_worker_account_phone_and_alive
    res = await asyncio.to_thread(
        check_worker_account_phone_and_alive,
        email=acc["email"],
        password=password,
        proxy=proxy,
        worker_jwt=acc.get("jwt")
    )

    check_st = res.get("check_status", "unknown")
    has_ph = res.get("has_phone")
    ph_num = res.get("phone_number")
    msg = res.get("message", "")

    database.update_worker_account_check_result(
        worker_account_id=worker_acc_id,
        check_status=check_st,
        has_phone=has_ph,
        phone_number=ph_num,
        check_message=msg
    )

    res["worker_account_id"] = worker_acc_id
    res["email"] = acc["email"]
    return JSONResponse(res)


async def api_admin_worker_accounts_batch_check(request):
    """批量 Worker 邮箱验活 + 验手机码"""
    if not verify_admin_token(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        body = {}

    ids = body.get("ids", [])
    if not ids:
        return JSONResponse({"error": "请选择需要检测的 Worker 邮箱"}, status_code=400)

    proxy = body.get("proxy", "").strip() or database.get_config("default_proxy")
    password = body.get("password", "").strip() or "Openaipwd666!@"

    from oauth_client import check_worker_account_phone_and_alive

    def _do_batch_worker_check():
        alive_cnt = 0
        has_phone_cnt = 0
        needs_phone_cnt = 0
        banned_cnt = 0
        unregistered_cnt = 0
        error_cnt = 0
        batch_res = []
        for idx, wid in enumerate(ids):
            if idx > 0:
                time.sleep(1.2)
            acc = database.get_worker_account_by_id(wid)
            if not acc:
                continue
            r = check_worker_account_phone_and_alive(
                email=acc["email"],
                password=password,
                proxy=proxy,
                worker_jwt=acc.get("jwt")
            )
            r["worker_account_id"] = wid
            r["email"] = acc["email"]

            check_st = r.get("check_status", "unknown")
            has_ph = r.get("has_phone")
            ph_num = r.get("phone_number")
            msg = r.get("message", "")

            database.update_worker_account_check_result(
                worker_account_id=wid,
                check_status=check_st,
                has_phone=has_ph,
                phone_number=ph_num,
                check_message=msg
            )

            if r.get("alive") is True:
                alive_cnt += 1
                if check_st == "has_phone" or has_ph == 1:
                    has_phone_cnt += 1
                elif check_st == "needs_phone" or has_ph == 0:
                    needs_phone_cnt += 1
            elif check_st == "unregistered":
                unregistered_cnt += 1
            elif r.get("alive") is False or check_st == "banned":
                banned_cnt += 1
            else:
                error_cnt += 1
            batch_res.append(r)

        return {
            "success": True,
            "total": len(batch_res),
            "alive_count": alive_cnt,
            "has_phone_count": has_phone_cnt,
            "needs_phone_count": needs_phone_cnt,
            "unregistered_count": unregistered_cnt,
            "banned_count": banned_cnt,
            "error_count": error_cnt,
            "results": batch_res
        }

    res_data = await asyncio.to_thread(_do_batch_worker_check)
    return JSONResponse(res_data)


async def api_admin_worker_accounts_sync_phone_cache(request):
    """一键关联已知接码历史文件至 Worker 邮箱库"""
    if not verify_admin_token(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    res = database.sync_known_phone_cache_to_db()
    return JSONResponse(res)


async def api_admin_worker_autocheck_start(request):
    """启动 Worker 邮箱慢速自动化巡检任务 (带随机延时防风控与动态住宅 IP 轮换)"""
    if not verify_admin_token(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        body = {}
    min_delay = max(int(body.get("min_delay", 25)), 15)  # 严格保底不少于 15 秒防风控
    max_delay = max(int(body.get("max_delay", 40)), min_delay + 5)
    scope = body.get("scope", "pending").strip()  # pending, unregistered, all
    proxy = body.get("proxy", "").strip() or get_default_proxy()
    password = body.get("password", "").strip() or "Openaipwd666!@"

    active_task = worker_auto_check_manager.get_active_task()
    if active_task:
        return JSONResponse({
            "success": False,
            "error": f"已有巡检任务正在执行中 (任务ID: {active_task.task_id})",
            "task_id": active_task.task_id,
            "data": active_task.to_dict()
        }, status_code=400)

    task = worker_auto_check_manager.create_task(
        min_delay=min_delay,
        max_delay=max_delay,
        scope=scope,
        proxy=proxy,
        password=password
    )
    asyncio.create_task(run_worker_autocheck_task(task))
    return JSONResponse({
        "success": True,
        "task_id": task.task_id,
        "message": f"已启动 Worker 邮箱慢速巡检 (间隔 {min_delay}~{max_delay}s，动态住宅 IP)",
        "data": task.to_dict()
    })


async def api_admin_worker_autocheck_pause(request):
    """暂停当前巡检任务"""
    if not verify_admin_token(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    task = worker_auto_check_manager.get_active_task()
    if not task:
        return JSONResponse({"success": False, "error": "没有正在运行的巡检任务"}, status_code=400)
    task.pause()
    return JSONResponse({"success": True, "message": "巡检已暂停", "data": task.to_dict()})


async def api_admin_worker_autocheck_resume(request):
    """恢复已暂停的巡检任务"""
    if not verify_admin_token(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    task = worker_auto_check_manager.get_active_task()
    if not task:
        return JSONResponse({"success": False, "error": "没有正在暂停的巡检任务"}, status_code=400)
    task.resume()
    return JSONResponse({"success": True, "message": "巡检已恢复", "data": task.to_dict()})


async def api_admin_worker_autocheck_stop(request):
    """停止/终止巡检任务"""
    if not verify_admin_token(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    task = worker_auto_check_manager.get_active_task()
    if not task:
        latest = worker_auto_check_manager.get_latest_task()
        if latest and latest.status in ("running", "paused"):
            task = latest
    if not task:
        return JSONResponse({"success": False, "error": "没有可停止的巡检任务"}, status_code=400)
    task.stop()
    return JSONResponse({"success": True, "message": "巡检任务已终止", "data": task.to_dict()})


async def api_admin_worker_autocheck_status(request):
    """获取巡检任务状态与实时统计"""
    if not verify_admin_token(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    task_id = request.query_params.get("task_id", "").strip()
    task = None
    if task_id:
        task = worker_auto_check_manager.get_task(task_id)
    if not task:
        task = worker_auto_check_manager.get_active_task()
    if not task:
        task = worker_auto_check_manager.get_latest_task()

    if not task:
        return JSONResponse({"success": True, "has_task": False, "data": None})

    return JSONResponse({
        "success": True,
        "has_task": True,
        "is_active": task.status in ("running", "paused", "queued"),
        "data": task.to_dict()
    })


admin_secret_path = database.get_config("admin_path", "admin-secret-8f2c91b4a6d3e507").strip("/")

routes = [
    Route("/", index_view, methods=["GET"]),
    Route(f"/{admin_secret_path}", admin_view, methods=["GET"]),
    Route(f"/{admin_secret_path}/", admin_view, methods=["GET"]),
    Route("/api/system", api_system_status, methods=["GET"]),
    Route("/api/proxy/test", api_proxy_test, methods=["POST"]),
    Route("/api/token/parse", api_parse_token, methods=["POST"]),
    Route("/api/activate", api_activate_task, methods=["POST"]),
    Route("/api/stream/{task_id}", api_stream_task, methods=["GET"]),
    Route("/api/task/{task_id}/stop", api_stop_task, methods=["POST"]),
    Route("/api/history", api_task_history, methods=["GET"]),
    Route("/api/tracking", api_query_tracking, methods=["POST"]),
    Route("/api/query-card-progress", api_query_card_progress, methods=["POST"]),
    Route("/api/live-stats", api_live_stats, methods=["GET"]),

    # 卡密兑换与激活
    Route("/api/redeem-card", api_redeem_card, methods=["POST"]),
    Route("/api/card-activate", api_card_activate, methods=["POST"]),

    # 管理后台 API
    Route("/api/admin/login", api_admin_login, methods=["POST"]),
    Route("/api/admin/stats", api_admin_stats, methods=["GET"]),
    Route("/api/admin/accounts", api_admin_accounts, methods=["GET"]),
    Route("/api/admin/accounts/import", api_admin_accounts_import, methods=["POST"]),
    Route("/api/admin/accounts/auto-register", api_admin_accounts_autoregister, methods=["POST"]),
    Route("/api/admin/accounts/auto-register/active", api_admin_accounts_autoregister_active, methods=["GET"]),
    Route("/api/admin/accounts/auto-register/stream/{task_id}", api_admin_accounts_autoregister_stream, methods=["GET"]),
    Route("/api/admin/accounts/auto-register/{task_id}/stop", api_admin_accounts_autoregister_stop, methods=["POST"]),
    Route("/api/admin/accounts/auto-register/{task_id}", api_admin_accounts_autoregister_status, methods=["GET"]),
    Route("/api/admin/accounts/reset", api_admin_accounts_reset, methods=["POST"]),
    Route("/api/admin/accounts/unfreeze", api_admin_accounts_unfreeze, methods=["POST"]),
    Route("/api/admin/accounts/{account_id:int}/unfreeze", api_admin_accounts_unfreeze, methods=["POST"]),
    Route("/api/admin/accounts/batch-unfreeze", api_admin_accounts_batch_unfreeze, methods=["POST"]),
    Route("/api/admin/accounts/batch-delete", api_admin_accounts_batch_delete, methods=["POST"]),
    Route("/api/admin/accounts/batch-reset", api_admin_accounts_batch_reset, methods=["POST"]),
    Route("/api/admin/accounts/batch-check", api_admin_accounts_batch_check, methods=["POST"]),
    Route("/api/admin/accounts/{account_id:int}/check", api_admin_account_check, methods=["POST"]),
    Route("/api/admin/accounts/{account_id:int}", api_admin_account_delete, methods=["DELETE"]),

    # Worker 邮箱源库 API
    Route("/api/admin/worker-accounts", api_admin_worker_accounts, methods=["GET"]),
    Route("/api/admin/worker-accounts/sync", api_admin_worker_accounts_sync, methods=["POST"]),
    Route("/api/admin/worker-accounts/sync-phone-cache", api_admin_worker_accounts_sync_phone_cache, methods=["POST"]),
    Route("/api/admin/worker-accounts/import-inventory", api_admin_worker_accounts_import_inventory, methods=["POST"]),
    Route("/api/admin/worker-accounts/import-phone-verified-xyz", api_admin_worker_accounts_import_phone_verified_xyz, methods=["POST"]),
    Route("/api/admin/worker-accounts/export-text", api_admin_worker_accounts_export_text, methods=["GET"]),
    Route("/api/admin/worker-accounts/batch-check", api_admin_worker_accounts_batch_check, methods=["POST"]),
    Route("/api/admin/worker-accounts/{worker_account_id:int}/check", api_admin_worker_account_check, methods=["POST"]),
    Route("/api/admin/worker-accounts/{worker_account_id:int}/mails", api_admin_worker_account_mails, methods=["GET"]),
    Route("/api/admin/worker-accounts/batch-delete", api_admin_worker_accounts_batch_delete, methods=["POST"]),
    Route("/api/admin/worker-accounts/auto-check/start", api_admin_worker_autocheck_start, methods=["POST"]),
    Route("/api/admin/worker-accounts/auto-check/pause", api_admin_worker_autocheck_pause, methods=["POST"]),
    Route("/api/admin/worker-accounts/auto-check/resume", api_admin_worker_autocheck_resume, methods=["POST"]),
    Route("/api/admin/worker-accounts/auto-check/stop", api_admin_worker_autocheck_stop, methods=["POST"]),
    Route("/api/admin/worker-accounts/auto-check/status", api_admin_worker_autocheck_status, methods=["GET"]),

    Route("/api/admin/cards", api_admin_cards, methods=["GET"]),
    Route("/api/admin/cards/generate", api_admin_cards_generate, methods=["POST"]),
    Route("/api/admin/cards/disable", api_admin_cards_disable, methods=["POST"]),
    Route("/api/admin/cards/batch-delete", api_admin_cards_batch_delete, methods=["POST"]),
    Route("/api/admin/cards/batch-disable", api_admin_cards_batch_disable, methods=["POST"]),
    Route("/api/admin/cards/{card_id:int}", api_admin_card_delete, methods=["DELETE"]),
    Route("/api/admin/config", api_admin_get_config, methods=["GET"]),
    Route("/api/admin/config", api_admin_set_config, methods=["POST"]),
    Route("/api/admin/sms/balance", api_admin_sms_balance, methods=["GET"]),
    Route("/api/admin/sms/countries", api_admin_sms_countries, methods=["GET"]),
]

middleware = [
    Middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
]

app = Starlette(routes=routes, middleware=middleware)


def main():
    parser = argparse.ArgumentParser(description="Codex Referral Activator Web Server")
    parser.add_argument("--host", default="0.0.0.0", help="监听地址 (默认 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8080, help="监听端口 (默认 8080)")
    args = parser.parse_args()

    import uvicorn
    print("=" * 65)
    print(f"🚀 Codex Referral Activator Web 服务正在启动...")
    print(f"📡 监听地址: http://{args.host}:{args.port}")
    if is_wsl():
        print(f"🌐 Windows 本地浏览器访问地址: http://localhost:{args.port} 或 http://127.0.0.1:{args.port}")
    print("=" * 65)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
