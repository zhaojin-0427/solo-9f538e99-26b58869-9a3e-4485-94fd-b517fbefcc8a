# XBRL 财报勾稽核查 API（FastAPI + SQLite）

财报入仓员用它判断一份 XBRL 实例与计算关系包之间的勾稽差额，**是否只是
`decimals` 舍入造成的**，从而决定受理或退回重报。

## 核查语义

- 每份实例只允许 **一个实体、一个期间、一种币种、无维度**；数值事实 ≤ 30 个，
  等式 ≤ 30 条，计算权重仅允许 `+1 / -1`，提交时强校验。
- 事实 `decimals=d` 的真值落在闭区间
  `[value − 0.5×10^−d, value + 0.5×10^−d]`；`decimals=INF` 表示精确值（区间退化为点）。
- **全部等式共用同一组事实真值**。核查是一个线性可行性问题（`scipy.optimize.linprog`，
  HiGHS）：在所有闭区间内找一组同时满足全部加权求和等式的真值。
- 三种结论：
  - `feasible`：差额可由舍入解释，返回一组金额 **见证**（每个事实的真值、区间、报告值）。
  - `infeasible`：无解，返回一组或多组 **相互矛盾的事实与等式**（最小不可行分组，
    含每条等式在中点见证下的残差）。
  - `pending`：任一等式引用了实例中不存在的事实，列出缺失概念，**整份待补**。

## 修订与并发（乐观锁）

- 关系包与实例都按修订号（revision）逐版保存，原始 XML 字节永不覆盖。
- 修订关系包需带 `Expected-Revision` 头；更正实例需带 `Expected-Package-Revision` 头；
  核查需同时带 `Expected-Instance-Revision` 与 `Expected-Package-Revision`。
- 受理时校验两侧所见修订：
  - 客户端修订 **领先** 服务器（或修订落后且未声明迟到）→ `412 stale_revision` 拒绝。
  - 更正期间允许继续提交；新报告生成后旧报告标记 `superseded=true`。
  - 对落后修订显式 `POST .../checks?late=true` 时，报告以 `late=true` **仅存历史**，
    不取代当前报告。
- 每份报告随存三份原始依据快照：实例、XSD、calculation linkbase，
  可通过 `/reports/{id}/snapshots/{instance|xsd|calc}` 原样下载。
- 数据全部在 SQLite（WAL 模式）+ 命名卷 `/data` 中，**重启后报告与快照仍可查**。

## 比率区间分析（舍入误差允许的比率范围）

财报分析员提交**当前可行报告 ID + 分子/分母概念**，服务端按报告保存的实例、
XSD 与计算关系**重建可行真值集合**（全部事实的舍入闭区间 ∩ 全部加权等式），
求比率 `分子真值 / 分母真值` 在整个可行多面体上的最小值与最大值：

- 极值是**线性分式规划**问题，用 Charnes–Cooper 变换（`y=t·x`，归一化
  `c_D·y=1`）转成普通 LP 后由 HiGHS 求解，两个端点各返回一组**达到该端点的
  完整金额见证**（实例中每个事实的真值、区间、报告值），并独立复核区间、
  等式与取等情况。
- **不允许**把两个独立科目的上下界直接相除：等式把各事实的真值耦合在一起，
  返回区间严格基于共享同一组真值的可行集合（通常比独立商更窄）。
- **不沿用**原报告里保存的见证，每次都从快照重新求解。
- 分母在**任一可行解中都不为正**（最大可行值 ≤ 0）→ `422 denominator_not_positive`
  拒绝；分母区间跨零但存在正的可行值时正常受理。
- 报告非 `feasible`（infeasible/pending）或概念不在该实例中 → `422` 拒绝。

### 修订、过期与失败原子性

- 发布分析前强校验报告所依据的实例/关系包修订：**任一来源已更正即 412 拒绝
  发布**；更正期间（修订仍匹配）可继续提交。
- 求解发生在写库之前，任何失败都**不会留下半份分析**。
- 历史分析按 `/ratio-analyses/{id}` 读取；来源被更正后，读取时惰性标记
  `stale=true` 并给出 `stale_reason`，**原报告与历史分析内容均不改写**。
- 在更正后的新修订上重新核查生成新报告，可再发布全新的、不过期的分析。

## 启动（Docker Compose）

```bash
docker compose up --build
# API:   http://localhost:8000
# 文档:  http://localhost:8000/docs   （可直接在 Swagger UI 更正、核查、读取报告）
```

首启会自动注入演示关系包与 4 个场景实例（feasible / rounding / infeasible / pending）。
设置 `XRECON_SEED_SAMPLES=0` 可关闭。

## 主要接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/packages` | 上传 XSD + calculation linkbase，创建关系包（rev 1） |
| POST | `/packages/{id}/revisions` | 修订关系包（头 `Expected-Revision`） |
| GET | `/packages`、`/packages/{id}` | 列表/详情（含解析后的等式） |
| POST | `/packages/{id}/instances` | 上传 XBRL 实例（rev 1） |
| POST | `/instances/{id}/revisions` | 更正实例（头 `Expected-Package-Revision`） |
| GET | `/instances`、`/instances/{id}` | 列表/详情（含事实与舍入区间） |
| POST | `/instances/{id}/checks` | 核查并出报告（两个 Expected 头；`?late=true` 迟到补报） |
| GET | `/instances/{id}/reports` | 当前报告 + 全部历史（含过期、迟到） |
| GET | `/reports/{id}` | 单份报告 |
| GET | `/reports/{id}/snapshots/{kind}` | 下载报告保存的原始依据 |
| POST | `/reports/{id}/ratio-analyses` | 按报告快照重建真值集合，求比率区间与端点见证 |
| GET | `/reports/{id}/ratio-analyses` | 该报告下的全部比率分析 |
| GET | `/ratio-analyses/{id}` | 按 ID 读取历史分析（来源更正后标 `stale`） |

## 本地开发与自测

```bash
uv venv .venv && uv pip install -r requirements.txt pytest httpx -p .venv
.venv/bin/python -m pytest -q
```

## 目录

```
app/            FastAPI 应用、解析器、核查求解器、比率区间（线性分式规划）、SQLite 持久化
tests/          端到端/单元测试（核查、校验、比率区间）+ 样本 XBRL
scripts/        容器首启演示数据播种
Dockerfile, docker-compose.yml
```
