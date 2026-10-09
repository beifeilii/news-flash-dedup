"""判定 policy 版本常量单源（2026-10-10，提交二，分支 p3-semantic-authority）。

出处：`log/快讯去重_判官与证明层改造_可执行技术方案.md` §5.2 文件 E——
建立**无循环依赖**的版本常量单源：`lib/run_manifest.py` 与新提示词模块
（decide/judge_prompt_v5.py / decide/judge_prompt_v6.py）都从这里导入；
提示词模块不得反向导入 run_manifest.py。

- `policy_v2`：《判定宪章-草案-v2-1009.md》（2026-10-09 业务方审签生效）；
  judge_proof.PROOF_POLICY_VERSION 继续锚它（证明组件机检口径与旧证明
  工件复验兼容性不变，提交二不改 judge_proof）。
- `policy_v3`：判官语义权威业务口径（配套 judge_v5）——同义改写/信息
  缺失可重复、同槽位冲突判不重复、无损等价正文含相对时间无锚点允许
  重复、一侧仅缺证券代码不算主体完全缺失、来源尾注不参与判定；
  "主体回填"与"前值修订"两条产品规则未冻结，统一判边界。
- `policy_v4`：一期 v6-lite 冻结口径（配套 judge_v6，2026-10-11 分支
  p3-v6-phase1 新建）——在 policy_v3 之上冻结两条产品规则：R8 前值修订
  改值判不重复（同指向同一修订值省略过程=重复、只补背景有效值相同=
  重复、角色/指标/时间对不上=存疑）；R7 主体单方缺失受约束回填六条件
  （条件①机器前置硬闸+②-⑤判官条款+⑥强制存疑转边界，原因码"主体单
  方缺失高置信对齐"）。默认模式仍 legacy_proof_gate——policy_v4 仅
  semantic_authority 模式映射消费，生产默认行为零变化。
- DEFAULT_POLICY_VERSION 维持 policy_v3：一期不改缺省——v5 链治理口径
  不变（v6/policy_v4 只经 semantic 开关生效；manifest 缺省仍按实际生效
  模式如实登记）。
"""

from __future__ import annotations

POLICY_VERSION_V2 = "policy_v2"
POLICY_VERSION_V3 = "policy_v3"
POLICY_VERSION_V4 = "policy_v4"
DEFAULT_POLICY_VERSION = POLICY_VERSION_V3

__all__ = [
    "POLICY_VERSION_V2",
    "POLICY_VERSION_V3",
    "POLICY_VERSION_V4",
    "DEFAULT_POLICY_VERSION",
]
