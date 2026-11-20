# 失败与拒答检查

| 题目 | 组 | 预期 | 实际 | 结果依据 |
| --- | --- | --- | --- | --- |
| db-atomic | C | answer | error | invalid_generation |
| db-atomic | D | answer | error | invalid_generation |
| neg-conflict | A | conflict | error | invalid_generation |
| neg-conflict | B | conflict | error | invalid_generation |
| neg-conflict | C | conflict | error | invalid_generation |
| neg-conflict | D | conflict | error | invalid_generation |
| neg-expired | B | expired | answered | 未通过事实规则或预期拒答，未匹配陈述 None |
| neg-expired | C | expired | answered | 未通过事实规则或预期拒答，未匹配陈述 None |
| neg-expired | D | expired | answered | 未通过事实规则或预期拒答，未匹配陈述 None |
| neg-part | A | partial | error | invalid_generation |
| neg-part | B | partial | error | invalid_generation |
| neg-part | C | partial | error | invalid_generation |
| neg-part | D | partial | error | invalid_generation |
| net-mtu | C | answer | error | invalid_generation |
| net-mtu | D | answer | error | invalid_generation |
| os-map | A | answer | insufficient_evidence | no_draft |
