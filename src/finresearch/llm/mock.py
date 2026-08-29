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
            data = {
                "counter_thesis": {
                    "text": "收入增长存在，但现金流转化偏弱，增长质量仍需更多期间数据确认。",
                    "claim_ids": ["claim-1", "claim-2"],
                    "assumptions": ["现金流与净利润的差异具有持续性。"],
                },
                "challenged_claim_ids": [],
                "risks": [
                    {
                        "text": "经营现金流低于净利润，利润质量需要持续核验。",
                        "claim_ids": ["claim-2"],
                        "assumptions": [],
                    }
                ],
                "missing_information": ["缺少应收账款、毛利率、资本开支和行业对比数据。"],
            }
        elif task == "synthesize":
            data = {
                "executive_summary": {
                    "text": (
                        "示例公司收入增长较快，但经营现金流转化弱于净利润，"
                        "暂不能仅凭两条证据判断增长质量稳健。"
                    ),
                    "claim_ids": ["claim-1", "claim-2"],
                    "assumptions": [],
                },
                "thesis": {
                    "text": "增长表观强劲，现金流质量是决定判断能否成立的关键变量。",
                    "claim_ids": ["claim-1", "claim-2"],
                    "assumptions": ["现金流转化能够代表增长质量。"],
                },
                "risks": payload["challenge"]["risks"],
                "missing_information": payload["challenge"]["missing_information"],
                "limitations": ["报告仅使用用户提供的示例证据，不含实时市场或行业数据。"],
                "monitoring_items": [
                    {
                        "text": "下一期经营现金流与净利润的匹配程度。",
                        "claim_ids": ["claim-2"],
                        "assumptions": [],
                    }
                ],
                "invalidation_conditions": [
                    {
                        "text": "后续审计或公告推翻当前收入、净利润或现金流数据。",
                        "claim_ids": ["claim-1", "claim-2"],
                        "assumptions": [],
                    }
                ],
            }
        else:
            raise ValueError(f"unknown mock task: {task}")
        return output_model.model_validate(data)
