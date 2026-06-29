# 健康数据 ETL 规范

> 本文件定义 Apple Health 数据的提取、转换、加载（ETL）流程规范。处理原始健康数据时读取此文件。

## 目录

1. [数据来源](#数据来源)
2. [ETL 流程](#etl-流程)
3. [输出数据结构](#输出数据结构)
4. [数据质量校验](#数据质量校验)
5. [性能优化](#性能优化)

---

## 数据来源

### Apple Health 导出格式
- 主文件：`export.xml`
- 附加数据：心电图（electrocardiograms/）、运动路线（workout-routes/）

### 核心记录类型

#### 数量型记录（HKQuantityTypeIdentifier）
- 步数相关：StepCount, DistanceWalkingRunning, FlightsClimbed
- 能量相关：ActiveEnergyBurned, BasalEnergyBurned
- 心率相关：HeartRate, RestingHeartRate, HeartRateVariabilitySDNN
- 血氧呼吸：OxygenSaturation, RespiratoryRate
- 身体指标：BodyMass, BodyMassIndex, BodyFatPercentage
- 运动能力：VO2Max, WalkingSpeed, WalkingStepLength
- 行走稳定性：AppleWalkingSteadiness, WalkingDoubleSupportPercentage

#### 类别型记录（HKCategoryTypeIdentifier）
- 睡眠分析：SleepAnalysis（含各阶段）
- 睡眠时长：SleepDurationGoal

#### 运动记录（Workout）
- 类型：Running, Walking, Cycling, Swimming 等
- 统计：距离、时长、能量、心率、配速、步幅等

---

## ETL 流程

### 第一步：提取（Extract）

使用流式 XML 解析（`xml.etree.ElementTree.iterparse`）处理大文件：

```python
import xml.etree.ElementTree as ET

context = ET.iterparse(export_xml, events=('start', 'end'))
_, root = next(context)
for event, elem in context:
    if event != 'end':
        continue
    # 处理每条记录
    process_record(elem)
    elem.clear()
    root.clear()
```

**注意**：必须及时清理内存，避免大文件 OOM。

### 第二步：转换（Transform）

#### 记录类型清洗
- 去除 `HKQuantityTypeIdentifier` 等前缀
- 统一单位命名

#### 时间处理
- 统一为 ISO 格式日期
- 处理时区偏移
- 按日期分组聚合

#### 数值清洗
- 过滤异常值（生理不可能范围）
- 处理缺失值
- 单位统一转换

### 第三步：加载（Load）

输出多个 CSV 文件到 `data/processed/` 目录：
- `daily_metrics.csv` — 每日指标长表
- `daily_monitoring_wide.csv` — 每日监控宽表
- `sleep_daily.csv` — 每日睡眠数据
- `workouts.csv` — 运动记录
- `record_types.csv` — 指标字典

---

## 输出数据结构

### daily_monitoring_wide.csv（核心宽表）

| 字段 | 类型 | 说明 |
|------|------|------|
| date | string | 日期 YYYY-MM-DD |
| steps | float | 步数 |
| walk_run_km | float | 步行/跑步距离 km |
| active_kcal | float | 活动能量 kcal |
| basal_kcal | float | 基础能量 kcal |
| exercise_min | float | 锻炼分钟 |
| stand_min | float | 站立分钟 |
| heart_rate_avg | float | 全天平均心率 bpm |
| resting_hr | float | 静息心率 bpm |
| walking_hr_avg | float | 步行平均心率 bpm |
| hrv_sdnn | float | HRV SDNN ms |
| vo2max | float | VO2 Max |
| respiratory_rate | float | 呼吸频率 |
| oxygen_saturation | float | 血氧饱和度（0-1） |
| body_mass | float | 体重 kg |
| asleep_min | float | 睡眠时长 min |
| deep_min | float | 深睡时长 min |
| rem_min | float | REM 时长 min |
| sleep_efficiency | float | 睡眠效率（0-1） |

### sleep_daily.csv

| 字段 | 类型 | 说明 |
|------|------|------|
| date | string | 日期 |
| sleep_start | string | 入睡时间 |
| sleep_end | string | 醒来时间 |
| in_bed_min | float | 卧床时长 min |
| asleep_min | float | 睡眠时长 min |
| core_min | float | 核心睡眠 min |
| deep_min | float | 深睡 min |
| rem_min | float | REM min |
| awake_min | float | 清醒 min |
| sleep_efficiency | float | 睡眠效率 |
| deep_pct | float | 深睡占比 |
| rem_pct | float | REM 占比 |
| sleep_midpoint_hour | float | 睡眠中点小时 |

### workouts.csv

| 字段 | 类型 | 说明 |
|------|------|------|
| date | string | 日期 |
| type | string | 运动类型 |
| duration_min | float | 时长 min |
| total_distance | float | 总距离 |
| distance_unit | string | 距离单位 |
| total_energy | float | 总能量 |
| avg_hr | float | 平均心率 |
| max_hr | float | 最大心率 |
| avg_speed | float | 平均速度 |
| avg_stride_length | float | 平均步幅 |
| avg_ground_contact_ms | float | 平均触地时间 ms |
| avg_running_power | float | 平均跑步功率 |
| workout_steps | float | 运动步数 |
| source | string | 数据来源 |
| start | string | 开始时间 |
| end | string | 结束时间 |

---

## 数据质量校验

### 必检项

1. **日期连续性**：检查是否有日期断层
2. **数值范围**：
   - 静息心率：30-100 bpm
   - 血氧：0.85-1.0
   - 睡眠时长：0-14 小时
   - 每日步数：0-50000 步
3. **逻辑一致性**：
   - 深睡 ≤ 总睡眠
   - 各睡眠阶段之和 ≈ 总睡眠
   - 运动距离 > 0 时运动时长 > 0

### 异常处理

- 单条异常记录：标记并排除
- 某天数据严重缺失：标记为数据不足
- 连续多天缺失：发出警告

---

## 性能优化

### 内存优化
- 使用流式 XML 解析而非 DOM
- 及时释放已处理元素
- 按日期分批处理

### 速度优化
- 使用 `defaultdict` 批量聚合
- 避免重复的日期解析
- 数值计算使用内置函数

### 文件大小
- CSV 使用最紧凑格式
- 可选：压缩输出（gzip）
- 可选：仅保留最近 N 天数据
