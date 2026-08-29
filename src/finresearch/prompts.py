from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel

PROMPT_VERSION = "v1.0.0"

SYSTEM_PROMPT = """你是受控金融研究流水线中的一个角色。
你只能使用输入 JSON 中给出的证据，不得引入外部事实、实时行情或隐含来源。
所有事实性结论必须引用输入中存在的 evidence_id。
不提供投资建议，不生成交易指令，不调用任何工具。
输出必须是且只能是符合给定 schema 的 JSON 对象。"""

TASK_INSTRUCTIONS = {
    "extract": (
        "提取最少但重要的原子 Claim。不要生成 as_of、状态或核验意见，"
        "也不要复述证据以外的信息。"
    ),
    "verify": (
        "逐条核对 Claim 与证据。每个 claim_id 必须恰好出现一次；"
        "只输出 verified、disputed 或 insufficient 状态和 verifier_notes。"
    ),
    "challenge": "以独立反证研究员身份寻找最强反例、风险与缺失信息。只能引用已有 claim_id。",
    "synthesize": (
        "以投委会秘书身份综合已核验 Claim 和反证意见。"
        "保留分歧，证据不足时明确说明，不给出买卖建议。"
    ),
}


def build_messages(
    task: str, payload: dict[str, Any], output_model: type[BaseModel]
) -> list[dict[str, str]]:
    if task not in TASK_INSTRUCTIONS:
        raise ValueError(f"unknown prompt task: {task}")
    schema = output_model.model_json_schema()
    user_content = {
        "task": TASK_INSTRUCTIONS[task],
        "output_json_schema": schema,
        "input": payload,
    }
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": json.dumps(user_content, ensure_ascii=False)},
    ]
