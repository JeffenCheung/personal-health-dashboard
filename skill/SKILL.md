---
name: personal-health-dashboard
description: "基于 Apple Health/Apple Watch 导出数据生成交互式个人健康监控仪表盘，包含碎脚指标、睡眠分析、跑步步态、心率变异等多维度分析，并支持飞书消息/日历/文档同步。当用户要求生成健康报告、健康数据可视化、碎脚指标分析、健康仪表盘、健康情况复盘、运动数据分析、睡眠质量分析、或同步健康数据到飞书时调用此技能。不要在用户仅询问健康常识、需要医疗诊断、或处理非 Apple Health 格式数据时使用。"
---

# 个人健康仪表盘生成器

## 描述

本技能基于 Apple Health 导出数据，生成功能完善的交互式 HTML 健康监控仪表盘。核心创新指标为"碎脚指标"——通过分析步数时间分布，量化日常行走的碎片化程度与能量效率。支持数据筛选、图表互动、趋势分析，并可将报告同步至飞书消息、日历和文档。

## 使用场景

**使用此技能（正向条件）：**
- 用户要求生成健康仪表盘、健康报告、健康数据可视化
- 用户提到"碎脚指标"、步行效率分析、碎片化行走分析
- 用户要求将健康数据同步到飞书（消息/日历/文档）
- 用户需要交互式健康数据看板、可筛选的健康趋势图
- 用户要求分析 Apple Watch/Apple Health 导出的健康数据

**不使用此技能（负向条件）：**
- 用户仅询问健康常识或医疗建议（不提供数据）
- 用户需要医学诊断或治疗方案
- 数据源不是 Apple Health 导出格式
- 用户仅查看已有报告而不要求生成新报告
- 实时健康监控或预警（本技能为离线分析）

## 输入

- `data_source`: string — Apple Health 导出数据路径（export.xml 或已处理的 CSV 目录）
- `output_dir`: string — 输出目录路径，默认为当前项目的 reports/ 目录
- `enable_feishu`: boolean — 是否启用飞书同步，默认 false
- `feishu_config`: object — 飞书配置（webhook、app_id、app_secret 等），可选
- `analysis_days`: number — 分析天数，默认 90 天

## 输出

- `dashboard_path`: string — 生成的交互式 HTML 仪表盘文件路径
- `report_path`: string — Markdown 格式深度分析报告路径
- `fragmentation_index`: object — 碎脚指标计算结果（含每日数据、趋势、评分）
- `feishu_result`: object — 飞书同步结果（如启用），含消息、日历、文档同步状态

## 指令

1. **检查数据源**：确认 Apple Health 导出文件是否存在，识别数据格式（XML 或 CSV）
   - 若为原始 XML，先执行 ETL 处理，参见 `references/health-etl-spec.md`
   - 若为已处理 CSV，直接加载数据

2. **计算碎脚指标**：基于步数时间分布计算碎片化行走指数
   - 算法细节参见 `references/fragmentation-index.md`
   - 核心公式：碎脚指数 = 碎片化行走段数 / 总行走段数 × 平均段间隔时间系数
   - 输出：每日碎脚分、趋势变化、效率评级（A/B/C/D）

3. **生成交互式仪表盘**：构建含数据筛选和图表互动的 HTML 页面
   - 使用 ECharts 实现交互式图表（折线、柱状、热力图）
   - 仪表盘模板参见 `templates/dashboard-template.html`
   - 功能模块：总览卡片、睡眠分析、跑步步态、心率恢复、碎脚指标、数据明细
   - 交互功能：日期范围筛选、指标切换、图表缩放、数据导出

4. **生成深度分析报告**：输出 Markdown 格式的健康分析报告
   - 报告结构：总览 → 跑步负荷 → 步态分析 → 睡眠质量 → 心率恢复 → 碎脚指标 → 行动建议
   - 重点关注用户主诉问题（如小腿疼痛、睡眠质量等）

5. **飞书同步（可选）**：将仪表盘和报告同步到飞书
   - 飞书消息：推送关键指标卡片和仪表盘链接，参见 `references/feishu-integration.md`
   - 飞书日历：记录运动和睡眠事件，标记异常日期
   - 飞书文档：同步完整报告和数据看板
   - 失败时记录错误但不阻塞主流程

6. **验证输出**：检查生成文件完整性，确认关键指标有数据

## 失败策略

- **数据源缺失**：提示用户提供 Apple Health 导出文件，并说明导出方法
- **数据量不足**：仍生成报告，但标注数据不足的指标，不中断流程
- **碎脚指标无法计算**：当日步数记录粒度不够时，降级为"今日步数总量"展示
- **飞书同步失败**：记录错误日志，继续生成本地报告，最后告知用户同步失败原因
- **图表渲染异常**：降级为静态 SVG 图表，确保页面可用

## 参考文件

| 文件 | 用途 | 何时读取 |
|------|------|----------|
| `references/health-etl-spec.md` | Apple Health 数据 ETL 处理规范 | 处理原始 XML 数据时 |
| `references/fragmentation-index.md` | 碎脚指标算法说明与计算公式 | 计算碎脚指标时 |
| `references/feishu-integration.md` | 飞书消息/日历/文档集成 API 说明 | 启用飞书同步时 |
| `references/dashboard-design.md` | 仪表盘交互设计规范与组件说明 | 设计和开发仪表盘时 |
| `templates/dashboard-template.html` | 交互式仪表盘 HTML 模板 | 生成仪表盘时 |
| `scripts/generate_dashboard.py` | 仪表盘生成主脚本 | 执行生成时 |
| `scripts/calc_fragmentation.py` | 碎脚指标计算脚本 | 计算碎脚指标时 |
| `examples/sample-output.md` | 输出示例 | 需要参考预期格式时 |
