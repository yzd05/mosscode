# 评估与版本对照

日常修改运行相关 pytest，不自动调用付费模型。公开能力评测使用锁定的 Harbor 0.26.0 与 Terminal-Bench 2.1；现有 13 个小任务继续用于受控回归，两者分开报告。评测脚本、任务清单与冻结配置保存在开发仓库的 `evals/` 与 `scripts/` 目录中（不随安装包或公开源码导出分发）。

## 准备公开基准

Harbor 只装在独立评估环境，不能和 MossCode 的生产虚拟环境混装。Windows 可使用原生 Python 或 WSL2，配合 Docker Desktop 的 Linux 容器；需确保当前环境的 `docker` 能连接 daemon。首次单题已在 Windows 原生 Python + Linux 容器下通过，WSL2 路径尚未做完整试跑。

```sh
uv venv .test-artifacts/harbor-venv --python 3.12
uv pip install --python .test-artifacts/harbor-venv/bin/python -r evals/harbor/requirements.txt
# Windows 原生 Python 路径为 .test-artifacts/harbor-venv/Scripts/python.exe。
```

后续命令在仓库根目录、使用评估环境的 Python 执行（可以先激活环境）。配置仅保存 `DEEPSEEK_API_KEY` 的环境变量名，密钥由运行环境提供；不读取用户 MossCode 配置、记忆、全局指令或凭据文件。Windows 刚设置用户环境变量时，已有终端不会自动继承，可在运行终端执行 `$env:DEEPSEEK_API_KEY = [Environment]::GetEnvironmentVariable("DEEPSEEK_API_KEY", "User")`。不要把变量值打印出来或写进配置。

```sh
python -m scripts.run_harbor_evals build fccece23950c1db4438a310ecb23bb282dac8288
python -m scripts.run_harbor_evals build <候选提交SHA>
```

构建只使用 `git archive` 中的提交内容，不包含当前工作区草稿。wheel 和记录其代码 SHA、wheel SHA256 的 JSON 默认写入 `.test-artifacts/evals/wheels/<SHA>/`。适配器校验摘要，在容器的 `/installed-agent/mosscode/` 中建立独立 Python 3.12.13 与虚拟环境，不向任务项目安装 MossCode 或替换项目依赖；实际依赖版本记录在 Agent 原始日志中。

## 先看计划，再显式执行

```sh
python -m scripts.run_harbor_evals run --series deepseek-tb21-v1 --run-id smoke-candidate --agent candidate --wheel candidate=<wheel路径> --budget-usd 1 --estimate-usd 0.1
# 确认计划和配置后，加 --execute 才会下载任务、启动容器并请求模型。
```

省略 `--agent` 同时运行候选 MossCode 和 Harbor 自带 Terminus 2；计划会把双方次数都算入。`--wheel anchor=<wheel路径>` 可新增旧版 MossCode 作为对照，配合 `--agent anchor --agent candidate` 做版本比较。预算数字仅为命令示例，不是 Terminal-Bench 实测费用。

- 第一次验证单任务时，追加 `--scope smoke --task cancel-async-tasks`；`--task` 可重复选择当前冻结清单内的题目，报告会标明选题抽检。
- `--scope smoke`：固定 3 题；`--scope dev`：固定 12 题；`--scope full`：官方固定提交的全部 89 题。`--repeat` 默认 1。
- 数据集提交和所有任务元数据保存在 `evals/harbor/dataset.lock.json`。开发清单在看成绩前，按类别和固定种子选择；全量任务不因本机资源不足而静默删除，环境错误保留在报告中。
- `evals/harbor/job.yaml` 使用 Harbor 原生配置，默认单并发、任务官方时限/资源、同一 DeepSeek 端点、服务默认采样/思考、128K 上下文、16384 单次输出上限。MossCode 保留自己的迭代策略，不宣称与 Terminus 2 有相同累计请求额度；共同设置不同会拒绝运行。
- `--timeout` 同时覆盖双方 Agent 时限；`--concurrency` 显式声明并发。内部筛查与受限配置不能冒充官方排行榜成绩。
- `--account-tier` 记录核实的账号层级，`--backend-version` 记录已经核实的实际模型版本；未知时保留空值并提示滚动别名漂移风险。
- `--prices <JSON>` 提供同一来源、同一日期的 USD/百万 Token 价格快照：`{"source":"价格来源URL","date":"核查日期","input":0.0,"cached_input":0.0,"output":0.0}`。示例中的零不是推荐价格，实际运行前应填写核实价格。不提供时仅保留各 Agent 原生费用估计，不计算费用变化比例，不能视作账单。

预算会在尝试间检查已知支出和下一次预计开销，并为在途尝试预留估计费用。未知费用时停止新派发；在途请求仍可能计费，因此不是账单硬封顶。长期任务的辅助调用纳入最终统计，旧版未报告的完整性和诊断字段保留缺失值，不填零。

### 冻结的首次开发基线

`evals/harbor/baseline.lock.json` 已冻结首次 12 题开发基线条件：MossCode 提交 `0d748f09eb0551dd23c99119ea3dfefdfdcef0f5`、Terminus 2.0.0、Terminal-Bench 2.1 的固定 12 题、Harbor 0.26.0、DeepSeek `deepseek-flash`、单并发和各一次。锁文件同时记录 wheel、数据集、共同配置和两个评估适配器的摘要；传入 `--baseline-lock` 时，任一项漂移都会在启动前拒绝运行。该锁文件目前已更新为包含超时日志回收修复的适配器摘要（与首次 `baseline-01` 实测时的早期适配器不同）；具体实测边界见下文。

正式运行前先做不请求模型的安装预检：

```sh
python -m scripts.run_harbor_evals run --series deepseek-tb21-dev12-v1 --run-id install-preflight-01 --scope dev --wheel candidate=<0d748f0 wheel路径> --backend-version DeepSeek-V4.1-Flash-1017 --baseline-lock evals/harbor/baseline.lock.json --prices evals/harbor/prices.deepseek-flash.json --install-only --execute
```

预检仍会下载任务、启动 24 个容器并安装双方 Agent，但跳过解题和判定，预计模型费用为零。共同配置给 Agent 安装阶段两倍 Harbor 默认时限；MossCode 只对网络型系统包失败重试一次。Terminus 2 使用原生提示词、工具、上下文和 Agent Loop，只在安装适配层重试 `tmux` 包下载并关闭不影响解题的 asciinema 录像。整题自动重试仍为 0，失败目录不会被 Harbor 删除。

3 题 smoke 中实际进入模型的 6 次尝试合计统一价格估计为 `$0.10635`，平均约 `$0.01772`。按此线性外推，24 次约 `$0.43`；DeepSeek 当前峰值价格为低谷价两倍，对应约 `$0.86`。12 题含更长任务，首次运行建议在低谷时段声明 `$1.50` 上限和 `$0.06` 单次预留：

```sh
python -m scripts.run_harbor_evals run --series deepseek-tb21-dev12-v1 --run-id baseline-01 --scope dev --wheel candidate=<0d748f0 wheel路径> --backend-version DeepSeek-V4.1-Flash-1017 --baseline-lock evals/harbor/baseline.lock.json --prices evals/harbor/prices.deepseek-flash.json --budget-usd 1.50 --estimate-usd 0.06 --execute
```

价格快照来自 DeepSeek 官方页面 2026-11-14 的低谷价：缓存未命中输入 `$0.15`、缓存命中输入 `$0.003`、输出 `$0.60`，单位均为每百万 Token；周末全天属于低谷时段。若改在工作日峰值时段运行，应按当时官方价格重新生成快照和预算。`$0.43/$0.86/$1.50` 分别是经验外推、峰值等价估算和派发上限，不是实际账单。

运行产物在 `.test-artifacts/evals/<series>/<run-id>/`：Harbor 原生配置、日志和判定；`manifest.json` 保存计划与指纹；`normalized.json` 保存归一化结果。运行产物会移除所选环境变量中的模型密钥。原始轨迹仍可能包含任务内容，不自动发布。

## 恢复与比较

恢复使用相同参数及 `--execute --resume`，沿用同一个 run-id。配置或代码/镜像条件不同会拒绝恢复；已经结束的尝试由 Harbor 复用。自动重试固定关闭，避免覆盖失败目录；安装可靠性由预检和安装适配层处理。额外独立尝试用新 run-id，保留旧失败与开销，诊断补跑不能并入预先声明的完整成绩。

```sh
python -m scripts.run_harbor_evals normalize <原始运行目录>
python -m scripts.compare_evals <基线目录>/normalized.json <候选目录>/normalized.json --output .test-artifacts/evals/compare.md
```

比较脚本不调用模型。可追加 `--prices <JSON>` 在同一价格快照下重算双方费用，不修改原始结果。不同任务/判定器配置、模型端点、后端版本、资源、并发或价格快照会拒绝直接比较。报告包括覆盖率、每题各次结果、按任务等权的端到端单次成功率、失败开销、耗时中位数/P90，以及双方均成功尝试的费用/耗时比例。零成功没有“每成功费用”；缺失和取消保留为不完整，正常结束但判定失败不自动称作谎报完成。

## 受控回归

继续使用源码仓库 `evals/tasks/` 中的现有 13 题（说明见 `evals/README.md`）：

```sh
python scripts/run_evals.py --output .test-artifacts/evals/local.json --budget-usd 1 --estimate-usd 0.05
python scripts/run_evals.py --output .test-artifacts/evals/local.json --budget-usd 1 --estimate-usd 0.05 --resume
```

每次结束立即保存检查点，恢复需匹配任务、配置和 Python 源码指纹。执行隐藏判定和项目可见测试；删除的原有测试会恢复，合法重命名等测试更新会保留。记录改动文件、文本增删行和测试改动数。可用 `--finish-command 'python -m pytest 可见测试.py'` 单独启用完成前验收，默认不读取用户 Hook；不能把隐藏判定或参考答案放入命令。共享 `run_budget` 随现有配置进入生产 Conversation。

新的受控检查点也可交给比较脚本；旧版没有实验标识的 JSON 只能保留为历史成本背景，不强行补全身份后比较。受控任务与公开基准不能混报。

## 验收与首次真实试跑

普通开发环境运行 `tests/test_eval_framework.py`、相关 `tests/test_evals.py` 和 CLI 输出测试，不需要 Harbor。独立评估环境可显式运行 `python -m pytest evals/harbor/test_agent.py`，使用真实 Harbor 接口、模拟环境和模拟 CLI，不调用模型或 Docker。

2026-11-14 已用 `deepseek-flash` 完成 `cancel-async-tasks` 单题：官方 reward 为 1，隐藏测试 6/6 通过，Agent 正常结束。产品 wheel 来自提交 `0d748f09eb0551dd23c99119ea3dfefdfdcef0f5`，评估适配器使用当前工作区实现。20 次请求，输入 469153 Token（其中缓存 457472）、输出 24538 Token；Agent 约 147 秒，含安装和判定共约 326 秒。按当日官方周末价格快照估算 $0.01785，未核对实际账单。

这次试跑设置最多 20 次请求、500000 Token，属于接入验证。结果位于 `.test-artifacts/evals/smoke-deepseek-20261114/cancel-async-01/`，原生结果和轨迹保留，`report.md` 为归一化报告。已验证恢复完整尝试不会新增模型调用，并修正了 Harbor 原生任务名带命名空间时的计划匹配问题。

同日已完成固定 3 题的 MossCode / Terminus 2 smoke，各题使用同一个 `deepseek-flash`、官方时限和模型上限。原始批次双方端到端均为 1/3，其中两条记录因安装失败未调用模型。只对这两个安装失败项另行补跑后，六个模型任务中 MossCode 2/3、Terminus 2 1/3；双方均未通过大文件编辑任务。初始失败保留，补跑结果单列，不改写原始端到端分数。

这批全部记录含补跑的统一价格估计为 $0.10635：MossCode $0.03054、Terminus 2 $0.07581。Agent 执行总耗时约 362 秒与 818 秒，排除安装及判定。产物在 `.test-artifacts/evals/deepseek-smoke3-20261114/`，`comparison.md` 同时展示原始批次、安装补跑与费用；临时监控入口另用账户扣费差额预留停止余量。预算监控快照不代替实际账单。

12 题的任务、产品提交和运行条件已经冻结。`install-preflight-02` 已完成 24/24 次安装预检，MossCode 与 Terminus 2 在 12 个任务容器中均安装成功，0 个安装异常且模型费用为零。

2026-11-15 完成首次付费开发基线（运行基于未含 `-p` `accounting` 字段的 `0d748f0` 与早期适配器）。主运行在 21/24 个尝试后因一条超时流的费用未知而停止派发；随后只补齐未派发的三个尝试。组合前逐项核对模型端点和后端版本、容器资源、Harbor/Terminus 配置、价格快照、任务配置、Agent 摘要及任务校验和，原始运行没有覆盖。组合产物位于 `.test-artifacts/evals/deepseek-tb21-dev12-v1/baseline-01-composed/`；此后适配器已补充超时日志回收并更新 `evals/harbor/baseline.lock.json` 摘要，因此该组合产物仅作首次接入与故障排查依据保留，不直接与当前锁文件下的后续运行自动比较。后续版本对照应在包含 `accounting` 的稳定提交上重跑开发基线或全量评估。

端到端主成绩为 MossCode 9/12（75.0%）、Terminus 2 7/12（58.3%）。三条零费用失败发生在模型启动前：MossCode 两次 Debian APT 502，Terminus 2 一次 tmux 启动失败；它们仍计入主成绩。排除这三条的诊断口径为 9/10 和 7/11，不能替代端到端结果。双方均在 `train-fasttext` 超时，均通过 `video-processing`。

统一低谷价格快照下，Terminus 2 的完整估算为 `$0.44789`；MossCode 约为 `$0.23777`，但其中 `train-fasttext` 只有超时前 61 个 assistant 事件，故是下界。合计可核对估算下界约 `$0.68566`，低于 `$1.50` 派发上限，实际账单未核对。每题仅运行一次，不能声称统计显著或官方 89 题成绩。MossCode 在 `video-processing` 中发生一次上下文压缩后通过，但 WSL2 完整路径和专门的长上下文回归仍未验证。
