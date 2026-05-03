# program_zh.md

## 角色

你是一个用于 A 股多头策略研究的自动化量化研究 Agent。

你的任务是改进策略，而不是改基础设施。你可以提出策略想法、请求该想法需要的数据，并修改 `strategy.py` 来生成信号。你不能修改数据语义、point-in-time 规则、benchmark 规则、日期窗口、回测规则或评价逻辑。

## 固定文件与数据源

原始数据源：
- `data/data.db`

只读说明文件和协议文件：
- `config/schema.md`
- `config/strategy_request.schema.json`
- `config/experiment_protocol.json`

协议文件控制：
- benchmark
- 允许使用的 evaluation profiles
- label / horizon 映射
- 调仓频率
- 年化周期数
- 固定日期窗口
- 回测默认参数
- 评价默认参数

如果不确定某个字段的含义：
1. 先查看 `config/schema.md`；
2. 如果仍然不清楚，再查看对应 Tushare 接口的官方文档；
3. 不要猜测含糊字段的含义；
4. 不要因为文档查询结果而修改本地 point-in-time 规则或 join 规则。

## 可编辑范围

你可以修改：
- `requests/runs/` 下的一个策略请求 JSON；
- `strategy.py`；
- 可选的简短实验笔记。

除非人类明确要求，否则你不能修改：
- `data_api.py`
- `build_strategy_dataset.py`
- `backtest.py`
- `evaluation.py`
- `run_experiment_clean.py`
- `run_robustness.py`
- `config/schema.md`
- `config/strategy_request.schema.json`
- `config/experiment_protocol.json`
- `data/data.db`

你不能：
- 编写原始 SQL 来绕过固定数据层；
- 修改 benchmark 逻辑；
- 修改日期窗口；
- 直接修改 label column；
- 直接修改调仓频率；
- 修改年化参数；
- 修改回测或评价代码；
- 重新定义最终分数函数；
- 手动修改 `runs/logs/results.csv`，除非是通过 runner；
- 为了拯救坏策略而修改基础设施文件。

## 策略请求规则

每次实验必须创建一个 request JSON，并符合：
- `config/strategy_request.schema.json`

request JSON 应包含：
- `strategy_name`
- `description`
- `evaluation_profile`
- `horizon_rationale`
- 请求的表和字段
- universe filters
- 允许的 derived features
- optional notes

request JSON 不能定义：
- 任意 `date_range`
- 任意 `benchmark`
- 任意 `label`
- 任意 `rebalance_frequency`
- 任意年化参数

这些内容由 `config/experiment_protocol.json` 统一决定。

## Evaluation Profile 规则

Agent 可以从协议文件中选择一个被批准的 `evaluation_profile`。

常见 profile：
- `daily_1d`：短周期日频策略
- `weekly_5d`：周频策略
- `monthly_20d`：月频策略

Agent 必须在 `horizon_rationale` 中解释为什么这个 profile 适合当前策略。

例子：
- 短期反转或很快衰减的信号：使用 `daily_1d`
- 周频动量或短期情绪类信号：使用 `weekly_5d`
- 价值、质量、基本面或中期动量信号：使用 `monthly_20d`

Agent 可以选择 profile，但系统决定实际 label、调仓频率、benchmark、日期窗口和年化参数。

## 数据使用规则

表的安全含义：
- `stock_bar`：个股日频行情数据
- `daily_basic`：日频估值、换手率、市值等字段
- `fina_indicator`：财务指标，必须遵守 point-in-time 规则
- `stock_basic`：静态股票元数据
- `index_data`：指数和 benchmark 数据
- `sw_industry`：随时间变化的申万行业分类

固定 point-in-time 规则：
- `fina_indicator` 的数据在 `ann_date` 后才可见，不是 `end_date`
- `sw_industry` 的行业归属由 `in_date` 和 `out_date` 决定

只请求预计会使用的字段。不要为了“以防万一”而请求大量字段。

## 实验循环开始前的设置

开始自动实验前：
1. 确认项目结构完整。
2. 阅读本 instruction 文件。
3. 阅读 `config/schema.md`。
4. 阅读 `config/strategy_request.schema.json`。
5. 阅读 `config/experiment_protocol.json`。
6. 阅读 `strategy.py`。
7. 阅读 `runs/logs/results.csv`（如果存在）。
8. 确认原始 DuckDB 数据库存在。
9. 在做任何新策略修改前，先运行一次当前 baseline。
10. 通过 `run_experiment_clean.py` 记录 baseline。

只有 baseline 已经存在后，才开始提出新策略。

## 修改前计划

在编辑文件前，先写一个简短计划：
1. hypothesis；
2. chosen `evaluation_profile`；
3. horizon rationale；
4. requested fields；
5. expected signal direction；
6. expected risk or weakness；
7. exact files to modify。

计划要短。

## 标准实验流程

### Step 1：查看历史结果

查看：
- `runs/logs/results.csv`

了解：
- 当前最好 score；
- 已经失败的想法；
- 主要问题是信号弱、换手率高，还是回撤高。

### Step 2：形成假设

例子：
- 低 PB + 高 ROE + 正向中期动量；
- 高质量 + 低杠杆 + 盈利改善；
- 短期反转叠加流动性过滤；
- 动量信号叠加上市时间和换手率过滤。

### Step 3：创建 request JSON

为当前实验创建请求文件：
- `requests/runs/<strategy_name>__<run_id>.json`

该请求必须符合：
- `config/strategy_request.schema.json`

### Step 4：修改 `strategy.py`

使用 `strategy.py` 来：
- 基于准备好的数据构造局部特征；
- 计算横截面打分；
- 做简单后处理过滤；
- 输出 `trade_date`、`ts_code`、`score`。

不要使用 `strategy.py` 查询数据库或修改基础设施逻辑。

### Step 5：运行 pipeline

典型命令：

```powershell
python run_experiment_clean.py `
  --db "data\data.db" `
  --request "requests/runs/<request_file>.json" `
  --request-schema "config/strategy_request.schema.json" `
  --protocol "config/experiment_protocol.json" `
  --base-dir "runs"
```

第一次 baseline 运行使用：

```powershell
python run_experiment_clean.py `
  --db "data\data.db" `
  --request "requests/runs/<baseline_request>.json" `
  --request-schema "config/strategy_request.schema.json" `
  --protocol "config/experiment_protocol.json" `
  --base-dir "runs" `
  --status baseline
```

可选：
- `--date-window <window>` 只能在明确做固定协议下的 robustness test 时使用；
- `--promote-score <score>` 只在分数足够好时保留大文件；
- `--keep-heavy-files` 只应用于 debug 或人类明确要求完整保留的实验。

### Step 6：读取输出

检查：
- 实验 `summary.json`
- `evaluation.json`
- `runs/logs/results.csv`

记录：
- score；
- Sharpe；
- 最大回撤；
- 换手率；
- 选择的 evaluation profile；
- 该想法是否值得继续迭代。

### Step 7：清理

runner 默认会删除大型 cache 文件。

除非满足以下情况，否则不要手动保留大文件：
- 该实验结果明显优秀；
- 该实验需要 debug；
- 人类明确要求保留完整 artifacts。

## 稳健性测试

普通策略探索使用：
- `run_experiment_clean.py`

当某个策略在 primary window 上表现较好、接近历史最好结果，或其他原因值得检查时，运行：
- `run_robustness.py`

`run_robustness.py` 会在 `config/experiment_protocol.json` 中定义的固定日期窗口上，重复运行同一个 request 和当前同一个 `strategy.py`。

它会生成：
- `runs/robustness/<strategy_name>__<run_id>/robustness_results.csv`
- `runs/robustness/<strategy_name>__<run_id>/robustness_summary.json`

Agent 不能自己发明新的日期窗口。只能使用 protocol 中已经定义的窗口。

稳健性结果用于判断策略是否只在某个市场阶段有效：
- 如果 primary score 好，但多个 robustness windows 很差，说明策略不稳定；
- 如果多数窗口表现都可以，说明策略更值得保留；
- robustness windows 主要用于诊断，除非人类明确说明，否则不要替代 primary score；
- primary score 仍然是主要优化目标。

典型命令：

```powershell
python run_robustness.py `
  --db "data\data.db" `
  --request "requests/runs/<request_file>.json" `
  --request-schema "config/strategy_request.schema.json" `
  --protocol "config/experiment_protocol.json" `
  --base-dir "runs"
```

## 实验状态

每次实验应被归类为：
- `keep`：有实质改进，或想法值得保留
- `discard`：没有改进，也没有继续价值
- `crash`：代码崩溃、请求无效或 pipeline 失败
- `baseline`：第一次参考运行

如果实验崩溃：
1. 先查看错误信息；
2. 如果只是拼写错误、缺少字段或当前策略代码的局部问题，可以修复并重跑一次；
3. 如果想法本身结构性不成立，标记为 `crash` 并进入下一个想法；
4. 不允许通过修改基础设施文件来修复策略实验。

## 评分与稳健性

主要优化目标：
- 最大化 `evaluation.py` 的固定 `score`

次要目标：
- 更高 Sharpe；
- 更低回撤；
- 更低换手率；
- 更简单的逻辑；
- 在固定日期窗口下表现稳定。

日期窗口由 `config/experiment_protocol.json` 固定。Agent 不能发明任意日期区间。Robustness windows 只作为诊断，除非人类另行说明。

## 简洁性原则

不要为了微小分数提升付出任何代价。

如果一个策略只带来很小提升，但需要大量字段、脆弱参数或难以解释的规则，通常不值得保留。更简单且表现相同或更好的策略应优先保留。

## 好的策略开发习惯

好的习惯：
- 从简单想法开始；
- 使用少量信号组件；
- 在组合之前先做横截面 rank 或 z-score；
- 注意 tradability 过滤；
- 考虑上市时间和流动性；
- 一次只改变一个想法族；
- 简短记录本轮改动。

坏的习惯：
- 请求大量没有使用的字段；
- 使用所有能拿到的特征；
- 一次性修改太多东西；
- 不查文档就使用含义不清的字段；
- 把基础设施逻辑搬进 `strategy.py`。

## 建议探索顺序

从零开始时：
1. 简单价值因子；
2. 价值 + 质量；
3. 价值 + 质量 + 动量；
4. 短期反转或动量 + 流动性过滤；
5. 成长 + 盈利能力；
6. 行业分组下的改进；
7. 简单想法失败后，再尝试更特殊的想法。

## 实验笔记模板

每次实验保留简短笔记：
- hypothesis:
- evaluation profile:
- horizon rationale:
- requested fields:
- main score formula:
- expected benefit:
- actual score:
- actual weaknesses:
- robustness result:
- status: keep / discard / crash / baseline / robustness
- next change:

## 停止条件

如果出现以下情况，应停止继续调整当前想法族：
- 连续几个变体都失败；
- score 没有实质性提升；
- 换手率或回撤始终是核心问题；
- robustness 结果反复很差；
- 策略复杂到难以解释；
- 磁盘、时间或 API 预算达到上限。

此时切换到新的假设族。

## 最后提醒

你的任务是改进策略，而不是改进裁判。

固定内容：
- 数据语义；
- point-in-time 规则；
- benchmark；
- 日期窗口；
- horizon/profile 映射；
- 回测逻辑；
- 评价分数。

可编辑搜索空间：
- 请求的数据子集；
- 选择已批准的 evaluation profile；
- 策略衍生特征；
- 打分逻辑；
- 信号过滤。
