from __future__ import annotations

import argparse
import getpass
import os
from pathlib import Path

from finresearch.evidence import filter_evidence_as_of, load_evidence, load_request
from finresearch.llm import DeepSeekAdapter, MockAdapter
from finresearch.settings import DeepSeekSettings
from finresearch.workflow import ResearchWorkflow

ALLOWED_LLM_MODES = frozenset({"mock", "deepseek"})


def validate_llm_mode(mode: str) -> str:
    if mode not in ALLOWED_LLM_MODES:
        raise ValueError(f"unsupported FIN_AGENT_LLM_MODE: {mode}")
    return mode


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="finresearch")
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate = subparsers.add_parser("validate", help="validate request/evidence and PIT gates")
    validate.add_argument("--request", type=Path, required=True)
    validate.add_argument("--evidence", type=Path, required=True)

    run = subparsers.add_parser("run", help="run the controlled research workflow")
    run.add_argument(
        "--mode",
        choices=("mock", "deepseek"),
        default=os.environ.get("FIN_AGENT_LLM_MODE", "mock"),
    )
    run.add_argument("--ask-key", action="store_true", help="read DeepSeek key without echo")
    run.add_argument("--request", type=Path, required=True)
    run.add_argument("--evidence", type=Path, required=True)
    run.add_argument("--output", type=Path, required=True)
    return parser


def main() -> None:
    parser = _parser()
    args = parser.parse_args()
    if args.command == "run":
        try:
            validate_llm_mode(args.mode)
        except ValueError as exc:
            parser.error(str(exc))
    request = load_request(args.request)
    evidence = load_evidence(args.evidence)

    if args.command == "validate":
        eligible, rejected = filter_evidence_as_of(request, evidence)
        print(f"valid: {len(eligible)} eligible, {len(rejected)} rejected by policy")
        return

    if args.mode == "mock":
        adapter = MockAdapter()
    else:
        api_key = None
        if args.ask_key and not os.environ.get("DEEPSEEK_API_KEY"):
            api_key = getpass.getpass("DeepSeek API key: ")
        settings = DeepSeekSettings.from_environment(api_key=api_key)
        adapter = DeepSeekAdapter(settings)

    report = ResearchWorkflow(adapter).run(request, evidence, args.output)
    print(f"completed: {report.stage}")
    print(f"report: {(args.output / 'report.json').resolve()}")
