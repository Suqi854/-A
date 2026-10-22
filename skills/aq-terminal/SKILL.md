---
name: aq-terminal
description: "查看 A量化 已公开发布的数据：今日竞价/尾盘选股结果、大盘温度、入选股与AI持仓的个股分析（评级、止损止盈、主力行为）、AI自动盘成绩单和因子迭代记录。适合手机或任何不能直连A股行情源的环境，用户问“今天选了什么”“这只票怎么看”“AI盘表现如何”时使用。"
---

# A量化 · 公开数据查看

A量化引擎在用户电脑上运行，每次选股后、盘中每 10 分钟、15:45 收盘后，会把**可公开**的数据推送到 GitHub 仓库 `Suqi854/-A` 的 `data` 分支。本技能只读这些 JSON，不需要连接行情源，所以手机上也能用。

## 数据地址

基础地址（任选一个能访问的）：

- `https://raw.githubusercontent.com/Suqi854/-A/data/data/`
- `https://suqi854.github.io/-A/data/`（开启 GitHub Pages 后可用；同一站点根目录 `https://suqi854.github.io/-A/` 是交易终端风的只读网页，可以直接把这个链接发给用户在手机上打开）

| 文件 | 内容 |
|---|---|
| `manifest.json` | 最近一次发布时间 `updated`、是否完整发布 `full`，以及当时的交易阶段 `phase` |
| `latest.json` | 行情总览：`indices` 指数、`overseas` 外围、`breadth` 市场温度（涨跌家数/涨跌停/成交额/主力净额/中位涨幅）、`regime` 大盘多空、`top` 涨幅榜、`inflow` 主力净流入榜、`last_run` 最近一次选股、`accounts.ai` AI自动盘摘要 |
| `picks/latest.json` | 最近一次选股：`items` 为入选股票（每项有 `score` 评分、`prob` 估计概率、`price` 选出价、`stop`、`target1`、`target2`、`strategies`、`reasons`、`risks`，次日起还会有 `ret_1d`、`ret_3d`、`ret_5d` 和 `outcome`）；`cand_list` 为候选前30 |
| `picks/YYYY-MM-DD.json` | 某一天的全部选股记录 |
| `runs.json` | 最近 60 次选股 |
| `analysis/index.json` | 有公开分析的股票列表（近7天入选股和AI持仓） |
| `analysis/<代码>.json` | 个股完整分析，代码写成 `sh600519` 或 `sz000858`。主要字段：`rating` 评级、`advice` 建议、`score`、`plan` 交易计划（`buy_zone`、`stop`、`target1`、`target2`、`rr`、`rules`、`supports`、`resistances`）、`main_force` 主力行为（`label`、`desc`、`evidence`）、`factors`、`signals`、`reasons`、`risks`、`lessons` 学习要点、`kline` 日K |
| `ai.json` | AI自动盘：`positions`、`trades`、`stats`（胜率、盈亏比、最大回撤）、`equity_curve` |
| `learn.json` | 自我迭代：当前因子权重 `weights`、历史版本 `history`（每次变化的原因）、策略胜率、概率模型的检验结果 `model.metrics`、选股成绩单 `picks` |

## 读取方法

1. 优先用网页抓取工具（WebFetch / 浏览）直接读上面的 URL。
2. 有命令行时也可以用 `curl -s <URL>`，或者进入插件的 `engine` 目录，运行 `python -m aq5.cli public latest|picks|ai|learn` 或 `python -m aq5.cli public analysis 600519`，输出就是整理好的中文 Markdown。
3. 先读 `manifest.json` 看 `updated`。如果数据不是今天的，或者盘中超过 30 分钟没更新，要先告诉用户数据时间，再说结论。

## 回答规范

- **今天选了什么**：列出每只入选股的名称、代码、评分、选出价、止损、止盈①、止盈②、触发的策略、1~3 条机会和风险。如果 `items` 为空，就明确说「宁缺毋滥，本次没有达到门槛的机会，建议观望」，并给出扫描数、门槛分和当时的大盘状态。
- **这只股票怎么看**：按「评级 → 交易计划 → 主力行为 → 看多理由 / 风险 → 学习要点」的顺序回答。用户是在学习量化交易，要用白话解释指标的含义（比如 RSI 超买、均线多头排列、ATR 止损、盈亏比）。
- **AI 盘表现**：给出总收益、胜率、盈亏比、最大回撤和当前持仓。另外可以引用 `learn.json` 的选股成绩单：次日 / 3日 / 5日胜率，以及达到止盈和触发止损的次数。
- **系统在怎么学习**：解释最近一次 `history[0].note`，说明哪个因子的 IC 上升或下降、权重怎么调整了，以及概率模型是否已经启用（`model.metrics.usable`）。
- 如果股票不在 `analysis/index.json` 里，就说明公开数据只覆盖近 7 天的入选股和 AI 持仓。如果当前环境有命令行且能访问行情源，可以改用 `aq-engine` 技能现场分析。
- 始终说明这些是模型输出、模拟数据，仅供学习，不构成投资建议。不要编造 JSON 里没有的价格或结论，也不要把概率说成确定会涨。

## 隐私

公开数据**不包含**用户的「我的模拟盘」、自选股和任何 token。用户如果问起这些，就告诉他们只能在电脑端的 A量化 页面里查看。
