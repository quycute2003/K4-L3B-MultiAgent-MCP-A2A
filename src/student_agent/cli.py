from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from .cases import load_case_set
from .config import Settings
from .contracts import Contracts
from .mcp_gateway import connect_gateway
from .submission import package_submission, validate_artifacts
from .trace import TraceWriter
from .workflow import solve_case


def _root(value: str) -> Path:
    return Path(value).resolve()


def _error_details(exc: BaseException) -> str:
    if isinstance(exc, BaseExceptionGroup):
        return " | ".join(_error_details(child) for child in exc.exceptions)
    return f"{type(exc).__name__}: {exc}"


async def _show_tools(root: Path) -> None:
    settings = Settings.load(root)
    contracts = Contracts(root / "contracts" / "schemas")
    async with connect_gateway(settings.mcp_endpoint, settings.team_api_key, contracts) as gateway:
        for tool in await gateway.list_tools():
            print(tool)


async def _run(root: Path, *, resume: bool = False) -> None:
    settings = Settings.load(root)
    case_set = load_case_set(root)
    contracts = Contracts(root / "contracts" / "schemas")
    output_root = root / "outputs"
    trace_path = root / "traces" / "trace.jsonl"
    output_root.mkdir(parents=True, exist_ok=True)
    trace_path.parent.mkdir(parents=True, exist_ok=True)
    completed = _prepare_run(
        output_root=output_root,
        trace_path=trace_path,
        case_ids=case_set.case_ids,
        contracts=contracts,
        resume=resume,
    )
    await _discover_tools(settings, contracts)
    for index, case_id in enumerate(case_set.case_ids, start=1):
        if case_id in completed:
            print(f"SKIP {index:03d}/{len(case_set.case_ids)} {case_id}")
            continue
        output, case_trace_path = await _solve_remote_case(
            root=root,
            case=case_set.cases[case_id],
            settings=settings,
            contracts=contracts,
        )
        contracts.validate_output(output, f"outputs/{case_id}.json")
        if output.get("case_id") != case_id:
            raise ValueError(f"solver returned a mismatched case_id for {case_id}")
        target = output_root / f"{case_id}.json"
        temporary = target.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        temporary.replace(target)
        with trace_path.open("a", encoding="utf-8") as destination:
            destination.write(case_trace_path.read_text(encoding="utf-8"))
        case_trace_path.unlink(missing_ok=True)
        print(f"OK   {index:03d}/{len(case_set.case_ids)} {case_id}")


def _prepare_run(
    *,
    output_root: Path,
    trace_path: Path,
    case_ids: tuple[str, ...],
    contracts: Contracts,
    resume: bool,
) -> set[str]:
    if not resume:
        for stale in output_root.glob("*.json"):
            stale.unlink()
        trace_path.unlink(missing_ok=True)
        return set()

    completed: set[str] = set()
    for case_id in case_ids:
        path = output_root / f"{case_id}.json"
        if not path.is_file():
            continue
        try:
            output = json.loads(path.read_text(encoding="utf-8"))
            contracts.validate_output(output, str(path))
        except (OSError, ValueError, json.JSONDecodeError):
            path.unlink(missing_ok=True)
            continue
        if output.get("case_id") == case_id:
            completed.add(case_id)

    if trace_path.is_file():
        kept: list[str] = []
        finalized: set[str] = set()
        for line in trace_path.read_text(encoding="utf-8").splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("case_id") in completed:
                kept.append(line)
                if event.get("event_type") == "case_finalized":
                    finalized.add(str(event["case_id"]))
        incomplete = completed - finalized
        for case_id in incomplete:
            (output_root / f"{case_id}.json").unlink(missing_ok=True)
        completed &= finalized
        kept = [line for line in kept if json.loads(line).get("case_id") in completed]
        trace_path.write_text("".join(f"{line}\n" for line in kept), encoding="utf-8")
    else:
        for case_id in completed:
            (output_root / f"{case_id}.json").unlink(missing_ok=True)
        completed.clear()
    return completed


async def _discover_tools(settings: Settings, contracts: Contracts) -> None:
    for attempt in range(1, 4):
        try:
            async with connect_gateway(
                settings.mcp_endpoint, settings.team_api_key, contracts
            ) as gateway:
                if not await gateway.list_tools():
                    raise RuntimeError("MCP Gateway returned no tools")
            return
        except Exception as exc:
            if attempt == 3:
                raise RuntimeError("MCP tool discovery failed after 3 attempts") from exc
            print(
                f"WARN MCP discovery attempt {attempt}/3 failed; reconnecting",
                file=sys.stderr,
            )


async def _solve_remote_case(
    *, root: Path, case: dict[str, Any], settings: Settings, contracts: Contracts
) -> tuple[dict[str, Any], Path]:
    case_id = str(case["case_id"])
    case_trace_path = root / "traces" / f".{case_id}.jsonl.tmp"
    for attempt in range(1, 4):
        case_trace_path.unlink(missing_ok=True)
        trace = TraceWriter(case_trace_path, contracts)
        trace.emit(case_id=case_id, event_type="case_received", actor="coordinator")
        try:
            async with connect_gateway(
                settings.mcp_endpoint, settings.team_api_key, contracts
            ) as gateway:
                output = await solve_case(case, gateway, trace)
            trace.emit(case_id=case_id, event_type="case_finalized", actor="coordinator")
            return output, case_trace_path
        except Exception as exc:
            case_trace_path.unlink(missing_ok=True)
            if attempt == 3:
                raise RuntimeError(
                    f"{case_id} failed after {attempt} connection attempts: "
                    f"{_error_details(exc)}"
                ) from exc
            print(
                f"WARN {case_id}: connection attempt {attempt}/3 failed; reconnecting",
                file=sys.stderr,
            )
    raise AssertionError("unreachable")


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Day09 L3B student workflow")
    result.add_argument("--root", default=".", help="repository root (default: current directory)")
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("validate-inputs", help="validate case-set.json and all 100 inputs")
    commands.add_parser("mcp-tools", help="authenticate and list discovered MCP tools")
    run = commands.add_parser("run", help="run the implemented workflow for all cases")
    run.add_argument(
        "--resume", action="store_true", help="keep valid outputs and continue an interrupted run"
    )
    commands.add_parser("validate", help="validate outputs and observable trace")
    package = commands.add_parser("package", help="validate and build the submission ZIP")
    package.add_argument("--output", default="dist/submission.zip")
    return result


def main() -> None:
    args = parser().parse_args()
    root = _root(args.root)
    try:
        if args.command == "validate-inputs":
            case_set = load_case_set(root)
            print(
                f"OK: {case_set.variant_id} / {case_set.version} / {len(case_set.case_ids)} cases"
            )
        elif args.command == "mcp-tools":
            asyncio.run(_show_tools(root))
        elif args.command == "run":
            asyncio.run(_run(root, resume=args.resume))
        elif args.command == "validate":
            case_set = load_case_set(root)
            contracts = Contracts(root / "contracts" / "schemas")
            _, trace = validate_artifacts(root, case_set, contracts)
            print(f"OK: {len(case_set.case_ids)} outputs / {len(trace)} trace events")
        elif args.command == "package":
            destination = package_submission(root, root / args.output)
            print(f"OK: {destination}")
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
