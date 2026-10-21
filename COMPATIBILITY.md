# 统一核心引用兼容说明

`A-` 维护可执行能力与网页，`-A` 维护分析流程和方法说明。本增量沿用七技能 `public-core-reference-v1` 文档合同，不新增技能、数值实现、服务或权限。文档合同版本不是服务端 schema 或核心版本。

## 冻结基线与证据范围

- 公开基线：[1.13.3-public.2 / a2f1a188](https://github.com/Suqi854/-A/commit/a2f1a188c51a6ed343e5da30ed0ef44edb03aade)；本增量候选 `1.13.3-public.3`。沿用 [SOURCE_PROVENANCE.json](SOURCE_PROVENANCE.json) 中正式来源 `1.13.3` 的已有证据，不重读或发布 PRIVATE 插件。
- 只读核对获授权的执行侧冻结源码 `1.34.0-cloud.3`，集成提交 `7e9ddec77271659ca159dfe4fe651071bde7df35`。这是云端集成候选，不证明正式网页、手机或本地已加载。
- 六字段从“名称待绑定”推进为“冻结源码映射已绑定”。只做一次定向引用检查，不重跑策略、回测、行情或旧测试。同版本真实业务响应、生产宿主加载及技能调用仍待当前宿主证据，不能写成全生产可用。
- 公开增量仅含通用契约和版本，不含私有实现、原始响应、行情、持仓、参数、凭据或数据库。

## 兼容版本和身份

| 层 | 已核版本 | 分列要求 |
| --- | --- | --- |
| 分析技能 | `1.13.3-public.3` 候选，七个既有技能 | 不写入核心原结果 |
| 执行软件 | `1.34.0-cloud.3` 冻结源码 | 不是正式部署或加载凭证 |
| 原策略结果 / 投影 | `strategy-result-v1` / `strategy-workshop-v1` | 同一 input_id、result_id、versions.core、mode |
| 共享核心 | `strategy-core-v1.0.0` | 取原 versions.core，不用软件/技能版本替代 |
| 引用对象 | `strategy-result-reference-v1` | 六字段外保留 schema_version 与 binding，不是新计算 |
| 核心候选 | candidates[].id / candidates[].version；已核登记版本 `1.0.0` | 投影 rows[].id / rows[].current_candidate_version，不等自动执行 |
| 核心登记 / 选择 | `strategy-registry-v1.0.0` / `close-conflict-wait-v1.0.0` | versions.registry / versions.selection |
| 参考方法 | `reference-result-v1`；`reference-adapter-v1.0.0` / `reference-registry-v1.0.0` / `reference-no-selection-v1.0.0` | 适配器为 versions.reference_core，共享核心仍为 versions.core |
| 旧回测报告 | `ashare-validation-report-v1`；当前固定样本核心 `synthetic-core-v1` | 独立报告分支，不冒充共享核心或真实回测 |
| 摘要传输 | `research-sync-summary-v1` | snapshot_id 不是 result_id；旧摘要可无 core.result_reference |

预览的 preview_id、parent.result_id、result.result_id 和登记 registration_id 分列。预览 versions.strategy 是方法/策略版本，parent.strategy_version 是父策略版本，versions.data 是数据版本，versions.parameters 是参数身份。只引用收到的原值，不公开或推导私人参数。登记定义存在不证明当前结果实际包含该候选。

## 六字段确定映射

技能直接读取宿主 result_reference。右列仅说明对应关系，不授权技能转换日期、重建引用或重算。

| 字段 | strategy-result-v1 / reference-result-v1 | ashare-validation-report-v1 |
| --- | --- | --- |
| result_id | result.result_id，非空 `result:` 加64位小写十六进制 | report.result_id，非空 `sha256:` 加64位小写十六进制，不改前缀 |
| source_trading_date | 宿主将 result.provenance.last_source_timestamp 按 Asia/Shanghai 呈现为日期；字符串或 null | 固定 null；information.actual_range 是测试区间，不是源交易日 |
| fetched_at | result.provenance.collected_at；字符串或 null | report.information.collected_at，缺失则 null；当前固定样本为 null |
| core_version | result.versions.core；非空字符串 | report.versions.core；投影可 null，完整报告仍受原版本约束 |
| data_coverage | result.coverage 整体只读复制；对象 | report.coverage 整体只读复制；对象，不套核心覆盖结构 |
| validation_status | 原状态的五键对象，各值可 null，见下节 | report.states 整体复制，保留 execution / evidence / promotion，不补成五键 |

source_trading_date 只是最后冻结输入条目的源时间日期，不证明交易所最终值、当前交易日或连续历史。fetched_at 是冻结输入接收时刻，不是源更新时间、页面刷新或回答时间；null 不允许以当前时间补齐。

binding.result_schema 区分三个原 schema；calculation_identity 为 unchanged_original_result_id，may_recalculate 为 false。核心/参考 performance_claim 为 none，当前合成报告为 synthetic_engineering_only。date_basis 原文保留，未知语义不自行“兼容”。

### 状态和覆盖分支

- 核心/参考五键：evidence_kind ← evidence.kind，calculation ← evidence.calculation，empirical_performance ← evidence.empirical_performance，execution ← execution.status，promotion ← promotion.status。五个层级不得压成“已验证”。
- 此基线 evidence_kind 为 synthetic / historical / forward_shadow / current；calculation 为 computed / partial / unavailable；绩效为 not_measured，执行和晋级为 not_requested。computed 只说明计算；current 不等真实成交，forward_shadow 不自动等前瞻通过。
- 报告 schema：execution=completed_offline_contract，evidence=synthetic|historical，promotion=not_authorized。当前固定样本 evidence=synthetic，只是离线契约证据，不能译成实盘执行完成或策略收益已验证。
- 核心覆盖键：status、basis、requested_bars、requested_start、requested_end、gaps、available_bars、evaluated_bars、requested_candidates、evaluated_candidates、failed_candidates。status 为 complete_window / partial / unknown；条数不等有效成交数。
- 参考覆盖没有 evaluated_bars，不补零、不借核心值。报告另含 data、会话、订单和成交等层级，按原对象与原因解释，不把合成 fills 变成本人真实成交。
- 方法可用性依当次 state、reason_codes 和输入证据，不永久沿用旧文档“四项禁用”，也不因实现存在全部启用。未知开盘时刻、量额或会话锚点不能由技能补造。

## 已核响应位置

这些是已有宿主的响应路径说明，不是新增工具、公开访问权或可直接使用的网络地址；调用仍须当前授权、实际工具和宿主鉴权。

- GET /api/strategy-workshop?view=core-latest：response.result 是原核心结果，response.result_reference 绑定它。
- view=core-web / core-skill：投影在 response.result，schema 为 strategy-workshop-v1；response.result_reference 与 response.result.result_reference 一致。不是 response.projection。
- 同响应 response.reference_result 是独立 reference-result-v1，不能套顶层核心引用。未收到与该参考结果同 ID 的宿主引用时，技能不拼装引用。已有参考预览的 preview.result_reference 绑定 preview.result.result_id。
- 冻结预览和读回始终保留 preview_id 与原 result_id；登记回执引用预览计算结果，登记拒绝及原原因不变，元数据登记不是启用。
- view=validation / validation-web / validation-skill：原报告/投影在 response.report；顶层 result_reference 绑定它，web/skill 的 report.result_reference 与顶层一致。
- POST /api/analysis-context 准备态：只在 core.status=compatible_host_result 且 core.may_explain_core=true 时解释 core.context，引用位于 core.context.result_reference。身份、版本、symbol 或引用漂移保持 blocked；独立 legacy 报价事实仍可分析。
- 摘要引用位于 body.core.result_reference，不是 snapshot_id。旧摘要无该字段时按旧版本读取旧字段，新增引用与覆盖保持未知，不重建 strategy-result-v1 或称六字段齐备。

## 财务 / ETF 可选证据兼容（1.6）

本轮只增补本说明与共享 data-coverage 文档，依据交接的公共 PR1 HEAD `3e29e13ee4f716b72218d5848e387ada9e5a903f`（仍为 draft、未合并）；既有基线、版本、六字段及文末原增量验收记录保留，不新增技能或数值实现。

交接记录中的新云端 runtime 为 `public17`，原站仍为 `v104` / 服务 `1.33.8`，本地 `.1` 未升级。云端候选或工程支持不证明旧宿主已具备新增能力；以当前实际 schema 和返回内容检测可选分支，不按版本标签强制开启、不猜接口。未返回扩展时沿用旧合同并保留新增证据缺口，不能声称线上、手机或本地已可用。本轮未调用模型或数据，亦未取得新增真实业务响应、宿主加载或历史 PIT 证据。

### 财务现金质量

| 路径 | 可选合同与读取边界 |
| --- | --- |
| `result.data` | 原财务摘要结构及语义不变，不用扩展覆盖原数据 |
| `result.cash_quality` | 可选 `stock-financial-cash-quality-v1` 结果，只读引用原核心输出 |
| `source.financialCashQuality` | `{symbol,base_summary_fetched_at,cash_quality,evidence_sources}`，保留原证据来源 |

逐报告期核对同一 symbol、同一报告期及同一摘要抓取基准：`base_summary_fetched_at` 对应本次基础摘要的原抓取时刻，旁路 `cash_quality` 与本次 `result.cash_quality` 一致。不拼接不同标的、报告期、抓取批次或修订版本；缺绑定、缺来源、未识别 schema、缺数据或冲突，仅将受影响项标为 `unavailable` 并保留原因，原 `result.data` 及独立已支持内容仍按旧合同读取。缺失不是零，不能填当前时间或自行修复绑定。

经营活动现金流（OCF）及含少数股东的合并净利润只引用原核心值和原指标，不用归母净利润混作分母，不自行求比、求差、年化、补算或另建现金质量评分。解释须保留核心给出的期间、主体、币种单位、利润口径差异及现金与利润背离等反证；没有对应原指标时只说明事实和缺口。报告期、披露日与抓取时刻分列，当前财报或修订后的现金质量不等于历史当时可得数据；没有当时可得版本及披露/修订链，不能当历史 PIT 或倒填历史决策。

### ETF 披露关系与估算

- 股 / 基金关联只表示已披露报告期的关系，保留报告期、披露来源、抓取时刻及覆盖；当前持仓为 `unknown`。旧报告、新抓取、单源空结果或未列入某份报告，都不能证明当前持有、已清仓或全市场无关联。
- ETF 名称、主题词或相似简称不能推导 `BK` 板块或成员。只引用实际返回且有来源的代码 / 分类映射；缺映射或来源冲突时仅相应映射未知，不将其变成确定行业归属、个股资金流或交易信号。
- 份额 × NAV 及其变化即使已有核心输出，也只按原标签解释为估算；拆分为 `unknown`，不等于现金净申购。份额、NAV 的时点或单位不齐及实物申赎、现金替代等未拆分因素须保留，不能据此断定现金流入或对某只股票的确定买卖；技能不自行相乘、求差或重算。
- `159919` 已有工程支持；交接记录中的真实两源仍拒绝、未实连。保留原拒绝及未验缺口，不把工程或拒绝分支支持当作两源实连、双源一致或线上可用，也不补造另一源绕过拒绝。

上述为独立可选证据，不扩写 `strategy-result-reference-v1` 或 `research-sync-summary-v1` 白名单，不替代核心结果身份、执行资格或原拒绝规则；未识别扩展只暂停受影响的解释，不把旧版缺可选字段判为整个分析失败。

## 未知字段和拒绝规则

1. 未登记 schema、核心版本、binding.result_schema 或状态语义：标“待核接口/状态”，停止受影响核心结论，不自动升级、重命名或猜兼容；独立来源事实仍可分析。
2. 未知引用字段、必需字段缺失、类型错误或原对象/引用不一致：不删字段、不填默认值、不拼接不同结果来绕过拒绝。现有精确引用匹配会报 CORE_RESULT_REFERENCE_MISMATCH（底层 STRATEGY_RESULT_REFERENCE_MISMATCH）；版本未知为 UNSUPPORTED_CORE_VERSION。
3. null、unknown、unavailable、partial、缺引用、拒绝和有效空结果分别解释。status=empty / NO_ACTUAL_CORE_OBSERVATION 不造 result_id，不当零收益或全市场零机会。
4. 摘要白名单拒绝额外键，错误为 RESEARCH_SUMMARY_INVALID。当前带引用摘要只支持 strategy-result-v1、strategy-core-v1.0.0、mode=current、已核来源和候选；不塞参考结果、合成报告或任意新候选。其他完整宿主分支不因此被全局判错。
5. 覆盖边界由原宿主校验，技能不重算或修复。参考缺 evaluated_bars 是合法分支差异；报告三状态不强制变成五状态。
6. 引用函数本身不等于完整输入/交易资格校验。原宿主身份和权限检查仍必需。宿主未严格枚举的新增状态或扩展字段，在解释层保持待核，不声称运行时代码已自动拒绝所有未知项。

拒绝规则不新增权限，不更改身份，不把分析失败转换成交易许可。费用和净绩效未知仍未知；历史、前瞻影子、真实成交分账，参考价不是策略，默认拒绝和原原因不变。

## 增量和验收边界

只更新共享合同、本说明、README、两清单版本、VERSION 和 provenance 摘要。七入口已引用同一合同，不复制七份映射、不增删技能。一次定向检查只覆盖文档变化、实际投影和拒绝分支，不重算价格/指标/收益，不请求行情，不迁移数据，不启用交易/提醒，不发布 PRIVATE、不合并公共默认分支。

安装、生产加载、模型调用、真实业务响应和手机可用性不在本次源码兼容结论内。继续使用已有获授权宿主与原仓库，不另建替代平台。
