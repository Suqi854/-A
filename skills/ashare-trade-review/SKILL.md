---
name: ashare-trade-review
description: 录入并复盘A股真实或模拟成交、持仓做T和动态调仓，复用本人确认账本、去重分账，以冻结持有基线比较成本后结果；保留失败与未闭环，新经验仅未来生效。
---

# 成交录入与复盘

本技能的数值输出先遵守[统一核心结果引用合同](../ashare-analysis/references/data-coverage.md#统一核心结果引用合同)：只引用核心原结果，不自行重算；逐项保留 result_id、source_trading_date、fetched_at、core_version、data_coverage、validation_status 的原值或“待绑定／未知”。本页及后续材料中的公式和计算口径只供解释核心结果；拒绝、参考价、历史/前瞻/真实成交的区分均按该合同，缺原结果就给结构条件与缺口。

先给本次最重要结论：继续、暂停或验证哪项动作，事实依据、恢复/失效条件及备选。盈利与过程有效性分别评价，违规盈利不证明规则有效。

新增输入先草稿→本人确认，既有确认直接复用，不反复确认。真实、纸面账户与报价情景分账；quote、触发和提醒不生成成交。实际量价费时来自用户确认记录，未知费用不填零；录入实际数量不等于建议下单数量。真实违规、部分成交和未闭环如实保留。

录入与归因按[复盘记录](references/review-contract.md)，全机会分母、实际净胜率、跨日冻结基线、事前协议规定的定期复盘统一按[验证流程](../ashare-strategy-validation/references/validation-protocol.md)。T失败后另作永久调仓也保留原失败、费用和剩余敞口，不重命名洗掉成绩。

同步按[账本契约](../ashare-analysis/references/ledger-and-sync.md)与[私人账户合同](../ashare-analysis/references/private-account-contract.md)核真实回执；CONFIRMED_PENDING_LOCAL仍待处理，本地COMMITTED与同版本读回一致才称本地记账完成，云副本与手机读回另核。本公开包只保留通用复盘流程；真实或模拟个体记录、凭据、原始行情、私有运行源码与策略参数不得打包。技能规则不自动创建定时任务、发送通知或连接券商交易，无实际设施就输出待落地模板与缺口。