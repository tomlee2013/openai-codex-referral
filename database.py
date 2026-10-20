import json
import os
import random
import re
import secrets
import sqlite3
import string
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "data" / "referral_system.db"
DEFAULT_ACCOUNT_PASSWORD = os.getenv("DEFAULT_ACCOUNT_PASSWORD", "ChangeMePassword123!@")


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with get_connection() as conn:
        cursor = conn.cursor()

        # 邮箱库存表
        cursor.execute(f"""
            CREATE TABLE IF NOT EXISTS accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT UNIQUE NOT NULL,
                password TEXT DEFAULT '{DEFAULT_ACCOUNT_PASSWORD}',
                worker_jwt TEXT DEFAULT '',
                client_id TEXT DEFAULT NULL,
                refresh_token TEXT DEFAULT NULL,
                mail_type TEXT DEFAULT 'worker',
                status TEXT DEFAULT 'available',
                assigned_card TEXT DEFAULT NULL,
                token_cache TEXT DEFAULT NULL,
                error_message TEXT DEFAULT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                completed_at TIMESTAMP DEFAULT NULL
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_accounts_status ON accounts(status)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_accounts_card ON accounts(assigned_card)")

        # 兼容性迁移: 增加 completed_at 字段
        try:
            cursor.execute("ALTER TABLE accounts ADD COLUMN completed_at TIMESTAMP DEFAULT NULL")
        except Exception:
            pass
        try:
            cursor.execute("UPDATE accounts SET completed_at = updated_at WHERE status = 'completed' AND completed_at IS NULL")
        except Exception:
            pass

        # 兼容性迁移: 增加 client_id 与 refresh_token 字段 (支持 Outlook 微软邮箱)
        try:
            cursor.execute("ALTER TABLE accounts ADD COLUMN client_id TEXT DEFAULT NULL")
        except Exception:
            pass
        try:
            cursor.execute("ALTER TABLE accounts ADD COLUMN refresh_token TEXT DEFAULT NULL")
        except Exception:
            pass

        # 兼容性迁移: 增加 accounts 验活与手机验证字段
        for col, col_def in [
            ("check_status", "TEXT DEFAULT NULL"),
            ("has_phone", "INTEGER DEFAULT NULL"),
            ("phone_number", "TEXT DEFAULT NULL"),
            ("checked_at", "TIMESTAMP DEFAULT NULL"),
        ]:
            try:
                cursor.execute(f"ALTER TABLE accounts ADD COLUMN {col} {col_def}")
            except Exception:
                pass

        # 卡密管理表
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS card_keys (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                card_key TEXT UNIQUE NOT NULL,
                status TEXT DEFAULT 'unused',
                assigned_email TEXT DEFAULT NULL,
                batch_note TEXT DEFAULT '',
                used_at TIMESTAMP DEFAULT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_card_status ON card_keys(status)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_card_key ON card_keys(card_key)")

        # Cloudflare Worker 邮箱源库
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS worker_accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                worker_id INTEGER,
                email TEXT UNIQUE NOT NULL,
                jwt TEXT NOT NULL,
                mail_count INTEGER DEFAULT 0,
                worker_created_at TEXT DEFAULT NULL,
                synced_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                check_status TEXT DEFAULT 'pending',
                has_phone INTEGER DEFAULT NULL,
                phone_number TEXT DEFAULT NULL,
                check_message TEXT DEFAULT NULL,
                checked_at TIMESTAMP DEFAULT NULL
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_worker_accounts_email ON worker_accounts(email)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_worker_accounts_worker_id ON worker_accounts(worker_id)")

        # 兼容性迁移: 增加 worker_accounts 验活与手机验证字段
        for col, col_def in [
            ("check_status", "TEXT DEFAULT 'pending'"),
            ("has_phone", "INTEGER DEFAULT NULL"),
            ("phone_number", "TEXT DEFAULT NULL"),
            ("check_message", "TEXT DEFAULT NULL"),
            ("checked_at", "TIMESTAMP DEFAULT NULL")
        ]:
            try:
                cursor.execute(f"ALTER TABLE worker_accounts ADD COLUMN {col} {col_def}")
            except Exception:
                pass

        cursor.execute("CREATE INDEX IF NOT EXISTS idx_worker_accounts_has_phone ON worker_accounts(has_phone)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_worker_accounts_check_status ON worker_accounts(check_status)")

        # 系统设置表
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS system_config (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)

        # 自动清洗历史脏数据中可能残留的空格、换行符
        try:
            cursor.execute("""
                UPDATE accounts 
                SET worker_jwt = REPLACE(REPLACE(REPLACE(REPLACE(worker_jwt, ' ', ''), char(9), ''), char(10), ''), char(13), '')
                WHERE worker_jwt IS NOT NULL AND worker_jwt != ''
            """)
            cursor.execute("""
                UPDATE worker_accounts 
                SET jwt = REPLACE(REPLACE(REPLACE(REPLACE(jwt, ' ', ''), char(9), ''), char(10), ''), char(13), '')
                WHERE jwt IS NOT NULL AND jwt != ''
            """)
        except Exception:
            pass

        # 写入默认设置 (优先读取环境变量，便于 Docker / 云原生部署)
        defaults = {
            "admin_password": os.getenv("ADMIN_PASSWORD", "admin123456"),
            "admin_path": os.getenv("ADMIN_PATH", "admin-secret-dashboard"),
            "worker_url": os.getenv("WORKER_URL", ""),
            "worker_admin_auth": os.getenv("WORKER_ADMIN_AUTH", ""),
            "worker_domain": os.getenv("WORKER_DOMAIN", ""),
            "default_proxy": os.getenv("DEFAULT_PROXY", ""),
            "sms_provider": os.getenv("SMS_PROVIDER", "5sim"),
            "sms_api_key": os.getenv("SMS_API_KEY", ""),
            "sms_country": os.getenv("SMS_COUNTRY", "vietnam"),
            "hero_sms_api_key": os.getenv("HERO_SMS_API_KEY", ""),
            "hero_sms_country": os.getenv("HERO_SMS_COUNTRY", "10"),
            "sms_online_api_key": os.getenv("SMS_ONLINE_API_KEY", ""),
            "sms_online_country": os.getenv("SMS_ONLINE_COUNTRY", "6"),
            "sms_online_max_price": os.getenv("SMS_ONLINE_MAX_PRICE", "0.06"),
        }
        for k, v in defaults.items():
            cursor.execute("INSERT OR IGNORE INTO system_config (key, value) VALUES (?, ?)", (k, v))

        conn.commit()


def get_config(key: str, default: str = "") -> str:
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT value FROM system_config WHERE key = ?", (key,))
        row = cursor.fetchone()
        return row["value"] if row else default


def set_config(key: str, value: str):
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO system_config (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """, (key, str(value)))
        conn.commit()


def get_all_config() -> dict:
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT key, value FROM system_config")
        return {row["key"]: row["value"] for row in cursor.fetchall()}


# ==================== 邮箱库存管理 ====================

def add_account(
    email: str,
    password: str = None,
    worker_jwt: str = "",
    client_id: str = "",
    refresh_token: str = "",
    mail_type: str = "worker"
) -> bool:
    email = email.strip()
    if not email or "@" not in email:
        return False
    password = (password or DEFAULT_ACCOUNT_PASSWORD).strip()
    worker_jwt = "".join(str(worker_jwt or "").split())
    refresh_token = "".join(str(refresh_token or "").split())
    client_id = str(client_id or "").strip()
    with get_connection() as conn:
        cursor = conn.cursor()
        try:
            cursor.execute("""
                INSERT INTO accounts (email, password, worker_jwt, client_id, refresh_token, mail_type, status)
                VALUES (?, ?, ?, ?, ?, ?, 'available')
                ON CONFLICT(email) DO UPDATE SET
                    password = excluded.password,
                    worker_jwt = CASE WHEN excluded.worker_jwt != '' THEN excluded.worker_jwt ELSE accounts.worker_jwt END,
                    client_id = CASE WHEN excluded.client_id != '' THEN excluded.client_id ELSE accounts.client_id END,
                    refresh_token = CASE WHEN excluded.refresh_token != '' THEN excluded.refresh_token ELSE accounts.refresh_token END,
                    mail_type = excluded.mail_type,
                    updated_at = CURRENT_TIMESTAMP
            """, (email, password, worker_jwt, client_id, refresh_token, mail_type))
            conn.commit()
            return True
        except Exception:
            return False


def batch_import_accounts(text: str) -> dict:
    """
    批量导入邮箱账号 (支持 Cloudflare Worker 邮箱与 Outlook 微软邮箱)
    支持格式:
    1. Outlook 微软邮箱: 邮箱----密码----client_id----refresh_token (兼容尾部追加状态与密码)
    2. Worker 域名邮箱: 邮箱----Worker_JWT 或 邮箱----密码----状态----Worker_JWT
    """
    lines = [l.strip() for l in text.strip().splitlines() if l.strip()]
    added = 0
    updated = 0
    errors = 0
    missing_tokens = 0
    outlook_count = 0
    worker_count = 0

    with get_connection() as conn:
        cursor = conn.cursor()
        for line in lines:
            try:
                # 过滤注释行
                if line.startswith("#") or line.startswith("//"):
                    continue

                parts = [p.strip() for p in re.split(r'-{2,}|\t|\|', line) if p.strip()]
                if not parts:
                    continue
                email = parts[0].strip()
                if "@" not in email:
                    errors += 1
                    continue

                email_lower = email.lower()
                is_outlook = False

                # 判断是否为 Outlook / 微软邮箱格式:
                # 1) 邮箱后缀为 outlook.com / hotmail.com / live.com / msn.com
                # 2) 或者包含 4 段以上且第 3 段形如 UUID，或第 4 段为 M. 开头 / 含 MsaArtifacts 的长 token
                if len(parts) >= 4:
                    p2, p3 = parts[2], parts[3]
                    if (len(p2) >= 30 and '-' in p2) or p3.startswith('M.') or 'Msa' in p3:
                        is_outlook = True
                    elif email_lower.endswith(('@outlook.com', '@hotmail.com', '@live.com', '@msn.com')):
                        is_outlook = True
                elif email_lower.endswith(('@outlook.com', '@hotmail.com', '@live.com', '@msn.com')) and len(parts) >= 3:
                    is_outlook = True

                if is_outlook:
                    mail_type = "outlook"
                    password = parts[1] if len(parts) > 1 else DEFAULT_ACCOUNT_PASSWORD
                    client_id = parts[2] if len(parts) > 2 and parts[2] else "9e5f94bc-e8a4-4e73-b8be-63364c29d753"
                    refresh_token = parts[3] if len(parts) > 3 else ""

                    # 若某格式 client_id 留空而在后续字段中出现 refresh_token
                    if not refresh_token:
                        for p in parts[1:]:
                            if p.startswith("M.") or "MsaArtifacts" in p:
                                refresh_token = p
                                break

                    if not refresh_token:
                        cursor.execute("SELECT refresh_token FROM accounts WHERE email = ?", (email,))
                        row = cursor.fetchone()
                        if row and row["refresh_token"]:
                            refresh_token = row["refresh_token"]
                        else:
                            missing_tokens += 1
                            errors += 1
                            continue

                    cursor.execute("SELECT id FROM accounts WHERE email = ?", (email,))
                    exists = cursor.fetchone()

                    cursor.execute("""
                        INSERT INTO accounts (email, password, worker_jwt, client_id, refresh_token, mail_type, status)
                        VALUES (?, ?, '', ?, ?, 'outlook', 'available')
                        ON CONFLICT(email) DO UPDATE SET
                            password = excluded.password,
                            client_id = excluded.client_id,
                            refresh_token = excluded.refresh_token,
                            mail_type = 'outlook',
                            updated_at = CURRENT_TIMESTAMP
                    """, (email, password, client_id, refresh_token))

                    if exists:
                        updated += 1
                    else:
                        added += 1
                    outlook_count += 1

                else:
                    mail_type = "worker"
                    jwt_val = ""
                    # 识别后续字段中的 JWT 凭证 (通常以 eyJ 开头且长度大于 30)
                    for p in parts[1:]:
                        if p.startswith("eyJ") and len(p) > 30:
                            jwt_val = p
                            break

                    # 兜底：若第 2 个字段看起来像自定义 token (长度大于 25 且非状态词)
                    if not jwt_val and len(parts) >= 2:
                        p1 = parts[1].strip()
                        if len(p1) > 25 and p1.lower() not in ("available", "completed", "banned", "used", "failed", "worker", "uploaded"):
                            jwt_val = p1

                    if jwt_val:
                        jwt_val = "".join(str(jwt_val).split())

                    # 严格校验：收取邮件验证码必须依赖 Worker JWT
                    if not jwt_val:
                        cursor.execute("SELECT worker_jwt FROM accounts WHERE email = ?", (email,))
                        row = cursor.fetchone()
                        if row and row["worker_jwt"]:
                            jwt_val = "".join(str(row["worker_jwt"]).split())
                        else:
                            missing_tokens += 1
                            errors += 1
                            continue

                    cursor.execute("SELECT id FROM accounts WHERE email = ?", (email,))
                    exists = cursor.fetchone()

                    cursor.execute(f"""
                        INSERT INTO accounts (email, password, worker_jwt, client_id, refresh_token, mail_type, status)
                        VALUES (?, '{DEFAULT_ACCOUNT_PASSWORD}', ?, '', '', 'worker', 'available')
                        ON CONFLICT(email) DO UPDATE SET
                            worker_jwt = excluded.worker_jwt,
                            mail_type = 'worker',
                            updated_at = CURRENT_TIMESTAMP
                    """, (email, jwt_val))

                    if exists:
                        updated += 1
                    else:
                        added += 1
                    worker_count += 1

            except Exception:
                errors += 1
        conn.commit()

    return {
        "total": len(lines),
        "added": added,
        "updated": updated,
        "errors": errors,
        "missing_jwt": missing_tokens,
        "missing_tokens": missing_tokens,
        "outlook_count": outlook_count,
        "worker_count": worker_count
    }


def get_account_by_email(email: str) -> Optional[dict]:
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM accounts WHERE email = ?", (email.strip(),))
        row = cursor.fetchone()
        return dict(row) if row else None


def get_account_by_id(account_id: int) -> Optional[dict]:
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM accounts WHERE id = ?", (int(account_id),))
        row = cursor.fetchone()
        return dict(row) if row else None


def update_account_status(email: str, status: str, token_cache: str = None, error_message: str = None):
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE accounts SET
                status = ?,
                token_cache = COALESCE(?, token_cache),
                error_message = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE email = ?
        """, (status, token_cache, error_message, email.strip()))
        conn.commit()


def update_account_check_result(
    account_id: int,
    status: str,
    message: str,
    token: str = None,
    check_status: str = None,
    has_phone: int = None,
    phone_number: str = None
) -> bool:
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT email, status, assigned_card FROM accounts WHERE id = ?", (int(account_id),))
        acc_row = cursor.fetchone()
        email = acc_row["email"] if acc_row else ""
        curr_st = acc_row["status"] if acc_row else status

        if status in ("banned", "failed"):
            if acc_row and acc_row["assigned_card"]:
                card_key = acc_row["assigned_card"]
                cursor.execute("UPDATE card_keys SET assigned_email = NULL, status = 'unused' WHERE card_key = ? AND status = 'bound'", (card_key,))
                cursor.execute("UPDATE accounts SET assigned_card = NULL WHERE id = ?", (int(account_id),))

        # 若原本账号已经是核销 completed 状态，且检测出存活，则保留 completed 状态，避免将冷冻账号误刷为 available
        final_status = status
        if curr_st == "completed" and status == "available":
            final_status = "completed"

        cursor.execute("""
            UPDATE accounts SET
                status = ?,
                check_status = COALESCE(?, check_status),
                has_phone = COALESCE(?, has_phone),
                phone_number = COALESCE(?, phone_number),
                error_message = ?,
                token_cache = COALESCE(?, token_cache),
                checked_at = CURRENT_TIMESTAMP,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
        """, (final_status, check_status, has_phone, phone_number, message, token, int(account_id)))

        # 若该邮箱在 worker_accounts 中存在，联动更新 worker_accounts
        if email:
            cursor.execute("""
                UPDATE worker_accounts SET
                    check_status = COALESCE(?, check_status),
                    has_phone = COALESCE(?, has_phone),
                    phone_number = COALESCE(?, phone_number),
                    check_message = ?,
                    checked_at = CURRENT_TIMESTAMP
                WHERE email = ?
            """, (check_status, has_phone, phone_number, message, email))

        conn.commit()
        return cursor.rowcount > 0


def enrich_account_cooldown_info(acc: dict) -> dict:
    """计算已核销账号的 60 天 (两个月) 冷冻期与解冻状态"""
    completed_at_str = acc.get("completed_at")
    if not completed_at_str and acc.get("status") == "completed":
        completed_at_str = acc.get("updated_at")
        acc["completed_at"] = completed_at_str

    if acc.get("status") == "completed" and completed_at_str:
        try:
            clean_ts = str(completed_at_str).replace("T", " ")
            dt_completed = datetime.strptime(clean_ts[:19], "%Y-%m-%d %H:%M:%S")
            dt_unfreeze = dt_completed + timedelta(days=60)
            now = datetime.now()
            diff_sec = (dt_unfreeze - now).total_seconds()
            days_left = diff_sec / 86400.0

            acc["cooldown_until"] = dt_unfreeze.strftime("%Y-%m-%d %H:%M:%S")
            if days_left > 0:
                acc["cooldown_days_left"] = max(1, int(days_left + 0.5))
                acc["is_cooling"] = True
                acc["can_unfreeze"] = False
            else:
                acc["cooldown_days_left"] = 0
                acc["is_cooling"] = False
                acc["can_unfreeze"] = True
        except Exception:
            acc["cooldown_days_left"] = 0
            acc["is_cooling"] = False
            acc["can_unfreeze"] = True
    else:
        acc["cooldown_days_left"] = 0
        acc["is_cooling"] = False
        acc["can_unfreeze"] = False
    return acc


def list_accounts(status: str = None, search: str = None, page: int = 1, page_size: int = 50) -> dict:
    offset = (max(page, 1) - 1) * page_size
    query = "SELECT * FROM accounts WHERE 1=1"
    count_query = "SELECT COUNT(*) as total FROM accounts WHERE 1=1"
    params = []

    if status and status != "all":
        if status == "cooling":
            cond = " AND status = 'completed' AND (julianday('now') - julianday(COALESCE(completed_at, updated_at)) < 60)"
            query += cond
            count_query += cond
        elif status in ("ready_to_unfreeze", "thawed"):
            cond = " AND status = 'completed' AND (julianday('now') - julianday(COALESCE(completed_at, updated_at)) >= 60)"
            query += cond
            count_query += cond
        elif status == "has_phone":
            cond = " AND (has_phone = 1 OR check_status = 'has_phone')"
            query += cond
            count_query += cond
        elif status == "needs_phone":
            cond = " AND (has_phone = 0 OR check_status = 'needs_phone')"
            query += cond
            count_query += cond
        elif status == "unchecked":
            cond = " AND (has_phone IS NULL AND (check_status IS NULL OR check_status = '' OR check_status = 'pending'))"
            query += cond
            count_query += cond
        elif status == "outlook":
            cond = " AND mail_type = 'outlook'"
            query += cond
            count_query += cond
        elif status == "worker":
            cond = " AND mail_type = 'worker'"
            query += cond
            count_query += cond
        else:
            query += " AND status = ?"
            count_query += " AND status = ?"
            params.append(status)

    if search:
        query += " AND (email LIKE ? OR assigned_card LIKE ? OR mail_type LIKE ? OR phone_number LIKE ?)"
        count_query += " AND (email LIKE ? OR assigned_card LIKE ? OR mail_type LIKE ? OR phone_number LIKE ?)"
        term = f"%{search.strip()}%"
        params.extend([term, term, term, term])

    query += " ORDER BY id DESC LIMIT ? OFFSET ?"
    query_params = list(params) + [page_size, offset]

    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(count_query, params)
        total = cursor.fetchone()["total"]

        cursor.execute(query, query_params)
        raw_items = [dict(row) for row in cursor.fetchall()]

    items = [enrich_account_cooldown_info(item) for item in raw_items]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


def reset_account(account_id: int) -> bool:
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE accounts SET
                status = 'available',
                assigned_card = NULL,
                error_message = NULL,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
        """, (account_id,))
        conn.commit()
        return cursor.rowcount > 0


def unfreeze_account(account_id: int) -> bool:
    """将已核销账号解冻并重新入库为可用状态 (available)，清除旧卡密绑定与令牌缓存"""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE accounts SET
                status = 'available',
                assigned_card = NULL,
                token_cache = NULL,
                error_message = NULL,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ? AND status = 'completed'
        """, (int(account_id),))
        conn.commit()
        return cursor.rowcount > 0


def unfreeze_eligible_accounts(days: int = 60) -> int:
    """自动将所有核销已满 60 天 (两个月) 的冷冻账号批量解冻为可用 (available)"""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(f"""
            UPDATE accounts SET
                status = 'available',
                assigned_card = NULL,
                token_cache = NULL,
                error_message = NULL,
                updated_at = CURRENT_TIMESTAMP
            WHERE status = 'completed'
              AND (
                  (completed_at IS NOT NULL AND (julianday('now') - julianday(completed_at) >= {days}))
                  OR (completed_at IS NULL AND updated_at IS NOT NULL AND (julianday('now') - julianday(updated_at) >= {days}))
              )
        """)
        conn.commit()
        return cursor.rowcount


def delete_account(account_id: int) -> bool:
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM accounts WHERE id = ?", (account_id,))
        conn.commit()
        return cursor.rowcount > 0


def batch_delete_accounts(account_ids: List[int]) -> int:
    if not account_ids:
        return 0
    clean_ids = [int(i) for i in account_ids if str(i).isdigit() or isinstance(i, int)]
    if not clean_ids:
        return 0
    placeholders = ",".join("?" for _ in clean_ids)
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(f"DELETE FROM accounts WHERE id IN ({placeholders})", clean_ids)
        conn.commit()
        return cursor.rowcount


def batch_reset_accounts(account_ids: List[int]) -> int:
    if not account_ids:
        return 0
    clean_ids = [int(i) for i in account_ids if str(i).isdigit() or isinstance(i, int)]
    if not clean_ids:
        return 0
    placeholders = ",".join("?" for _ in clean_ids)
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(f"""
            UPDATE accounts SET
                status = 'available',
                assigned_card = NULL,
                error_message = NULL,
                updated_at = CURRENT_TIMESTAMP
            WHERE id IN ({placeholders})
        """, clean_ids)
        conn.commit()
        return cursor.rowcount


# ==================== 卡密管理 ====================

def generate_random_card_code(prefix: str = "CDX") -> str:
    def chunk(length=4):
        return "".join(random.choices(string.ascii_uppercase + string.digits, k=length))
    return f"{prefix}-{chunk()}-{chunk()}-{chunk()}"


def generate_card_keys(count: int = 10, note: str = "", prefix: str = "CDX") -> List[str]:
    keys = []
    with get_connection() as conn:
        cursor = conn.cursor()
        for _ in range(count):
            card_key = generate_random_card_code(prefix)
            try:
                cursor.execute("""
                    INSERT INTO card_keys (card_key, status, batch_note)
                    VALUES (?, 'unused', ?)
                """, (card_key, note))
                keys.append(card_key)
            except sqlite3.IntegrityError:
                # 极小概率重复时重试一次
                card_key = generate_random_card_code(prefix)
                cursor.execute("""
                    INSERT INTO card_keys (card_key, status, batch_note)
                    VALUES (?, 'unused', ?)
                """, (card_key, note))
                keys.append(card_key)
        conn.commit()
    return keys


def list_cards(status: str = None, search: str = None, page: int = 1, page_size: int = 50) -> dict:
    offset = (max(page, 1) - 1) * page_size
    query = "SELECT * FROM card_keys WHERE 1=1"
    count_query = "SELECT COUNT(*) as total FROM card_keys WHERE 1=1"
    params = []

    if status and status != "all":
        query += " AND status = ?"
        count_query += " AND status = ?"
        params.append(status)

    if search:
        query += " AND (card_key LIKE ? OR assigned_email LIKE ? OR batch_note LIKE ?)"
        count_query += " AND (card_key LIKE ? OR assigned_email LIKE ? OR batch_note LIKE ?)"
        term = f"%{search.strip()}%"
        params.extend([term, term, term])

    query += " ORDER BY id DESC LIMIT ? OFFSET ?"
    query_params = list(params) + [page_size, offset]

    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(count_query, params)
        total = cursor.fetchone()["total"]

        cursor.execute(query, query_params)
        items = [dict(row) for row in cursor.fetchall()]

    return {"items": items, "total": total, "page": page, "page_size": page_size}


def disable_card(card_id: int) -> bool:
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE card_keys SET status = 'disabled' WHERE id = ?", (card_id,))
        conn.commit()
        return cursor.rowcount > 0


def delete_card(card_id: int) -> bool:
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM card_keys WHERE id = ?", (card_id,))
        conn.commit()
        return cursor.rowcount > 0


def batch_delete_cards(card_ids: List[int]) -> int:
    if not card_ids:
        return 0
    clean_ids = [int(i) for i in card_ids if str(i).isdigit() or isinstance(i, int)]
    if not clean_ids:
        return 0
    placeholders = ",".join("?" for _ in clean_ids)
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(f"DELETE FROM card_keys WHERE id IN ({placeholders})", clean_ids)
        conn.commit()
        return cursor.rowcount


def batch_disable_cards(card_ids: List[int]) -> int:
    if not card_ids:
        return 0
    clean_ids = [int(i) for i in card_ids if str(i).isdigit() or isinstance(i, int)]
    if not clean_ids:
        return 0
    placeholders = ",".join("?" for _ in clean_ids)
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(f"UPDATE card_keys SET status = 'disabled' WHERE id IN ({placeholders})", clean_ids)
        conn.commit()
        return cursor.rowcount


# ==================== 卡密兑换与核销闭环 ====================

def redeem_card(card_key: str) -> Tuple[bool, Optional[str], str]:
    """
    用户使用卡密兑换被邀请邮箱
    返回值: (success, email, message)
    """
    card_key = card_key.strip()
    if not card_key:
        return False, None, "请输入有效的卡密"

    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM card_keys WHERE card_key = ?", (card_key,))
        card = cursor.fetchone()

        if not card:
            return False, None, "卡密不存在，请核对后重试"

        card_status = card["status"]
        if card_status == "disabled":
            return False, None, "该卡密已被管理员作废"
        if card_status == "used":
            assigned = card["assigned_email"]
            return False, assigned, "该卡密已成功完成核销，不能重复使用"

        # 如果卡密之前已兑换过邮箱 (bound 状态)，支持断点续做
        if card_status == "bound" and card["assigned_email"]:
            cursor.execute("SELECT status FROM accounts WHERE email = ?", (card["assigned_email"],))
            bound_acc = cursor.fetchone()
            if bound_acc and bound_acc["status"] in ("banned", "failed"):
                # 原绑定的邮箱已被封禁或失效，自动解除并重新分配新的存活邮箱
                cursor.execute("UPDATE card_keys SET assigned_email = NULL, status = 'unused' WHERE card_key = ?", (card_key,))
                card_status = "unused"
            else:
                return True, card["assigned_email"], "已恢复该卡密绑定的被邀请邮箱（断点续做）"

        # 全新未使用卡密 (unused): 先尝试解冻已满 60 天 (两个月) 的冷冻账号自动扩充库存
        try:
            unfreeze_eligible_accounts(60)
        except Exception:
            pass

        # 原子分配一个空闲可用邮箱
        cursor.execute("""
            SELECT * FROM accounts
            WHERE status = 'available'
            ORDER BY id ASC LIMIT 1
        """)
        account = cursor.fetchone()

        if not account:
            return False, None, "当前库存不足，暂无可用的被邀请邮箱，请联系管理员补仓"

        email = account["email"]

        # 事务性绑定
        cursor.execute("""
            UPDATE accounts SET
                status = 'allocated',
                assigned_card = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
        """, (card_key, account["id"]))

        cursor.execute("""
            UPDATE card_keys SET
                status = 'bound',
                assigned_email = ?
            WHERE card_key = ?
        """, (email, card_key))

        conn.commit()
        return True, email, "卡密兑换成功！已为您分配专属被邀请邮箱"


def complete_activation(card_key: str, email: str, token_cache: str = None) -> bool:
    """激活核销成功后，标记卡密已使用、邮箱已完成并记录核销成功时间进入 60 天冷冻期"""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE card_keys SET
                status = 'used',
                used_at = CURRENT_TIMESTAMP
            WHERE card_key = ?
        """, (card_key.strip(),))

        cursor.execute("""
            UPDATE accounts SET
                status = 'completed',
                token_cache = COALESCE(?, token_cache),
                error_message = NULL,
                completed_at = CURRENT_TIMESTAMP,
                updated_at = CURRENT_TIMESTAMP
            WHERE email = ?
        """, (token_cache, email.strip()))

        conn.commit()
        return True


def query_card_progress(keyword: str) -> Optional[dict]:
    """用户通过卡密 (CDK) 或被邀请邮箱查询核销进度"""
    keyword = keyword.strip()
    if not keyword:
        return None

    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT c.*, a.status as account_status, a.updated_at as account_updated_at
            FROM card_keys c
            LEFT JOIN accounts a ON c.assigned_email = a.email
            WHERE c.card_key = ? OR c.assigned_email = ?
            ORDER BY c.id DESC LIMIT 1
        """, (keyword, keyword))
        row = cursor.fetchone()
        if row:
            res = dict(row)
            res["found"] = True
            return res

        cursor.execute("SELECT * FROM accounts WHERE email = ?", (keyword,))
        acc = cursor.fetchone()
        if acc:
            card_k = acc["assigned_card"]
            card_st = "unknown"
            used_at = None
            if card_k:
                cursor.execute("SELECT status, used_at FROM card_keys WHERE card_key = ?", (card_k,))
                c_row = cursor.fetchone()
                if c_row:
                    card_st = c_row["status"]
                    used_at = c_row["used_at"]

            return {
                "found": True,
                "card_key": card_k or "未绑定卡密",
                "status": card_st,
                "assigned_email": acc["email"],
                "account_status": acc["status"],
                "used_at": used_at,
                "created_at": acc["created_at"],
                "account_updated_at": acc["updated_at"]
            }

        return None


# ==================== 仪表盘数据统计 ====================

def get_dashboard_stats() -> dict:
    with get_connection() as conn:
        cursor = conn.cursor()

        # 邮箱统计
        cursor.execute("SELECT COUNT(*) as total FROM accounts")
        total_accounts = cursor.fetchone()["total"]

        cursor.execute("SELECT COUNT(*) as available FROM accounts WHERE status = 'available'")
        available_accounts = cursor.fetchone()["available"]

        cursor.execute("SELECT COUNT(*) as allocated FROM accounts WHERE status = 'allocated'")
        allocated_accounts = cursor.fetchone()["allocated"]

        cursor.execute("SELECT COUNT(*) as completed FROM accounts WHERE status = 'completed'")
        completed_accounts = cursor.fetchone()["completed"]

        cursor.execute("""
            SELECT COUNT(*) as cooling FROM accounts 
            WHERE status = 'completed' AND (julianday('now') - julianday(COALESCE(completed_at, updated_at)) < 60)
        """)
        cooling_accounts = cursor.fetchone()["cooling"]

        cursor.execute("""
            SELECT COUNT(*) as ready_to_unfreeze FROM accounts 
            WHERE status = 'completed' AND (julianday('now') - julianday(COALESCE(completed_at, updated_at)) >= 60)
        """)
        ready_to_unfreeze_accounts = cursor.fetchone()["ready_to_unfreeze"]

        cursor.execute("SELECT COUNT(*) as outlook FROM accounts WHERE mail_type = 'outlook'")
        outlook_accounts = cursor.fetchone()["outlook"]

        cursor.execute("SELECT COUNT(*) as worker FROM accounts WHERE mail_type = 'worker'")
        worker_accounts = cursor.fetchone()["worker"]

        cursor.execute("SELECT COUNT(*) as worker_pool FROM worker_accounts")
        worker_pool_total = cursor.fetchone()["worker_pool"]

        # 卡密统计
        cursor.execute("SELECT COUNT(*) as total FROM card_keys")
        total_cards = cursor.fetchone()["total"]

        cursor.execute("SELECT COUNT(*) as unused FROM card_keys WHERE status = 'unused'")
        unused_cards = cursor.fetchone()["unused"]

        cursor.execute("SELECT COUNT(*) as used FROM card_keys WHERE status = 'used'")
        used_cards = cursor.fetchone()["used"]

        cursor.execute("SELECT COUNT(*) as bound FROM card_keys WHERE status = 'bound'")
        bound_cards = cursor.fetchone()["bound"]

        return {
            "accounts": {
                "total": total_accounts,
                "available": available_accounts,
                "allocated": allocated_accounts,
                "completed": completed_accounts,
                "cooling": cooling_accounts,
                "ready_to_unfreeze": ready_to_unfreeze_accounts,
                "outlook": outlook_accounts,
                "worker": worker_accounts,
                "worker_pool_total": worker_pool_total,
            },
            "cards": {
                "total": total_cards,
                "unused": unused_cards,
                "used": used_cards,
                "bound": bound_cards,
            }
        }


def get_live_completed_stats() -> dict:
    """获取公开的实时核销数据统计与最近完成记录 (用于前端信任看板与跑马灯播报)"""
    with get_connection() as conn:
        cursor = conn.cursor()

        # 统计近 2 小时完成记录 (数据库记录以 YYYY-MM-DD HH:MM:SS 格式存储)
        two_hours_ago = (datetime.now() - timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")
        cursor.execute("SELECT COUNT(*) as c FROM card_keys WHERE status = 'used' AND used_at >= ?", (two_hours_ago,))
        row_2h = cursor.fetchone()
        real_2h_count = row_2h["c"] if row_2h else 0

        # 查询真实最近完成的卡密 (按 used_at 降序，最多 15 条)
        cursor.execute("""
            SELECT card_key, assigned_email, used_at 
            FROM card_keys 
            WHERE status = 'used' AND used_at IS NOT NULL 
            ORDER BY used_at DESC 
            LIMIT 15
        """)
        rows = cursor.fetchall()
        recent_items = []
        now = datetime.now()

        for r in rows:
            ck = r["card_key"]
            email = r["assigned_email"] or ""
            used_at_str = r["used_at"]

            # 脱敏处理: CDX-V60T-1XXD-FZJN -> CDX-V60T-****
            masked_card = ck[:8] + "-****" if len(ck) >= 8 else ck[:4] + "****"

            # 邮箱脱敏示例: username@example.com -> use***@example.com
            if "@" in email:
                u_part, d_part = email.split("@", 1)
                masked_email = (u_part[:3] if len(u_part) >= 3 else u_part) + "***@" + d_part
            else:
                masked_email = "***"

            # 计算相对时间
            time_ago = "刚刚"
            try:
                dt = datetime.strptime(used_at_str, "%Y-%m-%d %H:%M:%S")
                diff_sec = int((now - dt).total_seconds())
                if diff_sec < 60:
                    time_ago = "刚刚"
                elif diff_sec < 3600:
                    time_ago = f"{diff_sec // 60}分钟前"
                elif diff_sec < 86400:
                    time_ago = f"{diff_sec // 3600}小时前"
                else:
                    time_ago = f"{diff_sec // 86400}天前"
            except Exception:
                time_ago = "近期"

            recent_items.append({
                "card_key": masked_card,
                "email": masked_email,
                "time_ago": time_ago,
                "credits": "500 Credits",
                "status_text": "核销成功"
            })

        # 若真实记录不足 8 条，融合近期逼真种子轮播数据，确保跑马灯丰富流畅
        if len(recent_items) < 8:
            seed_templates = [
                {"card_prefix": "CDX-K82W", "email": "kpx***@example.com", "min_offset": 3},
                {"card_prefix": "CDX-9MA2", "email": "ytr***@example.com", "min_offset": 8},
                {"card_prefix": "CDX-37FD", "email": "wlo***@example.com", "min_offset": 14},
                {"card_prefix": "CDX-89QL", "email": "asq***@example.com", "min_offset": 22},
                {"card_prefix": "CDX-52NB", "email": "vbn***@example.com", "min_offset": 35},
                {"card_prefix": "CDX-11XY", "email": "mko***@example.com", "min_offset": 49},
                {"card_prefix": "CDX-70RA", "email": "plk***@example.com", "min_offset": 68},
                {"card_prefix": "CDX-46ZT", "email": "ujm***@example.com", "min_offset": 85},
            ]
            for s in seed_templates:
                if len(recent_items) >= 8:
                    break
                recent_items.append({
                    "card_key": f"{s['card_prefix']}-****",
                    "email": s["email"],
                    "time_ago": f"{s['min_offset']}分钟前",
                    "credits": "500 Credits",
                    "status_text": "核销成功"
                })

        try:
            base_offset = int(get_config("live_completed_base", "16"))
        except Exception:
            base_offset = 16

        return {
            "completed_2h": real_2h_count + base_offset,
            "recent_items": recent_items
        }


# =============================================================================
# Cloudflare Worker 邮箱源库管理
# =============================================================================

def get_known_worker_emails_with_jwt() -> Dict[str, str]:
    """获取本地已缓存且包含有效 JWT 的 Worker 邮箱字典"""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT email, jwt FROM worker_accounts WHERE jwt IS NOT NULL AND length(jwt) > 10")
        return {row["email"]: row["jwt"] for row in cursor.fetchall()}


def save_worker_accounts_batch(records: List[dict]) -> dict:
    """批量增量保存/更新 Worker 邮箱记录"""
    if not records:
        return {"added": 0, "updated": 0, "total": 0}

    added = 0
    updated = 0
    with get_connection() as conn:
        cursor = conn.cursor()
        for r in records:
            email = r.get("email", "").strip()
            if not email:
                continue
            worker_id = r.get("worker_id")
            jwt = r.get("jwt", "")
            mail_count = r.get("mail_count", 0)
            worker_created_at = r.get("worker_created_at")

            cursor.execute("SELECT id, jwt FROM worker_accounts WHERE email = ?", (email,))
            row = cursor.fetchone()
            if row:
                final_jwt = jwt if jwt else row["jwt"]
                cursor.execute("""
                    UPDATE worker_accounts SET
                        worker_id = COALESCE(?, worker_id),
                        jwt = ?,
                        mail_count = ?,
                        worker_created_at = COALESCE(?, worker_created_at),
                        synced_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                """, (worker_id, final_jwt, mail_count, worker_created_at, row["id"]))
                updated += 1
            else:
                cursor.execute("""
                    INSERT INTO worker_accounts (worker_id, email, jwt, mail_count, worker_created_at, synced_at)
                    VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                """, (worker_id, email, jwt, mail_count, worker_created_at))
                added += 1
        conn.commit()
    return {"added": added, "updated": updated, "total": len(records)}


def get_known_phone_mappings() -> Dict[str, str]:
    """
    从本地 accounts_phone.txt (或 WSL /opt/gptreg/accounts_phone.txt) 提取已知的手机号绑定映射:
    返回 {email: phone_number}
    格式: 手机号----密码----状态----JWT----邮箱
    """
    candidates = [
        BASE_DIR / "accounts_phone.txt",
        Path("/opt/gptreg/accounts_phone.txt"),
        Path("/opt/codex-referral/accounts_phone.txt")
    ]
    mapping = {}
    for p in candidates:
        if p.is_file():
            try:
                with open(p, "r", encoding="utf-8", errors="ignore") as f:
                    for line in f:
                        line = line.strip()
                        if not line or line.startswith("#"):
                            continue
                        parts = [x.strip() for x in line.split("----") if x.strip()]
                        if len(parts) >= 5 and "@" in parts[4]:
                            phone = parts[0]
                            email = parts[4].lower()
                            mapping[email] = phone
                        else:
                            phone_cand = ""
                            email_cand = ""
                            for seg in parts:
                                if seg.startswith("+") and seg[1:].isdigit() and len(seg) >= 8:
                                    phone_cand = seg
                                elif "@" in seg and "." in seg and not seg.startswith("+"):
                                    email_cand = seg.lower()
                            if phone_cand and email_cand:
                                mapping[email_cand] = phone_cand
            except Exception:
                pass
    return mapping


def sync_known_phone_cache_to_db() -> dict:
    """将 accounts_phone.txt 中已知的手机号绑定数据一键同步至 worker_accounts 表"""
    mapping = get_known_phone_mappings()
    if not mapping:
        return {"success": True, "count": 0, "message": "未扫描到已知手机号映射数据"}

    updated = 0
    with get_connection() as conn:
        cursor = conn.cursor()
        for email, phone in mapping.items():
            cursor.execute("""
                UPDATE worker_accounts SET
                    has_phone = 1,
                    phone_number = ?,
                    check_status = CASE WHEN check_status = 'banned' THEN 'banned' ELSE 'has_phone' END,
                    check_message = COALESCE(check_message, '历史已知已接手机码: ' || ?),
                    checked_at = COALESCE(checked_at, CURRENT_TIMESTAMP)
                WHERE lower(email) = lower(?)
            """, (phone, phone, email))
            updated += cursor.rowcount

            # 同步更新 accounts 表
            cursor.execute("""
                UPDATE accounts SET
                    has_phone = 1,
                    phone_number = ?,
                    check_status = CASE WHEN check_status = 'banned' THEN 'banned' ELSE 'has_phone' END,
                    checked_at = COALESCE(checked_at, CURRENT_TIMESTAMP)
                WHERE lower(email) = lower(?)
            """, (phone, email))
        conn.commit()
    return {"success": True, "count": updated, "total_known": len(mapping), "message": f"成功关联 {updated} 个邮箱的历史已接手机号！"}


def update_worker_account_check_result(
    worker_account_id: int,
    check_status: str,
    has_phone: Optional[int],
    phone_number: str = None,
    check_message: str = None
) -> bool:
    """更新 Worker 邮箱的验活与接手机码检测结果"""
    with get_connection() as conn:
        cursor = conn.cursor()
        if check_status == "unregistered":
            cursor.execute("""
                UPDATE worker_accounts SET
                    check_status = 'unregistered',
                    has_phone = NULL,
                    phone_number = NULL,
                    check_message = ?,
                    checked_at = CURRENT_TIMESTAMP
                WHERE id = ?
            """, (check_message, int(worker_account_id)))
        elif check_status == "banned":
            cursor.execute("""
                UPDATE worker_accounts SET
                    check_status = 'banned',
                    has_phone = NULL,
                    check_message = ?,
                    checked_at = CURRENT_TIMESTAMP
                WHERE id = ?
            """, (check_message, int(worker_account_id)))
        else:
            cursor.execute("""
                UPDATE worker_accounts SET
                    check_status = ?,
                    has_phone = ?,
                    phone_number = CASE WHEN ? IS NOT NULL AND length(?) > 0 THEN ? ELSE phone_number END,
                    check_message = ?,
                    checked_at = CURRENT_TIMESTAMP
                WHERE id = ?
            """, (check_status, has_phone, phone_number, phone_number, phone_number, check_message, int(worker_account_id)))
        
        # 若在 accounts 库存表中也存在该邮箱，同步更新状态
        cursor.execute("SELECT email FROM worker_accounts WHERE id = ?", (int(worker_account_id),))
        row = cursor.fetchone()
        if row and row["email"]:
            email = row["email"]
            if check_status == "banned":
                cursor.execute("""
                    UPDATE accounts SET
                        status = 'banned',
                        error_message = ?,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE email = ?
                """, (check_message or "已被 OpenAI 停用封禁", email))
            elif check_status in ("has_phone", "available", "needs_phone"):
                cursor.execute("""
                    UPDATE accounts SET
                        error_message = CASE WHEN status = 'available' THEN ? ELSE error_message END,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE email = ? AND status = 'available'
                """, (check_message, email))
        conn.commit()
        return cursor.rowcount > 0


def list_worker_accounts(
    search: str = None,
    inventory_filter: str = "all",
    domain_filter: str = "all",
    phone_filter: str = "all",
    page: int = 1,
    page_size: int = 50
) -> dict:
    """
    分页查询 Worker 邮箱列表，并关联现有邀请系统 accounts 库存状态与手机验活状态
    """
    offset = (max(page, 1) - 1) * page_size
    base_where = " WHERE 1=1"
    params = []

    if search:
        base_where += " AND (w.email LIKE ? OR w.phone_number LIKE ?)"
        params.extend([f"%{search.strip()}%", f"%{search.strip()}%"])

    if domain_filter and domain_filter != "all":
        base_where += " AND w.email LIKE ?"
        params.append(f"%@{domain_filter.strip()}")

    if inventory_filter and inventory_filter != "all":
        if inventory_filter == "not_imported":
            base_where += " AND a.id IS NULL"
        elif inventory_filter == "imported":
            base_where += " AND a.id IS NOT NULL"
        elif inventory_filter == "available":
            base_where += " AND a.status = 'available'"
        elif inventory_filter == "completed":
            base_where += " AND a.status = 'completed'"
        elif inventory_filter == "cooling":
            base_where += " AND a.status = 'completed' AND (julianday('now') - julianday(COALESCE(a.completed_at, a.updated_at)) < 60)"
        elif inventory_filter == "ready_to_unfreeze":
            base_where += " AND a.status = 'completed' AND (julianday('now') - julianday(COALESCE(a.completed_at, a.updated_at)) >= 60)"
        elif inventory_filter in ("allocated", "banned", "failed"):
            base_where += " AND a.status = ?"
            params.append(inventory_filter)

    if phone_filter and phone_filter != "all":
        if phone_filter == "has_phone":
            base_where += " AND (w.has_phone = 1 OR w.check_status = 'has_phone')"
        elif phone_filter == "needs_phone":
            base_where += " AND (w.has_phone = 0 OR w.check_status = 'needs_phone') AND (w.check_status NOT IN ('banned', 'unregistered'))"
        elif phone_filter == "banned":
            base_where += " AND w.check_status = 'banned'"
        elif phone_filter == "unregistered":
            base_where += " AND w.check_status = 'unregistered'"
        elif phone_filter == "unchecked":
            base_where += " AND (w.check_status = 'pending' AND (w.has_phone IS NULL OR w.has_phone = -1))"

    count_sql = f"""
        SELECT COUNT(*) as total
        FROM worker_accounts w
        LEFT JOIN accounts a ON w.email = a.email
        {base_where}
    """

    query_sql = f"""
        SELECT 
            w.id,
            w.worker_id,
            w.email,
            w.jwt,
            w.mail_count,
            w.worker_created_at,
            w.synced_at,
            w.check_status,
            w.has_phone,
            w.phone_number,
            w.check_message,
            w.checked_at,
            a.id AS inv_account_id,
            a.status AS inv_status,
            a.assigned_card AS inv_assigned_card,
            a.completed_at AS inv_completed_at,
            a.updated_at AS inv_updated_at,
            a.mail_type AS inv_mail_type
        FROM worker_accounts w
        LEFT JOIN accounts a ON w.email = a.email
        {base_where}
        ORDER BY w.worker_id DESC, w.id DESC
        LIMIT ? OFFSET ?
    """

    query_params = list(params) + [page_size, offset]

    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(count_sql, params)
        total = cursor.fetchone()["total"]

        cursor.execute(query_sql, query_params)
        raw_rows = [dict(r) for r in cursor.fetchall()]

        # 全局概览统计
        cursor.execute("SELECT COUNT(*) as cnt FROM worker_accounts")
        total_worker = cursor.fetchone()["cnt"]

        cursor.execute("""
            SELECT 
                COUNT(a.id) as imported_cnt,
                SUM(CASE WHEN a.status = 'available' THEN 1 ELSE 0 END) as available_cnt,
                SUM(CASE WHEN a.status = 'completed' THEN 1 ELSE 0 END) as completed_cnt
            FROM worker_accounts w
            INNER JOIN accounts a ON w.email = a.email
        """)
        inv_stat = cursor.fetchone()
        imported_cnt = inv_stat["imported_cnt"] or 0
        available_cnt = inv_stat["available_cnt"] or 0
        completed_cnt = inv_stat["completed_cnt"] or 0
        not_imported_cnt = max(0, total_worker - imported_cnt)

        # 手机绑定与验活概览统计
        cursor.execute("""
            SELECT 
                SUM(CASE WHEN has_phone = 1 OR check_status = 'has_phone' THEN 1 ELSE 0 END) as has_phone_cnt,
                SUM(CASE WHEN (has_phone = 0 OR check_status = 'needs_phone') AND check_status NOT IN ('banned', 'unregistered') THEN 1 ELSE 0 END) as needs_phone_cnt,
                SUM(CASE WHEN check_status = 'banned' THEN 1 ELSE 0 END) as banned_cnt,
                SUM(CASE WHEN check_status = 'unregistered' THEN 1 ELSE 0 END) as unregistered_cnt,
                SUM(CASE WHEN check_status = 'pending' AND (has_phone IS NULL OR has_phone = -1) THEN 1 ELSE 0 END) as unchecked_cnt
            FROM worker_accounts
        """)
        phone_stat = cursor.fetchone()
        has_phone_cnt = (phone_stat["has_phone_cnt"] or 0) if phone_stat else 0
        needs_phone_cnt = (phone_stat["needs_phone_cnt"] or 0) if phone_stat else 0
        banned_cnt = (phone_stat["banned_cnt"] or 0) if phone_stat else 0
        unregistered_cnt = (phone_stat["unregistered_cnt"] or 0) if phone_stat else 0
        unchecked_cnt = (phone_stat["unchecked_cnt"] or 0) if phone_stat else 0

        cursor.execute("SELECT MAX(synced_at) as last_sync FROM worker_accounts")
        last_sync_row = cursor.fetchone()
        last_sync = last_sync_row["last_sync"] if last_sync_row else None

    # 对列表数据做冷冻期修饰
    enriched_items = []
    for row in raw_rows:
        if row.get("inv_status") == "completed":
            row_inv = {
                "status": "completed",
                "completed_at": row.get("inv_completed_at"),
                "updated_at": row.get("inv_updated_at")
            }
            enriched = enrich_account_cooldown_info(row_inv)
            row["cooldown_days_left"] = enriched.get("cooldown_days_left", 0)
            row["is_cooling"] = enriched.get("is_cooling", False)
            row["can_unfreeze"] = enriched.get("can_unfreeze", False)
        else:
            row["cooldown_days_left"] = 0
            row["is_cooling"] = False
            row["can_unfreeze"] = False
        enriched_items.append(row)

    return {
        "items": enriched_items,
        "total": total,
        "page": page,
        "page_size": page_size,
        "summary": {
            "total_worker": total_worker,
            "imported_count": imported_cnt,
            "not_imported_count": not_imported_cnt,
            "available_count": available_cnt,
            "completed_count": completed_cnt,
            "has_phone_count": has_phone_cnt,
            "needs_phone_count": needs_phone_cnt,
            "banned_count": banned_cnt,
            "unregistered_count": unregistered_cnt,
            "unchecked_count": unchecked_cnt,
            "last_sync": last_sync
        }
    }


def import_worker_accounts_to_inventory(worker_ids: Optional[List[int]] = None, import_all_unimported: bool = False) -> dict:
    """
    将选定的 Worker 邮箱或全部未入库 Worker 邮箱导入到 accounts 库存表
    """
    with get_connection() as conn:
        cursor = conn.cursor()
        if import_all_unimported:
            cursor.execute("""
                SELECT w.id, w.email, w.jwt, w.check_status, w.has_phone, w.phone_number, w.checked_at, w.check_message 
                FROM worker_accounts w
                LEFT JOIN accounts a ON w.email = a.email
                WHERE a.id IS NULL AND w.jwt IS NOT NULL AND length(w.jwt) > 10
            """)
        elif worker_ids:
            placeholders = ",".join("?" for _ in worker_ids)
            cursor.execute(f"""
                SELECT id, email, jwt, check_status, has_phone, phone_number, checked_at, check_message 
                FROM worker_accounts 
                WHERE id IN ({placeholders}) AND jwt IS NOT NULL AND length(jwt) > 10
            """, worker_ids)
        else:
            return {"success": False, "error": "未指定要导入的 Worker 邮箱", "imported": 0, "updated": 0}

        rows = cursor.fetchall()
        if not rows:
            return {"success": True, "message": "没有需要导入的邮箱（可能已全部入库或未选定）", "imported": 0, "updated": 0}

        imported = 0
        updated = 0
        for r in rows:
            email = r["email"]
            jwt_val = "".join(str(r["jwt"] or "").split())
            cursor.execute("SELECT id FROM accounts WHERE email = ?", (email,))
            exists = cursor.fetchone()

            check_st = r["check_status"]
            has_ph = r["has_phone"]
            ph_num = r["phone_number"] or ""
            chk_at = r["checked_at"]
            msg = r["check_message"] or ""

            cursor.execute(f"""
                INSERT INTO accounts (
                    email, password, worker_jwt, client_id, refresh_token, mail_type, status,
                    check_status, has_phone, phone_number, checked_at, error_message
                )
                VALUES (
                    ?, '{DEFAULT_ACCOUNT_PASSWORD}', ?, '', '', 'worker', 'available',
                    ?, ?, ?, ?, ?
                )
                ON CONFLICT(email) DO UPDATE SET
                    worker_jwt = excluded.worker_jwt,
                    mail_type = 'worker',
                    check_status = CASE WHEN excluded.check_status IS NOT NULL THEN excluded.check_status ELSE accounts.check_status END,
                    has_phone = CASE WHEN excluded.has_phone IS NOT NULL THEN excluded.has_phone ELSE accounts.has_phone END,
                    phone_number = CASE WHEN excluded.phone_number != '' THEN excluded.phone_number ELSE accounts.phone_number END,
                    checked_at = CASE WHEN excluded.checked_at IS NOT NULL THEN excluded.checked_at ELSE accounts.checked_at END,
                    error_message = CASE WHEN excluded.error_message != '' THEN excluded.error_message ELSE accounts.error_message END,
                    updated_at = CURRENT_TIMESTAMP
            """, (email, jwt_val, check_st, has_ph, ph_num, chk_at, msg))
            if exists:
                updated += 1
            else:
                imported += 1
        conn.commit()

    return {
        "success": True,
        "imported": imported,
        "updated": updated,
        "total": len(rows),
        "message": f"成功导入 {imported} 个新邮箱至系统库存，更新 {updated} 个已存在邮箱凭证！"
    }


def import_worker_phone_verified_xyz_to_inventory() -> dict:
    """
    将 worker_accounts 表中所有域名后缀为 .xyz 且已接手机码 (has_phone=1 或带手机号) 的邮箱全部同步导入至 accounts 表
    """
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, email, jwt, check_status, has_phone, phone_number, checked_at, check_message
            FROM worker_accounts
            WHERE email LIKE '%.xyz' AND (has_phone = 1 OR (phone_number IS NOT NULL AND phone_number != ''))
              AND jwt IS NOT NULL AND length(jwt) > 10
        """)
        rows = cursor.fetchall()
        if not rows:
            return {"success": True, "message": "没有符合条件的 Worker 邮箱", "imported": 0, "updated": 0, "total": 0}

        imported = 0
        updated = 0
        for r in rows:
            email = r["email"]
            jwt_val = "".join(str(r["jwt"] or "").split())
            cursor.execute("SELECT id, status FROM accounts WHERE email = ?", (email,))
            exists = cursor.fetchone()

            check_st = r["check_status"] or "has_phone"
            has_ph = r["has_phone"] if r["has_phone"] is not None else 1
            ph_num = r["phone_number"] or ""
            chk_at = r["checked_at"]
            msg = r["check_message"] or "已接手机码"

            cursor.execute(f"""
                INSERT INTO accounts (
                    email, password, worker_jwt, client_id, refresh_token, mail_type, status,
                    check_status, has_phone, phone_number, checked_at, error_message
                )
                VALUES (
                    ?, '{DEFAULT_ACCOUNT_PASSWORD}', ?, '', '', 'worker', 'available',
                    ?, ?, ?, ?, ?
                )
                ON CONFLICT(email) DO UPDATE SET
                    worker_jwt = excluded.worker_jwt,
                    mail_type = 'worker',
                    check_status = excluded.check_status,
                    has_phone = excluded.has_phone,
                    phone_number = CASE WHEN excluded.phone_number != '' THEN excluded.phone_number ELSE accounts.phone_number END,
                    checked_at = CASE WHEN excluded.checked_at IS NOT NULL THEN excluded.checked_at ELSE accounts.checked_at END,
                    error_message = CASE WHEN excluded.error_message != '' THEN excluded.error_message ELSE accounts.error_message END,
                    status = CASE WHEN accounts.status = 'completed' THEN 'completed' ELSE 'available' END,
                    updated_at = CURRENT_TIMESTAMP
            """, (email, jwt_val, check_st, has_ph, ph_num, chk_at, msg))
            if exists:
                updated += 1
            else:
                imported += 1
        conn.commit()

    return {
        "success": True,
        "imported": imported,
        "updated": updated,
        "total": len(rows),
        "message": f"成功将 {len(rows)} 个已接码的 .xyz 邮箱同步至库存管理 (新增: {imported}, 更新: {updated})！"
    }


def get_all_worker_accounts_text(
    format_type: str = "email_jwt",
    filter_type: str = "all",
    phone_filter: str = "all",
    domain_filter: str = "all",
    inventory_filter: str = "all",
    search: str = None,
    ids: str = None
) -> str:
    """
    导出/获取 Worker 邮箱及 JWT 的纯文本格式，支持按手机接码状态、库存状态、域名及勾选范围过滤
    """
    where_clause = " WHERE w.jwt IS NOT NULL AND length(w.jwt) > 10"
    params = []

    # 1. 如果指定了勾选的 ID 集合 (filter_type == "selected" 或提供了 ids 且 filter_type 为 selected)
    if filter_type == "selected" and ids:
        id_list = [int(i.strip()) for i in ids.split(",") if i.strip().isdigit()]
        if id_list:
            placeholders = ",".join(["?"] * len(id_list))
            where_clause += f" AND w.id IN ({placeholders})"
            params.extend(id_list)
        else:
            return ""
    else:
        # 手机状态过滤
        effective_phone = None
        if filter_type in ("has_phone", "needs_phone", "banned", "unregistered", "unchecked"):
            effective_phone = filter_type
        elif (filter_type == "current" or phone_filter != "all") and phone_filter:
            effective_phone = phone_filter

        if effective_phone and effective_phone != "all":
            if effective_phone == "has_phone":
                where_clause += " AND (w.has_phone = 1 OR w.check_status = 'has_phone')"
            elif effective_phone == "needs_phone":
                where_clause += " AND (w.has_phone = 0 OR w.check_status = 'needs_phone') AND (w.check_status NOT IN ('banned', 'unregistered'))"
            elif effective_phone == "banned":
                where_clause += " AND w.check_status = 'banned'"
            elif effective_phone == "unregistered":
                where_clause += " AND w.check_status = 'unregistered'"
            elif effective_phone == "unchecked":
                where_clause += " AND (w.check_status = 'pending' AND (w.has_phone IS NULL OR w.has_phone = -1))"

        # 库存状态过滤
        effective_inv = None
        if filter_type in ("not_imported", "imported", "available", "completed", "cooling", "ready_to_unfreeze"):
            effective_inv = filter_type
        elif (filter_type == "current" or inventory_filter != "all") and inventory_filter:
            effective_inv = inventory_filter

        if effective_inv and effective_inv != "all":
            if effective_inv == "not_imported":
                where_clause += " AND a.id IS NULL"
            elif effective_inv == "imported":
                where_clause += " AND a.id IS NOT NULL"
            elif effective_inv == "available":
                where_clause += " AND a.status = 'available'"
            elif effective_inv == "completed":
                where_clause += " AND a.status = 'completed'"
            elif effective_inv == "cooling":
                where_clause += " AND a.status = 'completed' AND (julianday('now') - julianday(COALESCE(a.completed_at, a.updated_at)) < 60)"
            elif effective_inv == "ready_to_unfreeze":
                where_clause += " AND a.status = 'completed' AND (julianday('now') - julianday(COALESCE(a.completed_at, a.updated_at)) >= 60)"
            else:
                where_clause += " AND a.status = ?"
                params.append(effective_inv)

        # 域名过滤
        effective_domain = None
        if (filter_type == "current" or domain_filter != "all") and domain_filter:
            effective_domain = domain_filter

        if effective_domain and effective_domain != "all":
            where_clause += " AND w.email LIKE ?"
            params.append(f"%@{effective_domain.strip()}")

        # 搜索框过滤
        effective_search = None
        if (filter_type == "current" or search) and search:
            effective_search = search.strip()

        if effective_search:
            where_clause += " AND (w.email LIKE ? OR w.phone_number LIKE ?)"
            params.extend([f"%{effective_search}%", f"%{effective_search}%"])

    sql = f"""
        SELECT w.email, w.jwt 
        FROM worker_accounts w
        LEFT JOIN accounts a ON w.email = a.email
        {where_clause}
        ORDER BY w.worker_id DESC
    """
    lines = []
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(sql, tuple(params))
        for r in cursor.fetchall():
            email = r["email"]
            jwt = r["jwt"]
            if format_type == "email_jwt":
                lines.append(f"{email}----{jwt}")
            elif format_type == "full":
                lines.append(f"{email}----{DEFAULT_ACCOUNT_PASSWORD}----uploaded----{jwt}")
            elif format_type == "email_only":
                lines.append(email)
            else:
                lines.append(f"{email}----{jwt}")
    return "\n".join(lines)


def get_worker_account_by_id(worker_acc_id: int) -> Optional[dict]:
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM worker_accounts WHERE id = ?", (int(worker_acc_id),))
        row = cursor.fetchone()
        return dict(row) if row else None


def get_worker_account_by_email(email: str) -> Optional[dict]:
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM worker_accounts WHERE lower(email) = lower(?)", (email.strip(),))
        row = cursor.fetchone()
        return dict(row) if row else None


def delete_worker_accounts_batch(worker_acc_ids: List[int]) -> int:
    """批量删除本地 Worker 邮箱缓存记录"""
    if not worker_acc_ids:
        return 0
    with get_connection() as conn:
        cursor = conn.cursor()
        placeholders = ",".join("?" for _ in worker_acc_ids)
        cursor.execute(f"DELETE FROM worker_accounts WHERE id IN ({placeholders})", worker_acc_ids)
        conn.commit()
        return cursor.rowcount


def get_worker_accounts_for_auto_check(scope: str = "pending", limit: int = 1000) -> List[dict]:
    """获取待进行慢速自动巡检的 Worker 邮箱列表"""
    with get_connection() as conn:
        cursor = conn.cursor()
        if scope == "pending":
            cursor.execute("""
                SELECT id, worker_id, email, jwt, mail_count, check_status
                FROM worker_accounts
                WHERE check_status IN ('pending', 'unchecked', '') OR check_status IS NULL
                ORDER BY mail_count DESC, id ASC
                LIMIT ?
            """, (limit,))
        elif scope == "unregistered":
            cursor.execute("""
                SELECT id, worker_id, email, jwt, mail_count, check_status
                FROM worker_accounts
                WHERE check_status = 'unregistered'
                ORDER BY mail_count DESC, id ASC
                LIMIT ?
            """, (limit,))
        else:  # all
            cursor.execute("""
                SELECT id, worker_id, email, jwt, mail_count, check_status
                FROM worker_accounts
                ORDER BY mail_count DESC, id ASC
                LIMIT ?
            """, (limit,))
        return [dict(r) for r in cursor.fetchall()]



