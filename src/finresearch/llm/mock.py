from __future__ import annotations

from typing import Any

from finresearch.llm.base import ModelT


class MockAdapter:
    provider = "mock"
    model = "deterministic-fixture-v1"

    def generate(self, task: str, payload: dict[str, Any], output_model: type[ModelT]) -> ModelT:
        if task == "extract":
            claims = [
                {
                    "claim_id": f"claim-{index + 1}",
                    "kind": "fact",
                    "text": item["excerpt"],
                    "evidence_ids": [item["evidence_id"]],
                    "confidence": 0.85,
                    "assumptions": [],
                }
                for index, item in enumerate(payload["evidence"])
            ]
            data = {"claims": claims}
        elif task == "verify":
            claims = payload["claims"]
            data = {
                "verifications": [
                    {
                        "claim_id": claim["claim_id"],
                        "status": "verified",
                        "verifier_notes": "原文摘录与 Claim 一致；仅代表示例证据包内核验。",
                    }
                    for claim in claims
                ]
            }
        elif task == "challenge":
            claim_ids = [claim["claim_id"] for claim in payload["claims"]]
            last_claim_id = claim_ids[-1]
            data = {
                "counter_thesis": {
                    "text": "现有证据仅支持局部事实，结论仍需要更多时期与来源交叉验证。",
                    "claim_ids": claim_ids,
                    "assumptions": ["新增证据可能改变当前判断。"],
                },
                "challenged_claim_ids": [],
                "risks": [
                    {
                        "text": "证据覆盖范围有限，单一时点或来源可能不足以支撑稳健结论。",
                        "claim_ids": [last_claim_id],
                        "assumptions": [],
                    }
                ],
                "missing_information": ["缺少更长时间序列、独立来源和可比对象数据。"],
            }
        elif task == "synthesize":
            claim_ids = [claim["claim_id"] for claim in payload["claims"]]
            last_claim_id = claim_ids[-1]
            data = {
                "executive_summary": {
                    "text": "现有证据已通过结构和引用核验，但覆盖有限，不应外推至证据范围之外。",
                    "claim_ids": claim_ids,
                    "assumptions": [],
                },
                "thesis": {
                    "text": "当前判断只在已核验证据的时点、对象与字段范围内成立。",
                    "claim_ids": claim_ids,
                    "assumptions": ["证据的上游时间与来源标记可信。"],
                },
                "risks": payload["challenge"]["risks"],
                "missing_information": payload["challenge"]["missing_information"],
                "limitations": ["这是确定性 Mock 输出，用于验证工作流而非形成投资意见。"],
                "monitoring_items": [
                    {
                        "text": "监测后续更新是否改变当前已核验事实。",
                        "claim_ids": [last_claim_id],
                        "assumptions": [],
                    }
                ],
                "invalidation_conditions": [
                    {
                        "text": "后续上游修订、核心字段更正或原始证据验真失败。",
                        "claim_ids": claim_ids,
                        "assumptions": [],
                    }
                ],
            }
        else:
            raise ValueError(f"unknown mock task: {task}")
        return output_model.model_validate(data)
