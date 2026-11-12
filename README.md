# 航段时间清洗 · DST 安全入仓

面向航司数据工程师的网页工具：把地服导出的**当地时间**航段链解析为唯一 **UTC** 时间链，
避免夏令时（DST）切换时被错误入仓。

## 快速启动

```bash
docker compose up --build
```

浏览器打开 <http://localhost:8000> ，点击「📦 载入内置夏令时样例」→「开始清洗」即可看到完整演示。

本地开发（不用 Docker）：

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

自检（仅标准库）：`python3 tests/test_solver.py && python3 tests/test_assign.py`

## 输入 CSV 格式

UTF-8 编码，必需表头：

```csv
tail_number,seq,dep_local,dep_tz,arr_local,arr_tz,duration_min_minutes,duration_max_minutes
B-7001,1,2026-10-25 00:30,Europe/London,2026-10-25 02:15,Europe/Berlin,90,130
```

| 列 | 含义 |
| --- | --- |
| `tail_number` | 机尾号（同一机尾构成一条航段链） |
| `seq` | 航段顺序，整数，同一机尾内唯一，按升序链接 |
| `dep_local` / `arr_local` | 起飞/落地当地时间，`YYYY-MM-DD HH:MM`（也接受 `T` 分隔与秒） |
| `dep_tz` / `arr_tz` | 两端 IANA 时区名，如 `Europe/Berlin`、`America/New_York` |
| `duration_min_minutes` / `duration_max_minutes` | 飞行时长下/上限（分钟，**闭区间**） |

## 求解规则

1. 当地时间 → UTC 候选：
   - 普通时刻 → 1 个候选；
   - **重复时刻**（秋季拨回重叠，如柏林 2026-10-25 02:30）→ 保留 **2 个候选**参与整链求解；
   - **不存在的时刻**（春季拨快空洞，如柏林 2026-03-29 02:30）→ 该行报错，整架机尾禁止导出。
2. 每个航段的 (起飞, 落地) 候选组合必须满足：飞行时长 ∈ `[下限, 上限]`（闭区间）。
3. 按 `seq` 链接同一机尾：下一段起飞 **不得早于** 上一段落地（相等允许）。
4. 判定：
   - **整链唯一解** → 允许导出该机尾的 UTC CSV；
   - **无解** → 展示冲突航段（前向扫描定位第一个无法衔接的航段及其候选），禁止导出；
   - **多解** → 展示全部可行候选链（最多展示 20 条），禁止导出；
   - **行级错误**（不存在时刻 / 非法时区 / 格式错误 / seq 重复等）→ 禁止导出。

## 导出

- 网页上按机尾导出，或「导出全部可导出机尾」；
- 导出列：`tail_number,seq,dep_utc,arr_utc,duration_minutes`（UTC 为 ISO 8601 `Z` 格式）；
- 服务端在导出时会**重新求解并复核唯一性**，非唯一解返回 409，杜绝误导出。

## 飞参归段

清洗页顶部有「② 飞参归段」入口：上传 **1 份飞参 CSV** 与 **1～4 份清洗导出的 UTC 航段 CSV**，
为每条采样补上实际所属航段，便于按航段归档。

### 输入

| 文件 | 必需列 | 说明 |
| --- | --- | --- |
| 飞参 CSV | `tail_number`, `sample_utc` | 采样时刻，UTC ISO 8601（如 `2026-01-10T01:30:00Z`）；不要求排序；其余列原样保留 |
| 航段 CSV | `tail_number`, `seq`, `dep_utc`, `arr_utc` | 清洗页导出的 UTC 航段；可另含 `duration_minutes` 等列 |

### 匹配规则

- 按 `tail_number` 分组，区间判定为 **`dep_utc ≤ sample_utc < arr_utc`**（含起飞、不含落地）。
- 不同航段文件的记录**分别计数**：同一采样落在两份文件的航段中即为 2 次命中。
- 单次上限：**200 万条采样**、**合计 20 万个航段**；航段文件 **1～4 份**。

### 输出

结果 CSV 保留飞参的**全部原列与行序**，末尾追加三列：

| 列 | 含义 |
| --- | --- |
| `match_status` | `matched`（恰好命中 1 段）/ `unmatched`（0 段）/ `multiple`（≥2 段） |
| `segment_file_no` | 唯一命中时填航段文件的上传序号（1～4），否则留空 |
| `segment_seq` | 唯一命中时填该航段在航段 CSV 中的 `seq`，否则留空 |

- 服务端逐行读取飞参、逐行写入磁盘临时结果，**不把整份飞参或结果攒在内存**；全部输入读完且结果写完后才开始返回下载。
- 任一坏行（缺列、坏时间、非法 seq、`arr_utc <= dep_utc`、超限等）都会返回 400，并指出**文件与数据行号**；本次不产生任何部分结果。

## 内置样例（samples/dst_sample.csv）

| 机尾 | 场景 | 期望结果 |
| --- | --- | --- |
| B-7001 | 欧洲拨回日三连段，重复时刻由时长区间+链约束消歧 | 唯一解，可导出 |
| B-7002 | 柏林 2026-03-29 02:30 不存在（拨快空洞） | 数据错误 |
| B-7003 | 第二段起飞早于第一段落地 | 无解，展示冲突航段 |
| B-7004 | 起降均为重复时刻，区间宽松 | 多解（2 条候选链） |
| B-7005 | 美国拨回日（纽约/洛杉矶/丹佛） | 唯一解，可导出 |

### 归段样例（航段重叠）

`samples/assign_samples.csv` + `samples/assign_segments_1.csv` + `samples/assign_segments_2.csv`
演示跨文件重叠与边界时刻，网页「📦 载入航段重叠样例」可一键载入：

| 采样 | 期望结果 |
| --- | --- |
| B-8001 `00:30Z` | 唯一命中，文件 1 / seq 1 |
| B-8001 `02:00Z` | 等于第二段起飞，命中文件 1 / seq 2（含起飞） |
| B-8001 `04:00Z` | 等于第二段落地，`unmatched`（不含落地） |
| B-8002 `01:30Z` | 同时落在文件 1 与文件 2，`multiple` |
| B-8003 `02:00Z` | 唯一命中，文件 2 / seq 1 |
| B-9999 `02:00Z` | 无该机尾航段，`unmatched` |

## API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/` | 网页入口（含清洗 / 归段两个页签） |
| `GET` | `/api/sample` | 下载内置清洗样例 CSV |
| `POST` | `/api/solve` | 上传 CSV（multipart 字段 `file`），返回逐机尾求解结果 JSON |
| `POST` | `/api/export` | 字段 `file` + `tail`（`__all__` 表示全部可导出机尾），返回 UTC CSV；非唯一解 409 |
| `POST` | `/api/assign` | 字段 `samples`（飞参 CSV）+ `segments`（1～4 份航段 CSV，可重复），返回归段结果 CSV；坏行 400 |
| `GET` | `/api/assign/sample.zip` | 下载归段样例压缩包 |
| `GET` | `/api/assign/sample/{name}` | 下载单个归段样例文件 |
| `GET` | `/docs` | FastAPI 自动文档 |

归段请求示例：

```bash
curl -o assigned.csv \
  -F "samples=@samples/assign_samples.csv" \
  -F "segments=@samples/assign_segments_1.csv" \
  -F "segments=@samples/assign_segments_2.csv" \
  http://localhost:8000/api/assign
```

结果统计会随响应头返回：`X-Assign-Samples`、`X-Assign-Matched`、`X-Assign-Unmatched`、
`X-Assign-Multiple`、`X-Assign-Segments`，以及 JSON 格式的 `X-Assign-Summary`。

## 资源限制

`docker compose` 将 `web` 服务容器内存限制为 **256 MiB**（`mem_limit: 256m` /
`memswap_limit: 256m`）。归段时飞参与结果均逐行流式处理并落盘，内存中仅保留
最多 20 万条航段索引。

## 目录结构

```
app/
  main.py            # FastAPI 路由（清洗 + 归段）
  solver.py          # 候选生成 + 整链求解 + 冲突定位（纯标准库）
  assign.py          # 飞参流式归段 + 航段区间索引（纯标准库）
  static/index.html  # 单页前端（清洗 / 归段两个页签）
samples/dst_sample.csv
samples/assign_samples.csv
samples/assign_segments_1.csv
samples/assign_segments_2.csv
tests/test_solver.py
tests/test_assign.py
Dockerfile / docker-compose.yml / requirements.txt
```
