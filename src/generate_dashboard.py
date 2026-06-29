#!/usr/bin/env python3
"""
交互式健康仪表盘生成器
从 CSV 数据读取，生成单文件 HTML 仪表盘（ECharts 5.x）
"""

import csv
import json
import os
import sys
import argparse
from datetime import datetime, timedelta
from collections import defaultdict


# =============================================================================
# 配置常量
# =============================================================================

ECHARTS_CDN = "https://cdn.jsdelivr.net/npm/echarts@5.4.3/dist/echarts.min.js"

# 配色方案（来自 dashboard-design.md）
COLORS = {
    "primary": "#2563eb",
    "success": "#10b981",
    "warning": "#f59e0b",
    "danger": "#ef4444",
    "gray_dark": "#1f2937",
    "gray_mid": "#6b7280",
    "gray_light": "#e5e7eb",
    "purple": "#8b5cf6",
    "pink": "#ec4899",
    "cyan": "#06b6d4",
}


# =============================================================================
# 数据读取与处理
# =============================================================================

def read_csv(filepath):
    """读取 CSV 文件，返回字典列表"""
    if not os.path.exists(filepath):
        print("警告：文件不存在 - " + filepath)
        return []
    with open(filepath, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        return list(reader)


def safe_float(value, default=None):
    """安全转换为浮点数"""
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (ValueError, TypeError):
        return default


def safe_int(value, default=None):
    """安全转换为整数"""
    if value is None or value == "":
        return default
    try:
        return int(float(value))
    except (ValueError, TypeError):
        return default


def parse_date(date_str):
    """解析日期字符串"""
    if not date_str:
        return None
    try:
        return datetime.strptime(date_str.strip(), "%Y-%m-%d").date()
    except ValueError:
        return None


def compute_fragmentation_index(daily_data):
    """
    根据每日步数和步行距离估算碎脚指数
    碎脚指数 = 步行段碎片化程度量化值（0-100）
    使用步数与距离的比值来估算：步数/km 越高说明步行越细碎
    """
    frag_data = []
    for row in daily_data:
        steps = safe_float(row.get("steps", 0))
        walk_km = safe_float(row.get("walk_run_km", 0))
        date = row.get("date", "")

        if not steps or not walk_km or walk_km <= 0:
            continue

        # 步数密度（步/公里）
        steps_per_km = steps / walk_km

        # 估算碎脚指数：基于步/公里的标准化评分
        # 正常步幅约 0.7-0.8m，理想状态约 1250-1400 步/公里
        # 步/公里越高，说明行走越细碎（停顿多、步幅小）
        ideal_spkm = 1350  # 理想步数/公里
        max_spkm = 3000    # 极度碎片化阈值

        frag_index = max(0, min(100, ((steps_per_km - ideal_spkm) / (max_spkm - ideal_spkm)) * 100))

        # 评级
        if frag_index <= 20:
            grade = "A"
        elif frag_index <= 40:
            grade = "B"
        elif frag_index <= 60:
            grade = "C"
        elif frag_index <= 80:
            grade = "D"
        else:
            grade = "E"

        # 估算行走段数量（基于碎片化程度）
        walk_bouts = max(1, int(frag_index / 10) + 2)

        # 平均行走段时长（分钟）
        avg_bout_min = max(1, int(60 / (walk_bouts + 1)))

        frag_data.append({
            "date": date,
            "fragmentation_index": round(frag_index, 1),
            "grade": grade,
            "walk_bouts_count": walk_bouts,
            "avg_bout_min": avg_bout_min,
            "steps_per_km": round(steps_per_km, 0),
        })

    return frag_data


def load_all_data(data_dir):
    """加载所有数据文件"""
    daily_file = os.path.join(data_dir, "daily_monitoring_wide.csv")
    daily_data = read_csv(daily_file)

    sleep_file = os.path.join(data_dir, "sleep_daily.csv")
    sleep_data = read_csv(sleep_file)

    workouts_file = os.path.join(data_dir, "workouts.csv")
    workouts_data = read_csv(workouts_file)

    frag_file = os.path.join(data_dir, "fragmentation.csv")
    if os.path.exists(frag_file):
        frag_data = read_csv(frag_file)
        frag_data = [{
            "date": r.get("date", ""),
            "fragmentation_index": safe_float(r.get("fragmentation_index", r.get("frag_index", 0))),
            "grade": r.get("grade", ""),
            "walk_bouts_count": safe_int(r.get("walk_bouts_count", r.get("walk_bouts", 0)), 0),
            "avg_bout_min": safe_float(r.get("avg_bout_min", r.get("avg_walk_min", 0)), 0),
        } for r in frag_data]
    else:
        print("提示：fragmentation.csv 不存在，将从 daily_monitoring_wide.csv 估算碎脚指数")
        frag_data = compute_fragmentation_index(daily_data)

    return {
        "daily": daily_data,
        "sleep": sleep_data,
        "workouts": workouts_data,
        "fragmentation": frag_data,
    }


# =============================================================================
# 数据聚合与统计
# =============================================================================

def filter_by_date_range(data, date_field, start_date, end_date):
    """按日期范围过滤数据"""
    result = []
    for row in data:
        d = parse_date(row.get(date_field, ""))
        if d is None:
            continue
        if start_date <= d <= end_date:
            result.append(row)
    return result


def get_date_range(daily_data, days=None):
    """获取日期范围"""
    dates = []
    for row in daily_data:
        d = parse_date(row.get("date", ""))
        if d:
            dates.append(d)

    if not dates:
        today = datetime.now().date()
        return today - timedelta(days=30), today

    min_date = min(dates)
    max_date = max(dates)

    if days and days > 0:
        start_date = max(min_date, max_date - timedelta(days=days - 1))
    else:
        start_date = min_date

    return start_date, max_date


def compute_kpi_card(daily_data, sleep_data, workouts_data, frag_data, days=30):
    """计算关键指标卡片数据"""
    start_date, end_date = get_date_range(daily_data, days)
    prev_start = start_date - timedelta(days=days)
    prev_end = start_date - timedelta(days=1)

    current_daily = filter_by_date_range(daily_data, "date", start_date, end_date)
    prev_daily = filter_by_date_range(daily_data, "date", prev_start, prev_end)

    current_sleep = filter_by_date_range(sleep_data, "date", start_date, end_date)
    current_workouts = filter_by_date_range(workouts_data, "date", start_date, end_date)
    current_frag = filter_by_date_range(frag_data, "date", start_date, end_date)

    def avg(data, field, default=0):
        vals = [safe_float(r.get(field)) for r in data if safe_float(r.get(field)) is not None]
        return sum(vals) / len(vals) if vals else default

    def sum_val(data, field, default=0):
        vals = [safe_float(r.get(field)) for r in data if safe_float(r.get(field)) is not None]
        return sum(vals) if vals else default

    def pct_change(current, previous):
        if previous == 0:
            return 0
        return ((current - previous) / previous) * 100

    avg_steps = avg(current_daily, "steps")
    avg_sleep = avg(current_sleep, "asleep_min")
    avg_resting_hr = avg(current_daily, "resting_hr")
    avg_hrv = avg(current_daily, "hrv_sdnn")
    avg_frag = avg(current_frag, "fragmentation_index")
    total_run_km = sum_val([r for r in current_workouts if r.get("type") == "Running"], "total_distance")

    prev_avg_steps = avg(prev_daily, "steps")
    prev_avg_sleep = avg(filter_by_date_range(sleep_data, "date", prev_start, prev_end), "asleep_min")
    prev_avg_resting_hr = avg(prev_daily, "resting_hr")
    prev_avg_hrv = avg(prev_daily, "hrv_sdnn")

    run_count = len([r for r in current_workouts if r.get("type") == "Running"])

    def steps_status(val):
        if val >= 10000: return "success"
        if val >= 7000: return "warning"
        return "danger"

    def sleep_status(minutes):
        hours = minutes / 60
        if 7 <= hours <= 9: return "success"
        if 6 <= hours < 7 or 9 < hours <= 10: return "warning"
        return "danger"

    def hr_status(hr):
        if hr <= 60: return "success"
        if hr <= 70: return "warning"
        return "danger"

    def hrv_status(hrv):
        if hrv >= 50: return "success"
        if hrv >= 30: return "warning"
        return "danger"

    def frag_status(fi):
        if fi <= 40: return "success"
        if fi <= 60: return "warning"
        return "danger"

    cards = [
        {
            "id": "steps",
            "title": "日均步数",
            "value": round(avg_steps),
            "unit": "步",
            "trend": round(pct_change(avg_steps, prev_avg_steps), 1),
            "status": steps_status(avg_steps),
            "icon": "steps",
        },
        {
            "id": "sleep",
            "title": "日均睡眠",
            "value": round(avg_sleep / 60, 1),
            "unit": "小时",
            "trend": round(pct_change(avg_sleep, prev_avg_sleep), 1),
            "status": sleep_status(avg_sleep),
            "icon": "sleep",
        },
        {
            "id": "resting_hr",
            "title": "静息心率",
            "value": round(avg_resting_hr),
            "unit": "bpm",
            "trend": round(pct_change(avg_resting_hr, prev_avg_resting_hr), 1),
            "trend_inverse": True,
            "status": hr_status(avg_resting_hr),
            "icon": "heart",
        },
        {
            "id": "hrv",
            "title": "HRV (SDNN)",
            "value": round(avg_hrv),
            "unit": "ms",
            "trend": round(pct_change(avg_hrv, prev_avg_hrv), 1),
            "status": hrv_status(avg_hrv),
            "icon": "pulse",
        },
        {
            "id": "fragmentation",
            "title": "碎脚指数",
            "value": round(avg_frag, 1),
            "unit": "分",
            "trend": 0,
            "trend_inverse": True,
            "status": frag_status(avg_frag),
            "icon": "walk",
        },
        {
            "id": "running",
            "title": "跑步距离",
            "value": round(total_run_km, 1),
            "unit": "km",
            "sub_text": str(run_count) + " 次训练",
            "status": "success" if total_run_km >= 20 else ("warning" if total_run_km >= 10 else "danger"),
            "icon": "run",
        },
    ]

    return cards


# =============================================================================
# HTML 模板
# =============================================================================

def get_html_template():
    """返回HTML模板字符串，使用 __PLACEHOLDER__ 形式的占位符"""
    return r'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>个人健康仪表盘</title>
<script src="__ECHARTS_CDN__"></script>
<style>
:root {
    --color-primary: __COLOR_PRIMARY__;
    --color-success: __COLOR_SUCCESS__;
    --color-warning: __COLOR_WARNING__;
    --color-danger: __COLOR_DANGER__;
    --color-gray-dark: __COLOR_GRAY_DARK__;
    --color-gray-mid: __COLOR_GRAY_MID__;
    --color-gray-light: __COLOR_GRAY_LIGHT__;
    --color-purple: __COLOR_PURPLE__;
    --color-pink: __COLOR_PINK__;
    --color-cyan: __COLOR_CYAN__;
    --bg-color: #f8fafc;
    --card-bg: #ffffff;
    --text-primary: #1f2937;
    --text-secondary: #6b7280;
    --text-tertiary: #9ca3af;
    --border-color: #e5e7eb;
    --radius-sm: 8px;
    --radius-md: 12px;
    --radius-lg: 16px;
    --shadow-sm: 0 1px 3px rgba(0, 0, 0, 0.08);
    --shadow-md: 0 4px 12px rgba(0, 0, 0, 0.08);
    --shadow-lg: 0 8px 24px rgba(0, 0, 0, 0.12);
    --spacing-xs: 4px;
    --spacing-sm: 8px;
    --spacing-md: 16px;
    --spacing-lg: 24px;
    --spacing-xl: 32px;
    --font-main: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "PingFang SC", "Microsoft YaHei", sans-serif;
    --font-mono: "SF Mono", Menlo, Monaco, Consolas, monospace;
}
* { margin: 0; padding: 0; box-sizing: border-box; }
body {
    font-family: var(--font-main);
    background: var(--bg-color);
    color: var(--text-primary);
    font-size: 14px;
    line-height: 1.5;
    -webkit-font-smoothing: antialiased;
}
.header {
    background: var(--card-bg);
    border-bottom: 1px solid var(--border-color);
    padding: var(--spacing-md) var(--spacing-xl);
    position: sticky;
    top: 0;
    z-index: 100;
    box-shadow: var(--shadow-sm);
}
.header-inner {
    max-width: 1440px;
    margin: 0 auto;
    display: flex;
    align-items: center;
    justify-content: space-between;
    flex-wrap: wrap;
    gap: var(--spacing-md);
}
.header-title {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm);
    font-size: 20px;
    font-weight: 600;
    color: var(--text-primary);
}
.header-title-icon {
    width: 32px;
    height: 32px;
    background: linear-gradient(135deg, var(--color-primary), var(--color-cyan));
    border-radius: var(--radius-sm);
    display: flex;
    align-items: center;
    justify-content: center;
    color: white;
    font-size: 18px;
}
.header-controls {
    display: flex;
    align-items: center;
    gap: var(--spacing-md);
    flex-wrap: wrap;
}
.date-range-group {
    display: flex;
    align-items: center;
    gap: var(--spacing-xs);
    background: var(--bg-color);
    border-radius: var(--radius-sm);
    padding: 2px;
}
.date-btn {
    padding: 6px 12px;
    border: none;
    background: transparent;
    color: var(--text-secondary);
    font-size: 13px;
    font-weight: 500;
    border-radius: 6px;
    cursor: pointer;
    transition: all 0.2s;
    font-family: var(--font-main);
}
.date-btn:hover { color: var(--text-primary); background: var(--color-gray-light); }
.date-btn.active { background: var(--color-primary); color: white; }
.date-custom { display: flex; align-items: center; gap: var(--spacing-xs); }
.date-custom input[type="date"] {
    padding: 6px 8px;
    border: 1px solid var(--border-color);
    border-radius: 6px;
    font-size: 13px;
    font-family: var(--font-main);
    color: var(--text-primary);
    background: white;
}
.export-btn {
    padding: 8px 16px;
    background: var(--color-primary);
    color: white;
    border: none;
    border-radius: var(--radius-sm);
    font-size: 13px;
    font-weight: 500;
    cursor: pointer;
    transition: all 0.2s;
    font-family: var(--font-main);
    display: flex;
    align-items: center;
    gap: 6px;
}
.export-btn:hover { background: #1d4ed8; transform: translateY(-1px); box-shadow: var(--shadow-md); }
.container { max-width: 1440px; margin: 0 auto; padding: var(--spacing-lg) var(--spacing-xl); }
.kpi-row {
    display: grid;
    grid-template-columns: repeat(6, 1fr);
    gap: var(--spacing-md);
    margin-bottom: var(--spacing-lg);
}
.kpi-card {
    background: var(--card-bg);
    border-radius: var(--radius-md);
    padding: var(--spacing-md) var(--spacing-lg);
    box-shadow: var(--shadow-sm);
    transition: all 0.2s;
    cursor: pointer;
    border-left: 4px solid transparent;
}
.kpi-card:hover { box-shadow: var(--shadow-md); transform: translateY(-2px); }
.kpi-card.success { border-left-color: var(--color-success); }
.kpi-card.warning { border-left-color: var(--color-warning); }
.kpi-card.danger { border-left-color: var(--color-danger); }
.kpi-card-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: var(--spacing-sm);
}
.kpi-card-title { font-size: 13px; color: var(--text-secondary); font-weight: 500; }
.kpi-card-icon {
    width: 28px; height: 28px; border-radius: 6px;
    display: flex; align-items: center; justify-content: center;
    font-size: 14px; background: var(--bg-color);
}
.kpi-card-value {
    font-size: 28px; font-weight: 700; color: var(--text-primary);
    font-family: var(--font-mono); line-height: 1.2; margin-bottom: 4px;
}
.kpi-card-unit { font-size: 14px; font-weight: 500; color: var(--text-tertiary); margin-left: 4px; }
.kpi-card-footer { display: flex; align-items: center; justify-content: space-between; }
.kpi-trend { display: flex; align-items: center; gap: 4px; font-size: 12px; font-weight: 500; }
.kpi-trend.up { color: var(--color-success); }
.kpi-trend.down { color: var(--color-danger); }
.kpi-trend.flat { color: var(--text-tertiary); }
.kpi-sub-text { font-size: 12px; color: var(--text-tertiary); }
.tab-container {
    background: var(--card-bg);
    border-radius: var(--radius-md);
    box-shadow: var(--shadow-sm);
    overflow: hidden;
}
.tab-header {
    display: flex;
    border-bottom: 1px solid var(--border-color);
    padding: 0 var(--spacing-lg);
    overflow-x: auto;
    scrollbar-width: none;
}
.tab-header::-webkit-scrollbar { display: none; }
.tab-item {
    padding: var(--spacing-md) var(--spacing-md);
    font-size: 14px;
    font-weight: 500;
    color: var(--text-secondary);
    cursor: pointer;
    border-bottom: 2px solid transparent;
    white-space: nowrap;
    transition: all 0.2s;
}
.tab-item:hover { color: var(--text-primary); }
.tab-item.active { color: var(--color-primary); border-bottom-color: var(--color-primary); }
.tab-content { padding: var(--spacing-lg); }
.tab-panel { display: none; }
.tab-panel.active { display: block; }
.chart-grid {
    display: grid;
    grid-template-columns: repeat(2, 1fr);
    gap: var(--spacing-lg);
}
.chart-grid.full { grid-template-columns: 1fr; }
.chart-card {
    background: var(--card-bg);
    border-radius: var(--radius-md);
    box-shadow: var(--shadow-sm);
    border: 1px solid var(--border-color);
    overflow: hidden;
}
.chart-card-header {
    padding: var(--spacing-md) var(--spacing-lg);
    border-bottom: 1px solid var(--border-color);
    display: flex;
    align-items: center;
    justify-content: space-between;
}
.chart-card-title { font-size: 15px; font-weight: 600; color: var(--text-primary); }
.chart-card-subtitle { font-size: 12px; color: var(--text-tertiary); margin-top: 2px; }
.chart-container {
    width: 100%;
    height: 320px;
    padding: var(--spacing-sm);
}
.chart-container.tall { height: 380px; }
.frag-insight-card {
    background: linear-gradient(135deg, #eff6ff, #ecfeff);
    border-radius: var(--radius-md);
    padding: var(--spacing-lg);
    margin-top: var(--spacing-lg);
}
.frag-insight-title {
    font-size: 15px;
    font-weight: 600;
    color: var(--color-primary);
    margin-bottom: var(--spacing-sm);
    display: flex;
    align-items: center;
    gap: 8px;
}
.frag-insight-content { font-size: 13px; color: var(--text-secondary); line-height: 1.8; }
.frag-insight-content ul { margin-left: var(--spacing-md); margin-top: var(--spacing-sm); }
.frag-insight-content li { margin-bottom: 4px; }
.data-table-wrapper { overflow-x: auto; }
.data-table { width: 100%; border-collapse: collapse; font-size: 13px; }
.data-table th {
    background: var(--bg-color);
    padding: 10px 12px;
    text-align: left;
    font-weight: 600;
    color: var(--text-secondary);
    border-bottom: 2px solid var(--border-color);
    position: sticky;
    top: 0;
    white-space: nowrap;
}
.data-table td {
    padding: 10px 12px;
    border-bottom: 1px solid var(--border-color);
    color: var(--text-primary);
}
.data-table tr:hover { background: #f9fafb; }
.data-table .num { font-family: var(--font-mono); text-align: right; }
.badge {
    display: inline-block;
    padding: 2px 8px;
    border-radius: 10px;
    font-size: 11px;
    font-weight: 600;
}
.badge.success { background: #d1fae5; color: #065f46; }
.badge.warning { background: #fef3c7; color: #92400e; }
.badge.danger { background: #fee2e2; color: #991b1b; }
.footer {
    text-align: center;
    padding: var(--spacing-lg);
    color: var(--text-tertiary);
    font-size: 12px;
}
@media (max-width: 1200px) { .kpi-row { grid-template-columns: repeat(3, 1fr); } }
@media (max-width: 1024px) {
    .kpi-row { grid-template-columns: repeat(3, 1fr); }
    .chart-grid { grid-template-columns: 1fr; }
    .container { padding: var(--spacing-md); }
}
@media (max-width: 768px) {
    .header { padding: var(--spacing-sm) var(--spacing-md); }
    .header-inner { flex-direction: column; align-items: flex-start; }
    .kpi-row { grid-template-columns: repeat(2, 1fr); gap: var(--spacing-sm); }
    .kpi-card { padding: var(--spacing-sm) var(--spacing-md); }
    .kpi-card-value { font-size: 22px; }
    .tab-content { padding: var(--spacing-md); }
    .chart-container { height: 260px; }
    .date-range-group { overflow-x: auto; max-width: 100%; }
}
@media (max-width: 480px) {
    .kpi-row { grid-template-columns: 1fr 1fr; }
    .header-title { font-size: 16px; }
}
.chart-loading {
    display: flex;
    align-items: center;
    justify-content: center;
    height: 100%;
    color: var(--text-tertiary);
    font-size: 13px;
}
</style>
</head>
<body>
<header class="header">
    <div class="header-inner">
        <div class="header-title">
            <div class="header-title-icon">&#10084;&#65039;</div>
            <span>个人健康仪表盘</span>
        </div>
        <div class="header-controls">
            <div class="date-range-group" id="dateRangeGroup">
                <button class="date-btn active" data-days="7">7天</button>
                <button class="date-btn" data-days="14">14天</button>
                <button class="date-btn" data-days="30">30天</button>
                <button class="date-btn" data-days="90">90天</button>
                <button class="date-btn" data-days="0">全部</button>
            </div>
            <div class="date-custom">
                <input type="date" id="startDate" title="开始日期">
                <span style="color: var(--text-tertiary);">~</span>
                <input type="date" id="endDate" title="结束日期">
            </div>
            <button class="export-btn" onclick="exportDashboard()">
                <span>&#11015;</span> 导出
            </button>
        </div>
    </div>
</header>

<main class="container">
    <div class="kpi-row" id="kpiRow"></div>
    <div class="tab-container">
        <div class="tab-header" id="tabHeader">
            <div class="tab-item active" data-tab="overview">总览</div>
            <div class="tab-item" data-tab="sleep">睡眠分析</div>
            <div class="tab-item" data-tab="running">跑步分析</div>
            <div class="tab-item" data-tab="hrv">心率恢复</div>
            <div class="tab-item" data-tab="fragmentation">碎脚指标</div>
            <div class="tab-item" data-tab="detail">数据明细</div>
        </div>
        <div class="tab-content">
            <div class="tab-panel active" id="tab-overview">
                <div class="chart-grid">
                    <div class="chart-card" style="grid-column: 1 / -1;">
                        <div class="chart-card-header">
                            <div>
                                <div class="chart-card-title">健康综合趋势</div>
                                <div class="chart-card-subtitle">步数、睡眠、心率多维度对比</div>
                            </div>
                        </div>
                        <div class="chart-container tall" id="chart-overview-trend"></div>
                    </div>
                    <div class="chart-card">
                        <div class="chart-card-header"><div class="chart-card-title">步数分布热力图</div></div>
                        <div class="chart-container" id="chart-overview-heatmap"></div>
                    </div>
                    <div class="chart-card">
                        <div class="chart-card-header"><div class="chart-card-title">健康维度雷达图</div></div>
                        <div class="chart-container" id="chart-overview-radar"></div>
                    </div>
                </div>
            </div>

            <div class="tab-panel" id="tab-sleep">
                <div class="chart-grid">
                    <div class="chart-card" style="grid-column: 1 / -1;">
                        <div class="chart-card-header">
                            <div>
                                <div class="chart-card-title">睡眠时长趋势</div>
                                <div class="chart-card-subtitle">每日睡眠时长与阶段分布</div>
                            </div>
                        </div>
                        <div class="chart-container tall" id="chart-sleep-duration"></div>
                    </div>
                    <div class="chart-card">
                        <div class="chart-card-header"><div class="chart-card-title">睡眠阶段平均占比</div></div>
                        <div class="chart-container" id="chart-sleep-stages"></div>
                    </div>
                    <div class="chart-card">
                        <div class="chart-card-header"><div class="chart-card-title">睡眠中点时间分布</div></div>
                        <div class="chart-container" id="chart-sleep-midpoint"></div>
                    </div>
                    <div class="chart-card" style="grid-column: 1 / -1;">
                        <div class="chart-card-header"><div class="chart-card-title">睡眠效率 vs 深睡占比</div></div>
                        <div class="chart-container" id="chart-sleep-scatter"></div>
                    </div>
                </div>
            </div>

            <div class="tab-panel" id="tab-running">
                <div class="chart-grid">
                    <div class="chart-card" style="grid-column: 1 / -1;">
                        <div class="chart-card-header">
                            <div>
                                <div class="chart-card-title">跑步距离与配速趋势</div>
                                <div class="chart-card-subtitle">每次跑步训练的距离和平均配速</div>
                            </div>
                        </div>
                        <div class="chart-container tall" id="chart-run-trend"></div>
                    </div>
                    <div class="chart-card">
                        <div class="chart-card-header"><div class="chart-card-title">心率区间分布</div></div>
                        <div class="chart-container" id="chart-run-hrzones"></div>
                    </div>
                    <div class="chart-card">
                        <div class="chart-card-header"><div class="chart-card-title">配速分布直方图</div></div>
                        <div class="chart-container" id="chart-run-pacehist"></div>
                    </div>
                    <div class="chart-card" style="grid-column: 1 / -1;">
                        <div class="chart-card-header"><div class="chart-card-title">跑步训练明细</div></div>
                        <div class="data-table-wrapper" style="padding: 16px; max-height: 400px; overflow-y: auto;">
                            <table class="data-table" id="runDetailTable">
                                <thead>
                                    <tr>
                                        <th>日期</th>
                                        <th class="num">距离 (km)</th>
                                        <th class="num">时长 (min)</th>
                                        <th class="num">配速 (min/km)</th>
                                        <th class="num">平均心率</th>
                                        <th class="num">消耗 (kcal)</th>
                                    </tr>
                                </thead>
                                <tbody id="runDetailBody"></tbody>
                            </table>
                        </div>
                    </div>
                </div>
            </div>

            <div class="tab-panel" id="tab-hrv">
                <div class="chart-grid">
                    <div class="chart-card" style="grid-column: 1 / -1;">
                        <div class="chart-card-header">
                            <div>
                                <div class="chart-card-title">静息心率与 HRV 趋势</div>
                                <div class="chart-card-subtitle">恢复状态双指标监控</div>
                            </div>
                        </div>
                        <div class="chart-container tall" id="chart-hrv-trend"></div>
                    </div>
                    <div class="chart-card">
                        <div class="chart-card-header"><div class="chart-card-title">静息心率分布</div></div>
                        <div class="chart-container" id="chart-hr-resting"></div>
                    </div>
                    <div class="chart-card">
                        <div class="chart-card-header"><div class="chart-card-title">HRV 分布</div></div>
                        <div class="chart-container" id="chart-hrv-dist"></div>
                    </div>
                    <div class="chart-card" style="grid-column: 1 / -1;">
                        <div class="chart-card-header"><div class="chart-card-title">睡眠时长 vs 静息心率</div></div>
                        <div class="chart-container" id="chart-sleep-hr-scatter"></div>
                    </div>
                </div>
            </div>

            <div class="tab-panel" id="tab-fragmentation">
                <div class="chart-grid">
                    <div class="chart-card" style="grid-column: 1 / -1;">
                        <div class="chart-card-header">
                            <div>
                                <div class="chart-card-title">每日碎脚指数趋势</div>
                                <div class="chart-card-subtitle">按评级颜色区分（A 优秀 ~ E 极差）</div>
                            </div>
                        </div>
                        <div class="chart-container tall" id="chart-frag-trend"></div>
                    </div>
                    <div class="chart-card">
                        <div class="chart-card-header"><div class="chart-card-title">碎脚评级分布</div></div>
                        <div class="chart-container" id="chart-frag-pie"></div>
                    </div>
                    <div class="chart-card">
                        <div class="chart-card-header"><div class="chart-card-title">平均行走段时长分布</div></div>
                        <div class="chart-container" id="chart-frag-histogram"></div>
                    </div>
                </div>
                <div class="frag-insight-card">
                    <div class="frag-insight-title">
                        <span>&#128161;</span> 碎脚指标解读与建议
                    </div>
                    <div class="frag-insight-content">
                        <p><strong>什么是碎脚指数？</strong></p>
                        <p>碎脚指数量化日常行走的碎片化程度，反映行走的能量效率。指数越低，说明行走越连续高效；指数越高，说明行走越细碎（频繁停顿、步幅短小）。</p>
                        <ul>
                            <li><strong>A 级 (0-20 分)</strong>：高效连续行走，能量利用率高</li>
                            <li><strong>B 级 (21-40 分)</strong>：轻度碎片化，整体良好</li>
                            <li><strong>C 级 (41-60 分)</strong>：中度碎片化，建议增加连续步行</li>
                            <li><strong>D 级 (61-80 分)</strong>：高度碎片化，行走质量需改善</li>
                            <li><strong>E 级 (81-100 分)</strong>：极度碎片化，建议专项训练</li>
                        </ul>
                        <p style="margin-top: 12px;"><strong>改善建议：</strong></p>
                        <ul>
                            <li>每天安排 1-2 次连续 20 分钟以上的步行</li>
                            <li>有意识地加大步幅，保持稳定节奏</li>
                            <li>减少边走边看手机的碎片化行走</li>
                            <li>尝试健走或慢跑训练，提升行走效率</li>
                        </ul>
                    </div>
                </div>
            </div>

            <div class="tab-panel" id="tab-detail">
                <div class="chart-card">
                    <div class="chart-card-header"><div class="chart-card-title">每日数据明细</div></div>
                    <div class="data-table-wrapper" style="padding: 16px; max-height: 600px; overflow-y: auto;">
                        <table class="data-table" id="dailyDetailTable">
                            <thead>
                                <tr>
                                    <th>日期</th>
                                    <th class="num">步数</th>
                                    <th class="num">步行距离 (km)</th>
                                    <th class="num">活动消耗 (kcal)</th>
                                    <th class="num">静息心率</th>
                                    <th class="num">HRV (ms)</th>
                                    <th class="num">睡眠 (min)</th>
                                    <th>碎脚评级</th>
                                </tr>
                            </thead>
                            <tbody id="dailyDetailBody"></tbody>
                        </table>
                    </div>
                </div>
            </div>
        </div>
    </div>
</main>

<footer class="footer">
    数据来源：Apple Health | 生成时间：__GENERATED_TIME__ | 共 __TOTAL_DAYS__ 天数据
</footer>

<script>
// =============================================================================
// 数据注入
// =============================================================================
const dashboardData = __DASHBOARD_DATA__;

// =============================================================================
// 全局状态
// =============================================================================
let currentDateRange = { days: 7, start: null, end: null };
let charts = {};
let initializedTabs = new Set(['overview']);

// =============================================================================
// 工具函数
// =============================================================================
function parseDate(dateStr) { return new Date(dateStr + 'T00:00:00'); }
function formatDate(date) {
    const y = date.getFullYear();
    const m = String(date.getMonth() + 1).padStart(2, '0');
    const d = String(date.getDate()).padStart(2, '0');
    return y + '-' + m + '-' + d;
}
function filterDataByDate(data, dateField, startDate, endDate) {
    const start = new Date(startDate + 'T00:00:00');
    const end = new Date(endDate + 'T23:59:59');
    return data.filter(function(item) {
        const d = new Date(item[dateField] + 'T00:00:00');
        return d >= start && d <= end;
    });
}
function getCurrentDateRange() {
    const allDates = dashboardData.allDaily.map(function(d) { return d.date; }).sort();
    const maxDate = allDates[allDates.length - 1];
    const minDate = allDates[0];
    if (currentDateRange.days > 0) {
        const endD = new Date(maxDate + 'T00:00:00');
        const startD = new Date(endD);
        startD.setDate(startD.getDate() - (currentDateRange.days - 1));
        return { start: formatDate(startD), end: maxDate };
    } else {
        return { start: minDate, end: maxDate };
    }
}
function avg(arr, field) {
    const vals = arr.filter(function(d) { return d[field] != null && d[field] !== ''; }).map(function(d) { return d[field]; });
    if (vals.length === 0) return 0;
    return vals.reduce(function(a, b) { return a + b; }, 0) / vals.length;
}
function sum(arr, field) {
    const vals = arr.filter(function(d) { return d[field] != null && d[field] !== ''; }).map(function(d) { return d[field]; });
    return vals.reduce(function(a, b) { return a + b; }, 0);
}

// =============================================================================
// KPI 卡片
// =============================================================================
function renderKPICards() {
    const range = getCurrentDateRange();
    const daily = filterDataByDate(dashboardData.allDaily, 'date', range.start, range.end);
    const sleep = filterDataByDate(dashboardData.allSleep, 'date', range.start, range.end);
    const workouts = filterDataByDate(dashboardData.allWorkouts, 'date', range.start, range.end);
    const frag = filterDataByDate(dashboardData.allFragmentation, 'date', range.start, range.end);

    const daysCount = Math.ceil((new Date(range.end) - new Date(range.start)) / (1000 * 60 * 60 * 24)) + 1;
    const prevEnd = new Date(range.start + 'T00:00:00');
    prevEnd.setDate(prevEnd.getDate() - 1);
    const prevStart = new Date(prevEnd);
    prevStart.setDate(prevStart.getDate() - (daysCount - 1));
    const prevStartStr = formatDate(prevStart);
    const prevEndStr = formatDate(prevEnd);

    const prevDaily = filterDataByDate(dashboardData.allDaily, 'date', prevStartStr, prevEndStr);
    const prevSleepData = filterDataByDate(dashboardData.allSleep, 'date', prevStartStr, prevEndStr);
    const prevFragData = filterDataByDate(dashboardData.allFragmentation, 'date', prevStartStr, prevEndStr);

    function pctChange(cur, prev) {
        if (prev === 0) return 0;
        return ((cur - prev) / prev) * 100;
    }

    const curSteps = avg(daily, 'steps');
    const prevSteps = avg(prevDaily, 'steps');
    const curSleep = avg(sleep, 'asleep_min');
    const prevSleep = avg(prevSleepData, 'asleep_min');
    const curRHR = avg(daily, 'resting_hr');
    const prevRHR = avg(prevDaily, 'resting_hr');
    const curHRV = avg(daily, 'hrv_sdnn');
    const prevHRV = avg(prevDaily, 'hrv_sdnn');
    const curFrag = avg(frag, 'fragmentation_index');
    const prevFrag = avg(prevFragData, 'fragmentation_index');
    const totalRun = sum(workouts, 'total_distance');
    const runCount = workouts.length;

    const cards = [
        { id: 'steps', title: '日均步数', value: Math.round(curSteps), unit: '步',
          trend: pctChange(curSteps, prevSteps), inverse: false,
          status: curSteps >= 10000 ? 'success' : (curSteps >= 7000 ? 'warning' : 'danger'), icon: '\ud83d\udc5f' },
        { id: 'sleep', title: '日均睡眠', value: (curSleep / 60).toFixed(1), unit: '小时',
          trend: pctChange(curSleep, prevSleep), inverse: false,
          status: (curSleep/60 >= 7 && curSleep/60 <= 9) ? 'success' : ((curSleep/60 >= 6 && curSleep/60 <= 10) ? 'warning' : 'danger'), icon: '\ud83d\ude34' },
        { id: 'rhr', title: '静息心率', value: Math.round(curRHR), unit: 'bpm',
          trend: pctChange(curRHR, prevRHR), inverse: true,
          status: curRHR <= 60 ? 'success' : (curRHR <= 70 ? 'warning' : 'danger'), icon: '\u2764\ufe0f' },
        { id: 'hrv', title: 'HRV (SDNN)', value: Math.round(curHRV), unit: 'ms',
          trend: pctChange(curHRV, prevHRV), inverse: false,
          status: curHRV >= 50 ? 'success' : (curHRV >= 30 ? 'warning' : 'danger'), icon: '\ud83d\udcca' },
        { id: 'frag', title: '碎脚指数', value: curFrag.toFixed(1), unit: '分',
          trend: pctChange(curFrag, prevFrag), inverse: true,
          status: curFrag <= 40 ? 'success' : (curFrag <= 60 ? 'warning' : 'danger'), icon: '\ud83e\uddb6' },
        { id: 'run', title: '跑步距离', value: totalRun.toFixed(1), unit: 'km',
          subText: runCount + ' 次训练', trend: 0, inverse: false,
          status: totalRun >= 20 ? 'success' : (totalRun >= 10 ? 'warning' : 'danger'), icon: '\ud83c\udfc3' },
    ];

    const row = document.getElementById('kpiRow');
    row.innerHTML = cards.map(function(card) {
        let trendClass = 'flat';
        let trendArrow = '\u2014 持平';
        if (card.trend > 0.5) {
            trendClass = card.inverse ? 'down' : 'up';
            trendArrow = '\u2191 ' + Math.abs(card.trend).toFixed(1) + '%';
        } else if (card.trend < -0.5) {
            trendClass = card.inverse ? 'up' : 'down';
            trendArrow = '\u2193 ' + Math.abs(card.trend).toFixed(1) + '%';
        }
        let subHtml = '';
        if (card.subText) { subHtml = '<span class="kpi-sub-text">' + card.subText + '</span>'; }
        return '<div class="kpi-card ' + card.status + '" onclick="switchTab(\'overview\')">' +
            '<div class="kpi-card-header">' +
            '<span class="kpi-card-title">' + card.title + '</span>' +
            '<span class="kpi-card-icon">' + card.icon + '</span>' +
            '</div>' +
            '<div class="kpi-card-value">' + card.value + '<span class="kpi-card-unit">' + card.unit + '</span></div>' +
            '<div class="kpi-card-footer">' +
            '<span class="kpi-trend ' + trendClass + '">' + trendArrow + '</span>' +
            subHtml +
            '</div></div>';
    }).join('');
}

// =============================================================================
// Tab 切换
// =============================================================================
function switchTab(tabId) {
    document.querySelectorAll('.tab-item').forEach(function(el) {
        el.classList.toggle('active', el.dataset.tab === tabId);
    });
    document.querySelectorAll('.tab-panel').forEach(function(el) {
        el.classList.toggle('active', el.id === 'tab-' + tabId);
    });
    if (!initializedTabs.has(tabId)) {
        initializedTabs.add(tabId);
        initTabCharts(tabId);
    }
    updateTabCharts(tabId);
    setTimeout(function() {
        Object.values(charts).forEach(function(chart) {
            if (chart && chart.resize) chart.resize();
        });
    }, 50);
}

document.getElementById('tabHeader').addEventListener('click', function(e) {
    const tabItem = e.target.closest('.tab-item');
    if (tabItem) { switchTab(tabItem.dataset.tab); }
});

// =============================================================================
// 日期范围切换
// =============================================================================
document.getElementById('dateRangeGroup').addEventListener('click', function(e) {
    const btn = e.target.closest('.date-btn');
    if (btn) {
        document.querySelectorAll('.date-btn').forEach(function(b) { b.classList.remove('active'); });
        btn.classList.add('active');
        currentDateRange.days = parseInt(btn.dataset.days);
        currentDateRange.start = null;
        currentDateRange.end = null;
        const range = getCurrentDateRange();
        document.getElementById('startDate').value = range.start;
        document.getElementById('endDate').value = range.end;
        onDateRangeChange();
    }
});

document.getElementById('startDate').addEventListener('change', onCustomDateChange);
document.getElementById('endDate').addEventListener('change', onCustomDateChange);

function onCustomDateChange() {
    const start = document.getElementById('startDate').value;
    const end = document.getElementById('endDate').value;
    if (start && end) {
        document.querySelectorAll('.date-btn').forEach(function(b) { b.classList.remove('active'); });
        currentDateRange.days = -1;
        currentDateRange.start = start;
        currentDateRange.end = end;
        onDateRangeChange();
    }
}

function onDateRangeChange() {
    renderKPICards();
    initializedTabs.forEach(function(tabId) { updateTabCharts(tabId); });
}

// =============================================================================
// ECharts 通用配置
// =============================================================================
const baseOption = {
    animation: true,
    tooltip: {
        trigger: 'axis',
        backgroundColor: 'rgba(31, 41, 55, 0.95)',
        borderColor: 'transparent',
        textStyle: { color: '#fff', fontSize: 12 },
        axisPointer: { type: 'cross', label: { backgroundColor: '#2563eb' } }
    },
    grid: { left: '3%', right: '4%', bottom: '3%', top: '10%', containLabel: true },
    textStyle: { fontFamily: '-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif' }
};

function getChart(id) {
    if (!charts[id]) {
        const el = document.getElementById(id);
        if (el) { charts[id] = echarts.init(el); }
    }
    return charts[id];
}

function deepMerge(target, source) {
    const result = {};
    for (const key in target) { result[key] = target[key]; }
    for (const key in source) {
        if (source[key] && typeof source[key] === 'object' && !Array.isArray(source[key]) &&
            target[key] && typeof target[key] === 'object' && !Array.isArray(target[key])) {
            result[key] = deepMerge(target[key], source[key]);
        } else {
            result[key] = source[key];
        }
    }
    return result;
}

// =============================================================================
// 总览 Tab
// =============================================================================
function initOverviewCharts() {
    getChart('chart-overview-trend');
    getChart('chart-overview-heatmap');
    getChart('chart-overview-radar');
}

function updateOverviewCharts() {
    const range = getCurrentDateRange();
    const daily = filterDataByDate(dashboardData.allDaily, 'date', range.start, range.end);
    const sleep = filterDataByDate(dashboardData.allSleep, 'date', range.start, range.end);
    const dates = daily.map(function(d) { return d.date; });

    const trendChart = getChart('chart-overview-trend');
    if (trendChart) {
        const sleepByDate = {};
        sleep.forEach(function(s) { sleepByDate[s.date] = s; });
        trendChart.setOption({
            ...baseOption,
            legend: { data: ['步数', '睡眠时长', '静息心率'], top: 0, textStyle: { fontSize: 12, color: '#6b7280' } },
            xAxis: {
                type: 'category', data: dates, boundaryGap: false,
                axisLine: { lineStyle: { color: '#e5e7eb' } },
                axisLabel: { color: '#6b7280', fontSize: 11 }
            },
            yAxis: [
                { type: 'value', name: '步数', position: 'left', axisLine: { show: false }, axisTick: { show: false },
                  splitLine: { lineStyle: { color: '#f3f4f6' } }, axisLabel: { color: '#6b7280', fontSize: 11 } },
                { type: 'value', name: '小时/bpm', position: 'right', axisLine: { show: false }, axisTick: { show: false },
                  splitLine: { show: false }, axisLabel: { color: '#6b7280', fontSize: 11 } }
            ],
            dataZoom: [
                { type: 'inside', start: 0, end: 100 },
                { type: 'slider', height: 20, bottom: 0, start: 0, end: 100 }
            ],
            series: [
                {
                    name: '步数', type: 'bar',
                    data: daily.map(function(d) { return d.steps || 0; }),
                    itemStyle: {
                        color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
                            { offset: 0, color: '#3b82f6' },
                            { offset: 1, color: '#93c5fd' }
                        ]),
                        borderRadius: [3, 3, 0, 0]
                    },
                    barWidth: '40%'
                },
                {
                    name: '睡眠时长', type: 'line', yAxisIndex: 1,
                    data: dates.map(function(date) {
                        const s = sleepByDate[date];
                        return s ? parseFloat((s.asleep_min / 60).toFixed(1)) : null;
                    }),
                    smooth: true, lineStyle: { color: '#8b5cf6', width: 2 }, itemStyle: { color: '#8b5cf6' },
                    areaStyle: {
                        color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
                            { offset: 0, color: 'rgba(139, 92, 246, 0.3)' },
                            { offset: 1, color: 'rgba(139, 92, 246, 0.05)' }
                        ])
                    },
                    connectNulls: true
                },
                {
                    name: '静息心率', type: 'line', yAxisIndex: 1,
                    data: daily.map(function(d) { return d.resting_hr != null ? d.resting_hr : null; }),
                    smooth: true, lineStyle: { color: '#ef4444', width: 2 }, itemStyle: { color: '#ef4444' },
                    connectNulls: true
                }
            ]
        }, true);
    }

    const heatmapChart = getChart('chart-overview-heatmap');
    if (heatmapChart) {
        const heatmapData = daily.map(function(d) { return [d.date, 0, d.steps || 0]; });
        heatmapChart.setOption({
            tooltip: {
                formatter: function(params) {
                    return params.data[0] + '<br/>步数: ' + params.data[2];
                }
            },
            xAxis: { type: 'category', data: dates, axisLine: { show: false }, axisTick: { show: false }, axisLabel: { show: false } },
            yAxis: { type: 'category', data: ['步数'], axisLine: { show: false }, axisTick: { show: false }, axisLabel: { color: '#6b7280', fontSize: 12 } },
            visualMap: {
                min: 0, max: 20000, calculable: false, orient: 'horizontal',
                left: 'center', bottom: 0,
                inRange: { color: ['#dbeafe', '#3b82f6', '#1d4ed8'] },
                textStyle: { fontSize: 11, color: '#6b7280' }
            },
            grid: { left: '10%', right: '5%', top: '10%', bottom: '20%' },
            series: [{ type: 'heatmap', data: heatmapData, label: { show: false }, itemStyle: { borderRadius: 2 } }]
        }, true);
    }

    const radarChart = getChart('chart-overview-radar');
    if (radarChart) {
        const avgSteps = avg(daily, 'steps');
        const avgSleep = avg(sleep, 'asleep_min') / 60;
        const avgRHR = avg(daily, 'resting_hr');
        const avgHRV = avg(daily, 'hrv_sdnn');
        const frag = filterDataByDate(dashboardData.allFragmentation, 'date', range.start, range.end);
        const avgFrag = avg(frag, 'fragmentation_index');

        const stepsScore = Math.min(100, (avgSteps / 10000) * 100);
        const sleepScore = Math.min(100, Math.max(0, 100 - Math.abs(avgSleep - 8) * 20));
        const hrScore = Math.min(100, Math.max(0, (80 - avgRHR) * 2.5));
        const hrvScore = Math.min(100, (avgHRV / 50) * 100);
        const fragScore = Math.min(100, Math.max(0, 100 - avgFrag));

        radarChart.setOption({
            tooltip: {},
            radar: {
                indicator: [
                    { name: '步数', max: 100 },
                    { name: '睡眠', max: 100 },
                    { name: '心率', max: 100 },
                    { name: 'HRV', max: 100 },
                    { name: '行走质量', max: 100 }
                ],
                shape: 'polygon', splitNumber: 4,
                axisName: { color: '#6b7280', fontSize: 12 },
                splitLine: { lineStyle: { color: '#e5e7eb' } },
                splitArea: { areaStyle: { color: ['#fff', '#f9fafb'] } },
                axisLine: { lineStyle: { color: '#e5e7eb' } }
            },
            series: [{
                type: 'radar',
                data: [{
                    value: [Math.round(stepsScore), Math.round(sleepScore), Math.round(hrScore), Math.round(hrvScore), Math.round(fragScore)],
                    name: '健康评分',
                    itemStyle: { color: '#2563eb' },
                    lineStyle: { width: 2 },
                    areaStyle: {
                        color: new echarts.graphic.RadialGradient(0.5, 0.5, 1, [
                            { offset: 0, color: 'rgba(37, 99, 235, 0.6)' },
                            { offset: 1, color: 'rgba(37, 99, 235, 0.1)' }
                        ])
                    }
                }]
            }]
        }, true);
    }
}

// =============================================================================
// 睡眠分析 Tab
// =============================================================================
function initSleepCharts() {
    getChart('chart-sleep-duration');
    getChart('chart-sleep-stages');
    getChart('chart-sleep-midpoint');
    getChart('chart-sleep-scatter');
}

function updateSleepCharts() {
    const range = getCurrentDateRange();
    const sleep = filterDataByDate(dashboardData.allSleep, 'date', range.start, range.end);
    const dates = sleep.map(function(d) { return d.date; });

    const durChart = getChart('chart-sleep-duration');
    if (durChart) {
        durChart.setOption({
            ...baseOption,
            legend: { data: ['深睡', 'REM', '核心睡眠', '清醒', '睡眠效率'], top: 0, textStyle: { fontSize: 12, color: '#6b7280' } },
            xAxis: { type: 'category', data: dates, axisLine: { lineStyle: { color: '#e5e7eb' } }, axisLabel: { color: '#6b7280', fontSize: 11 } },
            yAxis: { type: 'value', name: '分钟', axisLine: { show: false }, axisTick: { show: false },
                     splitLine: { lineStyle: { color: '#f3f4f6' } }, axisLabel: { color: '#6b7280', fontSize: 11 } },
            dataZoom: [
                { type: 'inside', start: 0, end: 100 },
                { type: 'slider', height: 20, bottom: 0, start: 0, end: 100 }
            ],
            series: [
                { name: '深睡', type: 'bar', stack: 'sleep', data: sleep.map(function(d) { return d.deep_min || 0; }),
                  itemStyle: { color: '#1e40af' }, barWidth: '50%' },
                { name: 'REM', type: 'bar', stack: 'sleep', data: sleep.map(function(d) { return d.rem_min || 0; }),
                  itemStyle: { color: '#8b5cf6' } },
                { name: '核心睡眠', type: 'bar', stack: 'sleep', data: sleep.map(function(d) { return d.core_min || 0; }),
                  itemStyle: { color: '#93c5fd' } },
                { name: '清醒', type: 'bar', stack: 'sleep', data: sleep.map(function(d) { return d.awake_min || 0; }),
                  itemStyle: { color: '#fbbf24' } },
                { name: '睡眠效率', type: 'line',
                  data: sleep.map(function(d) { return d.sleep_efficiency != null ? d.sleep_efficiency : null; }),
                  smooth: true, lineStyle: { color: '#10b981', width: 2 }, itemStyle: { color: '#10b981' }, connectNulls: true }
            ]
        }, true);
    }

    const stagesChart = getChart('chart-sleep-stages');
    if (stagesChart) {
        const avgDeep = avg(sleep, 'deep_min');
        const avgRem = avg(sleep, 'rem_min');
        const avgCore = avg(sleep, 'core_min');
        const avgAwake = avg(sleep, 'awake_min');
        stagesChart.setOption({
            tooltip: { trigger: 'item', formatter: '{b}: {c} 分钟 ({d}%)' },
            legend: { bottom: 0, textStyle: { fontSize: 12, color: '#6b7280' } },
            series: [{
                type: 'pie', radius: ['45%', '70%'], center: ['50%', '45%'],
                itemStyle: { borderRadius: 6, borderColor: '#fff', borderWidth: 2 },
                label: { formatter: '{b}\n{d}%', fontSize: 11 },
                labelLine: { length: 10, length2: 10 },
                data: [
                    { value: Math.round(avgDeep), name: '深睡', itemStyle: { color: '#1e40af' } },
                    { value: Math.round(avgRem), name: 'REM', itemStyle: { color: '#8b5cf6' } },
                    { value: Math.round(avgCore), name: '核心睡眠', itemStyle: { color: '#93c5fd' } },
                    { value: Math.round(avgAwake), name: '清醒', itemStyle: { color: '#fbbf24' } }
                ]
            }]
        }, true);
    }

    const midChart = getChart('chart-sleep-midpoint');
    if (midChart) {
        const histogram = {};
        sleep.filter(function(d) { return d.sleep_midpoint_hour != null && d.sleep_midpoint_hour > 0; })
             .forEach(function(d) {
                 const bucket = Math.floor(d.sleep_midpoint_hour * 2) / 2;
                 histogram[bucket] = (histogram[bucket] || 0) + 1;
             });
        const keys = Object.keys(histogram).sort();
        const vals = keys.map(function(k) { return histogram[k]; });
        midChart.setOption({
            ...baseOption,
            xAxis: { type: 'category', data: keys.map(function(k) { return k + ':00'; }),
                     axisLine: { lineStyle: { color: '#e5e7eb' } }, axisLabel: { color: '#6b7280', fontSize: 11 } },
            yAxis: { type: 'value', name: '天数', axisLine: { show: false }, axisTick: { show: false },
                     splitLine: { lineStyle: { color: '#f3f4f6' } }, axisLabel: { color: '#6b7280', fontSize: 11 } },
            series: [{
                type: 'bar', data: vals,
                itemStyle: {
                    color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
                        { offset: 0, color: '#8b5cf6' },
                        { offset: 1, color: '#c4b5fd' }
                    ]),
                    borderRadius: [4, 4, 0, 0]
                },
                barWidth: '60%'
            }]
        }, true);
    }

    const scatterChart = getChart('chart-sleep-scatter');
    if (scatterChart) {
        const scatterData = sleep.filter(function(d) {
            return d.sleep_efficiency != null && d.deep_pct != null;
        }).map(function(d) { return [d.deep_pct * 100, d.sleep_efficiency, d.date]; });
        scatterChart.setOption({
            ...baseOption,
            tooltip: {
                formatter: function(params) {
                    return params.data[2] + '<br/>深睡占比: ' + params.data[0].toFixed(1) + '%<br/>睡眠效率: ' + params.data[1].toFixed(1) + '%';
                }
            },
            xAxis: { type: 'value', name: '深睡占比 (%)', axisLine: { lineStyle: { color: '#e5e7eb' } },
                     axisLabel: { color: '#6b7280', fontSize: 11 }, splitLine: { lineStyle: { color: '#f3f4f6' } } },
            yAxis: { type: 'value', name: '睡眠效率 (%)', axisLine: { lineStyle: { color: '#e5e7eb' } },
                     axisLabel: { color: '#6b7280', fontSize: 11 }, splitLine: { lineStyle: { color: '#f3f4f6' } } },
            series: [{
                type: 'scatter', data: scatterData, symbolSize: 8,
                itemStyle: { color: 'rgba(37, 99, 235, 0.6)', borderColor: '#2563eb', borderWidth: 1 }
            }]
        }, true);
    }
}

// =============================================================================
// 跑步分析 Tab
// =============================================================================
function initRunningCharts() {
    getChart('chart-run-trend');
    getChart('chart-run-hrzones');
    getChart('chart-run-pacehist');
}

function updateRunningCharts() {
    const range = getCurrentDateRange();
    const runs = filterDataByDate(dashboardData.allWorkouts, 'date', range.start, range.end);
    const dates = runs.map(function(d) { return d.date; });

    const trendChart = getChart('chart-run-trend');
    if (trendChart) {
        trendChart.setOption({
            ...baseOption,
            legend: { data: ['跑步距离', '平均配速', '平均心率'], top: 0, textStyle: { fontSize: 12, color: '#6b7280' } },
            xAxis: { type: 'category', data: dates, axisLine: { lineStyle: { color: '#e5e7eb' } }, axisLabel: { color: '#6b7280', fontSize: 11 } },
            yAxis: [
                { type: 'value', name: '距离 (km)', position: 'left', axisLine: { show: false },
                  splitLine: { lineStyle: { color: '#f3f4f6' } }, axisLabel: { color: '#6b7280', fontSize: 11 } },
                { type: 'value', name: '配速 (min/km)', position: 'right', inverse: true, axisLine: { show: false },
                  splitLine: { show: false }, axisLabel: { color: '#6b7280', fontSize: 11 } }
            ],
            dataZoom: [
                { type: 'inside', start: 0, end: 100 },
                { type: 'slider', height: 20, bottom: 0, start: 0, end: 100 }
            ],
            series: [
                {
                    name: '跑步距离', type: 'bar',
                    data: runs.map(function(d) { return d.total_distance; }),
                    itemStyle: {
                        color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
                            { offset: 0, color: '#10b981' },
                            { offset: 1, color: '#6ee7b7' }
                        ]),
                        borderRadius: [4, 4, 0, 0]
                    },
                    barWidth: '40%'
                },
                {
                    name: '平均配速', type: 'line', yAxisIndex: 1,
                    data: runs.map(function(d) { return d.pace_min_km || null; }),
                    smooth: true, lineStyle: { color: '#f59e0b', width: 2 }, itemStyle: { color: '#f59e0b' }, connectNulls: true
                },
                {
                    name: '平均心率', type: 'line', yAxisIndex: 0,
                    data: runs.map(function(d) { return d.avg_hr || null; }),
                    smooth: true, lineStyle: { color: '#ef4444', width: 2, type: 'dashed' }, itemStyle: { color: '#ef4444' }, connectNulls: true
                }
            ]
        }, true);
    }

    const hrChart = getChart('chart-run-hrzones');
    if (hrChart) {
        const zones = {};
        runs.forEach(function(r) {
            if (r.avg_hr) {
                let zone;
                if (r.avg_hr < 120) zone = '轻松 (<120)';
                else if (r.avg_hr < 140) zone = '燃脂 (120-140)';
                else if (r.avg_hr < 160) zone = '有氧 (140-160)';
                else if (r.avg_hr < 180) zone = '无氧 (160-180)';
                else zone = '极限 (>180)';
                zones[zone] = (zones[zone] || 0) + r.total_distance;
            }
        });
        const zoneNames = Object.keys(zones);
        const zoneVals = zoneNames.map(function(k) { return Math.round(zones[k] * 100) / 100; });
        const zoneColors = ['#10b981', '#3b82f6', '#f59e0b', '#ef4444', '#dc2626'];
        hrChart.setOption({
            tooltip: { trigger: 'item', formatter: '{b}: {c} km ({d}%)' },
            legend: { bottom: 0, textStyle: { fontSize: 12, color: '#6b7280' } },
            series: [{
                type: 'pie', radius: ['40%', '65%'], center: ['50%', '45%'],
                itemStyle: { borderRadius: 4, borderColor: '#fff', borderWidth: 2 },
                label: { formatter: '{b}\n{d}%', fontSize: 11 },
                data: zoneNames.map(function(name, i) {
                    return { value: zoneVals[i], name: name, itemStyle: { color: zoneColors[i % zoneColors.length] } };
                })
            }]
        }, true);
    }

    const paceChart = getChart('chart-run-pacehist');
    if (paceChart) {
        const histogram = {};
        runs.filter(function(r) { return r.pace_min_km > 0; }).forEach(function(r) {
            const bucket = Math.floor(r.pace_min_km * 2) / 2;
            histogram[bucket] = (histogram[bucket] || 0) + 1;
        });
        const keys = Object.keys(histogram).sort(function(a, b) { return a - b; });
        const vals = keys.map(function(k) { return histogram[k]; });
        paceChart.setOption({
            ...baseOption,
            xAxis: { type: 'category', data: keys.map(function(k) { return k + " min/km"; }),
                     axisLine: { lineStyle: { color: '#e5e7eb' } }, axisLabel: { color: '#6b7280', fontSize: 10, rotate: 30 } },
            yAxis: { type: 'value', name: '次数', axisLine: { show: false },
                     splitLine: { lineStyle: { color: '#f3f4f6' } }, axisLabel: { color: '#6b7280', fontSize: 11 } },
            series: [{
                type: 'bar', data: vals,
                itemStyle: {
                    color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
                        { offset: 0, color: '#06b6d4' },
                        { offset: 1, color: '#67e8f9' }
                    ]),
                    borderRadius: [4, 4, 0, 0]
                },
                barWidth: '60%'
            }]
        }, true);
    }

    const tbody = document.getElementById('runDetailBody');
    if (tbody) {
        const sortedRuns = runs.slice().sort(function(a, b) { return b.date.localeCompare(a.date); });
        tbody.innerHTML = sortedRuns.map(function(r) {
            return '<tr>' +
                '<td>' + r.date + '</td>' +
                '<td class="num">' + r.total_distance.toFixed(2) + '</td>' +
                '<td class="num">' + r.duration_min.toFixed(1) + '</td>' +
                '<td class="num">' + (r.pace_min_km ? r.pace_min_km.toFixed(2) : '-') + '</td>' +
                '<td class="num">' + (r.avg_hr ? Math.round(r.avg_hr) : '-') + '</td>' +
                '<td class="num">' + Math.round(r.total_energy) + '</td>' +
                '</tr>';
        }).join('');
    }
}

// =============================================================================
// 心率恢复 Tab
// =============================================================================
function initHRVCharts() {
    getChart('chart-hrv-trend');
    getChart('chart-hr-resting');
    getChart('chart-hrv-dist');
    getChart('chart-sleep-hr-scatter');
}

function updateHRVCharts() {
    const range = getCurrentDateRange();
    const daily = filterDataByDate(dashboardData.allDaily, 'date', range.start, range.end);
    const sleep = filterDataByDate(dashboardData.allSleep, 'date', range.start, range.end);
    const dates = daily.map(function(d) { return d.date; });

    const trendChart = getChart('chart-hrv-trend');
    if (trendChart) {
        trendChart.setOption({
            ...baseOption,
            legend: { data: ['静息心率', 'HRV (SDNN)'], top: 0, textStyle: { fontSize: 12, color: '#6b7280' } },
            xAxis: { type: 'category', data: dates, boundaryGap: false,
                     axisLine: { lineStyle: { color: '#e5e7eb' } }, axisLabel: { color: '#6b7280', fontSize: 11 } },
            yAxis: [
                { type: 'value', name: '心率 (bpm)', position: 'left', axisLine: { show: false },
                  splitLine: { lineStyle: { color: '#f3f4f6' } }, axisLabel: { color: '#6b7280', fontSize: 11 } },
                { type: 'value', name: 'HRV (ms)', position: 'right', axisLine: { show: false },
                  splitLine: { show: false }, axisLabel: { color: '#6b7280', fontSize: 11 } }
            ],
            dataZoom: [
                { type: 'inside', start: 0, end: 100 },
                { type: 'slider', height: 20, bottom: 0, start: 0, end: 100 }
            ],
            series: [
                {
                    name: '静息心率', type: 'line',
                    data: daily.map(function(d) { return d.resting_hr != null ? d.resting_hr : null; }),
                    smooth: true, lineStyle: { color: '#ef4444', width: 2 }, itemStyle: { color: '#ef4444' },
                    areaStyle: {
                        color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
                            { offset: 0, color: 'rgba(239, 68, 68, 0.2)' },
                            { offset: 1, color: 'rgba(239, 68, 68, 0.02)' }
                        ])
                    },
                    connectNulls: true
                },
                {
                    name: 'HRV (SDNN)', type: 'line', yAxisIndex: 1,
                    data: daily.map(function(d) { return d.hrv_sdnn != null ? d.hrv_sdnn : null; }),
                    smooth: true, lineStyle: { color: '#10b981', width: 2 }, itemStyle: { color: '#10b981' },
                    areaStyle: {
                        color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
                            { offset: 0, color: 'rgba(16, 185, 129, 0.2)' },
                            { offset: 1, color: 'rgba(16, 185, 129, 0.02)' }
                        ])
                    },
                    connectNulls: true
                }
            ]
        }, true);
    }

    const rhrChart = getChart('chart-hr-resting');
    if (rhrChart) {
        const histogram = {};
        daily.filter(function(d) { return d.resting_hr != null; }).forEach(function(d) {
            const bucket = Math.floor(d.resting_hr / 2) * 2;
            histogram[bucket] = (histogram[bucket] || 0) + 1;
        });
        const keys = Object.keys(histogram).sort(function(a, b) { return a - b; });
        const vals = keys.map(function(k) { return histogram[k]; });
        rhrChart.setOption({
            ...baseOption,
            xAxis: { type: 'category', data: keys.map(function(k) { return k + ' bpm'; }),
                     axisLine: { lineStyle: { color: '#e5e7eb' } }, axisLabel: { color: '#6b7280', fontSize: 10 } },
            yAxis: { type: 'value', name: '天数', axisLine: { show: false }, axisTick: { show: false },
                     splitLine: { lineStyle: { color: '#f3f4f6' } }, axisLabel: { color: '#6b7280', fontSize: 11 } },
            series: [{
                type: 'bar', data: vals,
                itemStyle: {
                    color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
                        { offset: 0, color: '#ef4444' },
                        { offset: 1, color: '#fca5a5' }
                    ]),
                    borderRadius: [4, 4, 0, 0]
                },
                barWidth: '60%'
            }]
        }, true);
    }

    const hrvDistChart = getChart('chart-hrv-dist');
    if (hrvDistChart) {
        const histogram = {};
        daily.filter(function(d) { return d.hrv_sdnn != null; }).forEach(function(d) {
            const bucket = Math.floor(d.hrv_sdnn / 5) * 5;
            histogram[bucket] = (histogram[bucket] || 0) + 1;
        });
        const keys = Object.keys(histogram).sort(function(a, b) { return a - b; });
        const vals = keys.map(function(k) { return histogram[k]; });
        hrvDistChart.setOption({
            ...baseOption,
            xAxis: { type: 'category', data: keys.map(function(k) { return k + ' ms'; }),
                     axisLine: { lineStyle: { color: '#e5e7eb' } }, axisLabel: { color: '#6b7280', fontSize: 10, rotate: 30 } },
            yAxis: { type: 'value', name: '天数', axisLine: { show: false }, axisTick: { show: false },
                     splitLine: { lineStyle: { color: '#f3f4f6' } }, axisLabel: { color: '#6b7280', fontSize: 11 } },
            series: [{
                type: 'bar', data: vals,
                itemStyle: {
                    color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
                        { offset: 0, color: '#10b981' },
                        { offset: 1, color: '#6ee7b7' }
                    ]),
                    borderRadius: [4, 4, 0, 0]
                },
                barWidth: '60%'
            }]
        }, true);
    }

    const scatterChart = getChart('chart-sleep-hr-scatter');
    if (scatterChart) {
        const dateMap = {};
        daily.forEach(function(d) {
            if (d.resting_hr != null) {
                dateMap[d.date] = dateMap[d.date] || {};
                dateMap[d.date].rhr = d.resting_hr;
            }
        });
        sleep.forEach(function(d) {
            if (d.asleep_min > 0) {
                dateMap[d.date] = dateMap[d.date] || {};
                dateMap[d.date].sleep = d.asleep_min / 60;
            }
        });
        const scatterData = [];
        Object.keys(dateMap).forEach(function(date) {
            const v = dateMap[date];
            if (v.rhr != null && v.sleep != null) {
                scatterData.push([v.sleep, v.rhr, date]);
            }
        });
        scatterChart.setOption({
            ...baseOption,
            tooltip: {
                formatter: function(params) {
                    return params.data[2] + '<br/>睡眠: ' + params.data[0].toFixed(1) + ' h<br/>静息心率: ' + params.data[1] + ' bpm';
                }
            },
            xAxis: { type: 'value', name: '睡眠时长 (小时)', axisLine: { lineStyle: { color: '#e5e7eb' } },
                     axisLabel: { color: '#6b7280', fontSize: 11 }, splitLine: { lineStyle: { color: '#f3f4f6' } } },
            yAxis: { type: 'value', name: '静息心率 (bpm)', axisLine: { lineStyle: { color: '#e5e7eb' } },
                     axisLabel: { color: '#6b7280', fontSize: 11 }, splitLine: { lineStyle: { color: '#f3f4f6' } } },
            series: [{
                type: 'scatter', data: scatterData, symbolSize: 9,
                itemStyle: { color: 'rgba(139, 92, 246, 0.6)', borderColor: '#8b5cf6', borderWidth: 1 }
            }]
        }, true);
    }
}

// =============================================================================
// 碎脚指标 Tab
// =============================================================================
function initFragmentationCharts() {
    getChart('chart-frag-trend');
    getChart('chart-frag-pie');
    getChart('chart-frag-histogram');
}

function updateFragmentationCharts() {
    const range = getCurrentDateRange();
    const frag = filterDataByDate(dashboardData.allFragmentation, 'date', range.start, range.end);
    const dates = frag.map(function(d) { return d.date; });

    const trendChart = getChart('chart-frag-trend');
    if (trendChart) {
        const grades = ['A', 'B', 'C', 'D', 'E'];
        const gradeColors = ['#10b981', '#34d399', '#f59e0b', '#f97316', '#ef4444'];

        const scatterSeries = grades.map(function(grade, i) {
            return {
                name: grade + '级', type: 'scatter',
                data: frag.filter(function(d) { return d.grade === grade; }).map(function(d) { return [d.date, d.fragmentation_index]; }),
                symbolSize: 8, itemStyle: { color: gradeColors[i] }, z: 10
            };
        });

        const lineSeries = {
            name: '碎脚指数', type: 'line',
            data: frag.map(function(d) { return d.fragmentation_index; }),
            smooth: true, lineStyle: { color: '#2563eb', width: 2 }, itemStyle: { color: '#2563eb' },
            areaStyle: {
                color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
                    { offset: 0, color: 'rgba(37, 99, 235, 0.15)' },
                    { offset: 1, color: 'rgba(37, 99, 235, 0.02)' }
                ])
            },
            markArea: {
                silent: true,
                data: [
                    [{ yAxis: 0, itemStyle: { color: 'rgba(16, 185, 129, 0.05)' } }, { yAxis: 20 }],
                    [{ yAxis: 20, itemStyle: { color: 'rgba(52, 211, 153, 0.05)' } }, { yAxis: 40 }],
                    [{ yAxis: 40, itemStyle: { color: 'rgba(245, 158, 11, 0.05)' } }, { yAxis: 60 }],
                    [{ yAxis: 60, itemStyle: { color: 'rgba(249, 115, 22, 0.05)' } }, { yAxis: 80 }],
                    [{ yAxis: 80, itemStyle: { color: 'rgba(239, 68, 68, 0.05)' } }, { yAxis: 100 }]
                ]
            },
            z: 5
        };

        trendChart.setOption({
            ...baseOption,
            legend: { data: ['碎脚指数', 'A级', 'B级', 'C级', 'D级', 'E级'], top: 0, textStyle: { fontSize: 12, color: '#6b7280' } },
            xAxis: { type: 'category', data: dates, boundaryGap: false,
                     axisLine: { lineStyle: { color: '#e5e7eb' } }, axisLabel: { color: '#6b7280', fontSize: 11 } },
            yAxis: { type: 'value', name: '碎脚指数', min: 0, max: 100, axisLine: { show: false },
                     splitLine: { lineStyle: { color: '#f3f4f6' } }, axisLabel: { color: '#6b7280', fontSize: 11 } },
            dataZoom: [
                { type: 'inside', start: 0, end: 100 },
                { type: 'slider', height: 20, bottom: 0, start: 0, end: 100 }
            ],
            series: [lineSeries].concat(scatterSeries)
        }, true);
    }

    const pieChart = getChart('chart-frag-pie');
    if (pieChart) {
        const gradeDist = {};
        frag.forEach(function(d) {
            const g = d.grade || 'N/A';
            gradeDist[g] = (gradeDist[g] || 0) + 1;
        });
        const gradeColors = { A: '#10b981', B: '#34d399', C: '#f59e0b', D: '#f97316', E: '#ef4444' };
        const gradeNames = {
            A: 'A级 (高效)', B: 'B级 (轻度)', C: 'C级 (中度)', D: 'D级 (高度)', E: 'E级 (极度)'
        };
        const data = Object.keys(gradeDist).sort().map(function(g) {
            return {
                value: gradeDist[g],
                name: gradeNames[g] || g,
                itemStyle: { color: gradeColors[g] || '#6b7280' }
            };
        });
        pieChart.setOption({
            tooltip: { trigger: 'item', formatter: '{b}: {c} 天 ({d}%)' },
            legend: { bottom: 0, textStyle: { fontSize: 12, color: '#6b7280' } },
            series: [{
                type: 'pie', radius: ['40%', '65%'], center: ['50%', '45%'],
                itemStyle: { borderRadius: 6, borderColor: '#fff', borderWidth: 2 },
                label: { formatter: '{b}\n{d}%', fontSize: 11 },
                labelLine: { length: 10, length2: 8 },
                data: data
            }]
        }, true);
    }

    const histChart = getChart('chart-frag-histogram');
    if (histChart) {
        const histogram = {};
        frag.forEach(function(d) {
            const bout = d.avg_bout_min || 0;
            const bucket = Math.floor(bout / 5) * 5;
            histogram[bucket] = (histogram[bucket] || 0) + 1;
        });
        const keys = Object.keys(histogram).map(Number).sort(function(a, b) { return a - b; });
        const vals = keys.map(function(k) { return histogram[k]; });
        histChart.setOption({
            ...baseOption,
            xAxis: { type: 'category', data: keys.map(function(k) { return k + '-' + (k + 5) + ' min'; }),
                     axisLine: { lineStyle: { color: '#e5e7eb' } }, axisLabel: { color: '#6b7280', fontSize: 10, rotate: 30 } },
            yAxis: { type: 'value', name: '天数', axisLine: { show: false },
                     splitLine: { lineStyle: { color: '#f3f4f6' } }, axisLabel: { color: '#6b7280', fontSize: 11 } },
            series: [{
                type: 'bar', data: vals,
                itemStyle: {
                    color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
                        { offset: 0, color: '#f59e0b' },
                        { offset: 1, color: '#fcd34d' }
                    ]),
                    borderRadius: [4, 4, 0, 0]
                },
                barWidth: '60%'
            }]
        }, true);
    }
}

// =============================================================================
// 数据明细 Tab
// =============================================================================
function initDetailTab() {
    updateDetailTable();
}

function updateDetailTable() {
    const range = getCurrentDateRange();
    const daily = filterDataByDate(dashboardData.allDaily, 'date', range.start, range.end);
    const frag = filterDataByDate(dashboardData.allFragmentation, 'date', range.start, range.end);

    const fragMap = {};
    frag.forEach(function(f) { fragMap[f.date] = f; });

    const tbody = document.getElementById('dailyDetailBody');
    if (tbody) {
        const sorted = daily.slice().sort(function(a, b) { return b.date.localeCompare(a.date); });
        tbody.innerHTML = sorted.map(function(d) {
            const f = fragMap[d.date];
            const grade = f ? f.grade : '-';
            let gradeHtml = grade;
            if (grade !== '-') {
                const gradeClass = (grade === 'A' || grade === 'B') ? 'success' :
                                   (grade === 'C' ? 'warning' :
                                   ((grade === 'D' || grade === 'E') ? 'danger' : ''));
                if (gradeClass) {
                    gradeHtml = '<span class="badge ' + gradeClass + '">' + grade + '级</span>';
                }
            }
            return '<tr>' +
                '<td>' + d.date + '</td>' +
                '<td class="num">' + (d.steps ? d.steps.toLocaleString() : '-') + '</td>' +
                '<td class="num">' + (d.walk_run_km ? d.walk_run_km.toFixed(2) : '-') + '</td>' +
                '<td class="num">' + (d.active_kcal ? Math.round(d.active_kcal) : '-') + '</td>' +
                '<td class="num">' + (d.resting_hr != null ? d.resting_hr : '-') + '</td>' +
                '<td class="num">' + (d.hrv_sdnn != null ? d.hrv_sdnn.toFixed(1) : '-') + '</td>' +
                '<td class="num">' + (d.asleep_min != null ? Math.round(d.asleep_min) : '-') + '</td>' +
                '<td>' + gradeHtml + '</td>' +
                '</tr>';
        }).join('');
    }
}

// =============================================================================
// Tab 路由
// =============================================================================
function initTabCharts(tabId) {
    switch (tabId) {
        case 'overview': initOverviewCharts(); break;
        case 'sleep': initSleepCharts(); break;
        case 'running': initRunningCharts(); break;
        case 'hrv': initHRVCharts(); break;
        case 'fragmentation': initFragmentationCharts(); break;
        case 'detail': initDetailTab(); break;
    }
}

function updateTabCharts(tabId) {
    switch (tabId) {
        case 'overview': updateOverviewCharts(); break;
        case 'sleep': updateSleepCharts(); break;
        case 'running': updateRunningCharts(); break;
        case 'hrv': updateHRVCharts(); break;
        case 'fragmentation': updateFragmentationCharts(); break;
        case 'detail': updateDetailTable(); break;
    }
}

// =============================================================================
// 导出功能
// =============================================================================
function exportDashboard() {
    const dataStr = JSON.stringify(dashboardData, null, 2);
    const blob = new Blob([dataStr], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = 'health_dashboard_data.json';
    a.click();
    URL.revokeObjectURL(url);
}

// =============================================================================
// 窗口大小变化
// =============================================================================
window.addEventListener('resize', function() {
    Object.values(charts).forEach(function(chart) {
        if (chart && chart.resize) chart.resize();
    });
});

// =============================================================================
// 初始化
// =============================================================================
function init() {
    document.getElementById('startDate').min = dashboardData.minDate;
    document.getElementById('startDate').max = dashboardData.maxDate;
    document.getElementById('endDate').min = dashboardData.minDate;
    document.getElementById('endDate').max = dashboardData.maxDate;

    const range = getCurrentDateRange();
    document.getElementById('startDate').value = range.start;
    document.getElementById('endDate').value = range.end;

    renderKPICards();
    initTabCharts('overview');
    updateTabCharts('overview');
}

if (typeof echarts !== 'undefined') {
    init();
} else {
    window.addEventListener('load', init);
}
</script>
</body>
</html>'''


# =============================================================================
# HTML 生成
# =============================================================================

def generate_html(data, output_path):
    """生成完整的 HTML 仪表盘"""

    daily_data = data["daily"]
    sleep_data = data["sleep"]
    workouts_data = data["workouts"]
    frag_data = data["fragmentation"]

    # 获取完整日期范围
    start_date, end_date = get_date_range(daily_data)
    min_date = start_date.strftime("%Y-%m-%d")
    max_date = end_date.strftime("%Y-%m-%d")

    # KPI 卡片（30天）
    kpi_cards = compute_kpi_card(daily_data, sleep_data, workouts_data, frag_data, days=30)

    # 每日图表数据
    daily_chart_data = []
    for row in daily_data:
        date = row.get("date", "")
        if not date:
            continue
        daily_chart_data.append({
            "date": date,
            "steps": safe_int(row.get("steps"), 0),
            "walk_run_km": safe_float(row.get("walk_run_km"), 0),
            "resting_hr": safe_float(row.get("resting_hr")),
            "hrv_sdnn": safe_float(row.get("hrv_sdnn")),
            "active_kcal": safe_float(row.get("active_kcal"), 0),
            "exercise_min": safe_float(row.get("exercise_min"), 0),
            "asleep_min": safe_float(row.get("asleep_min")),
        })

    # 睡眠数据
    sleep_chart_data = []
    for row in sleep_data:
        date = row.get("date", "")
        if not date:
            continue
        sleep_chart_data.append({
            "date": date,
            "asleep_min": safe_float(row.get("asleep_min"), 0),
            "deep_min": safe_float(row.get("deep_min"), 0),
            "rem_min": safe_float(row.get("rem_min"), 0),
            "core_min": safe_float(row.get("core_min"), 0),
            "awake_min": safe_float(row.get("awake_min"), 0),
            "sleep_efficiency": safe_float(row.get("sleep_efficiency")),
            "sleep_midpoint_hour": safe_float(row.get("sleep_midpoint_hour")),
            "deep_pct": safe_float(row.get("deep_pct")),
            "rem_pct": safe_float(row.get("rem_pct")),
        })

    # 跑步数据
    run_workouts = [r for r in workouts_data if r.get("type") == "Running"]
    run_chart_data = []
    for row in run_workouts:
        date = row.get("date", "")
        if not date:
            continue
        dist = safe_float(row.get("total_distance"), 0)
        dur = safe_float(row.get("duration_min"), 0)
        pace = dur / dist if dist > 0 else 0
        run_chart_data.append({
            "date": date,
            "total_distance": round(dist, 2),
            "duration_min": round(dur, 2),
            "avg_hr": safe_float(row.get("avg_hr")),
            "max_hr": safe_float(row.get("max_hr")),
            "avg_speed": safe_float(row.get("avg_speed")),
            "avg_stride_length": safe_float(row.get("avg_stride_length")),
            "avg_ground_contact_ms": safe_float(row.get("avg_ground_contact_ms")),
            "pace_min_km": round(pace, 2),
            "total_energy": safe_float(row.get("total_energy"), 0),
        })

    # 碎脚数据
    frag_chart_data = []
    for row in frag_data:
        date = row.get("date", "")
        if not date:
            continue
        frag_chart_data.append({
            "date": date,
            "fragmentation_index": safe_float(row.get("fragmentation_index"), 0),
            "grade": row.get("grade", ""),
            "walk_bouts_count": safe_int(row.get("walk_bouts_count"), 0),
            "avg_bout_min": safe_float(row.get("avg_bout_min"), 0),
        })

    # 碎脚评级分布
    grade_distribution = defaultdict(int)
    for row in frag_chart_data:
        grade = row["grade"] or "N/A"
        grade_distribution[grade] += 1

    # 行走段时长分布
    bout_histogram = defaultdict(int)
    for row in frag_chart_data:
        bout_min = row.get("avg_bout_min", 0)
        bucket = int(bout_min // 5) * 5
        bout_histogram[bucket] += 1

    # 跑步心率区间
    hr_zones = defaultdict(float)
    for row in run_chart_data:
        avg_hr = row.get("avg_hr")
        if avg_hr:
            if avg_hr < 120:
                zone = "轻松 (<120)"
            elif avg_hr < 140:
                zone = "燃脂 (120-140)"
            elif avg_hr < 160:
                zone = "有氧 (140-160)"
            elif avg_hr < 180:
                zone = "无氧 (160-180)"
            else:
                zone = "极限 (>180)"
            hr_zones[zone] += row["total_distance"]

    # 构建注入数据
    dashboard_data = {
        "allDaily": daily_chart_data,
        "allSleep": sleep_chart_data,
        "allWorkouts": run_chart_data,
        "allFragmentation": frag_chart_data,
        "kpiCards": kpi_cards,
        "minDate": min_date,
        "maxDate": max_date,
        "gradeDistribution": dict(grade_distribution),
        "hrZones": dict(hr_zones),
        "boutHistogram": dict(bout_histogram),
    }

    dashboard_json = json.dumps(dashboard_data, ensure_ascii=False)

    # 获取模板并替换占位符
    html = get_html_template()
    html = html.replace("__ECHARTS_CDN__", ECHARTS_CDN)
    html = html.replace("__COLOR_PRIMARY__", COLORS["primary"])
    html = html.replace("__COLOR_SUCCESS__", COLORS["success"])
    html = html.replace("__COLOR_WARNING__", COLORS["warning"])
    html = html.replace("__COLOR_DANGER__", COLORS["danger"])
    html = html.replace("__COLOR_GRAY_DARK__", COLORS["gray_dark"])
    html = html.replace("__COLOR_GRAY_MID__", COLORS["gray_mid"])
    html = html.replace("__COLOR_GRAY_LIGHT__", COLORS["gray_light"])
    html = html.replace("__COLOR_PURPLE__", COLORS["purple"])
    html = html.replace("__COLOR_PINK__", COLORS["pink"])
    html = html.replace("__COLOR_CYAN__", COLORS["cyan"])
    html = html.replace("__DASHBOARD_DATA__", dashboard_json)
    html = html.replace("__GENERATED_TIME__", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    html = html.replace("__TOTAL_DAYS__", str(len(daily_data)))

    # 确保输出目录存在
    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    # 写入 HTML 文件
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)

    file_size = len(html.encode("utf-8")) / 1024
    print("仪表盘已生成: " + output_path)
    print("  - 数据天数: " + str(len(daily_data)) + " 天")
    print("  - 睡眠记录: " + str(len(sleep_data)) + " 条")
    print("  - 跑步记录: " + str(len(run_workouts)) + " 次")
    print("  - 碎脚数据: " + str(len(frag_data)) + " 天")
    print("  - 文件大小: {:.1f} KB".format(file_size))


# =============================================================================
# 主入口
# =============================================================================

def main():
    script_dir = os.path.dirname(os.path.abspath(__file__))
    project_dir = os.path.dirname(script_dir)

    parser = argparse.ArgumentParser(description="生成交互式健康仪表盘")
    parser.add_argument(
        "--data",
        default=os.path.join(project_dir, "data", "processed"),
        help="数据目录路径（包含 CSV 文件）",
    )
    parser.add_argument(
        "--output",
        default=os.path.join(project_dir, "reports", "interactive_health_dashboard.html"),
        help="输出 HTML 文件路径",
    )

    args = parser.parse_args()

    data_dir = os.path.abspath(args.data)
    output_path = os.path.abspath(args.output)

    print("数据目录: " + data_dir)
    print("输出文件: " + output_path)
    print()

    # 加载数据
    print("正在加载数据...")
    data = load_all_data(data_dir)
    print("  - daily_monitoring_wide: " + str(len(data["daily"])) + " 行")
    print("  - sleep_daily: " + str(len(data["sleep"])) + " 行")
    print("  - workouts: " + str(len(data["workouts"])) + " 行")
    print("  - fragmentation: " + str(len(data["fragmentation"])) + " 行")
    print()

    # 生成 HTML
    print("正在生成仪表盘...")
    generate_html(data, output_path)
    print()
    print("完成！")


if __name__ == "__main__":
    main()
