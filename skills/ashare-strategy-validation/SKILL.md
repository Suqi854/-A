---
name: ashare-strategy-validation
description: 审查A股策略、参数与风险，分别验证全部机会的报价路径、前瞻影子和真实成交，按冻结持有基线比较成本后结果；报告覆盖、版本与不确定性，不以测试或观察期进度代替盈利证据。
---

# 策略与量化验证

先给当前首选方案、证据层级和决定性缺口，再说明触发、失效与备选。允许等待与持有不操作；优先简单基准与可解释规则。

日内规则唯一入口是[日内决策框架](../ashare-position-t/references/intraday-decision-framework.md)；全部机会、快慢对照、报价与成交分账、跨日基线、事前冻结的观察期与复盘节奏统一按[验证流程](references/validation-protocol.md)。成交录入与归因用[交易复盘](../ashare-trade-review/SKILL.md)，不因验证问题强制全市场研究。

理论、合成、样本内、滚动样本外、前瞻影子与真实成交分别报告。未知费用不填零，也不阻止不依赖费用的方向观察；未闭环、失败和资料缺口保留，规则更新只影响生效后的新机会。没有实际持久化与运行证据时只交付待落地记录模板，不称已自动追踪或完成验证。

按需读[数据规范](../ashare-analysis/references/data-coverage.md)、[风险与数值](references/risk-models.md)、[证据与A股约束](references/evidence-and-ashare-gates.md)及[经验复盘](references/daily-review-method.md)。本公开包只保留通用方法，不包含真实/模拟个体记录、私有运行源码、个人策略参数、原始行情或凭据。私人接入按[通用账户合同](../ashare-analysis/references/private-account-contract.md)，逐次核当前授权与真实回执。