import json
import os
import re
import time
import secrets
import hashlib
import base64
import logging
import subprocess
import urllib
import urllib.parse
import urllib.request
import urllib.error
import quopri
from dataclasses import dataclass
from typing import Any, Dict

import tls_client
import requests as std_requests

log = logging.getLogger("auto_register")

# ========== 常量 ==========

AUTH_URL = "https://auth.openai.com/oauth/authorize"
TOKEN_URL = "https://auth.openai.com/oauth/token"
CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
DEFAULT_REDIRECT_URI = "http://localhost:1455/auth/callback"
DEFAULT_SCOPE = "openid email profile offline_access"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36"
TLS_CLIENT_ID = "chrome_133"
DEFAULT_PASSWORD = os.getenv("DEFAULT_ACCOUNT_PASSWORD", "ChangeMePassword123!@")


# ========== 代理 ==========


def get_proxies(cfg: dict = None):
    proxy = os.environ.get("HTTP_PROXY") or os.environ.get("HTTPS_PROXY")
    if not proxy and cfg:
        proxy = cfg.get("proxy", "")
    if proxy:
        return {"http": proxy, "https": proxy}
    return None


# ========== 5sim 接码平台 ==========


def _5sim_headers(api_key: str) -> dict:
    return {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}


def _5sim_buy_number(cfg: dict):
    api_key = cfg.get("sms_api_key", "")
    if not api_key:
        log.error("未配置 sms_api_key，无法购买接码号码")
        return None
    country = cfg.get("sms_country", "usa").lower().strip()
    operator = cfg.get("sms_operator", "any").lower().strip()
    product = cfg.get("sms_product", "openai").lower().strip()
    url = f"https://5sim.net/v1/user/buy/activation/{country}/{operator}/{product}"
    try:
        resp = std_requests.get(url, headers=_5sim_headers(api_key), timeout=15)
        if resp.status_code == 200:
            try:
                data = resp.json()
            except Exception as e:
                body = resp.text[:500]
                log.warning(f"[5sim] 购买号码返回非 JSON: {body}")
                return None
            order_id = data.get("id")
            phone = data.get("phone", "")
            log.info(
                f"[5sim] 购买号码成功: order={order_id} phone={phone} country={data.get('country')} price={data.get('price')}"
            )
            return {"order_id": order_id, "phone": phone, "raw": data}
        else:
            body = resp.text[:300]
            log.warning(f"[5sim] 购买号码失败: {resp.status_code} - {body}")
            return None
    except Exception as e:
        log.error(f"[5sim] 购买号码异常: {e}")
        return None


def _5sim_poll_sms(order_id: int, cfg: dict):
    api_key = cfg.get("sms_api_key", "")
    timeout = int(cfg.get("sms_poll_timeout", 180))
    interval = int(cfg.get("sms_poll_interval", 5))
    url = f"https://5sim.net/v1/user/check/{order_id}"
    elapsed = 0
    while elapsed < timeout:
        try:
            resp = std_requests.get(url, headers=_5sim_headers(api_key), timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                status = data.get("status", "")
                sms_list = data.get("sms") or []
                if status == "RECEIVED" and sms_list:
                    code = sms_list[-1].get("code", "")
                    if code:
                        log.info(f"[5sim] 收到短信验证码: {code}")
                        return code
                if status in ("CANCELED", "BANNED", "EXPIRED"):
                    log.warning(f"[5sim] 订单状态异常: {status}")
                    return None
                log.info(f"[5sim] 等待短信... status={status} elapsed={elapsed}s")
            else:
                log.warning(f"[5sim] 轮询失败: {resp.status_code}")
        except Exception as e:
            log.warning(f"[5sim] 轮询异常: {e}")
        time.sleep(interval)
        elapsed += interval
    log.error(f"[5sim] 轮询超时 ({timeout}s)")
    return None


def _5sim_finish_order(order_id: int, cfg: dict):
    api_key = cfg.get("sms_api_key", "")
    url = f"https://5sim.net/v1/user/finish/{order_id}"
    try:
        std_requests.get(url, headers=_5sim_headers(api_key), timeout=10)
    except Exception:
        pass


def _5sim_cancel_order(order_id: int, cfg: dict):
    api_key = cfg.get("sms_api_key", "")
    url = f"https://5sim.net/v1/user/cancel/{order_id}"
    try:
        std_requests.get(url, headers=_5sim_headers(api_key), timeout=10)
    except Exception:
        pass


# ========== HeroSMS 接码平台 ==========

HERO_SMS_BASE_URL = "https://hero-sms.com/stubs/handler_api.php"
HERO_SMS_SERVICE = "dr"


def _resolve_countries(cfg: dict) -> list:
    country = cfg.get("hero_smsonline_country", 52)
    if isinstance(country, (list, tuple)):
        return [int(c) for c in country]
    return [int(country)]


def _hero_sms_request(params: dict, cfg: dict, label: str = "") -> str | None:
    api_key = cfg.get("hero_sms_api_key", "")
    if not api_key:
        log.error(f"[HeroSMS] 未配置 hero_sms_api_key")
        return None
    url = HERO_SMS_BASE_URL
    qs = {**params, "api_key": api_key}
    try:
        resp = std_requests.get(url, params=qs, timeout=20)
        text = resp.text.strip()
        if label:
            log.info(f"[HeroSMS] {label}: {text[:200]}")
        return text
    except Exception as e:
        log.error(f"[HeroSMS] {label} 请求异常: {e}")
        return None


def hero_sms_get_prices(cfg: dict, country=None) -> list:
    if country is None:
        country = cfg.get("hero_smsonline_country", 52)
    service = cfg.get("hero_smsonline_service", HERO_SMS_SERVICE)
    text = _hero_sms_request(
        {"action": "getPrices", "service": service, "country": str(country)},
        cfg, "查询价格",
    )
    if text is None:
        return []
    try:
        data = json.loads(text)
    except Exception as e:
        log.warning(f"[HeroSMS] 价格数据非 JSON: {text[:300]}")
        return []
    tiers = []
    country_data = data.get(str(country), data)
    if not isinstance(country_data, dict):
        return []
    service_data = country_data.get(service, country_data)
    if not isinstance(service_data, dict):
        return []
    cost = service_data.get("cost")
    if cost is not None:
        try:
            cost_val = float(cost)
        except (ValueError, TypeError):
            cost_val = None
        if cost_val is not None:
            count_val = 0
            for key in ("count", "qty", "left", "stock", "available", "physicalCount"):
                if key in service_data:
                    try:
                        count_val = int(service_data[key])
                        break
                    except (ValueError, TypeError):
                        continue
            tiers.append({"price": cost_val, "count": count_val})
    for key, val in service_data.items():
        if key in ("cost", "count", "qty", "left", "stock", "available", "physicalCount"):
            continue
        try:
            price_val = float(key)
        except (ValueError, TypeError):
            continue
        if isinstance(val, dict):
            sub_count = 0
            for sub_key in ("count", "qty", "left", "stock", "available", "physicalCount"):
                if sub_key in val:
                    try:
                        sub_count = int(val[sub_key])
                        break
                    except (ValueError, TypeError):
                        continue
            tiers.append({"price": price_val, "count": sub_count})
        elif isinstance(val, (int, float)):
            tiers.append({"price": price_val, "count": int(val)})
    if not tiers:
        log.warning(f"[HeroSMS] 未解析到价格档位: {text[:300]}")
    return tiers


def _hero_sms_buy_for_country(cfg: dict, country: int, service: str, max_price, max_retry: int):
    if max_price is not None:
        try:
            max_price = float(max_price)
        except (ValueError, TypeError):
            log.error(f"[HeroSMS] hero_smsonline_max_price 无效: {max_price}")
            return None

        log.info(f"[HeroSMS] 国家{country} 最高价格 {max_price}，查询价格档位...")
        tiers = hero_sms_get_prices(cfg, country=country)
        if not tiers:
            log.warning("[HeroSMS] 未获取到价格档位，直接购买")
            return _hero_sms_buy_direct(cfg, country, service, max_price, max_retry)

        affordable = [t for t in tiers if t["price"] <= max_price]
        if not affordable:
            log.error(
                f"[HeroSMS] 国家{country} 预算内无有库存的档位 (max_price={max_price}), "
                f"可用档位: {tiers}"
            )
            return None

        affordable.sort(key=lambda t: t["price"])
        log.info(
            f"[HeroSMS] 国家{country} 价格档位(<= {max_price}): "
            + ", ".join(f"{t['price']:.3f}(库存={t['count']})" for t in affordable)
        )

        for i, tier in enumerate(affordable):
            log.info(
                f"[HeroSMS] 国家{country} 尝试档位 {i+1}/{len(affordable)}: "
                f"价格={tier['price']:.3f}, 库存={tier['count']}"
            )
            result = _hero_sms_buy_direct(
                cfg, country, service, tier["price"], max_retry
            )
            if result is not None:
                return result
            log.info(f"[HeroSMS] 国家{country} 档位价格={tier['price']:.3f} 无号，尝试下一档")

        log.info(f"[HeroSMS] 国家{country} 所有档位无号，用 maxPrice={max_price} 直接购买...")
        result = _hero_sms_buy_direct(cfg, country, service, max_price, max_retry)
        if result is not None:
            return result
        log.error(f"[HeroSMS] 国家{country} 降级购买也失败")
        return None

    return _hero_sms_buy_direct(cfg, country, service, None, max_retry)


def hero_sms_buy_number(cfg: dict):
    countries = _resolve_countries(cfg)
    service = cfg.get("hero_smsonline_service", HERO_SMS_SERVICE)
    max_price = cfg.get("hero_smsonline_max_price")
    max_retry = int(cfg.get("hero_smsonline_max_retry", 3))

    if len(countries) > 1:
        log.info(f"[HeroSMS] 多国家轮询模式: {countries}")

    for country in countries:
        if len(countries) > 1:
            country_names = {6: "印尼", 10: "越南", 16: "英国", 24: "柬埔寨", 43: "德国", 52: "泰国", 73: "法国", 151: "智利", 187: "美国"}
            cname = country_names.get(country, "")
            log.info(f"[HeroSMS] 尝试国家 {country} {cname}...")

        result = _hero_sms_buy_for_country(cfg, country, service, max_price, max_retry)
        if result is not None:
            return result
        log.info(f"[HeroSMS] 国家 {country} 无号，尝试下一个")

    log.error("[HeroSMS] 所有国家均购买失败")
    return None


def _hero_sms_buy_direct(cfg: dict, country, service, max_price, max_retry: int):
    params = {"action": "getNumber", "service": service, "country": str(country)}
    if max_price is not None:
        params["maxPrice"] = str(max_price)
    text = _hero_sms_request(params, cfg, "购买号码")
    if text is None:
        return None
    if text.startswith("ACCESS_NUMBER:"):
        parts = text.split(":", 2)
        if len(parts) == 3:
            activation_id = parts[1].strip()
            phone = parts[2].strip()
            if not phone.startswith("+"):
                phone = f"+{phone}"
            log.info(f"[HeroSMS] 购买号码成功: activation_id={activation_id} phone={phone}")
            return {"activation_id": activation_id, "phone": phone}
    if "NO_NUMBERS" in text:
        log.warning(f"[HeroSMS] 暂无可用号码: {text}")
        return None
    if "NO_BALANCE" in text or "NOT_ENOUGH_BALANCE" in text:
        log.error(f"[HeroSMS] 余额不足: {text}")
        return None
    if "WRONG_MAX_PRICE" in text:
        match = re.search(r"WRONG_MAX_PRICE:(\d+\.?\d*)", text)
        if match:
            min_price = float(match.group(1))
            log.warning(
                f"[HeroSMS] 最低价格为 {min_price}，当前 maxPrice={max_price}，"
                f"尝试以 {min_price} 购买"
            )
            params["maxPrice"] = str(min_price)
            text2 = _hero_sms_request(params, cfg, f"以最低价格 {min_price} 购买")
            if text2 and text2.startswith("ACCESS_NUMBER:"):
                parts2 = text2.split(":", 2)
                if len(parts2) == 3:
                    activation_id = parts2[1].strip()
                    phone = parts2[2].strip()
                    if not phone.startswith("+"):
                        phone = f"+{phone}"
                    log.info(f"[HeroSMS] 购买号码成功: activation_id={activation_id} phone={phone}")
                    return {"activation_id": activation_id, "phone": phone}
            if text2 and "NO_NUMBERS" in text2:
                log.warning(f"[HeroSMS] 最低价格也无号")
                return None
            if text2:
                log.error(f"[HeroSMS] 购买失败: {text2[:300]}")
            return None
        log.error(f"[HeroSMS] 价格设置错误: {text}")
        return None
    log.error(f"[HeroSMS] 购买号码失败: {text[:300]}")
    return None


def hero_sms_poll_sms(activation_id: str, cfg: dict, timeout: int = None, interval: int = None):
    if timeout is None:
        timeout = int(cfg.get("sms_poll_timeout", 180))
    if interval is None:
        interval = int(cfg.get("sms_poll_interval", 5))
    log.info(f"[HeroSMS] 开始轮询短信验证码 (activation_id={activation_id}, 超时={timeout}s, 间隔={interval}s)")
    params_base = {"action": "getStatus", "id": str(activation_id)}
    elapsed = 0
    while elapsed < timeout:
        text = _hero_sms_request(params_base, cfg, f"getStatus({elapsed}s)")
        if text is None:
            log.warning(f"[HeroSMS] 轮询请求失败... 已等待 {elapsed}s / {timeout}s")
            time.sleep(interval)
            elapsed += interval
            continue
        if text.startswith("STATUS_OK:"):
            code = text.split(":", 1)[1].strip()
            digits = re.sub(r"\D", "", code)
            if 4 <= len(digits) <= 8:
                log.info(f"[HeroSMS] 收到短信验证码: {digits} (耗时: {elapsed}s)")
                return digits
            log.info(f"[HeroSMS] 收到短信内容: {code} (耗时: {elapsed}s)")
            return code
        if "STATUS_WAIT_CODE" in text or "STATUS_WAIT_RETRY" in text or "STATUS_WAIT_RESEND" in text:
            log.info(f"[HeroSMS] 等待短信... 已等待 {elapsed}s / {timeout}s")
        elif "STATUS_CANCEL" in text:
            log.error(f"[HeroSMS] 订单已取消: {text}")
            return None
        else:
            log.warning(f"[HeroSMS] 未知状态: {text[:200]}... 已等待 {elapsed}s / {timeout}s")
        time.sleep(interval)
        elapsed += interval
    log.error(f"[HeroSMS] 轮询超时 ({timeout}s)")
    return None


def hero_sms_set_status(activation_id: str, status: int, cfg: dict):
    params = {"action": "setStatus", "id": str(activation_id), "status": str(status)}
    return _hero_sms_request(params, cfg, f"setStatus({status})")


def hero_sms_finish_order(activation_id: str, cfg: dict):
    return hero_sms_set_status(activation_id, 6, cfg)


def _remaining_cancel_wait(cfg: dict, order_started_at: float | None = None) -> int:
    wait_seconds = int(cfg.get("hero_smsonline_cancel_wait", 122))
    if order_started_at is None:
        return wait_seconds
    elapsed = max(0, int(time.time() - order_started_at))
    return max(0, wait_seconds - elapsed)


def hero_sms_cancel_order(activation_id: str, cfg: dict, order_started_at: float | None = None):
    hero_sms_set_status(activation_id, 1, cfg)
    wait_seconds = _remaining_cancel_wait(cfg, order_started_at)
    retry_wait = int(cfg.get("hero_smsonline_cancel_retry_wait", 60))
    if wait_seconds > 0:
        log.info(f"[HeroSMS] 通知就绪 activation_id={activation_id}，等待 {wait_seconds}s 后取消...")
        time.sleep(wait_seconds)
    else:
        log.info(f"[HeroSMS] 通知就绪 activation_id={activation_id}，已达到最小激活时间，直接取消...")
    result = hero_sms_set_status(activation_id, 8, cfg)
    if result and "EARLY_CANCEL_DENIED" in result:
        log.warning(f"[HeroSMS] 暂无法取消，再等 {retry_wait}s 重试: activation_id={activation_id}")
        if retry_wait > 0:
            time.sleep(retry_wait)
        result = hero_sms_set_status(activation_id, 8, cfg)
    if result and "ACCESS_CANCEL" in result:
        log.info(f"[HeroSMS] 订单已取消: activation_id={activation_id}")
    elif result:
        log.warning(f"[HeroSMS] 取消结果: activation_id={activation_id}, {result[:200]}")
    return result


def hero_sms_get_balance(cfg: dict):
    return _hero_sms_request({"action": "getBalance"}, cfg, "查询余额")


# ========== sms-online.pro 接码平台 ==========

SMS_ONLINE_BASE_URL = "https://sms-online.pro/stubs/handler_api.php"


def _sms_online_key(cfg: dict) -> str:
    return (
        cfg.get("sms_online_key", "")
        or (cfg.get("gopay_plus") or {}).get("sms_online_key", "")
        or os.environ.get("OPAI_SMSONLINEPRO_API_KEY", "")
    )


def _sms_online_countries(cfg: dict) -> list:
    country = cfg.get("hero_smsonline_country", 52)
    if isinstance(country, (list, tuple)):
        return [str(c) for c in country] if country else ["52"]
    return [str(country)]


def _sms_online_service(cfg: dict) -> str:
    return str(cfg.get("hero_smsonline_service", HERO_SMS_SERVICE) or HERO_SMS_SERVICE)


def _sms_online_max_price(cfg: dict):
    return cfg.get("hero_smsonline_max_price")


def _sms_online_request(params: dict, cfg: dict, label: str = "") -> str | None:
    api_key = _sms_online_key(cfg)
    if not api_key:
        log.error("[sms-online.pro] 未配置 sms_online_key")
        return None
    qs = {**params, "api_key": api_key}
    try:
        resp = std_requests.get(SMS_ONLINE_BASE_URL, params=qs, timeout=20)
        text = resp.text.strip()
        if label:
            log.info(f"[sms-online.pro] {label}: {text[:200]}")
        return text
    except Exception as e:
        log.error(f"[sms-online.pro] {label} 请求异常: {e}")
        return None


def sms_online_get_balance(cfg: dict):
    return _sms_online_request({"action": "getBalance"}, cfg, "查询余额")


def sms_online_buy_number(cfg: dict):
    service = _sms_online_service(cfg)
    max_price = _sms_online_max_price(cfg)
    max_retry = int(cfg.get("hero_smsonline_max_retry", 3))
    countries = _sms_online_countries(cfg)
    key = _sms_online_key(cfg)
    key_suffix = key[-6:] if key else ""
    log.info(
        f"[sms-online.pro] 使用配置: countries={countries}, "
        f"service={service}, maxPrice={max_price if max_price not in (None, '') else '-'}, "
        f"key_suffix={key_suffix}"
    )
    sms_online_get_balance(cfg)
    for country in countries:
        for attempt in range(1, max_retry + 1):
            params = {"action": "getNumber", "service": service, "country": str(country)}
            if max_price not in (None, ""):
                params["maxPrice"] = str(max_price)
            text = _sms_online_request(params, cfg, f"购买号码 country={country} attempt={attempt}/{max_retry}")
            if text and text.startswith("ACCESS_NUMBER:"):
                parts = text.split(":", 2)
                if len(parts) == 3:
                    activation_id = parts[1].strip()
                    phone = parts[2].strip()
                    if not phone.startswith("+"):
                        phone = f"+{phone}"
                    log.info(f"[sms-online.pro] 购买号码成功: activation_id={activation_id} phone={phone}")
                    return {"activation_id": activation_id, "phone": phone}
            if text and ("NO_NUMBERS" in text or "NO_BALANCE" in text or "BAD_KEY" in text):
                log.warning(f"[sms-online.pro] 购买失败: {text[:200]}")
                if "NO_BALANCE" in text or "BAD_KEY" in text:
                    return None
        log.info(f"[sms-online.pro] 指定国家 {country} 无号")
    log.error("[sms-online.pro] 所有国家均购买失败")
    return None


def sms_online_poll_sms(activation_id: str, cfg: dict, timeout: int = None, interval: int = None):
    if timeout is None:
        timeout = int(cfg.get("sms_poll_timeout", 180))
    if interval is None:
        interval = int(cfg.get("sms_poll_interval", 5))
    log.info(f"[sms-online.pro] 开始轮询短信验证码 (activation_id={activation_id}, 超时={timeout}s, 间隔={interval}s)")
    elapsed = 0
    while elapsed < timeout:
        text = _sms_online_request({"action": "getStatus", "id": str(activation_id)}, cfg, f"getStatus({elapsed}s)")
        if text is None:
            log.warning(f"[sms-online.pro] 轮询请求失败... 已等待 {elapsed}s / {timeout}s")
        elif text.startswith("STATUS_OK:"):
            code = text.split(":", 1)[1].strip()
            digits = re.sub(r"\D", "", code)
            if 4 <= len(digits) <= 8:
                log.info(f"[sms-online.pro] 收到短信验证码: {digits} (耗时: {elapsed}s)")
                return digits
            log.info(f"[sms-online.pro] 收到短信内容: {code} (耗时: {elapsed}s)")
            return code
        elif "STATUS_WAIT_CODE" in text or "STATUS_WAIT_RETRY" in text or "STATUS_WAIT_RESEND" in text:
            log.info(f"[sms-online.pro] 等待短信... 已等待 {elapsed}s / {timeout}s")
        elif "STATUS_CANCEL" in text:
            log.error(f"[sms-online.pro] 订单已取消: {text}")
            return None
        else:
            log.warning(f"[sms-online.pro] 未知状态: {text[:200]}... 已等待 {elapsed}s / {timeout}s")
        time.sleep(interval)
        elapsed += interval
    log.error(f"[sms-online.pro] 轮询超时 ({timeout}s)")
    return None


def sms_online_set_status(activation_id: str, status: int, cfg: dict):
    params = {"action": "setStatus", "id": str(activation_id), "status": str(status)}
    return _sms_online_request(params, cfg, f"setStatus({status})")


def sms_online_finish_order(activation_id: str, cfg: dict):
    return sms_online_set_status(activation_id, 6, cfg)


def sms_online_cancel_order(activation_id: str, cfg: dict, order_started_at: float | None = None):
    wait_seconds = _remaining_cancel_wait(cfg, order_started_at)
    retry_wait = int(cfg.get("hero_smsonline_cancel_retry_wait", 60))
    if wait_seconds > 0:
        log.info(f"[sms-online.pro] 等待 {wait_seconds}s 后取消 activation_id={activation_id}")
        time.sleep(wait_seconds)
    else:
        log.info(f"[sms-online.pro] 已达到最小激活时间，直接取消 activation_id={activation_id}")
    result = sms_online_set_status(activation_id, 8, cfg)
    if result and "EARLY_CANCEL_DENIED" in result:
        log.warning(f"[sms-online.pro] 暂无法取消，再等 {retry_wait}s 重试: activation_id={activation_id}")
        if retry_wait > 0:
            time.sleep(retry_wait)
        result = sms_online_set_status(activation_id, 8, cfg)
    if result and "ACCESS_CANCEL" in result:
        log.info(f"[sms-online.pro] 订单已取消: activation_id={activation_id}")
    elif result:
        log.warning(f"[sms-online.pro] 取消结果: activation_id={activation_id}, {result[:200]}")
    return result


def _manual_input_otp():
    while True:
        code = input("请输入手机验证码（6位数字）: ").strip()
        if re.match(r'^\d{6}$', code):
            return code
        print("格式错误，请输入6位数字")


def handle_add_phone(s2, cfg: dict, current_step: dict, current_continue_url: str, phone: str = "", order_id: int = None, is_hero: bool = False, phone_channel: str = "sms", sms_provider: str = "", order_started_at: float | None = None):
    otp_max_attempt = 3
    provider = str(sms_provider or ("hero" if is_hero else "5sim")).strip().lower()

    def poll_provider_code():
        if is_hero or provider == "hero":
            return hero_sms_poll_sms(str(order_id), cfg)
        if provider in ("sms_online", "sms_online_pro", "smsonline", "sms-online"):
            return sms_online_poll_sms(str(order_id), cfg)
        return _5sim_poll_sms(order_id, cfg)

    def finish_provider_order():
        if is_hero or provider == "hero":
            return hero_sms_finish_order(str(order_id), cfg)
        if provider in ("sms_online", "sms_online_pro", "smsonline", "sms-online"):
            return sms_online_finish_order(str(order_id), cfg)
        return _5sim_finish_order(order_id, cfg)

    def cancel_provider_order():
        if is_hero:
            return None
        if provider == "hero":
            return hero_sms_cancel_order(str(order_id), cfg, order_started_at=order_started_at)
        if provider in ("sms_online", "sms_online_pro", "smsonline", "sms-online"):
            return sms_online_cancel_order(str(order_id), cfg, order_started_at=order_started_at)
        return _5sim_cancel_order(order_id, cfg)

    if phone and order_id is not None:
        if not phone.startswith("+"):
            phone = f"+{phone}"
        log.info(f"[add_phone] 使用预购号码: {phone}，order_id={order_id}")

        try:
            add_phone_url = current_continue_url or "https://auth.openai.com/add-phone"
            add_resp = s2.get(
                add_phone_url,
                headers={
                    "referer": "https://auth.openai.com/",
                    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                },
                allow_redirects=True,
            )
            add_referer = str(getattr(add_resp, "url", "") or add_phone_url)
            log.info(f"[add_phone] fresh GET add-phone page: status={add_resp.status_code}, url={add_referer[:120]}")

            send_resp = s2.post(
                "https://auth.openai.com/api/accounts/add-phone/send",
                json={"phone_number": phone, "channel": phone_channel},
                headers={
                    "referer": add_referer,
                    "origin": "https://auth.openai.com",
                },
            )
            log.info(
                f"[add_phone] send: {send_resp.status_code} - {send_resp.text[:300]}"
            )

            if send_resp.status_code not in (200, 201):
                resp_text = send_resp.text[:500]

                if "invalid_state" in resp_text:
                    log.warning("[add_phone] session 失效，重新获取 add-phone 页面后重试...")
                    try:
                        retry_add_resp = s2.get(current_continue_url or add_phone_url, headers={
                            "referer": "https://auth.openai.com/",
                            "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                        }, allow_redirects=True)
                        add_referer = str(getattr(retry_add_resp, "url", "") or add_referer)
                    except Exception:
                        pass
                    time.sleep(2)
                    send_resp = s2.post(
                        "https://auth.openai.com/api/accounts/add-phone/send",
                        json={"phone_number": phone, "channel": phone_channel},
                        headers={
                            "referer": add_referer,
                            "origin": "https://auth.openai.com",
                        },
                    )
                    log.info(f"[add_phone] 重试 send: {send_resp.status_code} - {send_resp.text[:200]}")
                    if send_resp.status_code in (200, 201):
                        resp_text = send_resp.text[:500]
                    else:
                        log.error(f"[add_phone] 重试后仍失败: {send_resp.status_code}")
                        cancel_provider_order()
                        return None, None

                rejected = any(
                    kw in resp_text
                    for kw in [
                        "fraud_guard",
                        "voip_phone_number_disallowed",
                        "landline_disallowed",
                        "unsupported_phone_number",
                        "phone_already_in_use",
                        "invalid_phone_number",
                    ]
                )
                if rejected:
                    log.warning(f"[add_phone] 号码被拒: {phone}, 原因: {resp_text[:200]}")
                else:
                    log.error(f"[add_phone] 提交手机号失败: {send_resp.status_code}")
                
                cancel_provider_order()
                return None, None

            send_step = json.loads(send_resp.text) if send_resp.text else {}
            next_continue_url = send_step.get("continue_url", "")
            next_page_type = send_step.get("page", {}).get("type", "")

            if next_page_type == "add_phone" and not next_continue_url:
                log.warning("[add_phone] 提交后仍返回 add_phone，号码可能被拒")
                cancel_provider_order()
                return None, None

            log.info(f"[add_phone] 提交成功，page_type={next_page_type}")

            s2.get(
                "https://auth.openai.com/api/accounts/phone-otp/send",
                headers={"referer": "https://auth.openai.com/phone-verification"},
            )

            sms_code = poll_provider_code()

            if not sms_code:
                log.warning("[add_phone] 未收到短信验证码")
                cancel_provider_order()
                return None, None

            otp_success = False
            for attempt in range(1, otp_max_attempt + 1):
                if attempt > 1:
                    log.info(f"[add_phone] OTP 验证第 {attempt} 次重试...")
                    try:
                            resend_resp = s2.post(
                                "https://auth.openai.com/api/accounts/phone-otp/resend",
                                headers={
                                    "referer": "https://auth.openai.com/phone-verification",
                                    "origin": "https://auth.openai.com",
                                },
                            )
                            log.info(f"[add_phone] resend: {resend_resp.status_code}")
                            
                            sms_code = poll_provider_code()
                                
                            if not sms_code:
                                log.warning("[add_phone] 重发后仍未收到验证码")
                                break
                    except Exception as e:
                        log.warning(f"[add_phone] resend 异常: {e}")
                        break

                validate_resp = s2.post(
                    "https://auth.openai.com/api/accounts/phone-otp/validate",
                    json={"code": sms_code},
                    headers={
                        "referer": "https://auth.openai.com/phone-verification",
                        "origin": "https://auth.openai.com",
                    },
                )
                log.info(
                    f"[add_phone] validate: {validate_resp.status_code} - {validate_resp.text[:300]}"
                )

                if validate_resp.status_code in (200, 201):
                    otp_success = True
                    if not is_hero:
                        finish_provider_order()
                    current_step = (
                        json.loads(validate_resp.text) if validate_resp.text else {}
                    )
                    break
                elif (
                    validate_resp.status_code == 401
                    or "wrong_phone_otp_code" in validate_resp.text[:200]
                ):
                    log.warning("[add_phone] 验证码错误，重试...")
                    continue
                else:
                    log.error(f"[add_phone] 验证失败: {validate_resp.status_code}")
                    break

            if not otp_success:
                cancel_provider_order()
                return None, None

            return current_step, current_continue_url

        except Exception as e:
            log.error(f"[add_phone] 异常: {e}")
            cancel_provider_order()
            return None, None

    if phone:
        if not phone.startswith("+"):
            phone = f"+{phone}"
        log.info(f"[add_phone] 使用指定手机号: {phone}，手动输入OTP模式")

        try:
            send_resp = s2.post(
                "https://auth.openai.com/api/accounts/add-phone/send",
                json={"phone_number": phone, "channel": phone_channel},
                headers={
                    "referer": "https://auth.openai.com/add-phone",
                    "origin": "https://auth.openai.com",
                },
            )
            log.info(
                f"[add_phone] send: {send_resp.status_code} - {send_resp.text[:300]}"
            )

            if send_resp.status_code not in (200, 201):
                resp_text = send_resp.text[:500]
                log.error(
                    f"[add_phone] 提交手机号失败: {send_resp.status_code} - {resp_text[:300]}"
                )

                if "invalid_state" in resp_text:
                    log.warning("[add_phone] session 失效，重新获取 add-phone 页面后重试...")
                    try:
                        s2.get(current_continue_url, headers={
                            "referer": "https://auth.openai.com/",
                            "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                        }, allow_redirects=True)
                    except Exception:
                        pass
                    time.sleep(2)
                    send_resp = s2.post(
                        "https://auth.openai.com/api/accounts/add-phone/send",
                        json={"phone_number": phone, "channel": phone_channel},
                        headers={
                            "referer": "https://auth.openai.com/add-phone",
                            "origin": "https://auth.openai.com",
                        },
                    )
                    log.info(f"[add_phone] 重试 send: {send_resp.status_code} - {send_resp.text[:200]}")
                    if send_resp.status_code in (200, 201):
                        log.info("[add_phone] 重试成功")
                    else:
                        log.error(f"[add_phone] 重试后仍失败: {send_resp.status_code}")
                        return None, None

                else:
                    return None, None

            send_step = json.loads(send_resp.text) if send_resp.text else {}
            next_continue_url = send_step.get("continue_url", "")
            next_page_type = send_step.get("page", {}).get("type", "")

            if next_page_type == "add_phone" and not next_continue_url:
                log.warning("[add_phone] 提交后仍返回 add_phone，号码可能被拒")
                if not is_hero:
                    _5sim_cancel_order(order_id, cfg)
                return None, None

            log.info(f"[add_phone] 提交成功，page_type={next_page_type}")

            s2.get(
                "https://auth.openai.com/api/accounts/phone-otp/send",
                headers={"referer": "https://auth.openai.com/phone-verification"},
            )

            otp_success = False
            for attempt in range(1, otp_max_attempt + 1):
                sms_code = _manual_input_otp()
                log.info(f"[add_phone] 手动输入验证码: {sms_code}")

                validate_resp = s2.post(
                    "https://auth.openai.com/api/accounts/phone-otp/validate",
                    json={"code": sms_code},
                    headers={
                        "referer": "https://auth.openai.com/phone-verification",
                        "origin": "https://auth.openai.com",
                    },
                )
                log.info(
                    f"[add_phone] validate: {validate_resp.status_code} - {validate_resp.text[:300]}"
                )

                if validate_resp.status_code in (200, 201):
                    otp_success = True
                    current_step = (
                        json.loads(validate_resp.text) if validate_resp.text else {}
                    )
                    current_continue_url = current_step.get("continue_url", "")
                    log.info(
                        f"[add_phone] 手机验证成功! page_type={current_step.get('page', {}).get('type', '')}"
                    )
                    break
                elif (
                    validate_resp.status_code == 401
                    or "wrong_phone_otp_code" in validate_resp.text[:200]
                ):
                    log.warning("[add_phone] 验证码错误，请重新输入")
                    if attempt < otp_max_attempt:
                        try:
                            resend_resp = s2.post(
                                "https://auth.openai.com/api/accounts/phone-otp/resend",
                                headers={
                                    "referer": "https://auth.openai.com/phone-verification",
                                    "origin": "https://auth.openai.com",
                                },
                            )
                            log.info(f"[add_phone] resend: {resend_resp.status_code}")
                        except Exception as e:
                            log.warning(f"[add_phone] resend 异常: {e}")
                    continue
                else:
                    log.error(f"[add_phone] 验证失败: {validate_resp.status_code}")
                    break

            if not otp_success:
                log.error("[add_phone] 手动OTP验证失败")
                return None, None

            return current_step, current_continue_url

        except Exception as e:
            log.error(f"[add_phone] 异常: {e}")
            return None, None

    max_retry = int(cfg.get("sms_max_retry", 3))

    for retry in range(1, max_retry + 1):
        log.info(f"[add_phone] 第 {retry}/{max_retry} 次尝试绑定手机号")

        buy_result = _5sim_buy_number(cfg)
        if not buy_result:
            log.warning(f"[add_phone] 购买号码失败，重试...")
            continue

        order_id = buy_result["order_id"]
        phone = buy_result["phone"]

        if not phone.startswith("+"):
            phone = f"+{phone}"

        try:
            send_resp = s2.post(
                "https://auth.openai.com/api/accounts/add-phone/send",
                json={"phone_number": phone, "channel": phone_channel},
                headers={
                    "referer": "https://auth.openai.com/add-phone",
                    "origin": "https://auth.openai.com",
                },
            )
            log.info(
                f"[add_phone] send: {send_resp.status_code} - {send_resp.text[:300]}"
            )

            if send_resp.status_code not in (200, 201):
                resp_text = send_resp.text[:500]

                if "invalid_state" in resp_text:
                    log.warning("[add_phone] session 失效，重新获取 add-phone 页面后重试...")
                    try:
                        s2.get(current_continue_url or "https://auth.openai.com/add-phone", headers={
                            "referer": "https://auth.openai.com/",
                            "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                        }, allow_redirects=True)
                    except Exception:
                        pass
                    time.sleep(2)
                    send_resp = s2.post(
                        "https://auth.openai.com/api/accounts/add-phone/send",
                        json={"phone_number": phone, "channel": phone_channel},
                        headers={
                            "referer": "https://auth.openai.com/add-phone",
                            "origin": "https://auth.openai.com",
                        },
                    )
                    log.info(f"[add_phone] 重试 send: {send_resp.status_code} - {send_resp.text[:200]}")
                    if send_resp.status_code in (200, 201):
                        resp_text = send_resp.text[:500]
                    else:
                        log.error(f"[add_phone] 重试后仍失败: {send_resp.status_code}")
                        _5sim_cancel_order(order_id, cfg)
                        continue

                rejected = any(
                    kw in resp_text
                    for kw in [
                        "fraud_guard",
                        "voip_phone_number_disallowed",
                        "landline_disallowed",
                        "unsupported_phone_number",
                        "phone_already_in_use",
                        "invalid_phone_number",
                    ]
                )
                if rejected:
                    log.warning(
                        f"[add_phone] 号码被拒: {phone}, 原因: {resp_text[:200]}"
                    )
                    _5sim_cancel_order(order_id, cfg)
                    continue
                else:
                    log.error(f"[add_phone] 提交手机号失败: {send_resp.status_code}")
                    _5sim_cancel_order(order_id, cfg)
                    continue

            send_step = json.loads(send_resp.text) if send_resp.text else {}
            next_continue_url = send_step.get("continue_url", "")
            next_page_type = send_step.get("page", {}).get("type", "")

            if next_page_type == "add_phone" and not next_continue_url:
                log.warning(f"[add_phone] 提交后仍返回 add_phone，号码可能被拒")
                _5sim_cancel_order(order_id, cfg)
                continue

            log.info(f"[add_phone] 提交成功，等待 OTP... page_type={next_page_type}")

            s2.get(
                "https://auth.openai.com/api/accounts/phone-otp/send",
                headers={"referer": "https://auth.openai.com/phone-verification"},
            )

            sms_code = _5sim_poll_sms(order_id, cfg)
            if not sms_code:
                log.warning(f"[add_phone] 未收到短信验证码")
                _5sim_cancel_order(order_id, cfg)
                continue

            otp_success = False
            for attempt in range(1, otp_max_attempt + 1):
                if attempt > 1:
                    log.info(f"[add_phone] OTP 验证第 {attempt} 次重试...")
                    try:
                        resend_resp = s2.post(
                            "https://auth.openai.com/api/accounts/phone-otp/resend",
                            headers={
                                "referer": "https://auth.openai.com/phone-verification",
                                "origin": "https://auth.openai.com",
                            },
                        )
                        log.info(f"[add_phone] resend: {resend_resp.status_code}")
                        sms_code = _5sim_poll_sms(order_id, cfg)
                        if not sms_code:
                            log.warning(f"[add_phone] 重发后仍未收到验证码")
                            break
                    except Exception as e:
                        log.warning(f"[add_phone] resend 异常: {e}")
                        break

                validate_resp = s2.post(
                    "https://auth.openai.com/api/accounts/phone-otp/validate",
                    json={"code": sms_code},
                    headers={
                        "referer": "https://auth.openai.com/phone-verification",
                        "origin": "https://auth.openai.com",
                    },
                )
                log.info(
                    f"[add_phone] validate: {validate_resp.status_code} - {validate_resp.text[:300]}"
                )

                if validate_resp.status_code in (200, 201):
                    otp_success = True
                    _5sim_finish_order(order_id, cfg)
                    current_step = (
                        json.loads(validate_resp.text) if validate_resp.text else {}
                    )
                    current_continue_url = current_step.get("continue_url", "")
                    log.info(
                        f"[add_phone] 手机验证成功! page_type={current_step.get('page', {}).get('type', '')}"
                    )
                    break
                elif (
                    validate_resp.status_code == 401
                    or "wrong_phone_otp_code" in (validate_resp.text[:200])
                ):
                    log.warning(f"[add_phone] 验证码错误，重试...")
                    continue
                else:
                    log.error(f"[add_phone] 验证失败: {validate_resp.status_code}")
                    break

            if not otp_success:
                _5sim_cancel_order(order_id, cfg)
                if retry < max_retry:
                    log.info(f"[add_phone] 换号重试...")
                    continue
                else:
                    log.error(f"[add_phone] 所有重试均失败")
                    return None, None

            return current_step, current_continue_url

        except Exception as e:
            log.error(f"[add_phone] 异常: {e}")
            _5sim_cancel_order(order_id, cfg)
            if retry < max_retry:
                continue
            return None, None

    log.error(f"[add_phone] {max_retry} 次尝试均失败，放弃手机验证")
    return None, None


# ========== 验证码提取 ==========


def extract_verification_code(raw_mail: str) -> str:
    if not raw_mail:
        return None
    
    text = raw_mail.replace("\r\n", "\n").replace("\r", "\n")
    parts = text.split("\n\n", 1)
    body = parts[1] if len(parts) > 1 else text
    
    body = re.sub(r"<[^>]+>", " ", body)
    
    try:
        body = quopri.decodestring(body.encode()).decode("utf-8", errors="replace")
    except Exception:
        pass

    patterns = [
        r"code is (\d{6})",
        r"verification code[:\s]+(\d{6})",
        r"verification code.{0,500}?(\d{6})",
        r"code[:\s]+(\d{6})",
        r"\b(\d{6})\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, body, re.IGNORECASE | re.DOTALL)
        if match:
            return match.group(1) if match.lastindex else match.group(0)
    return None


# ========== 邮箱验证码获取 ==========


def get_oai_codes(email: str, cfg: dict, jwt: str = None) -> list:
    mode = cfg.get("email_mode", "worker")
    otp_timeout = int(os.environ.get("OTP_TIMEOUT", "120"))
    poll_interval = 3
    max_polls = otp_timeout // poll_interval

    if mode == "cloudmail":
        code = _get_code_cloudmail(email, cfg, max_polls, poll_interval, otp_timeout)
        return [code] if code else []
    else:
        return _get_codes_worker(email, jwt, cfg, max_polls, poll_interval, otp_timeout)


def _get_code_cloudmail(email, cfg, max_polls, poll_interval, otp_timeout):
    cm_url = cfg.get("cloudmail_url", "").rstrip("/")
    cm_token = cfg.get("cloudmail_token", "")
    regex = r"\b(\d{6})\b"

    log.info(f"正在通过 CloudMail 监听验证码 (目标: {email}, 超时: {otp_timeout}s)...")
    for i in range(max_polls):
        try:
            resp = std_requests.post(
                f"{cm_url}/api/public/emailList",
                headers={"Authorization": cm_token, "Content-Type": "application/json"},
                json={"toEmail": email, "type": 0},
                timeout=10,
            )
            if resp.status_code == 200:
                data = resp.json()
                emails = data.get("data") or []
                for msg in emails:
                    for field in ["subject", "text", "content"]:
                        text = msg.get(field) or ""
                        m = re.search(regex, text)
                        if m:
                            code = m.group(1)
                            log.info(
                                f"CloudMail 返回验证码: {code} (耗时: {(i + 1) * poll_interval}s)"
                            )
                            return code
        except Exception as e:
            log.warning(f"CloudMail 请求异常: {e}")
        if (i + 1) % 10 == 0:
            log.info(
                f"等待验证码中... 已等待 {(i + 1) * poll_interval}s / {otp_timeout}s"
            )
        time.sleep(poll_interval)
    return None


def _get_codes_worker(email, jwt, cfg, max_polls, poll_interval, otp_timeout):
    worker_url = cfg.get("worker_url")
    if not worker_url:
        raise ValueError("缺少 WORKER_URL 配置")
    if not jwt:
        log.warning("缺少 JWT，无法通过 Worker 获取验证码")
        return []

    worker_url = worker_url.rstrip("/")

    log.info(f"正在通过 Worker 监听验证码 (目标: {email}, 超时: {otp_timeout}s)...")
    for i in range(max_polls):
        try:
            resp = std_requests.get(
                f"{worker_url}/api/mails?limit=1&offset=0",
                headers={"Authorization": f"Bearer {jwt}"},
                timeout=10,
                proxies={"http": None, "https": None},
            )
            if resp.status_code == 200:
                try:
                    data = resp.json()
                    mails = (
                        data.get("results", [])
                        or data.get("data", [])
                        or data.get("list", [])
                    )

                    if mails:
                        codes = []
                        for mail_item in mails:
                            raw_mail = (
                                mail_item.get("raw")
                                or mail_item.get("source")
                                or mail_item.get("text")
                                or ""
                            )
                            code = extract_verification_code(raw_mail)
                            if code and code not in codes:
                                codes.append(code)
                        if codes:
                            log.info(
                                f"Worker 返回验证码: {codes} (耗时: {(i + 1) * poll_interval}s)"
                            )
                            return codes
                except Exception as e:
                    log.warning(f"解析邮件异常: {e}")
            elif resp.status_code == 401:
                log.warning("JWT 已过期或无效")
                break
        except Exception as e:
            log.warning(f"Worker 请求异常: {e}")

        if (i + 1) % 10 == 0:
            log.info(
                f"等待验证码中... 已等待 {(i + 1) * poll_interval}s / {otp_timeout}s"
            )
        time.sleep(poll_interval)
    return []


# ========== OAuth 工具 ==========


def _b64url_no_pad(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _sha256_b64url_no_pad(s: str) -> str:
    return _b64url_no_pad(hashlib.sha256(s.encode("ascii")).digest())


def _random_state(nbytes: int = 16) -> str:
    return secrets.token_urlsafe(nbytes)


def _pkce_verifier() -> str:
    return secrets.token_urlsafe(64)


def _parse_callback_url(callback_url: str) -> Dict[str, str]:
    candidate = callback_url.strip()
    if not candidate:
        return {"code": "", "state": "", "error": "", "error_description": ""}

    if "://" not in candidate:
        if candidate.startswith("?"):
            candidate = f"http://localhost{candidate}"
        elif any(ch in candidate for ch in "/?#") or ":" in candidate:
            candidate = f"http://{candidate}"
        elif "=" in candidate:
            candidate = f"http://localhost/?{candidate}"

    parsed = urllib.parse.urlparse(candidate)
    query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
    fragment = urllib.parse.parse_qs(parsed.fragment, keep_blank_values=True)

    for key, values in fragment.items():
        if key not in query or not query[key] or not (query[key][0] or "").strip():
            query[key] = values

    def get1(k: str) -> str:
        v = query.get(k, [""])
        return (v[0] or "").strip()

    code = get1("code")
    state = get1("state")
    error = get1("error")
    error_description = get1("error_description")

    if code and not state and "#" in code:
        code, state = code.split("#", 1)
    if not error and error_description:
        error, error_description = error_description, ""

    return {
        "code": code,
        "state": state,
        "error": error,
        "error_description": error_description,
    }


def _jwt_claims_no_verify(id_token: str) -> Dict[str, Any]:
    if not id_token or id_token.count(".") < 2:
        return {}
    payload_b64 = id_token.split(".")[1]
    pad = "=" * ((4 - (len(payload_b64) % 4)) % 4)
    try:
        payload = base64.urlsafe_b64decode((payload_b64 + pad).encode("ascii"))
        return json.loads(payload.decode("utf-8"))
    except Exception:
        return {}


def _to_int(v: Any) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def _post_form(
    url: str, data: Dict[str, str], timeout: int = 30, proxy: str = ""
) -> Dict[str, Any]:
    body = urllib.parse.urlencode(data).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
    )
    if proxy:
        handler = urllib.request.ProxyHandler({"http": proxy, "https": proxy})
        opener = urllib.request.build_opener(handler)
        do_req = opener.open
    else:
        do_req = urllib.request.urlopen
    try:
        with do_req(req, timeout=timeout) as resp:
            raw = resp.read()
            if resp.status != 200:
                raise RuntimeError(
                    f"token exchange failed: {resp.status}: {raw.decode('utf-8', 'replace')}"
                )
            return json.loads(raw.decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        raise RuntimeError(
            f"token exchange failed: {exc.code}: {raw.decode('utf-8', 'replace')}"
        ) from exc


@dataclass(frozen=True)
class OAuthStart:
    auth_url: str
    state: str
    code_verifier: str
    redirect_uri: str


def generate_oauth_url(
    *, redirect_uri: str = DEFAULT_REDIRECT_URI, scope: str = DEFAULT_SCOPE
) -> OAuthStart:
    state = _random_state()
    code_verifier = _pkce_verifier()
    code_challenge = _sha256_b64url_no_pad(code_verifier)

    params = {
        "client_id": CLIENT_ID,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "scope": scope,
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "prompt": "login",
        "id_token_add_organizations": "true",
        "codex_cli_simplified_flow": "true",
    }
    auth_url = f"{AUTH_URL}?{urllib.parse.urlencode(params)}"
    return OAuthStart(
        auth_url=auth_url,
        state=state,
        code_verifier=code_verifier,
        redirect_uri=redirect_uri,
    )


def submit_callback_url(
    *,
    callback_url: str,
    expected_state: str,
    code_verifier: str,
    redirect_uri: str = DEFAULT_REDIRECT_URI,
    proxy: str = "",
    password: str = "",
) -> str:
    cb = _parse_callback_url(callback_url)
    if cb["error"]:
        raise RuntimeError(
            f"oauth error: {cb['error']}: {cb['error_description']}".strip()
        )
    if not cb["code"]:
        raise ValueError("callback url missing ?code=")
    if not cb["state"]:
        raise ValueError("callback url missing ?state=")
    if cb["state"] != expected_state:
        raise ValueError("state mismatch")

    token_resp = _post_form(
        TOKEN_URL,
        {
            "grant_type": "authorization_code",
            "client_id": CLIENT_ID,
            "code": cb["code"],
            "redirect_uri": redirect_uri,
            "code_verifier": code_verifier,
        },
        proxy=proxy,
    )

    access_token = (token_resp.get("access_token") or "").strip()
    refresh_token = (token_resp.get("refresh_token") or "").strip()
    id_token = (token_resp.get("id_token") or "").strip()
    expires_in = _to_int(token_resp.get("expires_in"))

    claims = _jwt_claims_no_verify(id_token)
    email = str(claims.get("email") or "").strip()
    auth_claims = claims.get("https://api.openai.com/auth") or {}
    account_id = str(auth_claims.get("chatgpt_account_id") or "").strip()

    now = int(time.time())
    expired_rfc3339 = time.strftime(
        "%Y-%m-%dT%H:%M:%SZ", time.gmtime(now + max(expires_in, 0))
    )
    now_rfc3339 = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))

    config = {
        "id_token": id_token,
        "access_token": access_token,
        "refresh_token": refresh_token,
        "account_id": account_id,
        "last_refresh": now_rfc3339,
        "email": email,
        "type": "codex",
        "expired": expired_rfc3339,
    }
    if password:
        config["password"] = password
    return json.dumps(config, ensure_ascii=False, separators=(",", ":"))


# ========== Sentinel ==========


def generate_sentinel_token(
    did: str, user_agent: str, flow: str, referer: str, proxy: str = ""
) -> str:
    cli_input = json.dumps(
        {
            "device_id": did,
            "user_agent": user_agent,
            "flow": flow,
            "referer": referer,
            "proxy": proxy,
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
    )

    cli_path = "/opt/gptreg/sentinel-cli"
    if not os.path.exists(cli_path):
        cli_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "sentinel-cli"
        )
        if os.name == "nt" and not cli_path.endswith(".exe"):
            cli_path += ".exe"

    try:
        proc = subprocess.run(
            [cli_path, cli_input], capture_output=True, text=True, check=True
        )
        return proc.stdout.strip()
    except subprocess.CalledProcessError as e:
        log.error(f"sentinel-go 执行失败: {e.stderr}")
        return None
    except FileNotFoundError:
        log.error(f"找不到执行文件 {cli_path}")
        return None


# ========== 回调提取 / 重定向 ==========


def _extract_callback_from_url(raw_url: str) -> str:
    if not raw_url:
        return ""
    parsed = urllib.parse.urlparse(raw_url)
    query = urllib.parse.parse_qs(parsed.query)
    if query.get("code") and query.get("state"):
        return raw_url
    return ""


def _extract_callback_from_json(body: str) -> str:
    try:
        data = json.loads(body)
        for key in ["continue_url", "url", "redirect_url", "callback_url"]:
            url = data.get(key, "")
            cb = _extract_callback_from_url(url)
            if cb:
                return cb
        page = data.get("page", {})
        payload = page.get("payload", {})
        url = payload.get("url", "")
        cb = _extract_callback_from_url(url)
        if cb:
            return cb
    except Exception:
        pass
    return ""


def _extract_workspace_id_from_html(html: str) -> str:
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


def _extract_state_from_url(raw_url: str) -> str:
    if not raw_url:
        return ""
    parsed = urllib.parse.urlparse(raw_url)
    state = urllib.parse.parse_qs(parsed.query).get("state", [""])[0]
    if state:
        return state
    m = re.search(r'state=([^&"\'\\s]+)', raw_url)
    return m.group(1) if m else ""


def _follow_redirect_chain(s, start_url: str, referer: str, max_hops: int = 8) -> str:
    current_url = start_url
    current_referer = referer

    for attempt in range(max_hops):
        cb = _extract_callback_from_url(current_url)
        if cb:
            return cb

        log.info(f"redirect hop #{attempt}: {current_url[:80]}")
        try:
            resp = s.get(
                current_url,
                headers={
                    "referer": current_referer,
                    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                },
                allow_redirects=False,
            )
        except Exception as e:
            log.warning(f"redirect hop 异常: {e}")
            break

        cb = _extract_callback_from_url(resp.headers.get("Location", ""))
        if cb:
            return cb

        cb = _extract_callback_from_json(resp.text[:2000])
        if cb:
            return cb

        next_url = resp.headers.get("Location", "")
        if not next_url:
            step = {}
            try:
                step = json.loads(resp.text)
            except Exception:
                pass
            next_url = (
                step.get("continue_url", "")
                or step.get("page", {}).get("payload", {}).get("url", "")
                or step.get("url", "")
            )

        if not next_url or next_url == current_url:
            break

        current_referer = current_url
        current_url = next_url

    return ""


def _resume_and_capture_callback(s, state: str, max_attempts: int = 8) -> str:
    for attempt in range(max_attempts):
        resume_url = f"https://auth0.openai.com/authorize/resume?state={urllib.parse.quote(state)}"
        log.info(f"resume attempt #{attempt}: {resume_url[:80]}")
        try:
            resp = s.get(
                resume_url,
                headers={
                    "referer": "https://auth0.openai.com",
                    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                },
                allow_redirects=False,
            )
        except Exception as e:
            log.warning(f"resume 异常: {e}")
            continue

        cb = _extract_callback_from_url(resp.headers.get("Location", ""))
        if cb:
            return cb

        cb = _extract_callback_from_json(resp.text[:2000])
        if cb:
            return cb

        new_state = _extract_state_from_url(resp.headers.get("Location", ""))
        if new_state:
            state = new_state

    return ""


# ========== 上传 ==========


def upload_auth_file(filepath: str, upload_url: str, upload_token: str):
    upload_url = upload_url.rstrip("/")
    url = f"{upload_url}/v0/management/auth-files"
    boundary = f"----WebKitFormBoundary{secrets.token_hex(8)}"
    filename = os.path.basename(filepath)
    with open(filepath, "rb") as f:
        file_data = f.read()
    body = (
        (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
            f"Content-Type: application/json\r\n\r\n"
        ).encode()
        + file_data
        + f"\r\n--{boundary}--\r\n".encode()
    )
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {upload_token}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Accept": "application/json, text/plain, */*",
        },
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return resp.status, resp.read().decode("utf-8", "replace")


def remove_account(email: str):
    account_file = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "accounts.txt"
    )
    if os.path.exists(account_file):
        with open(account_file, "r", encoding="utf-8") as f:
            lines = f.readlines()
        with open(account_file, "w", encoding="utf-8") as f:
            for line in lines:
                if not line.strip().startswith(email + "----"):
                    f.write(line)
        log.info(f"已从 accounts.txt 删除账号: {email}")

    phone_file = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "accounts_phone.txt"
    )
    if os.path.exists(phone_file):
        with open(phone_file, "r", encoding="utf-8") as f:
            lines = f.readlines()
        with open(phone_file, "w", encoding="utf-8") as f:
            for line in lines:
                parts = line.strip().split("----")
                if len(parts) >= 5 and parts[4] == email:
                    continue
                f.write(line)
        log.info(f"已从 accounts_phone.txt 删除账号: {email}")
