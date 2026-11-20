# 节目载入编排台

剧场值班员将演出工单、工种值班表与固定预约汇到一个载入日。试排给出可下发的执行顺序、延误分钟、待排工序和来源证据。运行快照可复算并下载。

## 启动

```sh
docker compose up --build
```

访问 `http://localhost:18452`。使用 `docker compose exec -T app pytest -q` 运行测试。宿主可安装 `requirements.txt` 后执行 `pytest -q`。数据库存放在 Compose 命名卷 `ledger`，执行 `docker compose down -v` 会清空归档。配置见 `.env.example`。

## 值班演练

1. 打开「转场排程」，选择「冲突场次」。点选 `A01` 查阅跳过预约 `H01`、`H04` 的工序票签，再选 `X05` 查看夜间待排依据。
2. 在「超时勾稽」核对 15 道已落位、3 道待排、6 道工单隔离。`A03` 超时 15 分钟，新排工序资源碰撞为零。
3. 确认归档后打开「执行清单」，可复跑核验或下载包含输入摘要、执行步骤和隔离证据的联单。
4. 自带工单可展开「试排自备工单」，粘贴逐行工单与日历 JSON。样例位于 `sample_data/`，字段见 [数据字典](docs/DATA-DICTIONARY.md)。

## 接口

- `GET /api/health` 检查存储连通性。
- `GET /api/samples/{sample}` 和 `GET /api/samples/{sample}/...` 返回计算结果，样例为 `clean`、`dirty`、`revision`。七个子路径分别是 `orders`、`dependencies`、`calendars`、`conflicts`、`schedule`、`reconcile`、`dispatch`。排程支持 `?job=A01`。
- `POST /api/runs` 接收 `{"sample":"dirty"}` 或 `{"orders":"逐行文本","calendar":{...}}`。`GET /api/runs/{id}` 读取归档，`POST /api/runs/{id}/rerun` 复算比对，`GET /api/runs/{id}/export` 下载证据。

## 工单修订联单

演出前收到整批工单修订时，先归档基线联单，再在「修订联单」提交修订批次：

1. `POST /api/runs/{id}/revisions/preview` 不落库试排，逐项展示沿用旧时段还是改期及其依据；
2. `POST /api/runs/{id}/revisions` 生成修订草稿，编号由基线编号与修订输入摘要决定，同一修订重复提交只取回同一条，不会重复生成联单；
3. `POST /api/revisions/{id}/verify` 用同一版规则重算并做七项核对（行守恒、键集合、资源碰撞、前置顺序、锁定承诺、截止回归、删除出清）；
4. 核对通过后 `POST /api/revisions/{id}/publish` 才原子切换；未通过或未核对时发布被拒，旧版联单原样保留。`GET /api/revisions/{id}/export` 下载修订证据。

重排只覆盖受影响面：直接修订点、新增工序、被删工序的后继，再沿前序链和共享舞台/技师的旧时段交集扩张，集合之外的已落位工序逐字段锁定旧时段。日历本身被修订时全部已落位工序纳入重排。排程、超时勾稽与执行联单始终取自修订记录内的同一版 `result`。

## 排程边界

同一批次按依赖、优先级、截止、工单号依次落位，并以最早开始和技师号裁决同分候选。该策略是一轮确定性编排，跨场次全局优化和交互式拖拽排程超出当前范围。见 [规则与测试](docs/DEPTH.md)。
