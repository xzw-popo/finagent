from __future__ import annotations

import argparse
import getpass
import os
import sys
from pathlib import Path

from finresearch import __version__
from finresearch.evidence import filter_evidence_as_of, load_evidence, load_request
from finresearch.evidence_merge import (
    EvidenceMergeError,
    MergeLimits,
    merge_evidence_bundles,
)
from finresearch.fundamentals import (
    FinancialsCollectorConfig,
    LongbridgeFinancialsCollector,
    write_financial_collection,
)
from finresearch.llm import DeepSeekAdapter, MockAdapter
from finresearch.marketdata import (
    LongbridgeCollectionError,
    LongbridgeCollectorConfig,
    LongbridgeQuoteCollector,
    write_quote_collection,
)
from finresearch.policy import NoTradePolicy
from finresearch.settings import DeepSeekSettings
from finresearch.workflow import ResearchWorkflow

ALLOWED_LLM_MODES = frozenset({"mock", "deepseek"})

# Stable process exit codes for ``collect-quote``.  Exit 2 remains reserved for
# command-line/input errors emitted by argparse; 8 is reserved for local output
# failures.  Provider codes not listed here fail closed as a provider/protocol
# error (5).
COLLECT_QUOTE_EXIT_CODES = {
    "invalid_symbol": 2,
    "invalid_report": 2,
    "binary_not_found": 3,
    "timeout": 4,
    "authentication_required": 5,
    "region_unreachable": 5,
    "network_unavailable": 5,
    "rate_limited": 5,
    "command_failed": 5,
    "clock_error": 5,
    "empty_output": 5,
    "invalid_json": 5,
    "schema_mismatch": 5,
    "protocol_mismatch": 5,
    "output_too_large": 5,
    "no_data": 6,
    "partial_result": 7,
}
LOCAL_OUTPUT_EXIT_CODE = 8

# ``merge-evidence`` is a local, deterministic operation, so provider-specific
# statuses do not apply. Unexpected merge error codes fail closed as invalid
# input rather than leaking a traceback through the command-line interface.
MERGE_EVIDENCE_EXIT_CODES = {
    "output_error": 8,
    "invalid_bundle": 9,
    "input_error": 9,
    "duplicate_evidence_id": 10,
    "resource_limit": 11,
}


def validate_llm_mode(mode: str) -> str:
    if mode not in ALLOWED_LLM_MODES:
        raise ValueError(f"unsupported FIN_AGENT_LLM_MODE: {mode}")
    return mode


def _terminal_safe(value: str) -> str:
    """Keep normal IDs readable while escaping terminal-control characters."""

    escaped: list[str] = []
    for character in value:
        if character.isprintable() and character != "\x1b":
            escaped.append(character)
        else:
            codepoint = ord(character)
            width = 4 if codepoint <= 0xFFFF else 8
            prefix = "u" if width == 4 else "U"
            escaped.append(f"\\{prefix}{codepoint:0{width}x}")
    return "".join(escaped)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="finresearch")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate = subparsers.add_parser("validate", help="validate request/evidence and PIT gates")
    validate.add_argument("--request", type=Path, required=True)
    validate.add_argument("--evidence", type=Path, required=True)

    collect_quote = subparsers.add_parser(
        "collect-quote",
        help="collect read-only Longbridge quote snapshots as an auditable evidence bundle",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "stable exit codes:\n"
            "  2 input/configuration error    3 CLI unavailable\n"
            "  4 timeout                      5 provider/protocol error\n"
            "  6 no data                      7 partial result\n"
            "  8 local output error\n"
            "Runtime errors also report code=<name> and retryable=true|false on stderr."
        ),
    )
    collect_quote.add_argument(
        "symbols",
        nargs="+",
        help=(
            "up to 50 symbols in explicit <CODE>.<MARKET> format, "
            "e.g. NVDA.US or 700.HK"
        ),
    )
    collect_quote.add_argument(
        "--output",
        type=Path,
        required=True,
        help="new output directory; the path must not already exist",
    )
    collect_quote.add_argument(
        "--timeout",
        type=float,
        default=20.0,
        help="per-command timeout in seconds (0 < timeout <= 120; default: 20)",
    )
    collect_quote.add_argument(
        "--region",
        choices=("auto", "cn", "global"),
        default="auto",
        help="Longbridge access region; auto uses the CLI's own detection",
    )

    collect_financials = subparsers.add_parser(
        "collect-financials",
        help="collect complete Longbridge financial statements as auditable evidence",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "stable exit codes:\n"
            "  2 input/configuration error    3 CLI unavailable\n"
            "  4 timeout                      5 provider/protocol error\n"
            "  6 no data                      7 partial result\n"
            "  8 local output error\n"
            "Runtime errors also report code=<name> and retryable=true|false on stderr."
        ),
    )
    collect_financials.add_argument(
        "symbol",
        help="one symbol in explicit <CODE>.<MARKET> format, e.g. NVDA.US or 700.HK",
    )
    collect_financials.add_argument(
        "--report",
        choices=("af", "saf", "qf"),
        default="af",
        help=(
            "report period: af=annual, saf=semi-annual, qf=quarterly; "
            "cumul is intentionally unsupported for complete three-table bundles "
            "(default: af)"
        ),
    )
    collect_financials.add_argument(
        "--segments",
        action="store_true",
        help=(
            "also collect same-frequency historical business segments"
        ),
    )
    collect_financials.add_argument(
        "--output",
        type=Path,
        required=True,
        help="new output directory; the path must not already exist",
    )
    collect_financials.add_argument(
        "--timeout",
        type=float,
        default=20.0,
        help="per-command timeout in seconds (0 < timeout <= 120; default: 20)",
    )
    collect_financials.add_argument(
        "--region",
        choices=("auto", "cn", "global"),
        default="auto",
        help="Longbridge access region; auto uses the CLI's own detection",
    )
    collect_financials.set_defaults(_selected_parser=collect_financials)

    merge_evidence = subparsers.add_parser(
        "merge-evidence",
        help="merge two or more evidence bundles into one self-contained bundle",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "stable exit codes:\n"
            "  2 input/argument error         8 local output error\n"
            "  9 invalid or unreadable bundle\n"
            " 10 duplicate evidence_id       11 resource limit exceeded"
        ),
    )
    merge_evidence.add_argument(
        "--evidence",
        type=Path,
        action="append",
        required=True,
        help="input evidence.json file; repeat this option at least twice",
    )
    merge_evidence.add_argument(
        "--output",
        type=Path,
        required=True,
        help="new output directory; the path must not already exist",
    )

    run = subparsers.add_parser("run", help="run the controlled research workflow")
    run.add_argument(
        "--mode",
        choices=("mock", "deepseek"),
        default=os.environ.get("FIN_AGENT_LLM_MODE", "mock"),
    )
    run.add_argument("--ask-key", action="store_true", help="read DeepSeek key without echo")
    run.add_argument("--request", type=Path, required=True)
    run.add_argument("--evidence", type=Path, required=True)
    run.add_argument(
        "--output",
        type=Path,
        required=True,
        help="new output directory; the path must not already exist",
    )
    return parser


def _exit_for_collection_error(
    parser: argparse.ArgumentParser,
    exc: LongbridgeCollectionError,
    *,
    command: str = "collect-quote",
) -> None:
    status = COLLECT_QUOTE_EXIT_CODES.get(exc.code, 5)
    if status == 2:
        parser.error(f"{exc.code}: {exc}")
    retryable = str(exc.retryable).lower()
    message = _terminal_safe(str(exc))
    parser.exit(
        status,
        f"finresearch {command}: error code={exc.code} "
        f"retryable={retryable}: {message}\n",
    )


def _exit_for_local_output_error(
    parser: argparse.ArgumentParser,
    exc: Exception,
    *,
    command: str = "collect-quote",
) -> None:
    message = _terminal_safe(str(exc))
    parser.exit(
        LOCAL_OUTPUT_EXIT_CODE,
        f"finresearch {command}: local output error: {message}\n",
    )


def _exit_for_evidence_merge_error(
    parser: argparse.ArgumentParser, exc: EvidenceMergeError
) -> None:
    status = MERGE_EVIDENCE_EXIT_CODES.get(exc.code, 9)
    parser.exit(
        status,
        f"finresearch merge-evidence: error code={exc.code}: {exc}\n",
    )


def main() -> None:
    parser = _parser()
    args = parser.parse_args()
    if args.command == "collect-quote":
        policy = NoTradePolicy()
        try:
            policy.authorize("read_market_quote")
            collector = LongbridgeQuoteCollector(
                LongbridgeCollectorConfig(
                    timeout_seconds=args.timeout,
                    region=args.region,
                )
            )
        except ValueError as exc:
            parser.error(str(exc))
        try:
            collection = collector.collect(args.symbols)
        except LongbridgeCollectionError as exc:
            _exit_for_collection_error(parser, exc)
        try:
            policy.authorize("write_local_evidence")
            evidence_path = write_quote_collection(collection, args.output)
        except (OSError, ValueError) as exc:
            _exit_for_local_output_error(parser, exc)
        print(f"collected: {len(collection.evidence)} quote evidence record(s)")
        print(f"evidence: {evidence_path.resolve()}")
        print(f"available_at: {collection.available_at.isoformat()}")
        for item in collection.evidence:
            print(f"evidence_id: {_terminal_safe(item.evidence_id)}")
        print("数据来源：长桥证券")
        return

    if args.command == "collect-financials":
        command_parser = args._selected_parser
        policy = NoTradePolicy()
        try:
            policy.authorize("read_financial_statements")
            collector = LongbridgeFinancialsCollector(
                FinancialsCollectorConfig(
                    timeout_seconds=args.timeout,
                    region=args.region,
                )
            )
        except ValueError as exc:
            command_parser.error(str(exc))
        try:
            collection = collector.collect(
                args.symbol,
                report=args.report,
                include_segments=args.segments,
            )
        except LongbridgeCollectionError as exc:
            _exit_for_collection_error(
                command_parser, exc, command="collect-financials"
            )
        try:
            policy.authorize("write_local_evidence")
            result = write_financial_collection(collection, args.output)
        except (OSError, ValueError) as exc:
            _exit_for_local_output_error(
                parser, exc, command="collect-financials"
            )
        print(f"collected: {len(collection.evidence)} financial evidence record(s)")
        print(f"evidence: {result.evidence_path.resolve()}")
        print(f"manifest: {result.manifest_path.resolve()}")
        print(f"available_at: {collection.available_at.isoformat()}")
        for item in collection.evidence:
            print(f"evidence_id: {_terminal_safe(item.evidence_id)}")
        if not result.durability_confirmed:
            print(
                "warning: output is complete, but parent-directory fsync failed; "
                "crash durability is unconfirmed",
                file=sys.stderr,
            )
        print("数据来源：长桥证券")
        return

    if args.command == "merge-evidence":
        if len(args.evidence) < 2:
            parser.error("merge-evidence requires at least two --evidence inputs")
        try:
            result = merge_evidence_bundles(
                args.evidence,
                args.output,
                limits=MergeLimits(),
            )
        except EvidenceMergeError as exc:
            _exit_for_evidence_merge_error(parser, exc)
        print(
            f"merged: {result.evidence_count} evidence record(s) "
            f"from {result.input_count} bundle(s)"
        )
        print(f"evidence: {result.evidence_path.resolve()}")
        print(f"manifest: {result.manifest_path.resolve()}")
        print(
            "minimum_as_of_for_all_evidence: "
            f"{result.minimum_as_of_for_all_evidence.isoformat()}"
        )
        for evidence_id in sorted(result.evidence_ids):
            print(f"evidence_id: {_terminal_safe(evidence_id)}")
        if not getattr(result, "durability_confirmed", True):
            print(
                "warning: output is complete, but parent-directory fsync failed; "
                "crash durability is unconfirmed",
                file=sys.stderr,
            )
        return

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

    report = ResearchWorkflow(adapter).run(
        request,
        evidence,
        args.output,
        evidence_artifact_dir=args.evidence.parent,
    )
    print(f"completed: {report.stage}")
    print(f"report: {(args.output / 'report.json').resolve()}")
