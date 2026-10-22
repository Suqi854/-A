# A股量化分析 · 终端（插件 + 引擎）

一个仓库同时提供三样东西：

1. **Claude Code / Codex 通用插件**：9 个技能（`skills/`），可以对话式地看选股、分析个股、复盘。
2. **A量化引擎 5.0**（`engine/`）：本机运行的低频量化平台，功能包括实时监控、9 个因子加 8 种策略的评分、明确的交易计划和主力行为判断、每天 09:26 和 14:45 两次自动选股（宁缺毋滥）、模拟盘（我的盘 + AI 自动盘），以及根据实战结果自我迭代。界面为交易终端风格，电脑和手机都能用。
3. **公开数据和手机网页**（`data` 分支）：引擎会自动把行情、选股、个股分析、AI 盘成绩和迭代记录推送到这里。开启 GitHub Pages 后，手机打开网页就能看，插件也能读取这些数据。

> 仅供个人学习与模拟研究。所有评分、信号和交易计划都是模型输出，不构成投资建议，也不连接券商、不自动交易。

## 快速开始（Windows）

1. 安装 [Git for Windows](https://git-scm.com/download/win) 和 Python 3.10 或更高版本。
2. `git clone https://github.com/Suqi854/-A.git`，然后双击仓库根目录的 `launch.cmd`。浏览器会打开 http://127.0.0.1:18950 ；如果手机和电脑连的是同一个 WiFi，手机打开「设置」页显示的地址即可。
3. 想在手机上随时查看：打开「设置 → 公开发布」，勾选自动发布。引擎会在选股后、盘中每 10 分钟、以及 15:45 收盘后，把公开数据推送到本仓库的 `data` 分支。
4. 在 GitHub 仓库的 **Settings → Pages** 里，选择分支 `data`、目录 `/ (root)`。之后手机打开 `https://suqi854.github.io/-A/` 就是只读版的交易终端。

公开数据**只包含**：大盘行情、选股结果、入选股和 AI 持仓的个股分析、AI 自动盘、因子迭代记录。**不包含**：我的模拟盘、自选股、推送 token。

## 安装插件

**Claude Code**

```
/plugin marketplace add Suqi854/-A
/plugin install ashare-analysis-companion@suqi-ashare
```

**Codex**

```
codex plugin marketplace add Suqi854/-A
codex plugin add ashare-analysis-companion@suqi-ashare
```

**只用技能**：把 `skills/aq-terminal` 文件夹打包成 zip，上传到支持自定义技能的客户端，例如 Claude 应用的技能设置。这个技能只读取 GitHub 上的公开数据，不需要命令行，适合在手机上用。

## 技能一览

| 技能 | 用途 | 运行环境 |
|---|---|---|
| `aq-terminal` | 查看今天的选股、大盘温度、个股分析、AI 盘成绩和迭代记录 | 任何能访问网页的环境，包括手机 |
| `aq-engine` | 现场运行引擎：分析、选股、回测、启动网页、发布数据 | 需要命令行和 Python，并能访问腾讯/东财接口 |
| `ashare-analysis` | 综合行情与趋势分析路由 | 需要另外连接行情工具 |
| `ashare-position-t` | 持仓做 T 与动态调仓 | 同上 |
| `ashare-swing-screen` | 数周波段与行业候选筛选 | 同上 |
| `ashare-fundamental-research` | 基本面与估值研究 | 同上 |
| `ashare-strategy-validation` | 策略与量化验证 | 同上 |
| `ashare-session-briefing` | 盘前 / 盘后简报 | 同上 |
| `ashare-trade-review` | 成交复盘 | 同上 |

后 7 个技能继承自 1.13.3 公开整理版，来源说明见 `SOURCE_PROVENANCE.json`。行情插件在另一个仓库 [Suqi854/A-](https://github.com/Suqi854/A-) 维护。

## 引擎命令行

在 `engine` 目录下运行：

```
python -m aq5.cli market            # 大盘总览
python -m aq5.cli analyze 600519    # 个股分析与交易计划
python -m aq5.cli pick              # 立即全主板选股
python -m aq5.cli backtest 600519   # 回测
python -m aq5.cli public picks      # 读取已发布的公开选股
```

引擎的详细说明见 [engine/README.md](engine/README.md)。

## 校验与测试

```
python scripts/validate_plugin.py
cd engine && python tests/test_core.py && python tests/test_parsers.py
```
