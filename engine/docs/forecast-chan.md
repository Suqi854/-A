# 缠论结构适配器 v1

## 来源与固定口径

实际结构计算使用 [Vespa314/chan.py](https://github.com/Vespa314/chan.py/tree/f8ca3b5e44d7166e2abd756a8e7fa816c79faebe)，固定提交 `f8ca3b5e44d7166e2abd756a8e7fa816c79faebe`。`vendor/chan/PROVENANCE.json` 保存原始压缩包 SHA-256 和 49 个必需源码/许可证文件的 SHA-256；加载前核验。原始文件未改动，保留 MIT 许可证。无行情采集、策略演示、绘图库或可选付费功能依赖。

| 环节 | 本版本定义 | 确认依据 |
| --- | --- | --- |
| 包含处理与分型 | 上游合并K线，严格相邻分型识别 | 至少后续K线已完成且可用 |
| 笔 | normal、严格笔、strict 分型检查、终点必须极值、不允许次峰、不把缺口当K线 | 上游 `is_sure` |
| 线段 | chan 特征序列，未完成段按 peak 展示 | 上游 `is_sure`，虚段只观察 |
| 中枢及延伸/合并 | normal 段内中枢、按中枢区间重叠合并、关闭单笔中枢 | 上游中枢确认状态，延伸为新几何修订 |
| 背驰 | MACD(12,26,9)方向对应柱面积；离开/进入力度比例阈值 0.9 | 在本帧可知的结构上判断 |
| 一二三类买卖点 | `1,1p,2,2s,3a,3b`；二/三类追溯一类，二类延伸追溯二类，严格三类 | 所属笔/段与父段均确认后才可进入模型特征 |
| 多级别 | 日线、60/15/5分钟分别计算，再比较确认段方向 | 任一级数据不合格则组合不合格；方向冲突输出 conflict |

上游 `CSeg.cal_macd_metric` 只实现 slope/amp，因此适配器显式增加版本化 AREA 扩展：在线段起止单K线的闭区间内，向上段累加正 MACD 柱绝对值，向下段累加负柱绝对值，初值 `1e-7`；这与上游笔的 AREA 口径一致。不更改 vendor 源码。该扩展计入 `config_hash` 并独立测试。

这是固定、可验证的工程口径，不代表缠论原文的唯一解释。`1p/2s/3a/3b` 是上游细分类。结构识别通过不证明其有预测优势；虚笔、虚段及尚未确认买卖点保留显示，但不能据此制造已确认交易信号。

## 输入/输出合同

`analyze(code, timeframe, bars, asof)` 是无网络和持久化写入的确定性接口。`timeframe` 接受 `day/d/1d`、`60m/m60`、`15m/m15`、`5m/m5`；时间按 Asia/Shanghai 解释。

每行需 `date, open, high, low, close, vol`。`date` 必须是已完成K线**结束时点**，日线纯日期按15:00处理；允许显式 `end_at` 覆盖。`available_at` 是该修订真正可用时点，不得早于结束时点。未来、未完成及未来才可用的修订被过滤。相同K线采用截至 `asof` 已可用的最后修订；同时间戳冲突会禁止交易。

交易资格还要求：`source` 非空，`adjustment` 为 `none` 或 `pit`，`adjustment_verified=true`；`pit` 额外要求 `adjustment_asof<=asof`。调用方须先核验除权和完整性，不能因为价格正常就设置 verified。显式 `continuity_verified=false` 会拦截；上游数据预算最多4000根，不静默截断。少于60根保留结构观察但标记 insufficient_bars。

历史行情今天才取得时，历史结构只能记为今天已知，不能回填当年首次确认时间。缺少 `available_at` 仅允许观察，资格为 availability_unknown。不同历史K线可用时点按前缀最大值传播，避免在旧数据尚不可得时提前确认结构。

输出含稳定 `id/version/upstream_commit/config_hash/input_hash`、`quality{eligible,reason}`、`confirmed`、`source_time/known_at`、有限数值 `features`，以及可绘制 `structures`：

- `klines`：包含处理后的区间与合并数量。
- `fractals`、`strokes`、`segments`、`centers`、`buy_sell_points`：几何坐标、确认状态、发生时间、观察与首次确认时间、稳定身份。
- `segments` 的 `level=segment2` 是段的段；中枢和买卖点区分 stroke/segment 层。
- `revisions`：逐根回放时产生的确认与取代事件，不静默重画成历史成功。

`confirmed=true` 只说明存在已确认笔且输入资格合格，不等于当前可买卖。特征的最后一个确认买卖点可能很旧，调用方须检查 `chan_point_age_bars` 和 `chan_point_confirmation_age_bars`，并以结构身份去重。`align_levels` 仅表达多级别确认方向关系，不负责策略/下单。

引擎是逐根驱动，每次调用重建当前可知历史。应在预测后台按K线结束时点和输入哈希去重，不在3秒行情采集线程中同步执行。360根合成日线的一次本机测量约1秒，不构成持续运行性能承诺。

## 验收证据与边界

运行 `python -B tests/test_forecast_chan.py`，使用真实 vendored 引擎。合成曲线覆盖包含/分型/笔/特征序列段/中枢延伸及修订；两个固定随机种子的波段图例实际产生全部 `1/1p/2/2s/3a/3b` 分类。还验证截断回放一致、未来K线和未来修订不改变旧快照、输入不被修改、确认不回填、缺源/缺时标/未核验复权拦截、分钟级别和多级别冲突、虚结构不进入交易特征、段MACD面积公式。

这些是结构及因果工程验收，不是行情源合格证明、历史收益回测、前瞻预测效果或交易获利证明。实际交易日图例的人工审阅与前瞻样本仍须在整个平台运行后保留证据。
