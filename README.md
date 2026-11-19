# cc-schema-lab

CC_Codex 二期（26.11）**数据工程**产品线：**契约换版台**（aiohttp + 原生 JS SPA + SQLite）。

## 一句话骨架（自有，非外仓抄题）

**表契约版本登记 → 破坏性检查 → 换版应用 → 目录对账**

与现有九线正交（不是采集、ETL、同步、解析、编排、摄取、留存、清单、水印）。
不碰外仓常见指纹（Feed 归并 / CDC 游标 / 质检签发 / Slicegate 类仓名）。

## 领域

- **所属领域**：数据工程
- **子方向**：数仓与数据湖（表契约 / 换版闭环）

## 技术栈

| 层 | 选型 |
|----|------|
| 后端 | Python 3.12、aiohttp、SQLite |
| 前端 | 原生 JS SPA（全中文），暖杏纸面 + 墨绿强调；顶栏胶囊导航 |
| 测试 | pytest + pytest-aiohttp（本地秒级闭环，优先于 compose） |

## 基线已通主链

1. 表契约版本（表名 / 版本号 / 现行或草稿）
2. 对候选字段做破坏性检查（删列 / 改类型）
3. 换版应用（草稿升现行，旧现行停用）
4. 契约与目录表对账

## 退版窗口与误退收口（schema-bug-08）

| 规则 | 落点 |
|------|------|
| 窗口判定真实读库 | `meta.rollback_window_hours` + `meta.last_apply_at`，过窗即拦，不写版指针 |
| 窗口内退版单事务 | 契约现行状态与 `catalog_tables.live_version` 同一事务同步回退 |
| 契约版与目录版必须对齐 | 退版 / 收口前比对，拧花直接 409 拒绝 |
| 窗口外误退只有一个结局 | `撤回`（整笔恢复退前现行，误退版不可读）或 `留仓`（快照进只读仓，误退版仍可读），两条结局禁止同挂 |
| 左右仓管并发 | `BEGIN IMMEDIATE` + 条件更新（CAS），不互相覆盖版指针 |
| 留痕 | `rollback_events` / `rollback_archive`，时间线可查 |

页面四处对齐同一标记：**回滚台**（退版、误退收口、撤回/留仓芯片）、**时间线**（换版/退版/误退留痕详情）、
**目录对账**（漂移 + 误退待收口/已收口芯片）、**目录席**（契约版 vs 目录版 + 留仓误退版）。
清洗复验独立于对账再验一轮版号，并核对留仓误退版仍可读。


## 本地

```powershell
cd claude-workspace\cc-schema-lab
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
pytest
python run.py
```

http://127.0.0.1:7100/login · **admin / 123456**

## Docker

端口 **7100**。冒烟：`python ..\scripts\_smoke_lab_access.py --dir .`
