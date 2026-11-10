# 架构决策记录（ADR）

每个文件记录一个"改起来贵、所以值得写下来"的决定。格式：背景 → 决定 → 后果。

| # | 决定 | 状态 |
|---|---|---|
| [0001](0001-deterministic-sinking.md) | 计算下沉到确定性模块，模型不算数 | 已采纳 |
| [0002](0002-gate-downgrade-only.md) | 闸门只降不升 | 已采纳 |
| [0003](0003-stdlib-only-audit-bundle.md) | 审计包复算脚本只用标准库 | 已采纳 |
| [0004](0004-assumption-set-hash.md) | 用 assumption_set 哈希区分不可比运行 | 已采纳 |
| [0005](0005-report-unknowns.md) | 证据不足必须写成交付物 | 已采纳 |
