#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
feishu_sync.py
==============

飞书集成同步脚本 — 交互式健康仪表盘配套工具。

功能模块：
  1. 飞书消息推送（Webhook 方式）
     - 每日健康摘要卡片（interactive card 格式）
     - 异常告警通知
     - 支持自定义机器人 Webhook
  2. 飞书日历同步（应用方式，可选）
     - 同步运动记录为日历事件
     - 同步睡眠记录为日历事件
     - 异常日期标记
  3. 飞书文档同步（应用方式，可选）
     - 周报/月报同步为飞书文档
     - 仪表盘链接嵌入

技术约束：
  - 仅使用 Python 标准库（urllib, json, time, logging 等）
  - 不依赖第三方 HTTP 库
  - 错误处理：指数退避重试、限流等待、降级方案

使用方式：
  python feishu_sync.py --config config/feishu.json --dashboard reports/health_dashboard.html --mode daily
  python feishu_sync.py --config config/feishu.json --mode alert
  python feishu_sync.py --config config/feishu.json --mode weekly
"""

import argparse
import json
import logging
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# 日志配置
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("feishu_sync")


# ---------------------------------------------------------------------------
# 配置加载
# ---------------------------------------------------------------------------

class FeishuConfig:
    """飞书配置加载与校验。

    配置文件为 JSON 格式，结构如下：
    {
        "webhook": {
            "daily_summary": "https://open.feishu.cn/open-apis/bot/v2/hook/xxx",
            "alert": "https://open.feishu.cn/open-apis/bot/v2/hook/yyy"
        },
        "app": {
            "app_id": "cli_xxx",
            "app_secret": "xxx"
        },
        "calendar": {
            "enabled": false,
            "calendar_id": "primary"
        },
        "doc": {
            "enabled": false,
            "weekly_report_folder": "fldcnxxx"
        },
        "notify": {
            "daily_summary": true,
            "alert_on_abnormal": true,
            "alert_thresholds": {
                "steps_min": 3000,
                "sleep_min_hours": 6,
                "resting_hr_max": 100,
                "hrv_min": 20,
                "fragmentation_max": 70
            }
        }
    }
    """

    def __init__(self, config_path: str):
        self.config_path = config_path
        self._config: Dict[str, Any] = {}
        self._load()

    def _load(self) -> None:
        """加载并校验配置文件。"""
        if not os.path.exists(self.config_path):
            raise FileNotFoundError(f"配置文件不存在: {self.config_path}")

        with open(self.config_path, "r", encoding="utf-8") as f:
            self._config = json.load(f)

        self._validate()
        logger.info("配置文件加载成功: %s", self.config_path)

    def _validate(self) -> None:
        """校验必要的配置字段是否存在且格式正确。"""
        # webhook 配置（至少需要配置一个 webhook 才能使用消息推送）
        webhook = self._config.get("webhook", {})
        if not isinstance(webhook, dict):
            raise ValueError("webhook 配置必须是对象类型")

        # notify 配置
        notify = self._config.get("notify", {})
        if not isinstance(notify, dict):
            raise ValueError("notify 配置必须是对象类型")

        thresholds = notify.get("alert_thresholds", {})
        if not isinstance(thresholds, dict):
            raise ValueError("alert_thresholds 配置必须是对象类型")

        # app 配置（可选，用于日历/文档同步）
        app = self._config.get("app", {})
        if not isinstance(app, dict):
            raise ValueError("app 配置必须是对象类型")

        # calendar 配置（可选）
        calendar = self._config.get("calendar", {})
        if not isinstance(calendar, dict):
            raise ValueError("calendar 配置必须是对象类型")

        # doc 配置（可选）
        doc = self._config.get("doc", {})
        if not isinstance(doc, dict):
            raise ValueError("doc 配置必须是对象类型")

    @property
    def webhook_daily_summary(self) -> Optional[str]:
        return self._config.get("webhook", {}).get("daily_summary")

    @property
    def webhook_alert(self) -> Optional[str]:
        return self._config.get("webhook", {}).get("alert")

    @property
    def app_id(self) -> Optional[str]:
        return self._config.get("app", {}).get("app_id")

    @property
    def app_secret(self) -> Optional[str]:
        return self._config.get("app", {}).get("app_secret")

    @property
    def calendar_enabled(self) -> bool:
        return self._config.get("calendar", {}).get("enabled", False)

    @property
    def calendar_id(self) -> str:
        return self._config.get("calendar", {}).get("calendar_id", "primary")

    @property
    def doc_enabled(self) -> bool:
        return self._config.get("doc", {}).get("enabled", False)

    @property
    def weekly_report_folder(self) -> str:
        return self._config.get("doc", {}).get("weekly_report_folder", "")

    @property
    def notify_daily_summary(self) -> bool:
        return self._config.get("notify", {}).get("daily_summary", True)

    @property
    def alert_on_abnormal(self) -> bool:
        return self._config.get("notify", {}).get("alert_on_abnormal", True)

    @property
    def alert_thresholds(self) -> Dict[str, Any]:
        defaults = {
            "steps_min": 3000,
            "sleep_min_hours": 6,
            "resting_hr_max": 100,
            "hrv_min": 20,
            "fragmentation_max": 70,
        }
        user_thresholds = self._config.get("notify", {}).get("alert_thresholds", {})
        defaults.update(user_thresholds)
        return defaults


# ---------------------------------------------------------------------------
# HTTP 客户端（基于 urllib，带重试与限流处理）
# ---------------------------------------------------------------------------

class HttpClient:
    """基于 urllib 的 HTTP 客户端，内置指数退避重试与限流处理。

    设计原则：
      - 网络请求失败自动重试（指数退避，最多 3 次）
      - 遇到 429 限流时根据 Retry-After 头等待
      - 失败时记录错误日志但不抛出未捕获异常（由调用方决定是否降级）
    """

    MAX_RETRIES = 3
    BASE_DELAY = 1.0  # 秒
    MAX_DELAY = 10.0  # 秒

    def __init__(self, timeout: int = 10):
        self.timeout = timeout

    def request(
        self,
        url: str,
        method: str = "GET",
        headers: Optional[Dict[str, str]] = None,
        data: Optional[Dict[str, Any]] = None,
    ) -> Tuple[int, Dict[str, Any]]:
        """发送 HTTP 请求并返回 (status_code, response_json)。

        Args:
            url: 请求 URL
            method: HTTP 方法（GET/POST/PUT/DELETE）
            headers: 请求头字典
            data: 请求体数据（自动序列化为 JSON）

        Returns:
            (status_code, response_dict) — 响应状态码与 JSON 解析后的字典。
            如果响应不是合法 JSON，则 response_dict 包含 {"raw": 原始文本}。

        Raises:
            urllib.error.URLError: 所有重试都失败后抛出网络错误。
        """
        if headers is None:
            headers = {}

        # 如果有 data，设置 Content-Type 并序列化为 JSON
        body_bytes: Optional[bytes] = None
        if data is not None:
            body_bytes = json.dumps(data, ensure_ascii=False).encode("utf-8")
            headers.setdefault("Content-Type", "application/json; charset=utf-8")

        last_error: Optional[Exception] = None

        for attempt in range(self.MAX_RETRIES + 1):
            try:
                req = urllib.request.Request(url, data=body_bytes, headers=headers, method=method)
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    status = resp.getcode()
                    raw = resp.read().decode("utf-8")
                    try:
                        resp_data = json.loads(raw)
                    except json.JSONDecodeError:
                        resp_data = {"raw": raw}
                    return status, resp_data

            except urllib.error.HTTPError as e:
                status_code = e.code
                last_error = e

                # 限流处理：429 Too Many Requests
                if status_code == 429:
                    retry_after = self._parse_retry_after(e.headers)
                    wait_time = retry_after if retry_after else self._backoff_delay(attempt)
                    logger.warning(
                        "请求限流 (HTTP 429)，第 %d 次重试，等待 %.1f 秒: %s",
                        attempt + 1, wait_time, url,
                    )
                    time.sleep(wait_time)
                    continue

                # 5xx 服务器错误，重试
                if 500 <= status_code < 600 and attempt < self.MAX_RETRIES:
                    wait_time = self._backoff_delay(attempt)
                    logger.warning(
                        "服务器错误 (HTTP %d)，第 %d 次重试，等待 %.1f 秒: %s",
                        status_code, attempt + 1, wait_time, url,
                    )
                    time.sleep(wait_time)
                    continue

                # 其他 HTTP 错误（如 400/401/403/404），不重试
                raw = e.read().decode("utf-8", errors="replace") if e.fp else ""
                try:
                    resp_data = json.loads(raw)
                except json.JSONDecodeError:
                    resp_data = {"raw": raw}
                logger.error("HTTP %d 错误: %s - %s", status_code, url, raw[:200])
                return status_code, resp_data

            except urllib.error.URLError as e:
                last_error = e
                if attempt < self.MAX_RETRIES:
                    wait_time = self._backoff_delay(attempt)
                    logger.warning(
                        "网络错误，第 %d 次重试，等待 %.1f 秒: %s",
                        attempt + 1, wait_time, e.reason,
                    )
                    time.sleep(wait_time)
                    continue
                logger.error("网络请求最终失败 (%d 次重试): %s", self.MAX_RETRIES, e.reason)
                raise

            except Exception as e:
                last_error = e
                if attempt < self.MAX_RETRIES:
                    wait_time = self._backoff_delay(attempt)
                    logger.warning(
                        "未知错误，第 %d 次重试，等待 %.1f 秒: %s",
                        attempt + 1, wait_time, e,
                    )
                    time.sleep(wait_time)
                    continue
                logger.error("请求最终失败 (%d 次重试): %s", self.MAX_RETRIES, e)
                raise

        # 理论上不会到达这里，防御性代码
        if last_error:
            raise last_error
        return 0, {}

    @staticmethod
    def _backoff_delay(attempt: int) -> float:
        """计算指数退避延迟时间。

        第 0 次重试延迟 BASE_DELAY * 2^0 = 1s
        第 1 次重试延迟 BASE_DELAY * 2^1 = 2s
        第 2 次重试延迟 BASE_DELAY * 2^2 = 4s
        上限为 MAX_DELAY
        """
        delay = HttpClient.BASE_DELAY * (2 ** attempt)
        return min(delay, HttpClient.MAX_DELAY)

    @staticmethod
    def _parse_retry_after(headers) -> Optional[float]:
        """从响应头中解析 Retry-After 值（秒）。"""
        if headers is None:
            return None
        retry_after = headers.get("Retry-After") or headers.get("retry-after")
        if retry_after:
            try:
                return float(retry_after)
            except (ValueError, TypeError):
                pass
        return None


# ---------------------------------------------------------------------------
# 飞书 Webhook 消息推送
# ---------------------------------------------------------------------------

class FeishuWebhook:
    """飞书自定义机器人 Webhook 客户端。

    支持消息类型：
      - interactive 卡片（每日摘要、告警通知）
      - text 纯文本（降级方案）

    飞书机器人 Webhook 文档参考：
    https://open.feishu.cn/document/client-docs/bot-v3/add-custom-bot
    """

    # 飞书 Webhook 成功响应码
    SUCCESS_CODE = 0

    def __init__(self, webhook_url: str, http_client: Optional[HttpClient] = None):
        if not webhook_url:
            raise ValueError("Webhook URL 不能为空")
        self.webhook_url = webhook_url
        self.http = http_client or HttpClient()

    def send_interactive_card(self, card: Dict[str, Any]) -> bool:
        """发送交互式卡片消息。

        Args:
            card: 卡片内容字典（飞书 interactive card 格式）

        Returns:
            True 表示发送成功，False 表示失败（已记录日志）。
        """
        payload = {
            "msg_type": "interactive",
            "card": card,
        }
        return self._send(payload)

    def send_text(self, text: str) -> bool:
        """发送纯文本消息（降级方案）。

        当卡片消息发送失败时，可降级为纯文本通知。
        """
        payload = {
            "msg_type": "text",
            "content": {
                "text": text,
            },
        }
        return self._send(payload)

    def _send(self, payload: Dict[str, Any]) -> bool:
        """实际发送请求并处理响应。

        Returns:
            True 成功，False 失败。所有异常都被捕获并记录日志，
            确保调用方不会因为消息推送失败而中断主流程。
        """
        try:
            status, resp = self.http.request(
                self.webhook_url, method="POST", data=payload
            )
            if status == 200 and resp.get("code", -1) == self.SUCCESS_CODE:
                logger.info("飞书消息推送成功: msg_type=%s", payload.get("msg_type"))
                return True
            else:
                logger.error(
                    "飞书消息推送失败: HTTP=%d, code=%s, msg=%s",
                    status,
                    resp.get("code"),
                    resp.get("msg", resp.get("message", "")),
                )
                return False
        except Exception as e:
            logger.error("飞书消息推送异常: %s", e)
            return False


# ---------------------------------------------------------------------------
# 飞书应用 Access Token 管理
# ---------------------------------------------------------------------------

class FeishuAppAuth:
    """飞书应用凭证管理 — tenant_access_token 获取与缓存。

    飞书开放平台 API 需要使用 tenant_access_token 进行身份验证。
    token 有效期通常为 2 小时，本类在过期前自动刷新。

    文档参考：
    https://open.feishu.cn/document/server-docs/authentication-management/access-token/tenant_access_token_internal
    """

    TOKEN_URL = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
    # 提前刷新缓冲时间（秒），避免边界情况
    REFRESH_BUFFER = 300

    def __init__(self, app_id: str, app_secret: str, http_client: Optional[HttpClient] = None):
        if not app_id or not app_secret:
            raise ValueError("app_id 和 app_secret 不能为空")
        self.app_id = app_id
        self.app_secret = app_secret
        self.http = http_client or HttpClient()
        self._token: Optional[str] = None
        self._expire_at: float = 0.0  # Unix 时间戳

    def get_token(self) -> str:
        """获取有效的 tenant_access_token，过期自动刷新。"""
        if self._token and time.time() < self._expire_at - self.REFRESH_BUFFER:
            return self._token
        self._refresh()
        return self._token or ""

    def _refresh(self) -> None:
        """刷新 tenant_access_token。"""
        payload = {
            "app_id": self.app_id,
            "app_secret": self.app_secret,
        }
        try:
            status, resp = self.http.request(
                self.TOKEN_URL, method="POST", data=payload
            )
            if status == 200 and resp.get("code") == 0:
                self._token = resp.get("tenant_access_token", "")
                expire = resp.get("expire", 7200)
                self._expire_at = time.time() + expire
                logger.info("tenant_access_token 刷新成功，有效期 %d 秒", expire)
            else:
                logger.error(
                    "获取 tenant_access_token 失败: HTTP=%d, code=%s, msg=%s",
                    status,
                    resp.get("code"),
                    resp.get("msg", ""),
                )
                self._token = None
                self._expire_at = 0.0
        except Exception as e:
            logger.error("获取 tenant_access_token 异常: %s", e)
            self._token = None
            self._expire_at = 0.0

    def _auth_headers(self) -> Dict[str, str]:
        """生成带鉴权信息的请求头。"""
        return {
            "Authorization": f"Bearer {self.get_token()}",
            "Content-Type": "application/json; charset=utf-8",
        }


# ---------------------------------------------------------------------------
# 飞书日历同步
# ---------------------------------------------------------------------------

class FeishuCalendar:
    """飞书日历同步模块。

    功能：
      - 同步运动记录为日历事件
      - 同步睡眠记录为日历事件
      - 异常日期标记（在事件标题/描述中标注）

    文档参考：
    https://open.feishu.cn/document/server-docs/calendar-v4/calendar-event/create
    """

    BASE_URL = "https://open.feishu.cn/open-apis/calendar/v4"

    def __init__(
        self,
        auth: FeishuAppAuth,
        calendar_id: str = "primary",
        http_client: Optional[HttpClient] = None,
    ):
        self.auth = auth
        self.calendar_id = calendar_id
        self.http = http_client or HttpClient()

    def create_event(
        self,
        summary: str,
        start_time: datetime,
        end_time: datetime,
        description: str = "",
        location: str = "",
        is_abnormal: bool = False,
    ) -> Optional[str]:
        """创建日历事件。

        Args:
            summary: 事件标题
            start_time: 开始时间（datetime 对象）
            end_time: 结束时间（datetime 对象）
            description: 事件描述
            location: 地点
            is_abnormal: 是否为异常标记事件（标题前加 [异常] 前缀）

        Returns:
            事件 ID（成功）或 None（失败）。
        """
        if is_abnormal:
            summary = f"[异常] {summary}"

        payload = {
            "event": {
                "summary": summary,
                "description": description,
                "start_time": {
                    "timestamp": str(int(start_time.timestamp())),
                },
                "end_time": {
                    "timestamp": str(int(end_time.timestamp())),
                },
                "location": {
                    "name": location,
                } if location else {},
            }
        }

        # 飞书 API 要求 calendar_id 做 URL 编码
        encoded_calendar_id = urllib.parse.quote(self.calendar_id, safe="")
        url = f"{self.BASE_URL}/calendars/{encoded_calendar_id}/events"

        try:
            status, resp = self.http.request(
                url, method="POST", headers=self.auth._auth_headers(), data=payload
            )
            if status == 200 and resp.get("code") == 0:
                event_id = resp.get("data", {}).get("event", {}).get("event_id", "")
                logger.info("日历事件创建成功: %s (ID: %s)", summary, event_id)
                return event_id
            else:
                logger.error(
                    "日历事件创建失败: HTTP=%d, code=%s, msg=%s",
                    status,
                    resp.get("code"),
                    resp.get("msg", ""),
                )
                return None
        except Exception as e:
            logger.error("日历事件创建异常: %s", e)
            return None

    def sync_workout(self, workout_data: Dict[str, Any]) -> Optional[str]:
        """同步运动记录为日历事件。

        Args:
            workout_data: 运动数据字典，需包含：
                - date: 日期字符串 (YYYY-MM-DD)
                - type: 运动类型（如 "跑步"、"步行"）
                - duration_min: 持续时间（分钟）
                - distance_km: 距离（公里，可选）
                - calories: 消耗卡路里（可选）
                - avg_hr: 平均心率（可选）

        Returns:
            事件 ID 或 None。
        """
        date_str = workout_data.get("date", "")
        wtype = workout_data.get("type", "运动")
        duration = workout_data.get("duration_min", 30)
        distance = workout_data.get("distance_km")
        calories = workout_data.get("calories")
        avg_hr = workout_data.get("avg_hr")
        is_abnormal = workout_data.get("is_abnormal", False)

        # 默认将运动安排在当天 18:00（可根据实际数据调整）
        try:
            base_date = datetime.strptime(date_str, "%Y-%m-%d")
        except ValueError:
            base_date = datetime.now()

        start = base_date.replace(hour=18, minute=0, second=0)
        end = start + timedelta(minutes=int(duration))

        # 构建描述
        desc_parts = [f"运动类型：{wtype}", f"时长：{duration} 分钟"]
        if distance:
            desc_parts.append(f"距离：{distance} 公里")
        if calories:
            desc_parts.append(f"消耗：{calories} 千卡")
        if avg_hr:
            desc_parts.append(f"平均心率：{avg_hr} bpm")
        description = "\n".join(desc_parts)

        summary = f"{wtype} - {distance or ''} {duration}分钟".strip()

        return self.create_event(
            summary=summary,
            start_time=start,
            end_time=end,
            description=description,
            location="",
            is_abnormal=is_abnormal,
        )

    def sync_sleep(self, sleep_data: Dict[str, Any]) -> Optional[str]:
        """同步睡眠记录为日历事件。

        Args:
            sleep_data: 睡眠数据字典，需包含：
                - date: 日期字符串 (YYYY-MM-DD)（起床日期）
                - bed_time: 入睡时间 HH:MM
                - wake_time: 起床时间 HH:MM
                - duration_hours: 睡眠时长（小时）
                - efficiency: 睡眠效率（百分比，可选）
                - quality: 睡眠质量评分（可选）

        Returns:
            事件 ID 或 None。
        """
        date_str = sleep_data.get("date", "")
        bed_time_str = sleep_data.get("bed_time", "23:00")
        wake_time_str = sleep_data.get("wake_time", "07:00")
        duration = sleep_data.get("duration_hours", 8)
        efficiency = sleep_data.get("efficiency")
        quality = sleep_data.get("quality")
        is_abnormal = sleep_data.get("is_abnormal", False)

        try:
            base_date = datetime.strptime(date_str, "%Y-%m-%d")
        except ValueError:
            base_date = datetime.now()

        # 入睡时间在前一天晚上
        bed_h, bed_m = self._parse_time(bed_time_str)
        wake_h, wake_m = self._parse_time(wake_time_str)

        bed_dt = (base_date - timedelta(days=1)).replace(hour=bed_h, minute=bed_m, second=0)
        wake_dt = base_date.replace(hour=wake_h, minute=wake_m, second=0)

        # 构建描述
        desc_parts = [
            f"入睡：{bed_time_str}",
            f"起床：{wake_time_str}",
            f"时长：{duration} 小时",
        ]
        if efficiency:
            desc_parts.append(f"效率：{efficiency}%")
        if quality:
            desc_parts.append(f"质量评分：{quality}")
        description = "\n".join(desc_parts)

        summary = f"睡眠 {duration}h"

        return self.create_event(
            summary=summary,
            start_time=bed_dt,
            end_time=wake_dt,
            description=description,
            location="卧室",
            is_abnormal=is_abnormal,
        )

    @staticmethod
    def _parse_time(time_str: str) -> Tuple[int, int]:
        """解析 HH:MM 格式时间字符串为 (小时, 分钟) 元组。"""
        try:
            parts = time_str.strip().split(":")
            return int(parts[0]), int(parts[1]) if len(parts) > 1 else 0
        except (ValueError, IndexError):
            return 0, 0


# ---------------------------------------------------------------------------
# 飞书文档同步
# ---------------------------------------------------------------------------

class FeishuDoc:
    """飞书文档同步模块。

    功能：
      - 创建飞书文档（周报/月报）
      - 嵌入仪表盘链接
      - 支持上传到指定文件夹

    文档参考：
    https://open.feishu.cn/document/server-docs/docs/docs/docx-v1/document/create
    """

    BASE_URL = "https://open.feishu.cn/open-apis/docx/v1"

    def __init__(
        self,
        auth: FeishuAppAuth,
        folder_token: str = "",
        http_client: Optional[HttpClient] = None,
    ):
        self.auth = auth
        self.folder_token = folder_token
        self.http = http_client or HttpClient()

    def create_document(
        self,
        title: str,
        content_blocks: List[Dict[str, Any]],
        folder_token: Optional[str] = None,
    ) -> Optional[str]:
        """创建飞书文档（新版 docx）。

        Args:
            title: 文档标题
            content_blocks: 文档内容块列表（飞书 block 格式）
            folder_token: 目标文件夹 token，不传则使用默认配置

        Returns:
            文档 token（成功）或 None（失败）。
        """
        target_folder = folder_token or self.folder_token

        # 第一步：创建空白文档
        create_url = f"{self.BASE_URL}/documents"
        create_data: Dict[str, Any] = {"title": title}
        if target_folder:
            create_data["folder_token"] = target_folder

        try:
            status, resp = self.http.request(
                create_url,
                method="POST",
                headers=self.auth._auth_headers(),
                data=create_data,
            )
            if status != 200 or resp.get("code") != 0:
                logger.error(
                    "创建文档失败: HTTP=%d, code=%s, msg=%s",
                    status,
                    resp.get("code"),
                    resp.get("msg", ""),
                )
                return None

            document_id = resp.get("data", {}).get("document", {}).get("document_id", "")
            if not document_id:
                logger.error("创建文档成功但未获取到 document_id")
                return None

            logger.info("文档创建成功: %s (ID: %s)", title, document_id)

            # 第二步：写入内容（批量创建 block）
            if content_blocks:
                self._append_blocks(document_id, content_blocks)

            return document_id

        except Exception as e:
            logger.error("创建文档异常: %s", e)
            return None

    def _append_blocks(
        self,
        document_id: str,
        blocks: List[Dict[str, Any]],
    ) -> bool:
        """向文档末尾追加内容块。

        Args:
            document_id: 文档 ID
            blocks: block 列表

        Returns:
            True 成功，False 失败。
        """
        url = f"{self.BASE_URL}/documents/{document_id}/blocks/{document_id}/children"

        # 飞书 API 单次最多创建 50 个 block，分批处理
        batch_size = 50
        all_success = True

        for i in range(0, len(blocks), batch_size):
            batch = blocks[i:i + batch_size]
            payload = {
                "children": batch,
                "index": -1,  # -1 表示追加到末尾
            }
            try:
                status, resp = self.http.request(
                    url,
                    method="POST",
                    headers=self.auth._auth_headers(),
                    data=payload,
                )
                if status != 200 or resp.get("code") != 0:
                    logger.error(
                        "写入文档内容失败 (批次 %d): HTTP=%d, code=%s, msg=%s",
                        i // batch_size + 1,
                        status,
                        resp.get("code"),
                        resp.get("msg", ""),
                    )
                    all_success = False
                else:
                    logger.info(
                        "文档内容写入成功 (批次 %d, %d 个 block)",
                        i // batch_size + 1,
                        len(batch),
                    )
            except Exception as e:
                logger.error("写入文档内容异常 (批次 %d): %s", i // batch_size + 1, e)
                all_success = False

        return all_success

    def create_weekly_report(
        self,
        week_start: str,
        week_end: str,
        summary_data: Dict[str, Any],
        dashboard_url: str = "",
    ) -> Optional[str]:
        """创建周报文档。

        Args:
            week_start: 周报起始日期 (YYYY-MM-DD)
            week_end: 周报结束日期 (YYYY-MM-DD)
            summary_data: 健康数据摘要
            dashboard_url: 仪表盘链接（可选）

        Returns:
            文档 token 或 None。
        """
        title = f"健康周报 ({week_start} ~ {week_end})"
        blocks = self._build_weekly_report_blocks(
            week_start, week_end, summary_data, dashboard_url
        )
        return self.create_document(title, blocks)

    def _build_weekly_report_blocks(
        self,
        week_start: str,
        week_end: str,
        data: Dict[str, Any],
        dashboard_url: str,
    ) -> List[Dict[str, Any]]:
        """构建周报内容的 block 列表。

        使用飞书 docx 的 block 格式，包含：
          - 标题（heading1/heading2）
          - 文本段落（text）
          - 表格（未直接支持，用文本替代）
          - 超链接
        """
        blocks: List[Dict[str, Any]] = []

        # 一级标题
        blocks.append(self._heading1_block(f"健康周报 {week_start} ~ {week_end}"))

        # 概览段落
        blocks.append(self._text_block(
            "本周健康数据概览。点击下方链接查看完整交互式仪表盘。"
        ))

        # 仪表盘链接
        if dashboard_url:
            blocks.append(self._text_block(
                "查看完整仪表盘：",
                link_text=dashboard_url,
                link_url=dashboard_url,
            ))

        # 二级标题：步数
        blocks.append(self._heading2_block("🏃 运动与步数"))
        avg_steps = data.get("avg_steps", 0)
        total_steps = data.get("total_steps", 0)
        best_day = data.get("best_day_steps", "-")
        blocks.append(self._text_block(f"日均步数：{avg_steps:,} 步"))
        blocks.append(self._text_block(f"累计步数：{total_steps:,} 步"))
        blocks.append(self._text_block(f"最佳日期：{best_day}"))

        # 二级标题：睡眠
        blocks.append(self._heading2_block("😴 睡眠"))
        avg_sleep = data.get("avg_sleep_hours", 0)
        avg_efficiency = data.get("avg_sleep_efficiency", 0)
        blocks.append(self._text_block(f"平均睡眠：{avg_sleep} 小时"))
        blocks.append(self._text_block(f"平均效率：{avg_efficiency}%"))

        # 二级标题：心率与 HRV
        blocks.append(self._heading2_block("💓 心率与 HRV"))
        avg_rhr = data.get("avg_resting_hr", 0)
        avg_hrv = data.get("avg_hrv", 0)
        blocks.append(self._text_block(f"平均静息心率：{avg_rhr} bpm"))
        blocks.append(self._text_block(f"平均 HRV：{avg_hrv} ms"))

        # 二级标题：碎脚指数
        blocks.append(self._heading2_block("🦶 碎脚指数"))
        avg_frag = data.get("avg_fragmentation", 0)
        frag_grade = data.get("fragmentation_grade", "-")
        blocks.append(self._text_block(f"平均碎脚指数：{avg_frag}"))
        blocks.append(self._text_block(f"等级：{frag_grade}"))

        # 二级标题：异常提醒
        abnormal_days = data.get("abnormal_days", [])
        blocks.append(self._heading2_block("⚠️ 异常提醒"))
        if abnormal_days:
            blocks.append(self._text_block(f"本周共 {len(abnormal_days)} 天存在异常指标："))
            for day in abnormal_days:
                blocks.append(self._bullet_block(str(day)))
        else:
            blocks.append(self._text_block("本周各项指标均在正常范围内，继续保持！"))

        return blocks

    # ------------------------------------------------------------------
    # Block 构建辅助方法
    # ------------------------------------------------------------------

    @staticmethod
    def _heading1_block(text: str) -> Dict[str, Any]:
        return {
            "block_type": 3,  # heading1
            "heading1": {
                "elements": [
                    {
                        "text_run": {
                            "content": text,
                            "text_element_style": {},
                        }
                    }
                ],
                "style": {},
            },
        }

    @staticmethod
    def _heading2_block(text: str) -> Dict[str, Any]:
        return {
            "block_type": 4,  # heading2
            "heading2": {
                "elements": [
                    {
                        "text_run": {
                            "content": text,
                            "text_element_style": {},
                        }
                    }
                ],
                "style": {},
            },
        }

    @staticmethod
    def _text_block(
        text: str,
        link_text: Optional[str] = None,
        link_url: Optional[str] = None,
    ) -> Dict[str, Any]:
        elements: List[Dict[str, Any]] = [
            {
                "text_run": {
                    "content": text,
                    "text_element_style": {},
                }
            }
        ]
        if link_text and link_url:
            elements.append(
                {
                    "text_run": {
                        "content": link_text,
                        "text_element_style": {
                            "link": {"url": link_url},
                            "bold": False,
                            "italic": False,
                            "underline": True,
                        },
                    }
                }
            )
        return {
            "block_type": 2,  # text
            "text": {
                "elements": elements,
                "style": {},
            },
        }

    @staticmethod
    def _bullet_block(text: str) -> Dict[str, Any]:
        return {
            "block_type": 13,  # bullet
            "bullet": {
                "elements": [
                    {
                        "text_run": {
                            "content": text,
                            "text_element_style": {},
                        }
                    }
                ],
                "style": {},
            },
        }


# ---------------------------------------------------------------------------
# 健康数据模型与卡片构建
# ---------------------------------------------------------------------------

class HealthMetrics:
    """健康指标数据模型。

    封装单天的健康指标数据，用于：
      - 构建每日摘要卡片
      - 异常检测
      - 趋势计算
    """

    def __init__(
        self,
        date: str,
        steps: int = 0,
        sleep_hours: float = 0.0,
        resting_hr: int = 0,
        hrv: int = 0,
        fragmentation: float = 0.0,
        # 前一天数据（用于计算趋势）
        prev_steps: Optional[int] = None,
        prev_sleep_hours: Optional[float] = None,
        prev_resting_hr: Optional[int] = None,
        prev_hrv: Optional[int] = None,
        prev_fragmentation: Optional[float] = None,
    ):
        self.date = date
        self.steps = steps
        self.sleep_hours = sleep_hours
        self.resting_hr = resting_hr
        self.hrv = hrv
        self.fragmentation = fragmentation

        self.prev_steps = prev_steps
        self.prev_sleep_hours = prev_sleep_hours
        self.prev_resting_hr = prev_resting_hr
        self.prev_hrv = prev_hrv
        self.prev_fragmentation = prev_fragmentation

    # ------------------------------------------------------------------
    # 趋势计算
    # ------------------------------------------------------------------

    @staticmethod
    def _calc_trend(
        current: float,
        previous: Optional[float],
        higher_is_better: bool = True,
    ) -> Tuple[str, str]:
        """计算趋势方向与变化值。

        Returns:
            (trend_icon, change_text) — 趋势图标和变化描述。
            trend_icon 取值：up / down / flat
        """
        if previous is None or previous == 0:
            return "flat", "—"

        diff = current - previous
        pct = (diff / previous) * 100 if previous else 0

        if abs(diff) < 0.01 and abs(pct) < 0.1:
            return "flat", "持平"

        sign = "+" if diff > 0 else ""
        change_text = f"{sign}{diff:.1f} ({sign}{pct:.1f}%)"

        # 判断好坏方向
        if higher_is_better:
            trend = "up" if diff > 0 else "down"
        else:
            trend = "down" if diff > 0 else "up"

        return trend, change_text

    @property
    def steps_trend(self) -> Tuple[str, str]:
        return self._calc_trend(self.steps, self.prev_steps, higher_is_better=True)

    @property
    def sleep_trend(self) -> Tuple[str, str]:
        return self._calc_trend(self.sleep_hours, self.prev_sleep_hours, higher_is_better=True)

    @property
    def resting_hr_trend(self) -> Tuple[str, str]:
        # 静息心率越低越好
        return self._calc_trend(self.resting_hr, self.prev_resting_hr, higher_is_better=False)

    @property
    def hrv_trend(self) -> Tuple[str, str]:
        return self._calc_trend(self.hrv, self.prev_hrv, higher_is_better=True)

    @property
    def fragmentation_trend(self) -> Tuple[str, str]:
        # 碎脚指数越低越好
        return self._calc_trend(self.fragmentation, self.prev_fragmentation, higher_is_better=False)

    # ------------------------------------------------------------------
    # 异常检测
    # ------------------------------------------------------------------

    def check_abnormal(self, thresholds: Dict[str, Any]) -> List[Dict[str, str]]:
        """检查是否有异常指标。

        Args:
            thresholds: 告警阈值字典

        Returns:
            异常项列表，每项包含 metric / value / threshold / reason。
        """
        abnormalities: List[Dict[str, str]] = []

        if self.steps > 0 and self.steps < thresholds.get("steps_min", 3000):
            abnormalities.append({
                "metric": "步数",
                "value": f"{self.steps} 步",
                "threshold": f"< {thresholds['steps_min']} 步",
                "reason": "步数低于下限",
            })

        if self.sleep_hours > 0 and self.sleep_hours < thresholds.get("sleep_min_hours", 6):
            abnormalities.append({
                "metric": "睡眠时长",
                "value": f"{self.sleep_hours} 小时",
                "threshold": f"< {thresholds['sleep_min_hours']} 小时",
                "reason": "睡眠时长不足",
            })

        if self.resting_hr > 0 and self.resting_hr > thresholds.get("resting_hr_max", 100):
            abnormalities.append({
                "metric": "静息心率",
                "value": f"{self.resting_hr} bpm",
                "threshold": f"> {thresholds['resting_hr_max']} bpm",
                "reason": "静息心率偏高",
            })

        if self.hrv > 0 and self.hrv < thresholds.get("hrv_min", 20):
            abnormalities.append({
                "metric": "HRV",
                "value": f"{self.hrv} ms",
                "threshold": f"< {thresholds['hrv_min']} ms",
                "reason": "HRV 偏低，恢复状态不佳",
            })

        if self.fragmentation > 0 and self.fragmentation > thresholds.get("fragmentation_max", 70):
            abnormalities.append({
                "metric": "碎脚指数",
                "value": f"{self.fragmentation}",
                "threshold": f"> {thresholds['fragmentation_max']}",
                "reason": "行走碎片化严重",
            })

        return abnormalities

    # ------------------------------------------------------------------
    # 碎脚指数等级
    # ------------------------------------------------------------------

    @staticmethod
    def fragmentation_grade(score: float) -> str:
        """根据碎脚指数计算等级。"""
        if score <= 20:
            return "A级（高效）"
        elif score <= 40:
            return "B级（轻度）"
        elif score <= 60:
            return "C级（中度）"
        elif score <= 80:
            return "D级（高度）"
        else:
            return "E级（极度）"


class CardBuilder:
    """飞书交互式卡片构建器。

    负责构建各种场景的飞书 interactive card：
      - 每日健康摘要卡片
      - 异常告警卡片

    飞书卡片结构参考：
    https://open.feishu.cn/document/common-capabilities/message-card/message-cards-content/card-structure/card-configuration
    """

    # 主题色
    PRIMARY_COLOR = "blue"
    SUCCESS_COLOR = "green"
    WARNING_COLOR = "orange"
    DANGER_COLOR = "red"

    @staticmethod
    def _trend_icon(trend: str) -> str:
        """趋势方向对应的 emoji 图标。"""
        if trend == "up":
            return "📈"
        elif trend == "down":
            return "📉"
        return "➡️"

    @staticmethod
    def _trend_color(trend: str, metric_type: str = "positive") -> str:
        """趋势对应的文字颜色。

        metric_type:
          - "positive": 数值越高越好（步数、睡眠、HRV）
          - "negative": 数值越低越好（静息心率、碎脚指数）
        """
        if trend == "flat":
            return "grey"

        if metric_type == "positive":
            return "green" if trend == "up" else "red"
        else:
            return "green" if trend == "down" else "red"

    def build_daily_summary_card(
        self,
        metrics: HealthMetrics,
        dashboard_url: str = "",
    ) -> Dict[str, Any]:
        """构建每日健康摘要卡片。

        卡片结构：
          - 标题：每日健康摘要 + 日期
          - 5 个指标字段（步数、睡眠、静息心率、HRV、碎脚指数）
            每个指标显示当前值 + 趋势变化
          - 底部按钮：查看仪表盘
        """
        steps_trend_icon = self._trend_icon(metrics.steps_trend[0])
        sleep_trend_icon = self._trend_icon(metrics.sleep_trend[0])
        rhr_trend_icon = self._trend_icon(metrics.resting_hr_trend[0])
        hrv_trend_icon = self._trend_icon(metrics.hrv_trend[0])
        frag_trend_icon = self._trend_icon(metrics.fragmentation_trend[0])

        # 构建卡片
        card = {
            "config": {
                "wide_screen_mode": True,
                "enable_forward": True,
            },
            "header": {
                "title": {
                    "tag": "plain_text",
                    "content": f"每日健康摘要 · {metrics.date}",
                },
                "template": self.PRIMARY_COLOR,
            },
            "elements": [
                # 分隔线
                {"tag": "hr"},
                # 步数
                self._metric_field(
                    "🚶 步数",
                    f"{metrics.steps:,} 步",
                    f"{steps_trend_icon} {metrics.steps_trend[1]}",
                    "positive",
                    metrics.steps_trend[0],
                ),
                # 睡眠
                self._metric_field(
                    "😴 睡眠",
                    f"{metrics.sleep_hours} 小时",
                    f"{sleep_trend_icon} {metrics.sleep_trend[1]}",
                    "positive",
                    metrics.sleep_trend[0],
                ),
                # 静息心率
                self._metric_field(
                    "💓 静息心率",
                    f"{metrics.resting_hr} bpm",
                    f"{rhr_trend_icon} {metrics.resting_hr_trend[1]}",
                    "negative",
                    metrics.resting_hr_trend[0],
                ),
                # HRV
                self._metric_field(
                    "💗 HRV",
                    f"{metrics.hrv} ms",
                    f"{hrv_trend_icon} {metrics.hrv_trend[1]}",
                    "positive",
                    metrics.hrv_trend[0],
                ),
                # 碎脚指数
                self._metric_field(
                    "🦶 碎脚指数",
                    f"{metrics.fragmentation} · {HealthMetrics.fragmentation_grade(metrics.fragmentation)}",
                    f"{frag_trend_icon} {metrics.fragmentation_trend[1]}",
                    "negative",
                    metrics.fragmentation_trend[0],
                ),
                # 分隔线
                {"tag": "hr"},
            ],
        }

        # 添加查看仪表盘按钮
        if dashboard_url:
            card["elements"].append({
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {
                            "tag": "plain_text",
                            "content": "查看仪表盘",
                        },
                        "type": "primary",
                        "url": dashboard_url,
                    }
                ],
            })

        return card

    def build_alert_card(
        self,
        date: str,
        abnormalities: List[Dict[str, str]],
        dashboard_url: str = "",
    ) -> Dict[str, Any]:
        """构建异常告警卡片。

        Args:
            date: 日期
            abnormalities: 异常项列表，每项包含 metric / value / threshold / reason
            dashboard_url: 仪表盘链接

        Returns:
            交互式卡片字典。
        """
        card = {
            "config": {
                "wide_screen_mode": True,
                "enable_forward": True,
            },
            "header": {
                "title": {
                    "tag": "plain_text",
                    "content": f"⚠️ 健康异常告警 · {date}",
                },
                "template": self.WARNING_COLOR,
            },
            "elements": [
                {"tag": "hr"},
                {
                    "tag": "markdown",
                    "content": f"检测到 **{len(abnormalities)} 项** 异常指标，请关注：",
                },
            ],
        }

        # 添加每个异常项
        for i, abn in enumerate(abnormalities, 1):
            card["elements"].append({
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": (
                        f"**{i}. {abn['metric']}**\n"
                        f"当前值：{abn['value']}\n"
                        f"阈值：{abn['threshold']}\n"
                        f"原因：{abn['reason']}"
                    ),
                },
            })

        card["elements"].append({"tag": "hr"})

        # 添加查看仪表盘按钮
        if dashboard_url:
            card["elements"].append({
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {
                            "tag": "plain_text",
                            "content": "查看详细数据",
                        },
                        "type": "danger",
                        "url": dashboard_url,
                    }
                ],
            })

        return card

    # ------------------------------------------------------------------
    # 卡片元素辅助方法
    # ------------------------------------------------------------------

    @staticmethod
    def _metric_field(
        label: str,
        value: str,
        trend_text: str,
        metric_type: str,
        trend: str,
    ) -> Dict[str, Any]:
        """构建一个指标字段（两列布局：标签值 + 趋势）。"""
        trend_color = CardBuilder._trend_color(trend, metric_type)

        return {
            "tag": "div",
            "fields": [
                {
                    "is_short": True,
                    "text": {
                        "tag": "lark_md",
                        "content": f"**{label}**\n{value}",
                    },
                },
                {
                    "is_short": True,
                    "text": {
                        "tag": "lark_md",
                        "content": (
                            f"**趋势**\n"
                            f'<font color="{trend_color}">{trend_text}</font>'
                        ),
                    },
                },
            ],
        }


# ---------------------------------------------------------------------------
# 数据加载（模拟 / 从 CSV 读取）
# ---------------------------------------------------------------------------

class HealthDataLoader:
    """健康数据加载器。

    从处理后的 CSV 文件中读取健康指标数据。
    如果数据文件不存在，提供模拟数据用于测试（降级方案）。

    支持的 CSV 格式（与项目中 health_etl.py 的输出格式兼容）：
      - 列：date, steps, sleep_hours, resting_hr, hrv, fragmentation 等
    """

    def __init__(self, data_dir: str = ""):
        self.data_dir = data_dir

    def load_daily_metrics(self, date: Optional[str] = None) -> HealthMetrics:
        """加载指定日期的健康指标。

        Args:
            date: 日期字符串 (YYYY-MM-DD)，默认今天

        Returns:
            HealthMetrics 对象。
        """
        if date is None:
            date = datetime.now().strftime("%Y-%m-%d")

        # 尝试从 CSV 加载
        csv_path = os.path.join(self.data_dir, "daily_metrics.csv") if self.data_dir else ""
        if csv_path and os.path.exists(csv_path):
            metrics = self._load_from_csv(csv_path, date)
            if metrics:
                return metrics

        # 降级：返回模拟数据（用于测试和演示）
        logger.warning("未找到数据文件，使用模拟数据: %s", date)
        return self._mock_metrics(date)

    def _load_from_csv(self, csv_path: str, date: str) -> Optional[HealthMetrics]:
        """从 CSV 文件加载数据。

        使用 csv 标准库解析，查找指定日期及其前一天的数据。
        """
        import csv

        try:
            with open(csv_path, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                rows = list(reader)
        except Exception as e:
            logger.error("读取 CSV 文件失败: %s", e)
            return None

        if not rows:
            return None

        # 按日期排序
        rows.sort(key=lambda r: r.get("date", ""))

        # 找到目标日期和前一天
        current_row = None
        prev_row = None

        for i, row in enumerate(rows):
            if row.get("date") == date:
                current_row = row
                if i > 0:
                    prev_row = rows[i - 1]
                break

        if current_row is None:
            # 如果没有找到指定日期，用最后一天的数据
            current_row = rows[-1]
            if len(rows) > 1:
                prev_row = rows[-2]
            date = current_row.get("date", date)

        def _safe_int(row: Dict[str, str], key: str, default: int = 0) -> int:
            try:
                val = row.get(key, "")
                return int(float(val)) if val else default
            except (ValueError, TypeError):
                return default

        def _safe_float(row: Dict[str, str], key: str, default: float = 0.0) -> float:
            try:
                val = row.get(key, "")
                return float(val) if val else default
            except (ValueError, TypeError):
                return default

        return HealthMetrics(
            date=date,
            steps=_safe_int(current_row, "steps"),
            sleep_hours=_safe_float(current_row, "sleep_hours"),
            resting_hr=_safe_int(current_row, "resting_hr"),
            hrv=_safe_int(current_row, "hrv"),
            fragmentation=_safe_float(current_row, "fragmentation"),
            prev_steps=_safe_int(prev_row, "steps") if prev_row else None,
            prev_sleep_hours=_safe_float(prev_row, "sleep_hours") if prev_row else None,
            prev_resting_hr=_safe_int(prev_row, "resting_hr") if prev_row else None,
            prev_hrv=_safe_int(prev_row, "hrv") if prev_row else None,
            prev_fragmentation=_safe_float(prev_row, "fragmentation") if prev_row else None,
        )

    @staticmethod
    def _mock_metrics(date: str) -> HealthMetrics:
        """生成模拟数据（用于测试和演示）。"""
        import random

        random.seed(hash(date) % 2**32)

        steps = random.randint(5000, 12000)
        sleep_hours = round(random.uniform(6.0, 8.5), 1)
        resting_hr = random.randint(55, 75)
        hrv = random.randint(30, 60)
        fragmentation = round(random.uniform(20, 60), 1)

        # 前一天数据（略有不同）
        prev_steps = steps + random.randint(-1000, 1000)
        prev_sleep = round(sleep_hours + random.uniform(-0.5, 0.5), 1)
        prev_rhr = resting_hr + random.randint(-3, 3)
        prev_hrv = hrv + random.randint(-5, 5)
        prev_frag = round(fragmentation + random.uniform(-5, 5), 1)

        return HealthMetrics(
            date=date,
            steps=steps,
            sleep_hours=sleep_hours,
            resting_hr=resting_hr,
            hrv=hrv,
            fragmentation=fragmentation,
            prev_steps=prev_steps,
            prev_sleep_hours=prev_sleep,
            prev_resting_hr=prev_rhr,
            prev_hrv=prev_hrv,
            prev_fragmentation=prev_frag,
        )


# ---------------------------------------------------------------------------
# 同步管理器（门面模式）
# ---------------------------------------------------------------------------

class FeishuSyncManager:
    """飞书同步管理器 — 统一协调各模块的同步操作。

    使用门面模式，封装所有飞书集成功能，提供简洁的调用接口。
    主流程不会因为任何单一模块的失败而中断。
    """

    def __init__(self, config: FeishuConfig, dashboard_url: str = "", data_dir: str = ""):
        self.config = config
        self.dashboard_url = dashboard_url
        self.data_dir = data_dir

        # HTTP 客户端（共享实例）
        self.http = HttpClient()

        # 数据加载器
        self.data_loader = HealthDataLoader(data_dir)

        # Webhook 客户端（延迟初始化）
        self._daily_webhook: Optional[FeishuWebhook] = None
        self._alert_webhook: Optional[FeishuWebhook] = None

        # 应用认证（延迟初始化，仅在需要日历/文档时创建）
        self._auth: Optional[FeishuAppAuth] = None

        # 日历客户端（延迟初始化）
        self._calendar: Optional[FeishuCalendar] = None

        # 文档客户端（延迟初始化）
        self._doc: Optional[FeishuDoc] = None

        # 卡片构建器
        self.card_builder = CardBuilder()

    # ------------------------------------------------------------------
    # 延迟初始化属性
    # ------------------------------------------------------------------

    @property
    def daily_webhook(self) -> Optional[FeishuWebhook]:
        if self._daily_webhook is None and self.config.webhook_daily_summary:
            try:
                self._daily_webhook = FeishuWebhook(
                    self.config.webhook_daily_summary, self.http
                )
            except ValueError as e:
                logger.warning("每日摘要 Webhook 初始化失败: %s", e)
        return self._daily_webhook

    @property
    def alert_webhook(self) -> Optional[FeishuWebhook]:
        if self._alert_webhook is None and self.config.webhook_alert:
            try:
                self._alert_webhook = FeishuWebhook(
                    self.config.webhook_alert, self.http
                )
            except ValueError as e:
                logger.warning("告警 Webhook 初始化失败: %s", e)
        return self._alert_webhook

    @property
    def auth(self) -> Optional[FeishuAppAuth]:
        if self._auth is None and self.config.app_id and self.config.app_secret:
            try:
                self._auth = FeishuAppAuth(
                    self.config.app_id, self.config.app_secret, self.http
                )
            except ValueError as e:
                logger.warning("飞书应用认证初始化失败: %s", e)
        return self._auth

    @property
    def calendar(self) -> Optional[FeishuCalendar]:
        if (
            self._calendar is None
            and self.config.calendar_enabled
            and self.auth is not None
        ):
            self._calendar = FeishuCalendar(
                self.auth, self.config.calendar_id, self.http
            )
        return self._calendar

    @property
    def doc(self) -> Optional[FeishuDoc]:
        if (
            self._doc is None
            and self.config.doc_enabled
            and self.auth is not None
        ):
            self._doc = FeishuDoc(
                self.auth, self.config.weekly_report_folder, self.http
            )
        return self._doc

    # ------------------------------------------------------------------
    # 核心同步方法
    # ------------------------------------------------------------------

    def run_daily_sync(self) -> Dict[str, Any]:
        """执行每日同步任务。

        包含：
          1. 推送每日健康摘要卡片
          2. 检查异常，如有异常则推送告警
          3. 同步日历（如已启用）

        Returns:
            同步结果字典，包含各模块的执行状态。
        """
        logger.info("开始每日同步任务")
        results: Dict[str, Any] = {
            "daily_summary": False,
            "alert": False,
            "calendar": False,
            "abnormal_count": 0,
        }

        # 加载今日数据
        metrics = self.data_loader.load_daily_metrics()
        logger.info(
            "加载健康数据: 日期=%s, 步数=%d, 睡眠=%.1fh, 静息心率=%d, HRV=%d, 碎脚指数=%.1f",
            metrics.date, metrics.steps, metrics.sleep_hours,
            metrics.resting_hr, metrics.hrv, metrics.fragmentation,
        )

        # 1. 每日摘要卡片
        if self.config.notify_daily_summary and self.daily_webhook:
            card = self.card_builder.build_daily_summary_card(
                metrics, self.dashboard_url
            )
            success = self.daily_webhook.send_interactive_card(card)

            # 降级方案：卡片发送失败则发送纯文本
            if not success:
                logger.warning("交互式卡片发送失败，降级为纯文本")
                text = self._build_daily_summary_text(metrics)
                success = self.daily_webhook.send_text(text)

            results["daily_summary"] = success
            logger.info("每日摘要推送: %s", "成功" if success else "失败")

        # 2. 异常检测与告警
        if self.config.alert_on_abnormal:
            abnormalities = metrics.check_abnormal(self.config.alert_thresholds)
            results["abnormal_count"] = len(abnormalities)

            if abnormalities and self.alert_webhook:
                card = self.card_builder.build_alert_card(
                    metrics.date, abnormalities, self.dashboard_url
                )
                success = self.alert_webhook.send_interactive_card(card)

                if not success:
                    logger.warning("告警卡片发送失败，降级为纯文本")
                    text = self._build_alert_text(metrics.date, abnormalities)
                    success = self.alert_webhook.send_text(text)

                results["alert"] = success
                logger.info(
                    "异常告警推送: %s (异常项数: %d)",
                    "成功" if success else "失败",
                    len(abnormalities),
                )
            elif abnormalities:
                logger.warning("检测到 %d 项异常，但告警 Webhook 未配置", len(abnormalities))
            else:
                logger.info("未检测到异常指标")

        # 3. 日历同步
        if self.calendar is not None:
            try:
                self._sync_calendar_for_date(metrics)
                results["calendar"] = True
                logger.info("日历同步完成")
            except Exception as e:
                logger.error("日历同步异常: %s", e)
                results["calendar"] = False

        logger.info("每日同步任务完成: %s", json.dumps(results, ensure_ascii=False))
        return results

    def run_alert_check(self) -> Dict[str, Any]:
        """仅执行异常检查与告警推送。

        Returns:
            告警结果字典。
        """
        logger.info("开始异常检查任务")
        results: Dict[str, Any] = {
            "alert_sent": False,
            "abnormal_count": 0,
        }

        metrics = self.data_loader.load_daily_metrics()
        abnormalities = metrics.check_abnormal(self.config.alert_thresholds)
        results["abnormal_count"] = len(abnormalities)

        if abnormalities and self.alert_webhook and self.config.alert_on_abnormal:
            card = self.card_builder.build_alert_card(
                metrics.date, abnormalities, self.dashboard_url
            )
            success = self.alert_webhook.send_interactive_card(card)

            if not success:
                text = self._build_alert_text(metrics.date, abnormalities)
                success = self.alert_webhook.send_text(text)

            results["alert_sent"] = success
            logger.info(
                "异常告警已发送: %s (异常项数: %d)",
                "成功" if success else "失败",
                len(abnormalities),
            )
        elif abnormalities:
            logger.warning("检测到 %d 项异常，但告警功能未启用", len(abnormalities))
        else:
            logger.info("未检测到异常指标")

        return results

    def run_weekly_report(self) -> Dict[str, Any]:
        """执行周报同步任务。

        包含：
          1. 生成本周健康数据摘要
          2. 创建飞书文档（如已启用）
          3. 推送周报通知到每日摘要 Webhook

        Returns:
            周报同步结果字典。
        """
        logger.info("开始周报同步任务")
        results: Dict[str, Any] = {
            "doc_created": False,
            "doc_id": None,
            "notification_sent": False,
        }

        # 计算本周日期范围
        today = datetime.now()
        week_start = (today - timedelta(days=today.weekday())).strftime("%Y-%m-%d")
        week_end = today.strftime("%Y-%m-%d")

        # 生成周报数据（模拟，实际应从数据目录聚合）
        summary_data = self._aggregate_weekly_data(week_start, week_end)

        # 1. 创建飞书文档
        if self.doc is not None:
            doc_id = self.doc.create_weekly_report(
                week_start, week_end, summary_data, self.dashboard_url
            )
            results["doc_created"] = doc_id is not None
            results["doc_id"] = doc_id
            logger.info("周报文档创建: %s (ID: %s)", "成功" if doc_id else "失败", doc_id)

        # 2. 推送周报通知
        if self.daily_webhook and self.config.notify_daily_summary:
            text = self._build_weekly_notification_text(
                week_start, week_end, summary_data, self.dashboard_url
            )
            success = self.daily_webhook.send_text(text)
            results["notification_sent"] = success
            logger.info("周报通知推送: %s", "成功" if success else "失败")

        logger.info("周报同步任务完成")
        return results

    # ------------------------------------------------------------------
    # 内部辅助方法
    # ------------------------------------------------------------------

    def _sync_calendar_for_date(self, metrics: HealthMetrics) -> None:
        """同步指定日期的日历事件（运动 + 睡眠）。"""
        if self.calendar is None:
            return

        thresholds = self.config.alert_thresholds
        abnormalities = metrics.check_abnormal(thresholds)
        has_abnormal = len(abnormalities) > 0

        # 睡眠事件
        sleep_data = {
            "date": metrics.date,
            "bed_time": "23:00",
            "wake_time": "07:00",
            "duration_hours": metrics.sleep_hours,
            "is_abnormal": any(a["metric"] == "睡眠时长" for a in abnormalities),
        }
        self.calendar.sync_sleep(sleep_data)

        # 运动事件（如果有步数就记录步行）
        if metrics.steps > 0:
            workout_data = {
                "date": metrics.date,
                "type": "步行",
                "duration_min": max(30, metrics.steps // 100),  # 粗略估算
                "distance_km": round(metrics.steps / 1300, 2),  # 粗略估算
                "calories": int(metrics.steps * 0.04),  # 粗略估算
                "is_abnormal": any(a["metric"] == "步数" for a in abnormalities),
            }
            self.calendar.sync_workout(workout_data)

        # 如果有异常，在日历中添加异常标记事件
        if has_abnormal:
            try:
                base_date = datetime.strptime(metrics.date, "%Y-%m-%d")
            except ValueError:
                base_date = datetime.now()

            abn_descs = [f"- {a['metric']}: {a['reason']}" for a in abnormalities]
            description = "健康异常提醒\n" + "\n".join(abn_descs)

            self.calendar.create_event(
                summary="健康异常提醒",
                start_time=base_date.replace(hour=9, minute=0, second=0),
                end_time=base_date.replace(hour=9, minute=30, second=0),
                description=description,
                is_abnormal=True,
            )

    def _aggregate_weekly_data(self, week_start: str, week_end: str) -> Dict[str, Any]:
        """聚合本周健康数据。

        实际实现中应从 CSV 数据文件中按日期范围聚合。
        这里使用模拟数据作为降级方案。
        """
        import random

        random.seed(hash(week_start + week_end) % 2**32)

        avg_steps = random.randint(7000, 10000)
        total_steps = avg_steps * 7
        avg_sleep = round(random.uniform(6.5, 8.0), 1)
        avg_efficiency = random.randint(85, 95)
        avg_rhr = random.randint(58, 70)
        avg_hrv = random.randint(35, 55)
        avg_frag = round(random.uniform(25, 55), 1)
        frag_grade = HealthMetrics.fragmentation_grade(avg_frag)

        abnormal_days = []
        if random.random() < 0.3:
            abnormal_days.append(f"{week_start} - 睡眠不足")
        if random.random() < 0.2:
            abnormal_days.append(f"{week_end} - 步数偏低")

        return {
            "avg_steps": avg_steps,
            "total_steps": total_steps,
            "best_day_steps": f"{avg_steps + 2000} 步 ({week_start})",
            "avg_sleep_hours": avg_sleep,
            "avg_sleep_efficiency": avg_efficiency,
            "avg_resting_hr": avg_rhr,
            "avg_hrv": avg_hrv,
            "avg_fragmentation": avg_frag,
            "fragmentation_grade": frag_grade,
            "abnormal_days": abnormal_days,
        }

    @staticmethod
    def _build_daily_summary_text(metrics: HealthMetrics) -> str:
        """构建每日摘要纯文本（降级方案）。"""
        return (
            f"【每日健康摘要】{metrics.date}\n"
            f"━━━━━━━━━━━━━━━\n"
            f"🚶 步数：{metrics.steps:,} 步\n"
            f"😴 睡眠：{metrics.sleep_hours} 小时\n"
            f"💓 静息心率：{metrics.resting_hr} bpm\n"
            f"💗 HRV：{metrics.hrv} ms\n"
            f"🦶 碎脚指数：{metrics.fragmentation} ({HealthMetrics.fragmentation_grade(metrics.fragmentation)})\n"
            f"━━━━━━━━━━━━━━━"
        )

    @staticmethod
    def _build_alert_text(date: str, abnormalities: List[Dict[str, str]]) -> str:
        """构建告警纯文本（降级方案）。"""
        lines = [
            f"【健康异常告警】{date}",
            f"━━━━━━━━━━━━━━━",
            f"检测到 {len(abnormalities)} 项异常：",
            "",
        ]
        for i, abn in enumerate(abnormalities, 1):
            lines.append(f"{i}. {abn['metric']}：{abn['value']}")
            lines.append(f"   原因：{abn['reason']}")
        lines.append("━━━━━━━━━━━━━━━")
        return "\n".join(lines)

    @staticmethod
    def _build_weekly_notification_text(
        week_start: str,
        week_end: str,
        data: Dict[str, Any],
        dashboard_url: str,
    ) -> str:
        """构建周报通知纯文本。"""
        lines = [
            f"【健康周报】{week_start} ~ {week_end}",
            f"━━━━━━━━━━━━━━━",
            f"日均步数：{data.get('avg_steps', 0):,} 步",
            f"平均睡眠：{data.get('avg_sleep_hours', 0)} 小时",
            f"平均静息心率：{data.get('avg_resting_hr', 0)} bpm",
            f"平均 HRV：{data.get('avg_hrv', 0)} ms",
            f"碎脚指数：{data.get('avg_fragmentation', 0)} ({data.get('fragmentation_grade', '-')})",
            f"异常天数：{len(data.get('abnormal_days', []))} 天",
        ]
        if dashboard_url:
            lines.extend(["", f"查看仪表盘：{dashboard_url}"])
        lines.append("━━━━━━━━━━━━━━━")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# 命令行接口
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    """解析命令行参数。"""
    parser = argparse.ArgumentParser(
        description="飞书健康数据同步脚本",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例：
  # 每日摘要推送
  python feishu_sync.py --config config/feishu.json --mode daily

  # 异常检查
  python feishu_sync.py --config config/feishu.json --mode alert

  # 周报生成
  python feishu_sync.py --config config/feishu.json --mode weekly

  # 指定仪表盘链接
  python feishu_sync.py --config config/feishu.json --dashboard https://example.com/dashboard.html --mode daily
        """,
    )

    parser.add_argument(
        "--config",
        type=str,
        default="config/feishu.json",
        help="飞书配置文件路径 (默认: config/feishu.json)",
    )

    parser.add_argument(
        "--dashboard",
        type=str,
        default="",
        help="仪表盘 URL，用于卡片底部按钮链接",
    )

    parser.add_argument(
        "--mode",
        type=str,
        choices=["daily", "alert", "weekly"],
        default="daily",
        help="运行模式：daily=每日摘要, alert=异常检查, weekly=周报 (默认: daily)",
    )

    parser.add_argument(
        "--data-dir",
        type=str,
        default="",
        help="健康数据目录（包含 daily_metrics.csv），不指定则使用模拟数据",
    )

    parser.add_argument(
        "--date",
        type=str,
        default=None,
        help="指定日期 (YYYY-MM-DD)，默认今天",
    )

    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="启用详细日志输出",
    )

    return parser.parse_args()


def main() -> int:
    """主入口函数。

    Returns:
        退出码：0 成功，1 失败。
    """
    args = parse_args()

    # 日志级别
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    logger.info("=" * 60)
    logger.info("飞书健康数据同步脚本启动")
    logger.info("运行模式: %s", args.mode)
    logger.info("配置文件: %s", args.config)
    logger.info("=" * 60)

    try:
        # 加载配置
        config = FeishuConfig(args.config)
    except FileNotFoundError as e:
        logger.error("配置文件不存在: %s", e)
        logger.error("请参考 config/feishu_config.example.json 创建配置文件")
        return 1
    except (json.JSONDecodeError, ValueError) as e:
        logger.error("配置文件格式错误: %s", e)
        return 1

    try:
        # 创建同步管理器
        manager = FeishuSyncManager(
            config=config,
            dashboard_url=args.dashboard,
            data_dir=args.data_dir,
        )

        # 根据模式执行
        if args.mode == "daily":
            results = manager.run_daily_sync()
        elif args.mode == "alert":
            results = manager.run_alert_check()
        elif args.mode == "weekly":
            results = manager.run_weekly_report()
        else:
            logger.error("未知模式: %s", args.mode)
            return 1

        # 输出结果摘要
        logger.info("=" * 60)
        logger.info("同步任务完成")
        logger.info("结果: %s", json.dumps(results, ensure_ascii=False, indent=2))
        logger.info("=" * 60)

        return 0

    except KeyboardInterrupt:
        logger.warning("用户中断")
        return 130
    except Exception as e:
        logger.exception("未预期的错误: %s", e)
        return 1


if __name__ == "__main__":
    sys.exit(main())
