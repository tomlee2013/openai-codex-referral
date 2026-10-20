"""
Codex Desktop Client OAuth 2.0 自动授权与 Token 换取引擎
基于 /opt/gptreg/oauth_login.py 与 auth_utils.py 精炼重构构建。
专用于自动化获取包含官方客户端 client_id (app_EMoamEEZ73f0CkXaXp7hrann) 与 offline_access 权限的 Desktop Token。
"""

import base64
import hashlib
import json
import logging
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
import quopri
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Callable, Dict, Optional, Tuple

import requests
import tls_client

import database

log = logging.getLogger("oauth_client")

DEFAULT_ACCOUNT_PASSWORD = os.getenv("DEFAULT_ACCOUNT_PASSWORD", "ChangeMePassword123!@")
DEFAULT_WORKER_URL = os.getenv("WORKER_URL", "")
DEFAULT_WORKER_ADMIN_AUTH = os.getenv("WORKER_ADMIN_AUTH", "")
DEFAULT_WORKER_DOMAIN = os.getenv("WORKER_DOMAIN", "")

# 官方客户端授权常量
AUTH_URL = "https://auth.openai.com/oauth/authorize"
TOKEN_URL = "https://auth.openai.com/oauth/token"
CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
REDIRECT_URI = "http://localhost:1455/auth/callback"
SCOPE = "openid email profile offline_access"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36"
TLS_CLIENT_ID = "chrome_133"


def _b64url_no_pad(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _random_state(nbytes: int = 32) -> str:
    return _b64url_no_pad(secrets.token_bytes(nbytes))


def _pkce_verifier(nbytes: int = 32) -> str:
    return _b64url_no_pad(secrets.token_bytes(nbytes))


def _pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return _b64url_no_pad(digest)


def refresh_dynamic_proxy(proxy: str) -> str:
    """如果代理包含动态住宅 session 标识 (例如 -sid-XXXXX-)，则替换为全新随机 sid，以获取全新的有效住宅 IP"""
    if not proxy or "-sid-" not in proxy:
        return proxy
    try:
        new_sid = secrets.token_hex(4)
        return re.sub(r'-sid-[a-zA-Z0-9]+-', f'-sid-{new_sid}-', proxy)
    except Exception:
        return proxy


def parse_openai_error_payload(url_or_loc: str) -> dict:
    """从 OpenAI 重定向 URL (例如 https://auth.openai.com/error?payload=...) 中解析真正的错误明细"""
    if not url_or_loc or "payload=" not in url_or_loc:
        return {}
    try:
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(url_or_loc).query)
        payload_b64 = qs.get("payload", [""])[0]
        rem = len(payload_b64) % 4
        if rem:
            payload_b64 += "=" * (4 - rem)
        return json.loads(base64.urlsafe_b64decode(payload_b64).decode("utf-8", errors="replace"))
    except Exception:
        return {}


def _jwt_claims_no_verify(jwt_str: str) -> dict:
    try:
        parts = str(jwt_str or "").split(".")
        if len(parts) < 2:
            return {}
        rem = len(parts[1]) % 4
        payload = parts[1] + ("=" * (4 - rem) if rem else "")
        return json.loads(base64.urlsafe_b64decode(payload.encode("ascii")).decode("utf-8", errors="replace"))
    except Exception:
        return {}


def locate_sentinel_cli() -> Optional[str]:
    """定位 sentinel-cli 二进制文件"""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    is_windows = sys.platform == "win32"
    cands = [
        "/opt/gptreg/sentinel-cli",
        os.path.join(script_dir, "sentinel-cli.exe" if is_windows else "sentinel-cli"),
        shutil.which("sentinel-cli.exe" if is_windows else "sentinel-cli"),
    ]
    for c in cands:
        if c and os.path.isfile(c) and (is_windows or os.access(c, os.X_OK)):
            return c
    return None


def generate_sentinel_token(did: str, flow: str, referer: str, proxy: str = "") -> str:
    """生成 OpenAI Sentinel 校验 Token"""
    cli_path = locate_sentinel_cli()
    if not cli_path:
        log.warning("未检测到可执行的 sentinel-cli，跳过 Sentinel 注入")
        return ""

    proxy = refresh_dynamic_proxy(proxy or "")
    payload = {
        "device_id": did,
        "user_agent": USER_AGENT,
        "flow": flow,
        "referer": referer,
        "proxy": proxy or "",
        "platform": "Win32",
        "vendor": "Google Inc.",
        "screen_width": 1920,
        "screen_height": 1080,
        "language": "en-US",
        "languages_join": "en-US,en",
        "timezone_offset_min": 420,
        "hardware_concurrency": 8,
        "heap_limit": 4294705152,
    }

    try:
        proc = subprocess.run(
            [cli_path],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            timeout=12,
            check=False
        )
        if proc.returncode != 0:
            return ""
        out = proc.stdout.strip()
        start = out.find("{")
        end = out.rfind("}")
        if start != -1 and end != -1:
            raw_json = out[start : end + 1]
            try:
                data = json.loads(raw_json)
                if "token" in data and data["token"]:
                    return data["token"]
                return json.dumps(data, separators=(",", ":"))
            except Exception:
                return raw_json
    except Exception as e:
        log.warning(f"sentinel-cli 执行异常: {e}")
    return ""


def get_worker_jwt_for_email(worker_url: str, worker_admin_auth: str, email: str) -> str:
    """从 Cloudflare 邮箱 Worker 管理接口获取指定邮箱的 JWT 查看凭证"""
    if not worker_url or not worker_admin_auth or not email:
        return ""
    worker_url = worker_url.rstrip("/")
    headers = {"x-admin-auth": worker_admin_auth}
    try:
        r = requests.get(
            f"{worker_url}/admin/address",
            params={"query": email, "limit": 1, "offset": 0},
            headers=headers,
            timeout=10,
        )
        if r.status_code != 200:
            return ""
        data = r.json()
        results = data.get("results", [])
        if not results or str(results[0].get("name") or "").lower() != email.lower():
            return ""
        addr_id = results[0].get("id")
        if not addr_id:
            return ""
        r2 = requests.get(f"{worker_url}/admin/show_password/{addr_id}", headers=headers, timeout=10)
        if r2.status_code == 200:
            return str((r2.json() or {}).get("jwt") or "").strip()
    except Exception as e:
        log.warning(f"获取 Worker JWT 异常: {email} -> {e}")
    return ""


def extract_email_code(raw_content: str, email: str = "") -> Optional[str]:
    """从邮件原始报文 (MIME / HTML) 中精准提取 OpenAI 6 位临时验证码，杜绝从邮件头中匹配垃圾编号"""
    if not raw_content:
        return None

    ignored_codes = set()
    if email:
        for num in re.findall(r"\b\d{6}\b", email):
            ignored_codes.add(num)

    # 1. 拆分邮件头与正文，只在邮件正文中检索验证码！彻底避免匹配到邮件头中的 SMTP 节点编号、时间戳或 monotonic clock (如 m=+895374.668)
    text = raw_content.replace("\r\n", "\n").replace("\r", "\n")
    parts = text.split("\n\n", 1)
    headers_part = parts[0] if len(parts) > 1 else ""
    body_part = parts[1] if len(parts) > 1 else text

    # 检查是否包含验证码相关语义 (排除邀请邮件或纯营销邮件)
    header_lower = headers_part.lower()
    if "invited you to" in header_lower or "受邀加入" in text:
        return None

    try:
        body_decoded = quopri.decodestring(body_part.encode("latin1", errors="ignore")).decode("utf-8", errors="ignore")
    except Exception:
        body_decoded = body_part

    # Pattern 1: Enter this temporary verification code to continue: 383687.
    m0 = re.search(r"Enter this temporary verification code to continue:\s*(\d{6})", body_decoded, re.IGNORECASE)
    if m0 and m0.group(1) not in ignored_codes:
        return m0.group(1)

    # Pattern 2: HTML 居中大字号标签内独立的 6 位数字，如 >\n 371706 \n<
    tag_matches = re.findall(r">\s*(\d{6})\s*<", body_decoded)
    for cand in tag_matches:
        if cand not in ignored_codes:
            return cand

    # Pattern 3: verification code 或 login code
    m1 = re.search(r"(?:verification code|login code|security code|code is)[\s\S]{0,100}?(\d{6})", body_decoded, re.IGNORECASE)
    if m1 and m1.group(1) not in ignored_codes:
        return m1.group(1)

    # Pattern 4: enter this code:\s*(\d{6})
    m2 = re.search(r"enter this code[:\s]+(\d{6})", body_decoded, re.IGNORECASE)
    if m2 and m2.group(1) not in ignored_codes:
        return m2.group(1)

    return None


def get_latest_worker_mail_id(worker_url: str, jwt: str) -> int:
    """获取 Worker 邮箱中最新一封邮件的 ID，用于核验时精准捕获新验证码"""
    jwt = "".join(str(jwt or "").split())
    if not worker_url or not jwt:
        return 0
    try:
        r = requests.get(
            f"{worker_url.rstrip('/')}/api/mails?limit=1&offset=0",
            headers={"Authorization": f"Bearer {jwt}"},
            timeout=8,
            proxies={"http": None, "https": None}
        )
        if r.status_code == 200:
            mails = (r.json() or {}).get("results", []) or []
            if mails:
                return int(mails[0].get("id", 0))
    except Exception:
        pass
    return 0


def poll_email_code_worker(
    worker_url: str,
    jwt: str,
    timeout_sec: int = 40,
    interval_sec: int = 2,
    min_id: int = 0,
    email: str = "",
    log_fn: Callable = None
) -> Optional[str]:
    """通过 Worker JWT 轮询拉取最新的 OpenAI 6 位邮箱验证码"""
    jwt = "".join(str(jwt or "").split())
    if not worker_url or not jwt:
        return None
    worker_url = worker_url.rstrip("/")
    start_time = time.time()

    if log_fn:
        log_fn(f"正在监听邮箱 Worker 收信 (超时: {timeout_sec}s)...", "info")

    while time.time() - start_time < timeout_sec:
        try:
            resp = requests.get(
                f"{worker_url}/api/mails?limit=5&offset=0",
                headers={"Authorization": f"Bearer {jwt}"},
                timeout=10,
                proxies={"http": None, "https": None}
            )
            if resp.status_code == 200:
                data = resp.json() or {}
                mails = data.get("results", []) or data.get("data", []) or []
                for m in mails:
                    mail_id = int(m.get("id", 0))
                    raw_text = m.get("raw") or m.get("content") or ""

                    # 必须大于发起重发前的最新 ID，确保是本次请求生成的新验证码
                    if min_id > 0 and mail_id <= min_id:
                        continue

                    code = extract_email_code(raw_text, email=email)
                    if code:
                        if log_fn:
                            log_fn(f"🎉 成功接收到 OpenAI 最新邮箱验证码: {code} (邮件ID: {mail_id})", "success")
                        return code
        except Exception as e:
            log.warning(f"轮询邮箱异常: {e}")
        time.sleep(interval_sec)

    return None


OUTLOOK_DEFAULT_CLIENT_ID = "9e5f94bc-e8a4-4e73-b8be-63364c29d753"


def get_outlook_access_token(refresh_token: str, client_id: str = "") -> Optional[str]:
    """通过 refresh_token 换取 Outlook 邮箱 access_token (参考 /opt/gptreg/auto_outlook.py)"""
    cid = client_id.strip() if client_id else OUTLOOK_DEFAULT_CLIENT_ID
    body = urllib.parse.urlencode({
        "client_id": cid,
        "grant_type": "refresh_token",
        "refresh_token": refresh_token.strip(),
    }).encode()
    req = urllib.request.Request(
        "https://login.live.com/oauth20_token.srf",
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded", "User-Agent": USER_AGENT},
    )
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data.get("access_token")
    except Exception as e:
        log.warning(f"Outlook access_token 换取异常: {e}")
        return None


def poll_email_code_outlook(
    email: str,
    client_id: str,
    refresh_token: str,
    timeout_sec: int = 45,
    interval_sec: int = 3,
    since_time: float = None,
    log_fn: Callable = None
) -> Optional[str]:
    """
    通过官方 REST API 轮询 Outlook 微软收件箱与垃圾箱获取最新 OpenAI 6 位验证码
    参考 /opt/gptreg/auto_outlook.py 及 login_outlook.py 逻辑实现
    """
    if not refresh_token:
        return None
    if log_fn:
        log_fn(f"正在通过 Outlook 官方 API 连接收件箱 (目标: {email})...", "info")

    access_token = get_outlook_access_token(refresh_token, client_id)
    if not access_token:
        if log_fn:
            log_fn("❌ Outlook refresh_token 换取 access_token 失败，请核对 client_id 与 refresh_token", "error")
        return None

    if log_fn:
        log_fn(f"Outlook 凭证验证成功，正在监听邮件验证码 (超时: {timeout_sec}s)...", "info")

    start_time = time.time()
    folders = ["inbox", "junkemail"]
    patterns = [
        r"code is (\d{6})",
        r"verification code[:\s]+(\d{6})",
        r"verification code.{0,500}?(\d{6})",
        r"code[:\s]+(\d{6})",
        r"Enter this temporary verification code to continue:\s*(\d{6})",
        r">\s*(\d{6})\s*<",
        r"\b(\d{6})\b",
    ]

    while time.time() - start_time < timeout_sec:
        for folder in folders:
            try:
                params = urllib.parse.urlencode({
                    "$top": 15,
                    "$orderby": "ReceivedDateTime desc",
                    "$select": "Id,Subject,BodyPreview,Body,ReceivedDateTime,From",
                })
                url = f"https://outlook.office.com/api/v2.0/me/mailfolders/{folder}/messages?{params}"
                req = urllib.request.Request(url, headers={
                    "Authorization": f"Bearer {access_token}",
                    "Accept": "application/json",
                    "User-Agent": USER_AGENT,
                })
                with urllib.request.urlopen(req, timeout=15) as resp:
                    mail_data = json.loads(resp.read().decode("utf-8"))

                messages = mail_data.get("value") or []
                for m in messages:
                    received = m.get("ReceivedDateTime", "")
                    mail_ts = 0
                    if received:
                        try:
                            mail_ts = parsedate_to_datetime(received).timestamp()
                        except Exception:
                            try:
                                mail_ts = datetime.strptime(received[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc).timestamp()
                            except Exception:
                                pass

                    # 若传入了发送时间，只接收发送之后收到的邮件 (允许 10 秒时间差)
                    if since_time and mail_ts > 0 and mail_ts < (since_time - 10):
                        continue

                    subject = m.get("Subject", "")
                    preview = m.get("BodyPreview", "")
                    content = ((m.get("Body") or {}).get("Content")) or ""
                    combined = f"{subject}\n{preview}\n{content}"

                    for p in patterns:
                        match = re.search(p, combined, re.IGNORECASE | re.DOTALL)
                        if match:
                            code = match.group(1) if match.lastindex else match.group(0)
                            if code:
                                if log_fn:
                                    log_fn(f"🎉 成功从 Outlook ({folder}) 捕获到最新验证码: {code}", "success")
                                return code
            except Exception as e:
                if "401" in str(e):
                    new_token = get_outlook_access_token(refresh_token, client_id)
                    if new_token:
                        access_token = new_token
                log.debug(f"Outlook 查询邮件异常 ({folder}): {e}")
        time.sleep(interval_sec)

    return None


def poll_email_code_unified(
    email: str,
    mail_type: str = "worker",
    worker_url: str = "",
    worker_jwt: str = "",
    client_id: str = "",
    refresh_token: str = "",
    timeout_sec: int = 45,
    interval_sec: int = 2,
    min_id: int = 0,
    since_time: float = None,
    log_fn: Callable = None
) -> Optional[str]:
    """
    统一邮箱验证码获取分发入口:
    自动依据 mail_type / refresh_token / 邮箱后缀 路由至 Outlook API 或 Cloudflare Worker
    """
    worker_jwt = "".join(str(worker_jwt or "").split())
    is_outlook = (mail_type == "outlook") or bool(refresh_token) or email.lower().endswith(("@outlook.com", "@hotmail.com", "@live.com", "@msn.com"))
    if is_outlook and refresh_token:
        return poll_email_code_outlook(
            email=email,
            client_id=client_id,
            refresh_token=refresh_token,
            timeout_sec=timeout_sec,
            interval_sec=max(interval_sec, 3),
            since_time=since_time,
            log_fn=log_fn
        )
    else:
        return poll_email_code_worker(
            worker_url=worker_url,
            jwt=worker_jwt,
            timeout_sec=timeout_sec,
            interval_sec=interval_sec,
            min_id=min_id,
            email=email,
            log_fn=log_fn
        )


def _follow_chatgpt_web_redirects_and_get_token(session, start_url: str, max_hops: int = 8) -> Tuple[bool, str]:
    """
    跟随 OAuth / OTP 回调重定向进入 chatgpt.com 并调用 /api/auth/session 获取 Web Access Token
    """
    curr = start_url
    for hop in range(max_hops):
        if not curr:
            break
        try:
            r = session.get(
                curr,
                headers={"referer": "https://auth.openai.com/", "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"},
                allow_redirects=False
            )
        except Exception as e:
            log.warning(f"Follow redirect hop {hop+1} error: {e}")
            break
        loc = r.headers.get("Location") or r.headers.get("location") or ""
        if r.status_code in (301, 302, 303, 307, 308) and loc:
            curr = urllib.parse.urljoin(curr, loc)
            continue
        break

    # 请求 chatgpt session
    try:
        sess_resp = session.get("https://chatgpt.com/api/auth/session", headers={"referer": "https://chatgpt.com/", "accept": "application/json"})
        if sess_resp.status_code == 200:
            data = sess_resp.json() or {}
            token = data.get("accessToken", "")
            return True, token
    except Exception as e:
        log.warning(f"Get chatgpt session error: {e}")
    return False, ""


def _extract_callback_code(url_or_text: str) -> Optional[dict]:
    """从重定向 URL 或响应内容中提取 callback code 和 state"""
    if not url_or_text:
        return None
    if "code=" in url_or_text and "state=" in url_or_text:
        try:
            parsed = urllib.parse.urlparse(url_or_text)
            qs = urllib.parse.parse_qs(parsed.query)
            code = qs.get("code", [None])[0]
            state = qs.get("state", [None])[0]
            if code and state:
                return {"code": code, "state": state}
        except Exception:
            pass

        # 正则兜底
        c_m = re.search(r"[?&]code=([^&\s\"']+)", url_or_text)
        s_m = re.search(r"[?&]state=([^&\s\"']+)", url_or_text)
        if c_m and s_m:
            return {"code": c_m.group(1), "state": s_m.group(1)}
    return None


def _extract_workspace_id_from_html(html: str) -> str:
    """从 OpenAI Consent / Workspace 页面 HTML 中提取 workspace_id UUID"""
    if not html:
        return ""
    ws_idx = html.lower().find("workspaces")
    if ws_idx < 0:
        ws_idx = html.lower().find("workspace_id")
    if ws_idx < 0:
        return ""
    segment = html[ws_idx : ws_idx + 2048]
    m = re.search(
        r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}",
        segment,
    )
    return m.group(0) if m else ""


# ==================== 短信接码管理 (5sim & HeroSMS) ====================

def _5sim_buy_number(api_key: str, country: str = "vietnam", operator: str = "any", product: str = "openai") -> Optional[dict]:
    url = f"https://5sim.net/v1/user/buy/activation/{country}/{operator}/{product}"
    headers = {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}
    try:
        resp = requests.get(url, headers=headers, timeout=15)
        if resp.status_code == 200:
            data = resp.json()
            order_id = data.get("id")
            phone = data.get("phone", "")
            return {"order_id": order_id, "phone": phone, "provider": "5sim"}
    except Exception as e:
        log.warning(f"[5sim] 购买号码异常: {e}")
    return None


def _5sim_poll_sms(api_key: str, order_id: int, timeout: int = 120, interval: int = 5, log_fn: Callable = None) -> Optional[str]:
    url = f"https://5sim.net/v1/user/check/{order_id}"
    headers = {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}
    elapsed = 0
    while elapsed < timeout:
        try:
            resp = requests.get(url, headers=headers, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                status = data.get("status", "")
                sms_list = data.get("sms") or []
                if status == "RECEIVED" and sms_list:
                    code = sms_list[-1].get("code", "")
                    if code:
                        return code
                if status in ("CANCELED", "BANNED", "EXPIRED"):
                    return None
        except Exception:
            pass
        if log_fn and elapsed > 0 and elapsed % 15 == 0:
            log_fn(f"正在等待 5sim 短信验证码到达 ({elapsed}s/{timeout}s)...", "info")
        time.sleep(interval)
        elapsed += interval
    return None


def _5sim_finish_order(api_key: str, order_id: int):
    try:
        requests.get(f"https://5sim.net/v1/user/finish/{order_id}", headers={"Authorization": f"Bearer {api_key}"}, timeout=10)
    except Exception:
        pass


def _5sim_cancel_order(api_key: str, order_id: int):
    try:
        requests.get(f"https://5sim.net/v1/user/cancel/{order_id}", headers={"Authorization": f"Bearer {api_key}"}, timeout=10)
    except Exception:
        pass


def _hero_sms_buy_number(api_key: str, country: str = "10", service: str = "dr", max_price: str = None) -> Optional[dict]:
    url = "https://hero-sms.com/stubs/handler_api.php"
    params = {"api_key": api_key, "action": "getNumber", "service": service, "country": str(country)}
    if max_price:
        params["maxPrice"] = str(max_price)
    try:
        resp = requests.get(url, params=params, timeout=15)
        text = resp.text.strip()
        if text.startswith("ACCESS_NUMBER:"):
            parts = text.split(":", 2)
            if len(parts) == 3:
                return {"order_id": parts[1].strip(), "phone": parts[2].strip(), "provider": "hero"}
    except Exception as e:
        log.warning(f"[HeroSMS] 购买号码异常: {e}")
    return None


def _hero_sms_poll_sms(api_key: str, activation_id: str, timeout: int = 120, interval: int = 5, log_fn: Callable = None) -> Optional[str]:
    url = "https://hero-sms.com/stubs/handler_api.php"
    params = {"api_key": api_key, "action": "getStatus", "id": str(activation_id)}
    elapsed = 0
    while elapsed < timeout:
        try:
            resp = requests.get(url, params=params, timeout=10)
            text = resp.text.strip()
            if text.startswith("STATUS_OK:"):
                code = text.split(":", 1)[1].strip()
                digits = re.sub(r"\D", "", code)
                return digits if (4 <= len(digits) <= 8) else code
        except Exception:
            pass
        if log_fn and elapsed > 0 and elapsed % 15 == 0:
            log_fn(f"正在等待 HeroSMS 短信验证码到达 ({elapsed}s/{timeout}s)...", "info")
        time.sleep(interval)
        elapsed += interval
    return None


def _hero_sms_finish_order(api_key: str, activation_id: str):
    try:
        requests.get("https://hero-sms.com/stubs/handler_api.php", params={"api_key": api_key, "action": "setStatus", "status": "6", "id": str(activation_id)}, timeout=10)
    except Exception:
        pass


def _hero_sms_cancel_order(api_key: str, activation_id: str):
    try:
        requests.get("https://hero-sms.com/stubs/handler_api.php", params={"api_key": api_key, "action": "setStatus", "status": "8", "id": str(activation_id)}, timeout=10)
    except Exception:
        pass


def _sms_online_buy_number(api_key: str, country: str = "6", service: str = "dr", max_price: str = None) -> Optional[dict]:
    url = "https://sms-online.pro/stubs/handler_api.php"
    params = {"api_key": api_key, "action": "getNumber", "service": service, "country": str(country)}
    if max_price:
        params["maxPrice"] = str(max_price)
    try:
        resp = requests.get(url, params=params, timeout=15)
        text = resp.text.strip()
        if text.startswith("ACCESS_NUMBER:"):
            parts = text.split(":", 2)
            if len(parts) == 3:
                return {"order_id": parts[1].strip(), "phone": parts[2].strip(), "provider": "sms_online"}
        log.warning(f"[sms-online.pro] 购买号码返回: {text}")
    except Exception as e:
        log.warning(f"[sms-online.pro] 购买号码异常: {e}")
    return None


def _sms_online_poll_sms(api_key: str, activation_id: str, timeout: int = 120, interval: int = 5, log_fn: Callable = None) -> Optional[str]:
    url = "https://sms-online.pro/stubs/handler_api.php"
    params = {"api_key": api_key, "action": "getStatus", "id": str(activation_id)}
    elapsed = 0
    while elapsed < timeout:
        try:
            resp = requests.get(url, params=params, timeout=10)
            text = resp.text.strip()
            if text.startswith("STATUS_OK:"):
                code = text.split(":", 1)[1].strip()
                digits = re.sub(r"\D", "", code)
                return digits if (4 <= len(digits) <= 8) else code
            elif "STATUS_CANCEL" in text:
                log.warning(f"[sms-online.pro] 订单已取消: {text}")
                return None
        except Exception:
            pass
        if log_fn and elapsed > 0 and elapsed % 15 == 0:
            log_fn(f"正在等待 sms-online.pro 短信验证码到达 ({elapsed}s/{timeout}s)...", "info")
        time.sleep(interval)
        elapsed += interval
    return None


def _sms_online_finish_order(api_key: str, activation_id: str):
    try:
        requests.get("https://sms-online.pro/stubs/handler_api.php", params={"api_key": api_key, "action": "setStatus", "status": "6", "id": str(activation_id)}, timeout=10)
    except Exception:
        pass


def _sms_online_cancel_order(api_key: str, activation_id: str):
    try:
        requests.get("https://sms-online.pro/stubs/handler_api.php", params={"api_key": api_key, "action": "setStatus", "status": "8", "id": str(activation_id)}, timeout=10)
    except Exception:
        pass


def handle_add_phone_flow(
    session,
    current_continue_url: str,
    sms_cfg: dict,
    phone_channel: str = "sms",
    log_fn: Callable = None
) -> Tuple[Optional[dict], Optional[str]]:
    """
    处理 OpenAI 客户端授权中途触发的手机号验证绑定 (add_phone)
    具备：
    1. 真实浏览器导航 Headers 预热 add-phone 页面状态与 Session Cookies
    2. 对 invalid_state / session is no longer valid 自动重刷会话并重试
    3. 支持号码被拒 (fraud_guard / voip / in_use) 自动退单并换号重试 (最多 3 次)
    """
    provider = str(sms_cfg.get("sms_provider") or "5sim").lower()
    emit = log_fn or (lambda m, l="info": None)

    add_phone_url = current_continue_url or "https://auth.openai.com/add-phone"
    page_headers = {
        "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "referer": "https://auth.openai.com/",
        "sec-fetch-dest": "document",
        "sec-fetch-mode": "navigate",
        "sec-fetch-site": "same-origin",
        "sec-fetch-user": "?1",
        "upgrade-insecure-requests": "1",
    }

    max_phone_attempts = 3
    for phone_attempt in range(1, max_phone_attempts + 1):
        # 1. 访问 add-phone 页面同步会话状态与 Cookies
        try:
            add_resp = session.get(add_phone_url, headers=page_headers, allow_redirects=True)
            add_referer = str(getattr(add_resp, "url", "") or add_phone_url)
        except Exception as e:
            log.warning(f"访问 add-phone 异常: {e}")
            add_referer = add_phone_url

        # 2. 购买接码号码
        attempt_str = f" (第 {phone_attempt}/{max_phone_attempts} 次尝试)" if max_phone_attempts > 1 else ""
        emit(f"📱 触发手机号绑定流程{attempt_str}，正在调用接码平台 [{provider.upper()}] 购买号码...", "highlight")
        bought = None
        if provider in ("sms_online", "sms_online_pro", "smsonline", "sms-online"):
            so_key = sms_cfg.get("sms_online_api_key") or sms_cfg.get("sms_api_key")
            so_country = sms_cfg.get("sms_online_country") or "6"
            so_max_price = sms_cfg.get("sms_online_max_price") or "0.06"
            bought = _sms_online_buy_number(so_key, country=so_country, max_price=so_max_price)
        elif provider in ("hero", "herosms", "hero_sms"):
            h_key = sms_cfg.get("hero_sms_api_key") or sms_cfg.get("sms_api_key")
            h_country = sms_cfg.get("hero_sms_country") or sms_cfg.get("hero_smsonline_country") or "10"
            h_max_price = sms_cfg.get("hero_sms_max_price") or sms_cfg.get("sms_online_max_price") or None
            bought = _hero_sms_buy_number(h_key, country=h_country, max_price=h_max_price)
        else:
            # 默认 5sim
            f_key = sms_cfg.get("sms_api_key")
            f_country = sms_cfg.get("sms_country") or "vietnam"
            bought = _5sim_buy_number(f_key, country=f_country)

        if not bought or not bought.get("phone"):
            emit("❌ 接码平台购号失败，请检查接码平台余额或国家配置", "error")
            return None, None

        phone = bought["phone"]
        order_id = bought["order_id"]
        if not phone.startswith("+"):
            phone = f"+{phone}"
        emit(f"✅ 成功获取临时号码: {phone} (订单ID: {order_id})", "info")

        def finish_order():
            if provider in ("sms_online", "sms_online_pro", "smsonline", "sms-online"):
                _sms_online_finish_order(sms_cfg.get("sms_online_api_key", ""), str(order_id))
            elif provider in ("hero", "herosms", "hero_sms"):
                _hero_sms_finish_order(sms_cfg.get("hero_sms_api_key", ""), str(order_id))
            else:
                _5sim_finish_order(sms_cfg.get("sms_api_key", ""), order_id)

        def cancel_order():
            if provider in ("sms_online", "sms_online_pro", "smsonline", "sms-online"):
                _sms_online_cancel_order(sms_cfg.get("sms_online_api_key", ""), str(order_id))
            elif provider in ("hero", "herosms", "hero_sms"):
                _hero_sms_cancel_order(sms_cfg.get("hero_sms_api_key", ""), str(order_id))
            else:
                _5sim_cancel_order(sms_cfg.get("sms_api_key", ""), order_id)

        # 3. 提交手机号给 OpenAI
        emit(f"正在向 OpenAI 提交手机号: {phone}...", "info")
        send_resp = session.post(
            "https://auth.openai.com/api/accounts/add-phone/send",
            json={"phone_number": phone, "channel": phone_channel},
            headers={
                "referer": add_referer,
                "origin": "https://auth.openai.com",
                "accept": "application/json, text/plain, */*",
                "sec-fetch-dest": "empty",
                "sec-fetch-mode": "cors",
                "sec-fetch-site": "same-origin",
            }
        )
        resp_text = send_resp.text or ""

        # 针对 session 状态过期或 invalid_state 自动重试刷新机制
        if send_resp.status_code not in (200, 201) and ("invalid_state" in resp_text or "no longer valid" in resp_text or send_resp.status_code == 409):
            emit("检测到会话状态同步延迟 (invalid_state)，正在刷新页面状态并重试提交...", "info")
            try:
                retry_resp = session.get(add_phone_url, headers=page_headers, allow_redirects=True)
                add_referer = str(getattr(retry_resp, "url", "") or add_referer)
            except Exception:
                pass
            time.sleep(2)
            send_resp = session.post(
                "https://auth.openai.com/api/accounts/add-phone/send",
                json={"phone_number": phone, "channel": phone_channel},
                headers={
                    "referer": add_referer,
                    "origin": "https://auth.openai.com",
                    "accept": "application/json, text/plain, */*",
                    "sec-fetch-dest": "empty",
                    "sec-fetch-mode": "cors",
                    "sec-fetch-site": "same-origin",
                }
            )
            resp_text = send_resp.text or ""

        if send_resp.status_code not in (200, 201):
            rejected_kw = [
                "fraud_guard", "voip_phone_number_disallowed", "landline_disallowed",
                "unsupported_phone_number", "phone_already_in_use", "invalid_phone_number"
            ]
            is_rejected = any(kw in resp_text for kw in rejected_kw)
            if is_rejected:
                emit(f"⚠️ 号码 {phone} 被 OpenAI 拒收 (号码池风控/已被占用)，已自动取消该单并尝试更换新号...", "warning")
            else:
                emit(f"⚠️ 提交号码未通过 (HTTP {send_resp.status_code}): {resp_text[:140]}", "warning")
            cancel_order()
            time.sleep(1)
            continue

        send_step = send_resp.json() if send_resp.text else {}
        next_page_type = send_step.get("page", {}).get("type", "")
        if next_page_type == "add_phone" and not send_step.get("continue_url"):
            emit(f"⚠️ 提交号码后仍停留在 add_phone，该号可能不可用，取消并重试...", "warning")
            cancel_order()
            continue

        emit(f"✅ 手机号 {phone} 已成功被 OpenAI 受理，等待短信验证码...", "info")

        # 4. 触发发送短信验证码
        try:
            session.get("https://auth.openai.com/api/accounts/phone-otp/send", headers={"referer": "https://auth.openai.com/phone-verification"})
        except Exception:
            pass

        # 5. 轮询接码平台短信
        emit("正在等待接码平台收取 6 位短信验证码...", "info")
        sms_code = None
        if provider in ("sms_online", "sms_online_pro", "smsonline", "sms-online"):
            sms_code = _sms_online_poll_sms(sms_cfg.get("sms_online_api_key", ""), str(order_id), timeout=120, log_fn=emit)
        elif provider in ("hero", "herosms", "hero_sms"):
            sms_code = _hero_sms_poll_sms(sms_cfg.get("hero_sms_api_key", ""), str(order_id), timeout=120, log_fn=emit)
        else:
            sms_code = _5sim_poll_sms(sms_cfg.get("sms_api_key", ""), order_id, timeout=120, log_fn=emit)

        if not sms_code:
            emit(f"⚠️ 短信验证码获取超时，正在取消号码 {phone} 并换号重试...", "warning")
            cancel_order()
            continue

        emit(f"🎉 成功接收到短信验证码: {sms_code}，正在验证...", "success")

        # 6. 提交短信验证码
        val_resp = session.post(
            "https://auth.openai.com/api/accounts/phone-otp/validate",
            json={"code": sms_code},
            headers={"referer": "https://auth.openai.com/phone-verification", "origin": "https://auth.openai.com"}
        )
        if val_resp.status_code not in (200, 201):
            emit(f"❌ 手机验证码校验失败: {val_resp.text[:150]}", "error")
            cancel_order()
            continue

        finish_order()
        emit("✅ 手机号绑定成功！继续推进授权流程...", "success")
        val_data = val_resp.json() if val_resp.text else {}
        next_continue = val_data.get("continue_url", "")
        return val_data, next_continue

    emit("❌ 手机号绑定全部尝试均未成功，请检查接码平台余额或更换国家号池", "error")
    return None, None


class DesktopOAuthEngine:
    """
    官方 Codex Desktop OAuth 认证引擎
    """

    def __init__(self, proxy: str = None, worker_url: str = None, worker_admin_auth: str = None):
        self.proxy = refresh_dynamic_proxy(proxy or "")
        self.worker_url = worker_url or DEFAULT_WORKER_URL
        self.worker_admin_auth = worker_admin_auth or DEFAULT_WORKER_ADMIN_AUTH

    def acquire_desktop_token(
        self,
        email: str,
        password: str = None,
        worker_jwt: str = "",
        client_id: str = "",
        refresh_token: str = "",
        mail_type: str = "worker",
        sms_cfg: dict = None,
        skip_password: bool = False,
        log_fn: Callable = None
    ) -> Optional[dict]:
        """
        全自动执行官方桌面客户端 OAuth 登录，换取官方 Access Token 全家桶
        """
        password = password or DEFAULT_ACCOUNT_PASSWORD
        def emit(msg: str, level: str = "info"):
            if log_fn:
                log_fn(msg, level)
            log.info(f"[{email}] {msg}")

        # 读取短信接码与系统配置
        if not sms_cfg:
            cfg_dict = database.get_all_config()
            sms_cfg = {
                "sms_provider": cfg_dict.get("sms_provider", "5sim"),
                "sms_api_key": cfg_dict.get("sms_api_key", ""),
                "sms_country": cfg_dict.get("sms_country", "vietnam"),
                "hero_sms_api_key": cfg_dict.get("hero_sms_api_key", ""),
                "hero_sms_country": cfg_dict.get("hero_sms_country", "10"),
                "sms_online_api_key": cfg_dict.get("sms_online_api_key", ""),
                "sms_online_country": cfg_dict.get("sms_online_country", "6"),
                "sms_online_max_price": cfg_dict.get("sms_online_max_price", "0.06"),
            }

        has_sms_key = bool(
            sms_cfg.get("sms_api_key") or
            sms_cfg.get("hero_sms_api_key") or
            sms_cfg.get("sms_online_api_key")
        )

        emit(f"🚀 正在初始化官方 Codex Desktop OAuth 授权上下文 (Client ID: {CLIENT_ID})...", "highlight")

        # 1. 生成 PKCE 与 State 凭证
        state = _random_state()
        code_verifier = _pkce_verifier()
        code_challenge = _pkce_challenge(code_verifier)

        params = {
            "client_id": CLIENT_ID,
            "response_type": "code",
            "redirect_uri": REDIRECT_URI,
            "scope": SCOPE,
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "prompt": "login",
            "id_token_add_organizations": "true",
            "codex_cli_simplified_flow": "true",
        }
        oauth_start_url = f"{AUTH_URL}?{urllib.parse.urlencode(params)}"

        # 2. 建立 TLS 会话 (Chrome 133 指纹)
        session = tls_client.Session(client_identifier=TLS_CLIENT_ID, random_tls_extension_order=True)
        if self.proxy and self.proxy.lower() != "none":
            session.proxies = {"http": self.proxy, "https": self.proxy}

        session.headers.update({
            "User-Agent": USER_AGENT,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "sec-ch-ua": '"Not_A Brand";v="8", "Chromium";v="133", "Google Chrome";v="133"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
        })

        # 3. 访问 OAuth Authorize 入口
        emit("正在与 auth.openai.com 建立客户端授权握手...", "info")
        init_resp = session.get(
            oauth_start_url,
            headers={"referer": "https://auth.openai.com/", "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"},
            allow_redirects=True
        )
        if init_resp.status_code not in (200, 302, 307):
            emit(f"❌ OAuth 入口请求失败 (HTTP {init_resp.status_code})", "error")
            return None

        did = (session.cookies.get_dict().get("oai-did") if hasattr(session.cookies, "get_dict") else session.cookies.get("oai-did")) or str(secrets.token_hex(16))
        auth_final_url = getattr(init_resp, "url", "") or "https://auth.openai.com/"

        # 4. 生成 Sentinel Token 并提交 Identifier (邮箱)
        sentinel_tok = generate_sentinel_token(did=did, flow="authorize_continue", referer=auth_final_url, proxy=self.proxy)
        headers_continue = {
            "referer": auth_final_url,
            "origin": "https://auth.openai.com",
            "Content-Type": "application/json"
        }
        if sentinel_tok:
            headers_continue["openai-sentinel-token"] = sentinel_tok

        emit(f"正在向服务端提交被邀请邮箱: {email}...", "info")
        id_resp = session.post(
            "https://auth.openai.com/api/accounts/authorize/continue",
            json={"username": {"kind": "email", "value": email}},
            headers=headers_continue
        )
        if id_resp.status_code not in (200, 201):
            emit(f"❌ 提交邮箱失败: HTTP {id_resp.status_code} | {id_resp.text[:150]}", "error")
            if "account_deactivated" in id_resp.text:
                return {"error": "account_deactivated", "message": "账号已被 OpenAI 停用封禁"}
            return {"error": "submit_email_failed", "message": f"提交邮箱失败 (HTTP {id_resp.status_code})"}

        id_data = id_resp.json() if id_resp.text else {}
        page_type = id_data.get("page", {}).get("type", "")
        continue_url = id_data.get("continue_url", "")
        callback_info = None

        # 5. 分支判断: 手机验证码 / 免密码邮箱 OTP / 传统密码验证
        if page_type == "add_phone" or "add-phone" in (continue_url or "").lower():
            if not has_sms_key:
                emit("❌ OpenAI 要求手机号验证，但未配置短信接码平台 API Key。可在后台配置 5sim / HeroSMS / sms-online.pro，或使用免手机号的优质老号。", "error")
                return {"error": "no_sms_key", "message": "未配置短信接码平台 API Key"}
            phone_step, phone_continue = handle_add_phone_flow(session, continue_url, sms_cfg, log_fn=emit)
            if not phone_step:
                return {"error": "sms_failed", "message": "手机号绑定未成功 (接码平台无号或超时)"}
            continue_url = phone_continue

        elif skip_password or page_type == "email_otp_verification" or "email-otp" in (continue_url or "").lower() or "email-verification" in (continue_url or "").lower():
            is_passwordless_switch = bool(skip_password and (page_type == "login_password" or not page_type or "password" in (continue_url or "").lower()))
            if is_passwordless_switch:
                emit("已启用免密码验证模式，正在触发邮箱验证码 (Email OTP)...", "highlight")
            elif skip_password and page_type != "email_otp_verification":
                emit("已启用免密码验证模式，直接进入邮箱验证码 (Email OTP) 阶段...", "highlight")
            else:
                emit("服务端要求邮箱验证码 (Email OTP)...", "highlight")

            is_outlook = (mail_type == "outlook") or bool(refresh_token) or email.lower().endswith(("@outlook.com", "@hotmail.com", "@live.com", "@msn.com"))
            worker_jwt = "".join(str(worker_jwt or "").split())
            if not is_outlook and (not worker_jwt or len(worker_jwt) < 30):
                try:
                    w_acc = database.get_worker_account_by_email(email)
                    if w_acc and w_acc.get("jwt"):
                        worker_jwt = "".join(str(w_acc["jwt"]).split())
                except Exception:
                    pass
            if not is_outlook and (not worker_jwt or len(worker_jwt) < 30):
                fresh_jwt = get_worker_jwt_for_email(self.worker_url, self.worker_admin_auth, email)
                if fresh_jwt:
                    worker_jwt = "".join(str(fresh_jwt).split())

            otp_url = continue_url or "https://auth.openai.com/email-verification"
            send_started_at = time.time()
            last_id = 0
            if not is_outlook and worker_jwt:
                last_id = get_latest_worker_mail_id(self.worker_url, worker_jwt)

            sent = False
            if is_passwordless_switch:
                pwd_page_url = continue_url or "https://auth.openai.com/log-in/password"
                try:
                    session.get(
                        pwd_page_url,
                        headers={
                            "referer": auth_final_url,
                            "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                        },
                        allow_redirects=True,
                    )
                except Exception as e:
                    log.warning(f"打开密码页异常: {e}")

                otp_sentinel = generate_sentinel_token(
                    did=did or (session.cookies.get_dict().get("oai-did") if hasattr(session.cookies, "get_dict") else session.cookies.get("oai-did")) or "",
                    flow="authorize_continue",
                    referer="https://auth.openai.com/log-in/password",
                    proxy=self.proxy,
                )
                pwd_headers = {
                    "referer": "https://auth.openai.com/log-in/password",
                    "origin": "https://auth.openai.com",
                }
                if otp_sentinel:
                    pwd_headers["openai-sentinel-token"] = otp_sentinel

                try:
                    send_r = session.post(
                        "https://auth.openai.com/api/accounts/passwordless/send-otp",
                        json={},
                        headers=pwd_headers,
                    )
                    if send_r.status_code in (200, 201):
                        sent = True
                        step_data = send_r.json() if send_r.text else {}
                        otp_url = step_data.get("continue_url") or "https://auth.openai.com/email-verification"
                        emit("✅ 成功触发官方免密验证码下发 (passwordless/send-otp: 200)", "info")
                    else:
                        log.warning(f"passwordless/send-otp 失败 ({send_r.status_code}): {send_r.text[:200]}")
                except Exception as e:
                    log.warning(f"passwordless/send-otp 请求异常: {e}")

            if not sent:
                try:
                    session.get(
                        otp_url,
                        headers={
                            "referer": auth_final_url,
                            "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                        },
                        allow_redirects=True,
                    )
                except Exception as e:
                    log.warning(f"打开 OTP 页面异常: {e}")

                # 触发发送/重发验证码 (POST send -> POST resend -> GET send，参考 oauth_login.py)
                emit("正在触发发送官方邮箱验证码 (Email OTP)...", "info")
                for method, send_url in [
                    ("POST", "https://auth.openai.com/api/accounts/email-otp/send"),
                    ("POST", "https://auth.openai.com/api/accounts/email-otp/resend"),
                    ("GET", "https://auth.openai.com/api/accounts/email-otp/send"),
                ]:
                    try:
                        if method == "POST":
                            send_r = session.post(
                                send_url,
                                json={},
                                headers={
                                    "referer": otp_url,
                                    "origin": "https://auth.openai.com",
                                },
                            )
                        else:
                            send_r = session.get(send_url, headers={"referer": otp_url})
                        if send_r.status_code < 300:
                            break
                    except Exception as e:
                        log.warning(f"OTP send 异常: {e}")

            # 轮询最新邮件验证码 (自动路由至 Outlook API 或 Cloudflare Worker)
            otp_code = poll_email_code_unified(
                email=email,
                mail_type="outlook" if is_outlook else "worker",
                worker_url=self.worker_url,
                worker_jwt=worker_jwt,
                client_id=client_id,
                refresh_token=refresh_token,
                timeout_sec=45,
                interval_sec=2 if not is_outlook else 3,
                min_id=last_id,
                since_time=send_started_at,
                log_fn=emit
            )

            if not otp_code:
                mail_desc = "Outlook 微软收件箱" if is_outlook else "支持的 Worker 域名"
                emit(f"❌ 邮箱验证码获取超时，请确认该邮箱是否属于{mail_desc}", "error")
                return {"error": "otp_timeout", "message": "邮箱验证码接收超时"}

            emit(f"正在提交验证码 [{otp_code}] 完成校验...", "info")
            val_resp = session.post(
                "https://auth.openai.com/api/accounts/email-otp/validate",
                json={"code": otp_code},
                headers={"referer": otp_url, "origin": "https://auth.openai.com"}
            )
            val_text = val_resp.text or ""
            val_loc = val_resp.headers.get("Location") or ""
            err_payload = parse_openai_error_payload(val_loc)
            err_code = err_payload.get("errorCode", "")

            if val_resp.status_code == 403 or err_code == "account_deactivated" or "account_deactivated" in val_text:
                emit("❌ 登录失败: 账号已被 OpenAI 停用封禁 (Deactivated)", "error")
                return {"error": "account_deactivated", "message": "账号已被 OpenAI 停用封禁 (Deactivated)"}

            if err_code in ("user_blocked", "account_blocked") or "user_blocked" in val_text or "account_blocked" in val_text:
                emit("❌ 登录失败: 账号已被 OpenAI 屏蔽锁定 (user_blocked)", "error")
                return {"error": "user_blocked", "message": "账号已被 OpenAI 屏蔽锁定 (Blocked)"}

            if val_resp.status_code not in (200, 201):
                emit(f"❌ 验证码校验失败: {val_text[:150]}", "error")
                return {"error": "otp_validate_failed", "message": f"邮箱验证码校验未通过 ({val_text[:80]})"}

            val_data = val_resp.json() if val_resp.text else {}
            continue_url = val_data.get("continue_url", "") or val_data.get("page", {}).get("payload", {}).get("url", "")
            val_page_type = val_data.get("page", {}).get("type", "")

            # 邮箱 OTP 验证后若触发手机接码绑定
            if val_page_type == "add_phone" or "add-phone" in (continue_url or "").lower():
                if not has_sms_key:
                    emit("❌ 邮箱验证码通过后触发手机号绑定，但未配置短信接码平台 API Key。", "error")
                    return {"error": "no_sms_key", "message": "未配置接码 API Key"}
                phone_step, phone_continue = handle_add_phone_flow(session, continue_url, sms_cfg, log_fn=emit)
                if not phone_step:
                    return {"error": "sms_failed", "message": "邮箱验证后手机号绑定失败 (接码无号或验证码超时)"}
                continue_url = phone_continue

        elif page_type == "login_password" or not page_type:
            emit("服务端要求输入账号密码，正在提交...", "info")
            password_url = (id_data.get("continue_url")) or "https://auth.openai.com/log-in/password"
            try:
                session.get(password_url, headers={"referer": auth_final_url}, allow_redirects=True)
            except Exception:
                pass
            pwd_sentinel = generate_sentinel_token(did=did, flow="password_verify", referer=password_url, proxy=self.proxy)
            pwd_headers = {"referer": password_url, "origin": "https://auth.openai.com"}
            if pwd_sentinel:
                pwd_headers["openai-sentinel-token"] = pwd_sentinel

            pwd_resp = session.post(
                "https://auth.openai.com/api/accounts/password/verify",
                json={"password": password},
                headers=pwd_headers,
                allow_redirects=False
            )
            loc = pwd_resp.headers.get("Location") or pwd_resp.headers.get("location") or ""
            pwd_text = pwd_resp.text or ""
            err_payload = parse_openai_error_payload(loc)
            err_code = err_payload.get("errorCode", "")

            if err_code == "account_deactivated" or "account_deactivated" in pwd_text or "account_deactivated" in loc or "deactivated" in pwd_text.lower():
                emit("❌ 登录失败: 账号已被 OpenAI 停用封禁 (Deactivated)", "error")
                return {"error": "account_deactivated", "message": "账号已被 OpenAI 停用封禁 (Deactivated)"}

            if err_code in ("user_blocked", "account_blocked") or "user_blocked" in pwd_text or "account_blocked" in pwd_text or "blocked" in loc.lower():
                emit("❌ 登录失败: 账号已被 OpenAI 屏蔽锁定 (Blocked)", "error")
                return {"error": "user_blocked", "message": "账号已被 OpenAI 屏蔽锁定 (Blocked)"}

            fallback_sent = False
            if err_code == "invalid_username_or_password" or "invalid_username_or_password" in pwd_text:
                emit("⚠️ 密码核验失败 (密码错误)，正在尝试自动回退至邮箱免密验证码 (Email OTP)...", "highlight")
                otp_sentinel = generate_sentinel_token(
                    did=did or (session.cookies.get_dict().get("oai-did") if hasattr(session.cookies, "get_dict") else session.cookies.get("oai-did")) or "",
                    flow="authorize_continue",
                    referer="https://auth.openai.com/log-in/password",
                    proxy=self.proxy,
                )
                pwd_headers = {
                    "referer": "https://auth.openai.com/log-in/password",
                    "origin": "https://auth.openai.com",
                }
                if otp_sentinel:
                    pwd_headers["openai-sentinel-token"] = otp_sentinel

                try:
                    fallback_r = session.post(
                        "https://auth.openai.com/api/accounts/passwordless/send-otp",
                        json={},
                        headers=pwd_headers,
                    )
                    if fallback_r.status_code in (200, 201):
                        fallback_sent = True
                        step_data = fallback_r.json() if fallback_r.text else {}
                        continue_url = step_data.get("continue_url") or "https://auth.openai.com/email-verification"
                        next_page_type = "email_otp_verification"
                        emit("✅ 已成功触发免密验证码下发，进入邮箱验证流程...", "info")
                    else:
                        log.warning(f"密码错误后回退 passwordless/send-otp 失败 ({fallback_r.status_code}): {fallback_r.text[:200]}")
                except Exception as e:
                    log.warning(f"密码错误后回退 passwordless/send-otp 异常: {e}")

                if not fallback_sent:
                    emit("❌ 登录失败: 密码错误，请核对该账号在后台配置的密码", "error")
                    return {"error": "invalid_password", "message": "密码错误，请核对后台配置密码"}

            if pwd_resp.status_code in (302, 303, 307):
                if "error" in loc:
                    emit(f"❌ 官方授权重定向异常 ({err_code or 'error'}): {loc[:140]}", "error")
                    return {"error": "redirect_error", "message": f"授权重定向异常 ({err_code or 'error'})"}
                callback_info = _extract_callback_code(loc)
                continue_url = loc if not callback_info else ""
            elif fallback_sent or pwd_resp.status_code in (200, 201):
                if not fallback_sent:
                    pwd_data = pwd_resp.json() if pwd_resp.text else {}
                    continue_url = pwd_data.get("continue_url", "")
                    next_page_type = pwd_data.get("page", {}).get("type", "")

                # 密码后若触发手机接码
                if next_page_type == "add_phone" or "add-phone" in (continue_url or "").lower():
                    if not has_sms_key:
                        emit("❌ 密码通过后触发手机号验证，但未配置接码 API Key。", "error")
                        return {"error": "no_sms_key", "message": "未配置接码 API Key"}
                    phone_step, phone_continue = handle_add_phone_flow(session, continue_url, sms_cfg, log_fn=emit)
                    if not phone_step:
                        return {"error": "sms_failed", "message": "密码后手机号绑定失败 (接码无号或验证码超时)"}
                    continue_url = phone_continue

                # 密码后若触发邮箱 OTP
                elif next_page_type == "email_otp_verification" or "email-otp" in (continue_url or "").lower() or "email-verification" in (continue_url or "").lower():
                    emit("密码验证通过，触发二级邮箱安全验证码...", "highlight")
                    is_outlook = (mail_type == "outlook") or bool(refresh_token) or email.lower().endswith(("@outlook.com", "@hotmail.com", "@live.com", "@msn.com"))
                    worker_jwt = "".join(str(worker_jwt or "").split())
                    if not is_outlook and (not worker_jwt or len(worker_jwt) < 30):
                        try:
                            w_acc = database.get_worker_account_by_email(email)
                            if w_acc and w_acc.get("jwt"):
                                worker_jwt = "".join(str(w_acc["jwt"]).split())
                        except Exception:
                            pass
                    if not is_outlook and (not worker_jwt or len(worker_jwt) < 30):
                        fresh_jwt = get_worker_jwt_for_email(self.worker_url, self.worker_admin_auth, email)
                        if fresh_jwt:
                            worker_jwt = "".join(str(fresh_jwt).split())

                    send_started_at = time.time()
                    last_id = 0
                    if not is_outlook and worker_jwt:
                        last_id = get_latest_worker_mail_id(self.worker_url, worker_jwt)

                    # 访问 OTP 页面并触发发送/重发验证码
                    try:
                        session.get(continue_url or "https://auth.openai.com/email-verification", headers={"referer": password_url}, allow_redirects=True)
                        if not fallback_sent:
                            session.post(
                                "https://auth.openai.com/api/accounts/email-otp/resend",
                                json={},
                                headers={"referer": continue_url or "https://auth.openai.com/email-verification", "origin": "https://auth.openai.com"}
                            )
                    except Exception as e:
                        log.warning(f"触发密码后 OTP 发送异常: {e}")

                    # 轮询最新邮件验证码 (自动路由至 Outlook API 或 Cloudflare Worker)
                    otp_code = poll_email_code_unified(
                        email=email,
                        mail_type="outlook" if is_outlook else "worker",
                        worker_url=self.worker_url,
                        worker_jwt=worker_jwt,
                        client_id=client_id,
                        refresh_token=refresh_token,
                        timeout_sec=45,
                        interval_sec=2 if not is_outlook else 3,
                        min_id=last_id,
                        since_time=send_started_at,
                        log_fn=emit
                    )
                    if not otp_code:
                        emit("❌ 二级邮箱验证码获取超时", "error")
                        return {"error": "otp_timeout", "message": "二级邮箱验证码获取超时"}

                    emit(f"正在提交验证码 [{otp_code}] 完成校验...", "info")
                    val_resp = session.post(
                        "https://auth.openai.com/api/accounts/email-otp/validate",
                        json={"code": otp_code},
                        headers={"referer": continue_url or "https://auth.openai.com/email-verification", "origin": "https://auth.openai.com"}
                    )
                    val_text = val_resp.text or ""
                    val_loc = val_resp.headers.get("Location") or ""
                    err_payload = parse_openai_error_payload(val_loc)
                    err_code = err_payload.get("errorCode", "")

                    if val_resp.status_code == 403 or err_code == "account_deactivated" or "account_deactivated" in val_text:
                        emit("❌ 登录失败: 账号已被 OpenAI 停用封禁 (Deactivated)", "error")
                        return {"error": "account_deactivated", "message": "账号已被 OpenAI 停用封禁 (Deactivated)"}

                    if err_code in ("user_blocked", "account_blocked") or "user_blocked" in val_text or "account_blocked" in val_text:
                        emit("❌ 登录失败: 账号已被 OpenAI 屏蔽锁定 (user_blocked)", "error")
                        return {"error": "user_blocked", "message": "账号已被 OpenAI 屏蔽锁定 (Blocked)"}

                    if val_resp.status_code not in (200, 201):
                        emit(f"❌ 验证码校验失败: {val_text[:150]}", "error")
                        return {"error": "otp_validate_failed", "message": f"二级验证码校验失败 ({val_text[:80]})"}

                    val_data = val_resp.json() if val_resp.text else {}
                    continue_url = val_data.get("continue_url", "") or val_data.get("page", {}).get("payload", {}).get("url", "")
                    val_page_type = val_data.get("page", {}).get("type", "")

                    # 邮箱 OTP 验证后若触发手机接码绑定
                    if val_page_type == "add_phone" or "add-phone" in (continue_url or "").lower():
                        if not has_sms_key:
                            emit("❌ 邮箱验证码通过后触发手机号绑定，但未配置短信接码平台 API Key。", "error")
                            return {"error": "no_sms_key", "message": "未配置接码 API Key"}
                        phone_step, phone_continue = handle_add_phone_flow(session, continue_url, sms_cfg, log_fn=emit)
                        if not phone_step:
                            return {"error": "sms_failed", "message": "邮箱验证后手机号绑定失败 (接码无号或验证码超时)"}
                        continue_url = phone_continue
            else:
                emit(f"❌ 密码校验失败 (HTTP {pwd_resp.status_code}): {pwd_text[:150]}", "error")
                return {"error": "password_verify_failed", "message": f"密码校验未通过: HTTP {pwd_resp.status_code}"}

        # 6. 跟随重定向与 Consent 授权确认
        emit("正在处理客户端授权确认与回调路由...", "info")
        while continue_url:
            callback_info = _extract_callback_code(continue_url)
            if callback_info:
                break

            resp_next = session.get(continue_url, headers={"referer": "https://auth.openai.com/"}, allow_redirects=False)
            body_text = resp_next.text or ""
            loc_next = resp_next.headers.get("Location") or resp_next.headers.get("location") or ""
            final_next = loc_next or getattr(resp_next, "url", "") or continue_url

            callback_info = _extract_callback_code(final_next) or _extract_callback_code(body_text)
            if callback_info:
                break

            # 重定向中若触发 add-phone
            if "add-phone" in final_next.lower() or "add_phone" in body_text:
                if not has_sms_key:
                    emit("❌ 授权流程中触发手机号验证，但未配置短信接码平台 API Key。", "error")
                    return None
                phone_step, phone_continue = handle_add_phone_flow(session, final_next or continue_url, sms_cfg, log_fn=emit)
                if not phone_step:
                    return None
                continue_url = phone_continue
                continue

            # 检查是否有 consent 选择 workspace
            ws_id = _extract_workspace_id_from_html(body_text)
            if not ws_id and ("workspace_id" in body_text or "consent" in final_next.lower()):
                ws_match = re.search(r'data-workspace-id=["\']([^"\']+)["\']', body_text)
                if ws_match:
                    ws_id = ws_match.group(1)

            if ws_id:
                emit(f"确认授权 Workspace: {ws_id}", "info")
                ws_resp = session.post(
                    "https://auth.openai.com/api/accounts/workspace/select",
                    json={"workspace_id": ws_id},
                    headers={"referer": continue_url, "origin": "https://auth.openai.com", "Content-Type": "application/json"}
                )
                ws_data = ws_resp.json() if ws_resp.text else {}
                continue_url = ws_data.get("continue_url", "")
                callback_info = _extract_callback_code(continue_url)
                if callback_info:
                    break
                continue

            if loc_next:
                continue_url = loc_next
                continue

            break

        if not callback_info:
            emit("❌ 未能获取到 OAuth Callback 授权码 (Code)", "error")
            return None

        auth_code = callback_info["code"]
        emit(f"✅ 成功捕获官方客户端授权码: {auth_code[:12]}...", "success")

        # 7. 提交授权码换取官方 Desktop Token 全家桶
        emit("正在向 https://auth.openai.com/oauth/token 换取官方客户端 Access Token...", "highlight")
        token_data = {
            "grant_type": "authorization_code",
            "client_id": CLIENT_ID,
            "code": auth_code,
            "redirect_uri": REDIRECT_URI,
            "code_verifier": code_verifier,
        }

        tok_session = requests.Session()
        if self.proxy and self.proxy.lower() != "none":
            tok_session.proxies = {"http": self.proxy, "https": self.proxy}

        tok_resp = tok_session.post(TOKEN_URL, data=token_data, headers={"User-Agent": USER_AGENT}, timeout=20)
        if tok_resp.status_code != 200:
            emit(f"❌ 换取 Token 失败 (HTTP {tok_resp.status_code}): {tok_resp.text[:200]}", "error")
            return None

        tokens_json = tok_resp.json()
        access_token = tokens_json.get("access_token", "")
        refresh_token = tokens_json.get("refresh_token", "")
        id_token = tokens_json.get("id_token", "")
        expires_in = tokens_json.get("expires_in", 3600)

        claims = _jwt_claims_no_verify(id_token or access_token)
        account_id = (claims.get("https://api.openai.com/auth", {}) or {}).get("chatgpt_account_id", "")
        plan_type = (claims.get("https://api.openai.com/auth", {}) or {}).get("chatgpt_plan_type", "free")

        result = {
            "success": True,
            "email": email,
            "access_token": access_token,
            "refresh_token": refresh_token,
            "id_token": id_token,
            "account_id": account_id,
            "plan_type": plan_type,
            "client_id": CLIENT_ID,
            "expires_in": expires_in,
            "raw": tokens_json
        }

        emit(f"🎉 成功获取官方客户端授权凭证！Client ID: {CLIENT_ID} (方案: {plan_type.upper()})", "success")
        return result


def auto_register_openai_account(
    worker_url: str = None,
    worker_admin_auth: str = None,
    domain: str = None,
    default_password: str = None,
    proxy: str = None,
    log_fn: Callable = None
) -> Optional[dict]:
    """
    全自动注册全新 OpenAI 账号 (复刻 /opt/gptreg/auto.py 注册逻辑)
    """
    worker_url = worker_url or DEFAULT_WORKER_URL
    worker_admin_auth = worker_admin_auth or DEFAULT_WORKER_ADMIN_AUTH
    domain = domain or DEFAULT_WORKER_DOMAIN
    default_password = default_password or DEFAULT_ACCOUNT_PASSWORD
    def emit(msg: str, level: str = "info"):
        if log_fn:
            log_fn(msg, level)
        log.info(f"[AutoRegister] {msg}")

    # 1. 在 Worker 创建新邮箱
    emit("正在通过 Cloudflare Worker 生成新邮箱地址...", "info")
    name = f"{''.join(secrets.choice('abcdefghijklmnopqrstuvwxyz0123456789') for _ in range(7))}"
    try:
        w_resp = requests.post(
            f"{worker_url.rstrip('/')}/admin/new_address",
            json={"name": name, "domain": domain},
            headers={"x-admin-auth": worker_admin_auth},
            timeout=10,
            proxies={"http": None, "https": None}
        )
        if w_resp.status_code not in (200, 201):
            err_msg = f"Worker 创建邮箱失败: HTTP {w_resp.status_code} - {w_resp.text[:100]}"
            emit(f"❌ {err_msg}", "error")
            return {"success": False, "error": err_msg}
        w_data = w_resp.json()
        email = w_data.get("address") or f"{name}@{domain}"
        jwt = w_data.get("jwt") or ""
        emit(f"✅ 成功生成邮箱: {email}", "success")
    except Exception as e:
        err_msg = f"创建邮箱异常: {e}"
        emit(f"❌ {err_msg}", "error")
        return {"success": False, "error": err_msg}

    # 2. 建立 TLS 客户端会话
    s = tls_client.Session(client_identifier=TLS_CLIENT_ID, random_tls_extension_order=True)
    if proxy and proxy.lower() != "none":
        s.proxies = {"http": proxy, "https": proxy}

    s.headers.update({
        "User-Agent": USER_AGENT,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Origin": "https://auth.openai.com",
    })

    # 3. 开启 Auth 会话获取 DID
    try:
        csrf_resp = s.get("https://chatgpt.com/api/auth/csrf", headers={"referer": "https://chatgpt.com/"})
        csrf = (csrf_resp.json() or {}).get("csrfToken") if csrf_resp.status_code == 200 else ""
        signin_resp = s.post(
            "https://chatgpt.com/api/auth/signin/openai",
            params={"prompt": "login", "ext-oai-did": "", "login_hint": email},
            json={"csrfToken": csrf, "callbackUrl": "/", "json": "true"},
            headers={"referer": "https://chatgpt.com/"}
        )
        auth_url = (signin_resp.json() or {}).get("url") if signin_resp.status_code == 200 else ""
        if not auth_url:
            emit("❌ 初始化 OpenAI Auth 会话失败", "error")
            return {"success": False, "error": "初始化 OpenAI Auth 会话失败", "email": email}

        s.get(auth_url)
        did = (s.cookies.get_dict().get("oai-did") if hasattr(s.cookies, "get_dict") else s.cookies.get("oai-did")) or str(secrets.token_hex(16))
    except Exception as e:
        err_msg = f"建立 Auth 会话失败: {e}"
        emit(f"❌ {err_msg}", "error")
        return {"success": False, "error": err_msg, "email": email}

    # 4. 提交 Signup
    emit("正在向 OpenAI 提交注册信息...", "info")
    signup_sentinel = generate_sentinel_token(did=did, flow="username_password_create", referer="https://auth.openai.com/create-account", proxy=proxy)
    signup_resp = s.post(
        "https://auth.openai.com/api/accounts/authorize/continue",
        json={"username": {"value": email, "kind": "email"}, "screen_hint": "signup"},
        headers={"referer": "https://auth.openai.com/create-account", "openai-sentinel-token": signup_sentinel}
    )
    if signup_resp.status_code != 200:
        err_msg = f"提交注册邮箱失败: {signup_resp.text[:150]}"
        emit(f"❌ {err_msg}", "error")
        return {"success": False, "error": err_msg, "email": email}

    cont_url = (signup_resp.json() or {}).get("continue_url")
    if cont_url:
        try:
            s.get(cont_url)
        except Exception:
            pass

    # 5. 设置密码
    emit("正在设置账号登录密码...", "info")
    pwd_sentinel = generate_sentinel_token(did=did, flow="username_password_create", referer="https://auth.openai.com/create-account/password", proxy=proxy)
    pwd_resp = s.post(
        "https://auth.openai.com/api/accounts/user/register",
        json={"password": default_password, "username": email},
        headers={"referer": "https://auth.openai.com/create-account/password", "openai-sentinel-token": pwd_sentinel}
    )
    if pwd_resp.status_code not in (200, 201):
        err_msg = f"注册密码设置失败: {pwd_resp.text[:150]}"
        emit(f"❌ {err_msg}", "error")
        return {"success": False, "error": err_msg, "email": email}

    # 6. 发送与获取邮件验证码
    emit("正在触发 OpenAI 发送邮件验证码...", "info")
    try:
        s.get("https://auth.openai.com/api/accounts/email-otp/send", headers={"referer": "https://auth.openai.com/create-account/verify-email"})
    except Exception:
        pass

    emit("正在等待邮箱收取 6 位验证码...", "info")
    code = poll_email_code_worker(worker_url, jwt, timeout_sec=45, log_fn=emit)
    if not code:
        err_msg = "邮箱验证码获取超时"
        emit(f"❌ {err_msg}", "error")
        return {"success": False, "error": err_msg, "email": email}

    val_resp = s.post(
        "https://auth.openai.com/api/accounts/email-otp/validate",
        json={"code": code},
        headers={"referer": "https://auth.openai.com/create-account/verify-email", "origin": "https://auth.openai.com"}
    )
    if val_resp.status_code not in (200, 201):
        err_msg = f"邮箱验证码校验失败: {val_resp.text[:150]}"
        emit(f"❌ {err_msg}", "error")
        return {"success": False, "error": err_msg, "email": email}

    # 7. 创建账号 Profile
    emit("正在初始化账号基本信息 (Create Account)...", "info")
    create_sentinel = generate_sentinel_token(did=did, flow="oauth_create_account", referer="https://auth.openai.com/about-you", proxy=proxy)
    create_resp = s.post(
        "https://auth.openai.com/api/accounts/create_account",
        json={"name": "Neo", "birthdate": "2000-02-20"},
        headers={"referer": "https://auth.openai.com/about-you", "origin": "https://auth.openai.com", "openai-sentinel-token": create_sentinel}
    )
    if create_resp.status_code not in (200, 201):
        err_msg = f"创建 Profile 失败: {create_resp.text[:150]}"
        emit(f"❌ {err_msg}", "error")
        return {"success": False, "error": err_msg, "email": email}

    emit(f"🎉 账号 {email} 全自动注册成功！", "success")
    return {
        "success": True,
        "email": email,
        "password": default_password,
        "worker_jwt": jwt,
        "status": "available"
    }


def _extract_cookie_val(session, name: str) -> str:
    """安全提取 session cookie 值，避免 CookieConflictError"""
    try:
        val = session.cookies.get(name)
        if val:
            return val
    except Exception:
        pass
    values = []
    try:
        for cookie in session.cookies:
            if getattr(cookie, "name", "") == name:
                v = getattr(cookie, "value", "")
                if v:
                    values.append(v)
    except Exception:
        pass
    return values[-1] if values else ""


def check_account_status(
    email: str,
    password: str = None,
    proxy: str = None,
    mode: str = "fast",
    worker_url: str = None,
    worker_admin_auth: str = None,
    worker_jwt: str = "",
    client_id: str = "",
    refresh_token: str = "",
    mail_type: str = "worker",
    log_fn: Callable = None
) -> dict:
    """
    通用账号存活检测
    """
    password = password or DEFAULT_ACCOUNT_PASSWORD
    worker_url = worker_url or DEFAULT_WORKER_URL
    worker_admin_auth = worker_admin_auth or DEFAULT_WORKER_ADMIN_AUTH
    """
    OpenAI 账号验活引擎 (支持极速握手验活与深度网页登录换取 AT 验活)
    :param email: 待检测邮箱
    :param password: 账号密码
    :param proxy: 出口代理
    :param mode: "fast" (极速握手验活, 1~3秒) 或 "deep" (深度完整登录验活, 模拟 login_web.py)
    :param worker_url: Cloudflare Worker 基础 URL
    :param worker_admin_auth: Worker 管理鉴权密钥
    :param worker_jwt: Worker 邮箱 JWT 凭证
    :param log_fn: 进度日志回调
    :return: dict 包含 success, alive, status, code, message, access_token 等
    """
    def emit(msg: str, level: str = "info"):
        if log_fn:
            log_fn(msg, level)
        log.info(f"[AccountCheck:{email}] {msg}")

    emit(f"🔍 开始执行账号验活 [{'极速握手' if mode != 'deep' else '深度登录'}] 检测: {email}...", "info")

    for attempt in range(3):
        try:
            curr_proxy = refresh_dynamic_proxy(proxy or "")
            session = tls_client.Session(client_identifier=TLS_CLIENT_ID, random_tls_extension_order=True)
            if curr_proxy and curr_proxy.lower() != "none":
                session.proxies = {"http": curr_proxy, "https": curr_proxy}
            session.headers.update({
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
                "sec-ch-ua": '"Not_A Brand";v="8", "Chromium";v="133", "Google Chrome";v="133"',
                "sec-ch-ua-mobile": "?0",
                "sec-ch-ua-platform": '"Windows"',
            })

            # 1. 访问官方 OAuth 授权入口 (高可靠，无 Turnstile 拦截)
            params = {
                "client_id": CLIENT_ID,
                "response_type": "code",
                "redirect_uri": REDIRECT_URI,
                "scope": SCOPE,
                "state": _random_state(),
                "code_challenge": _pkce_challenge(_pkce_verifier()),
                "code_challenge_method": "S256",
                "prompt": "login",
                "id_token_add_organizations": "true",
                "codex_cli_simplified_flow": "true",
            }
            oauth_url = f"{AUTH_URL}?{urllib.parse.urlencode(params)}"
            auth_resp = session.get(oauth_url, headers={"referer": "https://auth.openai.com/"}, allow_redirects=True)
            if auth_resp.status_code not in (200, 302, 307):
                if attempt < 2:
                    time.sleep(1.2)
                    continue
                return {"success": False, "alive": None, "status": "unknown", "code": "init_failed", "message": f"OAuth 入口请求失败 (HTTP {auth_resp.status_code})"}

            auth_referer = getattr(auth_resp, "url", "") or "https://auth.openai.com/"
            did = _extract_cookie_val(session, "oai-did") or str(secrets.token_hex(16))

            # 2. 生成 Sentinel Token 并提交 Identifier (邮箱)
            sentinel = generate_sentinel_token(did=did, flow="authorize_continue", referer=auth_referer, proxy=curr_proxy)
            headers = {
                "referer": auth_referer,
                "origin": "https://auth.openai.com",
                "Content-Type": "application/json"
            }
            if sentinel:
                headers["openai-sentinel-token"] = sentinel

            id_resp = session.post(
                "https://auth.openai.com/api/accounts/authorize/continue",
                json={"username": {"kind": "email", "value": email}},
                headers=headers
            )
            text = id_resp.text or ""

            # 判定: 账号已被 OpenAI 停用/封禁
            if "account_deactivated" in text:
                emit("❌ 检测结果: 账号已被 OpenAI 停用封禁 (account_deactivated)", "error")
                return {
                    "success": True,
                    "alive": False,
                    "status": "banned",
                    "code": "account_deactivated",
                    "message": "账号已被 OpenAI 停用 (Deactivated)",
                    "time": time.strftime("%Y-%m-%d %H:%M:%S")
                }

            if "user_blocked" in text or "account_blocked" in text:
                emit("❌ 检测结果: 账号已被 OpenAI 屏蔽锁定 (user_blocked)", "error")
                return {
                    "success": True,
                    "alive": False,
                    "status": "banned",
                    "code": "user_blocked",
                    "message": "账号已被 OpenAI 屏蔽锁定 (Blocked)",
                    "time": time.strftime("%Y-%m-%d %H:%M:%S")
                }

            if "user_not_found" in text:
                emit("❌ 检测结果: 账号在 OpenAI 不存在或已被注销", "error")
                return {
                    "success": True,
                    "alive": False,
                    "status": "failed",
                    "code": "user_not_found",
                    "message": "账号在 OpenAI 不存在或已被注销",
                    "time": time.strftime("%Y-%m-%d %H:%M:%S")
                }

            if id_resp.status_code in (200, 201):
                id_data = id_resp.json() or {}
                page_type = id_data.get("page", {}).get("type", "")
                cont_url = id_data.get("continue_url", "")

                # 手机验证要求
                if page_type == "add_phone" or "add-phone" in cont_url.lower():
                    emit("✅ 检测结果: 账号正常存活 (免密码验活，要求绑定手机)", "success")
                    return {
                        "success": True,
                        "alive": True,
                        "status": "available",
                        "code": "requires_phone",
                        "message": "账号正常存活 (要求绑定手机)",
                        "time": time.strftime("%Y-%m-%d %H:%M:%S")
                    }

                # 极速免密码验活 (只要服务端握手成功且未封禁，即视为账号存活)
                if mode != "deep":
                    emit(f"✅ 检测结果: 账号正常存活 (免密码验活成功，页面类型: {page_type or 'Ready'})", "success")
                    return {
                        "success": True,
                        "alive": True,
                        "status": "available",
                        "code": "active",
                        "message": "账号正常存活 (免密码验活通过)",
                        "time": time.strftime("%Y-%m-%d %H:%M:%S")
                    }

                # 深度验活分支: 仅在 deep 模式下进行免密码邮箱 OTP 验证换取 Session 凭证
                emit("🔍 深度验活模式: 正在进行免密码邮箱验证码 (Email OTP) 深度核验...", "info")
                is_outlook = (mail_type == "outlook") or bool(refresh_token) or email.lower().endswith(("@outlook.com", "@hotmail.com", "@live.com", "@msn.com"))
                if not is_outlook and not worker_jwt:
                    worker_jwt = get_worker_jwt_for_email(worker_url, worker_admin_auth, email)

                if not is_outlook and not worker_jwt:
                    emit("⚠️ 未配置 Worker 邮箱凭证，跳过深度 OTP 收发，基于服务端握手确认存活", "warning")
                    return {
                        "success": True,
                        "alive": True,
                        "status": "available",
                        "code": "active",
                        "message": "账号正常存活 (免密码握手通过，无Worker凭证跳过深检)",
                        "time": time.strftime("%Y-%m-%d %H:%M:%S")
                    }

                if is_outlook and not refresh_token:
                    emit("⚠️ 未配置 Outlook refresh_token 凭证，跳过深度 OTP 收发，基于服务端握手确认存活", "warning")
                    return {
                        "success": True,
                        "alive": True,
                        "status": "available",
                        "code": "active",
                        "message": "账号正常存活 (免密码握手通过，无Outlook凭证跳过深检)",
                        "time": time.strftime("%Y-%m-%d %H:%M:%S")
                    }

                is_pwd_page = (page_type == "login_password" or "password" in (cont_url or "").lower())
                sent = False
                send_started_at = time.time()
                last_id = 0
                if not is_outlook and worker_jwt:
                    last_id = get_latest_worker_mail_id(worker_url, worker_jwt)

                if is_pwd_page:
                    pwd_page_url = cont_url or "https://auth.openai.com/log-in/password"
                    try:
                        session.get(pwd_page_url, headers={"referer": auth_referer, "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}, allow_redirects=True)
                    except Exception:
                        pass
                    sent_tok = generate_sentinel_token(
                        did=did,
                        user_agent=session.headers.get("User-Agent") or "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
                        flow="authorize_continue",
                        referer="https://auth.openai.com/log-in/password",
                        proxy=self.proxy,
                    )
                    headers_otp = {"referer": "https://auth.openai.com/log-in/password", "origin": "https://auth.openai.com"}
                    if sent_tok:
                        headers_otp["openai-sentinel-token"] = sent_tok
                    try:
                        res = session.post("https://auth.openai.com/api/accounts/passwordless/send-otp", json={}, headers=headers_otp)
                        if res.status_code in (200, 201):
                            sent = True
                            st_data = res.json() if res.text else {}
                            otp_url = st_data.get("continue_url") or "https://auth.openai.com/email-verification"
                            emit("✅ 成功触发免密验证码下发 (passwordless/send-otp: 200)", "info")
                    except Exception:
                        pass

                if not sent:
                    otp_url = cont_url or "https://auth.openai.com/email-verification"
                    try:
                        session.get(otp_url, headers={"referer": auth_referer, "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}, allow_redirects=True)
                    except Exception:
                        pass

                    # 触发发送/重发验证码 (POST send -> POST resend -> GET send)
                    for method, send_url in [
                        ("POST", "https://auth.openai.com/api/accounts/email-otp/send"),
                        ("POST", "https://auth.openai.com/api/accounts/email-otp/resend"),
                        ("GET", "https://auth.openai.com/api/accounts/email-otp/send"),
                    ]:
                        try:
                            if method == "POST":
                                session.post(send_url, json={}, headers={"referer": otp_url, "origin": "https://auth.openai.com"})
                            else:
                                session.get(send_url, headers={"referer": otp_url})
                            break
                        except Exception:
                            pass

                emit(f"正在监听 {'Outlook 微软收件箱' if is_outlook else 'Worker 邮箱'} 获取最新登录验证码...", "info")
                otp_code = poll_email_code_unified(
                    email=email,
                    mail_type="outlook" if is_outlook else "worker",
                    worker_url=worker_url,
                    worker_jwt=worker_jwt,
                    client_id=client_id,
                    refresh_token=refresh_token,
                    timeout_sec=35,
                    interval_sec=2 if not is_outlook else 3,
                    min_id=last_id,
                    since_time=send_started_at,
                    log_fn=emit
                )

                if not otp_code:
                    emit("⚠️ 深度验活收码超时，但账号在 OpenAI 存在且未被封禁", "warning")
                    return {
                        "success": True,
                        "alive": True,
                        "status": "available",
                        "code": "active",
                        "message": "账号正常存活 (邮箱收信超时，账号未封禁)",
                        "time": time.strftime("%Y-%m-%d %H:%M:%S")
                    }

                emit(f"正在向 OpenAI 提交验证码 ({otp_code}) 核验账号真实状态...", "info")
                val_resp = session.post(
                    "https://auth.openai.com/api/accounts/email-otp/validate",
                    json={"code": otp_code},
                    headers={"referer": otp_url, "origin": "https://auth.openai.com"}
                )
                val_text = val_resp.text or ""
                val_loc = val_resp.headers.get("Location") or ""
                err_payload = parse_openai_error_payload(val_loc)
                err_code = err_payload.get("errorCode", "")

                # 核心判定: 账号被停用/封禁 (在此处返回 403 account_deactivated)
                if val_resp.status_code == 403 or err_code == "account_deactivated" or "account_deactivated" in val_text:
                    emit("❌ 检测结果: 账号已被 OpenAI 停用封禁 (account_deactivated)", "error")
                    return {
                        "success": True,
                        "alive": False,
                        "status": "banned",
                        "code": "account_deactivated",
                        "message": "账号已被 OpenAI 停用 (Deactivated)",
                        "time": time.strftime("%Y-%m-%d %H:%M:%S")
                    }

                if err_code in ("user_blocked", "account_blocked") or "user_blocked" in val_text or "account_blocked" in val_text:
                    emit("❌ 检测结果: 账号已被 OpenAI 屏蔽锁定 (user_blocked)", "error")
                    return {
                        "success": True,
                        "alive": False,
                        "status": "banned",
                        "code": "user_blocked",
                        "message": "账号已被 OpenAI 屏蔽锁定 (Blocked)",
                        "time": time.strftime("%Y-%m-%d %H:%M:%S")
                    }

                if val_resp.status_code in (200, 201):
                    val_data = val_resp.json() or {}
                    cb_url = val_data.get("continue_url", "") or val_data.get("page", {}).get("payload", {}).get("url", "")
                    token = ""
                    if cb_url:
                        _, token = _follow_chatgpt_web_redirects_and_get_token(session, cb_url)
                    if token:
                        emit(f"🎉 深度验活成功！已建立 Web 会话并获取 AccessToken ({token[:15]}...)", "success")
                        return {
                            "success": True,
                            "alive": True,
                            "status": "available",
                            "code": "active",
                            "access_token": token,
                            "message": "账号正常存活，免密码深度校验通过",
                            "time": time.strftime("%Y-%m-%d %H:%M:%S")
                        }
                    emit("✅ 邮箱验证码通过，账号正常存活！", "success")
                    return {
                        "success": True,
                        "alive": True,
                        "status": "available",
                        "code": "active",
                        "message": "账号正常存活，免密码邮箱验证通过",
                        "time": time.strftime("%Y-%m-%d %H:%M:%S")
                    }
                else:
                    emit(f"❌ 验证码校验失败 (HTTP {val_resp.status_code}): {val_text[:120]}", "error")
                    return {
                        "success": False,
                        "alive": None,
                        "status": "failed",
                        "code": "otp_validation_failed",
                        "message": f"邮箱验证码核验未通过: {val_text[:100]}",
                        "time": time.strftime("%Y-%m-%d %H:%M:%S")
                    }

            if "invalid_state" in text and attempt == 0:
                time.sleep(1)
                continue

            return {
                "success": False,
                "alive": None,
                "status": "unknown",
                "code": "http_error",
                "message": f"检测异常: HTTP {id_resp.status_code} - {text[:80]}",
                "time": time.strftime("%Y-%m-%d %H:%M:%S")
            }

        except Exception as e:
            if attempt == 2:
                return {
                    "success": False,
                    "alive": None,
                    "status": "unknown",
                    "code": "exception",
                    "message": f"网络/代理异常: {e}",
                    "time": time.strftime("%Y-%m-%d %H:%M:%S")
                }
            time.sleep(1)

    return {
        "success": False,
        "alive": None,
        "status": "unknown",
        "code": "timeout",
        "message": "检测超时，请稍后重试",
        "time": time.strftime("%Y-%m-%d %H:%M:%S")
    }


def _extract_openai_error_code(text: str, loc: str) -> str:
    """从响应文本或重定向 URL 的 payload 参数中提取 OpenAI 具体的错误代码"""
    low_text = (text or "").lower()
    low_loc = (loc or "").lower()
    for code in ("account_deactivated", "user_blocked", "account_blocked", "account_suspended", "invalid_username_or_password", "user_not_found"):
        if code in low_text or code in low_loc:
            return code
    for cand_url in (loc, text):
        if not cand_url or "payload=" not in cand_url:
            continue
        try:
            parsed = urllib.parse.urlparse(cand_url)
            qs = urllib.parse.parse_qs(parsed.query)
            p_b64 = qs.get("payload", [""])[0]
            if p_b64:
                rem = len(p_b64) % 4
                if rem:
                    p_b64 += "=" * (4 - rem)
                decoded = base64.b64decode(p_b64).decode("utf-8", errors="ignore")
                p_json = json.loads(decoded)
                return str(p_json.get("errorCode", "") or p_json.get("code", "")).lower()
        except Exception:
            pass
    return ""


def check_worker_account_phone_and_alive(
    email: str,
    password: str = None,
    proxy: str = None,
    log_fn: Callable = None,
    worker_jwt: str = None,
    client_id: str = None,
    refresh_token: str = None,
    mail_type: str = "worker"
) -> dict:
    """
    专门针对 Worker 与库存邮箱的【验活 + 验是否接手机码】复合判定引擎:
    1. 判定账号是否存活 / 是否被封禁 / 是否未注册
    2. 深度探测账号是否已接手机码 (已绑手机号) 还是需要接码 (add_phone)
    3. 自动结合 accounts_phone.txt 历史接码记录补全手机号
    """
    password = password or DEFAULT_ACCOUNT_PASSWORD
    def emit(msg: str, level: str = "info"):
        if log_fn:
            log_fn(msg, level)
        log.info(f"[{email}] {msg}")

    # 1. 查询本地已知接码历史
    known_phone = ""
    try:
        mapping = database.get_known_phone_mappings()
        known_phone = mapping.get(email.lower().strip(), "")
    except Exception:
        pass

    # 2. 查询 Worker API 配置与邮箱 JWT 凭证
    cfg = database.get_all_config()
    worker_url = cfg.get("worker_url", DEFAULT_WORKER_URL).rstrip("/")
    worker_admin_auth = cfg.get("worker_admin_auth", DEFAULT_WORKER_ADMIN_AUTH).strip()

    is_outlook = (mail_type == "outlook") or bool(refresh_token) or email.lower().endswith(("@outlook.com", "@hotmail.com", "@live.com", "@msn.com"))
    if is_outlook and not refresh_token:
        try:
            acc = database.get_account_by_email(email)
            if acc:
                client_id = client_id or acc.get("client_id")
                refresh_token = refresh_token or acc.get("refresh_token")
        except Exception:
            pass

    worker_jwt = "".join(str(worker_jwt or "").split())
    if not is_outlook and (not worker_jwt or len(worker_jwt) < 30):
        try:
            w_acc = database.get_worker_account_by_email(email)
            if w_acc and w_acc.get("jwt"):
                worker_jwt = "".join(str(w_acc["jwt"]).split())
        except Exception:
            pass

    if not is_outlook and (not worker_jwt or len(worker_jwt) < 30):
        try:
            acc = database.get_account_by_email(email)
            if acc and acc.get("worker_jwt"):
                worker_jwt = "".join(str(acc["worker_jwt"]).split())
        except Exception:
            pass

    if not is_outlook and (not worker_jwt or len(worker_jwt) < 30) and worker_url and worker_admin_auth:
        try:
            fresh_jwt = get_worker_jwt_for_email(worker_url, worker_admin_auth, email)
            if fresh_jwt:
                worker_jwt = "".join(str(fresh_jwt).split())
        except Exception:
            pass

    if not proxy:
        proxy = database.get_config("default_proxy")

    def _verify_worker_email_otp(session, cont_url: str, auth_referer: str, last_id: int, since_time: float = None) -> dict:
        """
        通过邮箱自动收码，向 /api/accounts/email-otp/validate 提交验证码完成真实状态鉴权:
        - 若返回 account_deactivated / user_blocked / 403: 确认为【已封禁】
        - 若返回 add_phone / add-phone: 确认为【待接码/未绑手机】
        - 若顺利跳转授权 (code= / chatgpt): 确认为【已过手机验证】
        """
        nonlocal worker_jwt
        otp_target_url = cont_url or "https://auth.openai.com/email-verification"
        code = None

        if is_outlook:
            if not refresh_token:
                emit("⚠️ 账号要求邮箱验证码 (Email OTP)，但未配置 Outlook refresh_token 无法深检", "warning")
                return {
                    "success": True,
                    "alive": True,
                    "check_status": "unknown",
                    "has_phone": 1 if known_phone else None,
                    "phone_number": known_phone,
                    "message": "账号存活 (要求邮箱验证码，缺少 Outlook 凭证无法深检)",
                    "time": time.strftime("%Y-%m-%d %H:%M:%S")
                }
            emit("📬 监听 Outlook 微软收件箱获取最新验证码...", "info")
            code = poll_email_code_unified(
                email=email,
                mail_type="outlook",
                client_id=client_id,
                refresh_token=refresh_token,
                timeout_sec=45,
                interval_sec=3,
                since_time=(since_time - 10) if since_time else None,
                log_fn=emit
            )
        else:
            if not worker_jwt:
                emit("⚠️ 账号要求邮箱验证码 (Email OTP)，但未找到 Worker 凭证无法深检", "warning")
                return {
                    "success": True,
                    "alive": True,
                    "check_status": "unknown",
                    "has_phone": 1 if known_phone else None,
                    "phone_number": known_phone,
                    "message": "账号存活 (要求邮箱验证码，缺少 Worker 凭证无法深检)",
                    "time": time.strftime("%Y-%m-%d %H:%M:%S")
                }

            try:
                session.get(
                    otp_target_url,
                    headers={"referer": auth_referer, "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"},
                    allow_redirects=True
                )
            except Exception:
                pass

            emit(f"📬 监听 Worker 收件箱获取最新验证码 (基准邮件 ID: {last_id})...", "info")
            mail_url = f"{worker_url}/api/mails?limit=5&offset=0"
            for i in range(12):
                time.sleep(2)
                if i == 5 and not code:
                    # 超过 10 秒未收信时，触发官方 resend 兜底
                    try:
                        session.post(
                            "https://auth.openai.com/api/accounts/email-otp/resend",
                            json={},
                            headers={"referer": otp_target_url, "origin": "https://auth.openai.com"}
                        )
                    except Exception:
                        pass
                try:
                    mail_req = urllib.request.Request(
                        mail_url,
                        headers={"Authorization": f"Bearer {worker_jwt}", "User-Agent": "Mozilla/5.0"}
                    )
                    with urllib.request.urlopen(mail_req, timeout=10) as r_mail:
                        m_data = json.loads(r_mail.read().decode("utf-8"))
                        for m in m_data.get("results", []):
                            m_id = int(m.get("id", 0))
                            if m_id > last_id:
                                raw = m.get("raw", "")
                                cand_code = extract_email_code(raw, email)
                                if cand_code:
                                    code = cand_code
                                    emit(f"📩 捕获到邮箱最新验证码 [{code}] (邮件 #{m_id})", "info")
                                    break
                except urllib.error.HTTPError as he:
                    if he.code == 401:
                        # 401 凭证失效或带非法空格，自动从后台管理 API 或 worker_accounts 获取最新凭证自愈
                        log.warning(f"Worker JWT 401 Unauthorized for {email}, re-fetching fresh JWT...")
                        try:
                            fresh_jwt = None
                            if worker_url and worker_admin_auth:
                                fresh_jwt = get_worker_jwt_for_email(worker_url, worker_admin_auth, email)
                            if not fresh_jwt:
                                w_acc = database.get_worker_account_by_email(email)
                                if w_acc and w_acc.get("jwt"):
                                    fresh_jwt = w_acc["jwt"]
                            if fresh_jwt:
                                worker_jwt = "".join(str(fresh_jwt).split())
                        except Exception:
                            pass
                except Exception as e:
                    log.warning(f"Mail poll exception: {e}")
                if code:
                    break

        if not code:
            emit("⚠️ 邮箱验证码接收超时，无法进一步判定是否封禁或需接手机码", "warning")
            return {
                "success": False,
                "alive": None,
                "check_status": "unknown",
                "has_phone": 1 if known_phone else None,
                "phone_number": known_phone,
                "message": "邮箱验证码接收超时，无法完成深检",
                "time": time.strftime("%Y-%m-%d %H:%M:%S")
            }

        emit(f"正在向 OpenAI 提交验证码 [{code}] 进行深度核验...", "info")
        val_resp = session.post(
            "https://auth.openai.com/api/accounts/email-otp/validate",
            json={"code": code},
            headers={
                "referer": otp_target_url,
                "origin": "https://auth.openai.com",
                "Content-Type": "application/json"
            },
            allow_redirects=False
        )
        val_text = val_resp.text or ""
        val_loc = val_resp.headers.get("Location") or val_resp.headers.get("location") or ""
        err_payload = parse_openai_error_payload(val_loc)
        err_code = (err_payload.get("errorCode", "") or _extract_openai_error_code(val_text, val_loc)).lower()
        err_kind = str(err_payload.get("kind", "")).lower()

        # 1. 明确的封禁 / 停用判定
        if (
            val_resp.status_code == 403
            or err_code in ("account_deactivated", "account_suspended")
            or "accountdeactivated" in err_kind
            or "account_deactivated" in val_text.lower()
            or "deactivated" in val_text.lower()
            or "deactivated" in val_loc.lower()
        ):
            emit("❌ 深度核验失败: 账号已被 OpenAI 停用封禁 (account_deactivated)", "error")
            return {
                "success": True,
                "alive": False,
                "check_status": "banned",
                "has_phone": None,
                "phone_number": "",
                "message": "账号已被 OpenAI 停用封禁 (Deactivated)",
                "time": time.strftime("%Y-%m-%d %H:%M:%S")
            }

        if err_code in ("user_blocked", "account_blocked") or "user_blocked" in val_text or "blocked" in val_loc.lower():
            emit("❌ 深度核验失败: 账号已被 OpenAI 屏蔽锁定 (user_blocked)", "error")
            return {
                "success": True,
                "alive": False,
                "check_status": "banned",
                "has_phone": None,
                "phone_number": "",
                "message": "账号已被 OpenAI 屏蔽锁定 (Blocked)",
                "time": time.strftime("%Y-%m-%d %H:%M:%S")
            }

        # 2. 手机绑定判定 (add_phone)
        val_data = val_resp.json() if val_text.startswith("{") else {}
        val_next_page = val_data.get("page", {}).get("type", "")
        val_next_cont = val_data.get("continue_url", "")

        if (
            val_next_page == "add_phone"
            or "add-phone" in val_loc.lower()
            or "add-phone" in (val_next_cont or "").lower()
            or "add_phone" in val_text.lower()
        ):
            emit("⚠️ 邮箱验证码通过，但服务端要求绑定手机号 (add_phone)，该账号未接手机码", "warning")
            return {
                "success": True,
                "alive": True,
                "check_status": "needs_phone",
                "has_phone": 0,
                "phone_number": "",
                "message": "账号存活 (邮箱验证通过，要求绑定手机)",
                "time": time.strftime("%Y-%m-%d %H:%M:%S")
            }

        # 3. 成功通过 (已过手机验证)
        if val_resp.status_code in (200, 201, 302, 303, 307) and ("error?" not in val_loc):
            p_str = f": {known_phone}" if known_phone else ""
            emit(f"🎉 探测通过: 邮箱验证成功，账号正常存活并已接手机码{p_str}", "success")
            return {
                "success": True,
                "alive": True,
                "check_status": "has_phone",
                "has_phone": 1,
                "phone_number": known_phone,
                "message": f"账号正常存活 (已过手机验证{p_str})",
                "time": time.strftime("%Y-%m-%d %H:%M:%S")
            }

        emit(f"❌ 邮箱验证码核验未明确: HTTP {val_resp.status_code} - {err_code or val_loc[:60]}", "error")
        return {
            "success": False,
            "alive": None,
            "check_status": "unknown",
            "has_phone": 1 if known_phone else None,
            "phone_number": known_phone,
            "message": f"邮箱验证码核验未通过 ({err_code or val_resp.status_code})",
            "time": time.strftime("%Y-%m-%d %H:%M:%S")
        }

    emit(f"🔍 启动 Worker 邮箱验活&手机绑定探测: {email}{f' (已知历史手机: {known_phone})' if known_phone else ''}...", "highlight")

    for attempt in range(2):
        check_started_at = time.time()
        try:
            curr_proxy = refresh_dynamic_proxy(proxy or "")
            session = tls_client.Session(client_identifier=TLS_CLIENT_ID, random_tls_extension_order=True)
            if curr_proxy and curr_proxy.lower() != "none":
                session.proxies = {"http": curr_proxy, "https": curr_proxy}
            session.headers.update({
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
                "sec-ch-ua": '"Not_A Brand";v="8", "Chromium";v="133", "Google Chrome";v="133"',
                "sec-ch-ua-mobile": "?0",
                "sec-ch-ua-platform": '"Windows"',
            })

            # 2. 访问官方 OAuth 授权入口
            params = {
                "client_id": CLIENT_ID,
                "response_type": "code",
                "redirect_uri": REDIRECT_URI,
                "scope": SCOPE,
                "state": _random_state(),
                "code_challenge": _pkce_challenge(_pkce_verifier()),
                "code_challenge_method": "S256",
                "prompt": "login",
                "id_token_add_organizations": "true",
                "codex_cli_simplified_flow": "true",
            }
            oauth_url = f"{AUTH_URL}?{urllib.parse.urlencode(params)}"
            auth_resp = session.get(oauth_url, headers={"referer": "https://auth.openai.com/"}, allow_redirects=True)
            if auth_resp.status_code not in (200, 302, 307):
                if attempt == 0:
                    time.sleep(1.2)
                    continue
                return {
                    "success": False,
                    "alive": None,
                    "check_status": "unknown",
                    "has_phone": 1 if known_phone else None,
                    "phone_number": known_phone,
                    "message": f"OAuth 入口请求失败 (HTTP {auth_resp.status_code})"
                }

            auth_referer = getattr(auth_resp, "url", "") or "https://auth.openai.com/"
            did = _extract_cookie_val(session, "oai-did") or str(secrets.token_hex(16))

            # 记录提交邮箱前的最新 Worker 邮件 ID，以便精准比对后续到达的验证码
            last_mail_id = 0
            if worker_jwt and worker_url:
                try:
                    last_mail_id = get_latest_worker_mail_id(worker_url, worker_jwt)
                except Exception:
                    pass

            # 3. 提交 Identifier (邮箱)
            sentinel = generate_sentinel_token(did=did, flow="authorize_continue", referer=auth_referer, proxy=curr_proxy)
            headers = {
                "referer": auth_referer,
                "origin": "https://auth.openai.com",
                "Content-Type": "application/json"
            }
            if sentinel:
                headers["openai-sentinel-token"] = sentinel

            id_resp = session.post(
                "https://auth.openai.com/api/accounts/authorize/continue",
                json={"username": {"kind": "email", "value": email}},
                headers=headers
            )
            text = id_resp.text or ""
            loc_id = id_resp.headers.get("Location") or id_resp.headers.get("location") or ""
            err_code_id = _extract_openai_error_code(text, loc_id)

            # 封禁停用判定
            if err_code_id in ("account_deactivated", "user_blocked", "account_blocked", "account_suspended") or "deactivated" in text.lower() or "blocked" in text.lower():
                emit(f"❌ 检测结果: 账号已被 OpenAI 停用封禁 ({err_code_id or 'deactivated'})", "error")
                return {
                    "success": True,
                    "alive": False,
                    "check_status": "banned",
                    "has_phone": None,
                    "phone_number": "",
                    "message": f"账号已被 OpenAI 停用封禁 ({err_code_id or 'Deactivated'})",
                    "time": time.strftime("%Y-%m-%d %H:%M:%S")
                }

            if "user_not_found" in text or err_code_id == "user_not_found":
                emit("❌ 检测结果: 账号在 OpenAI 不存在或已被注销", "error")
                return {
                    "success": True,
                    "alive": False,
                    "check_status": "unregistered",
                    "has_phone": None,
                    "phone_number": "",
                    "message": "账号在 OpenAI 未注册或不存在",
                    "time": time.strftime("%Y-%m-%d %H:%M:%S")
                }

            if id_resp.status_code in (200, 201):
                id_data = id_resp.json() or {}
                page_type = id_data.get("page", {}).get("type", "")
                cont_url = id_data.get("continue_url", "")

                # 手机验证探测: 若初筛直接要求 add_phone，明确为【未接手机码】
                if page_type == "add_phone" or "add-phone" in (cont_url or "").lower():
                    emit("⚠️ 检测结果: 账号正常存活，但尚未绑定手机号 (要求接码)", "warning")
                    return {
                        "success": True,
                        "alive": True,
                        "check_status": "needs_phone",
                        "has_phone": 0,
                        "phone_number": "",
                        "message": "账号存活 (未绑定手机号，要求接码)",
                        "time": time.strftime("%Y-%m-%d %H:%M:%S")
                    }

                # 密码验证步骤探测: 提交密码核验
                if page_type == "login_password":
                    pwd_url = cont_url or "https://auth.openai.com/log-in/password"
                    try:
                        session.get(pwd_url, headers={"referer": auth_referer}, allow_redirects=True)
                    except Exception:
                        pass

                    pwd_sentinel = generate_sentinel_token(did=did, flow="password_verify", referer=pwd_url, proxy=curr_proxy)
                    pwd_headers = {"referer": pwd_url, "origin": "https://auth.openai.com"}
                    if pwd_sentinel:
                        pwd_headers["openai-sentinel-token"] = pwd_sentinel

                    pwd_resp = session.post(
                        "https://auth.openai.com/api/accounts/password/verify",
                        json={"password": password},
                        headers=pwd_headers,
                        allow_redirects=False
                    )
                    loc = pwd_resp.headers.get("Location") or pwd_resp.headers.get("location") or ""
                    pwd_text = pwd_resp.text or ""
                    err_code_pwd = _extract_openai_error_code(pwd_text, loc)

                    if err_code_pwd in ("account_deactivated", "user_blocked", "account_blocked", "account_suspended") or "deactivated" in pwd_text.lower() or "blocked" in loc.lower():
                        emit(f"❌ 密码核验失败: 账号已被 OpenAI 停用封禁 ({err_code_pwd or 'deactivated'})", "error")
                        return {
                            "success": True,
                            "alive": False,
                            "check_status": "banned",
                            "has_phone": None,
                            "phone_number": "",
                            "message": f"账号已被 OpenAI 停用/封禁 ({err_code_pwd or 'Deactivated'})",
                            "time": time.strftime("%Y-%m-%d %H:%M:%S")
                        }

                    # 若密码错误或账号不存在: 401 invalid_username_or_password
                    if pwd_resp.status_code == 401 or err_code_pwd == "invalid_username_or_password" or "invalid_username_or_password" in pwd_text:
                        emit("❌ 密码核验失败: 账号未在 OpenAI 注册或密码不符 (401)", "error")
                        return {
                            "success": True,
                            "alive": False,
                            "check_status": "unregistered",
                            "has_phone": None,
                            "phone_number": "",
                            "message": "未注册或密码不符 (401 invalid_username_or_password)",
                            "time": time.strftime("%Y-%m-%d %H:%M:%S")
                        }

                    if pwd_resp.status_code in (200, 201):
                        pwd_data = pwd_resp.json() or {}
                        next_page = pwd_data.get("page", {}).get("type", "")
                        next_cont = pwd_data.get("continue_url", "")

                        if next_page == "add_phone" or "add-phone" in (next_cont or "").lower():
                            emit("⚠️ 密码通过后触发手机号验证，该账号未接手机码", "warning")
                            return {
                                "success": True,
                                "alive": True,
                                "check_status": "needs_phone",
                                "has_phone": 0,
                                "phone_number": "",
                                "message": "账号存活 (密码通过，要求绑定手机)",
                                "time": time.strftime("%Y-%m-%d %H:%M:%S")
                            }
                        elif next_page == "email_otp_verification" or "email-verification" in (next_cont or "").lower() or "email-otp" in (next_cont or "").lower():
                            emit("🔍 密码核验成功，账号要求邮箱验证码 (Email OTP)，启动 Worker 自动取码进行深度核验...", "info")
                            return _verify_worker_email_otp(session, next_cont or cont_url, auth_referer, last_mail_id, since_time=check_started_at)
                        elif "code=" in (next_cont or "") or "chatgpt.com" in (next_cont or ""):
                            p_str = f": {known_phone}" if known_phone else ""
                            emit(f"🎉 探测通过: 账号正常存活，已成功接手机码并授权{p_str}", "success")
                            return {
                                "success": True,
                                "alive": True,
                                "check_status": "has_phone",
                                "has_phone": 1,
                                "phone_number": known_phone,
                                "message": f"账号正常存活 (已过手机验证，授权成功{p_str})",
                                "time": time.strftime("%Y-%m-%d %H:%M:%S")
                            }
                        else:
                            emit(f"⚠️ 密码核验返回未明确状态页面: {next_page}", "warning")
                            return {
                                "success": True,
                                "alive": True,
                                "check_status": "has_phone" if known_phone else "available",
                                "has_phone": 1 if known_phone else None,
                                "phone_number": known_phone,
                                "message": f"账号存活 (密码核验通过: {next_page})",
                                "time": time.strftime("%Y-%m-%d %H:%M:%S")
                            }

                    if pwd_resp.status_code in (302, 303, 307):
                        if "add-phone" in loc.lower():
                            emit("⚠️ 密码通过后重定向至绑定手机，该账号未接手机码", "warning")
                            return {
                                "success": True,
                                "alive": True,
                                "check_status": "needs_phone",
                                "has_phone": 0,
                                "phone_number": "",
                                "message": "账号存活 (重定向至绑定手机)",
                                "time": time.strftime("%Y-%m-%d %H:%M:%S")
                            }
                        elif "code=" in loc or "chatgpt.com" in loc or "callback" in loc:
                            p_str = f": {known_phone}" if known_phone else ""
                            emit(f"🎉 探测通过: 账号正常存活，已成功接手机码{p_str}", "success")
                            return {
                                "success": True,
                                "alive": True,
                                "check_status": "has_phone",
                                "has_phone": 1,
                                "phone_number": known_phone,
                                "message": f"账号正常存活 (已过手机验证{p_str})",
                                "time": time.strftime("%Y-%m-%d %H:%M:%S")
                            }
                        elif "email-verification" in loc or "email-otp" in loc:
                            emit("🔍 密码核验重定向至邮箱验证码，启动 Worker 自动取码进行深度核验...", "info")
                            return _verify_worker_email_otp(session, loc, auth_referer, last_mail_id, since_time=check_started_at)
                        else:
                            emit(f"❌ 密码校验重定向至异常页面: {loc[:80]}", "error")
                            return {
                                "success": True,
                                "alive": False,
                                "check_status": "unregistered",
                                "has_phone": None,
                                "phone_number": "",
                                "message": f"未注册或登录失败 (重定向到: {loc[:50]})",
                                "time": time.strftime("%Y-%m-%d %H:%M:%S")
                            }

                    # 其余 HTTP 状态码（如 400 等）一律视为未通过
                    emit(f"❌ 密码校验返回异常: HTTP {pwd_resp.status_code}", "error")
                    return {
                        "success": True,
                        "alive": False,
                        "check_status": "unregistered",
                        "has_phone": 0,
                        "phone_number": "",
                        "message": f"账号在 OpenAI 鉴权失败 (HTTP {pwd_resp.status_code})",
                        "time": time.strftime("%Y-%m-%d %H:%M:%S")
                    }

                # 若初筛直接要求邮箱 OTP (如封禁账号或无密码存活账号)
                if page_type == "email_otp_verification" or "email-otp" in (cont_url or "").lower() or "email-verification" in (cont_url or "").lower():
                    emit("🔍 账号初筛要求邮箱验证码 (Email OTP)，启动 Worker 自动取码进行深度核验...", "info")
                    return _verify_worker_email_otp(session, cont_url, auth_referer, last_mail_id, since_time=check_started_at)

                # 兜底：未匹配到任何成功登录状态
                return {
                    "success": False,
                    "alive": None,
                    "check_status": "unknown",
                    "has_phone": None,
                    "phone_number": "",
                    "message": f"未知页面类型: {page_type}",
                    "time": time.strftime("%Y-%m-%d %H:%M:%S")
                }

            if "invalid_state" in text and attempt == 0:
                time.sleep(1)
                continue

            return {
                "success": False,
                "alive": None,
                "check_status": "unknown",
                "has_phone": 1 if known_phone else None,
                "phone_number": known_phone,
                "message": f"检测异常: HTTP {id_resp.status_code} - {text[:80]}",
                "time": time.strftime("%Y-%m-%d %H:%M:%S")
            }

        except Exception as e:
            if attempt == 1:
                return {
                    "success": False,
                    "alive": None,
                    "check_status": "unknown",
                    "has_phone": 1 if known_phone else None,
                    "phone_number": known_phone,
                    "message": f"网络/代理异常: {e}",
                    "time": time.strftime("%Y-%m-%d %H:%M:%S")
                }
            time.sleep(1)

    return {
        "success": False,
        "alive": None,
        "check_status": "unknown",
        "has_phone": None,
        "phone_number": "",
        "message": "检测超时，请稍后重试",
        "time": time.strftime("%Y-%m-%d %H:%M:%S")
    }
