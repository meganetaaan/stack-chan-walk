#!/usr/bin/env python3
"""Run read-only KiCad ERC/DRC; never equate a clean report with board approval.

Python 3.10+, standard library only. Exit 0: requested checks passed;
1: violations; 2: input/tool/report failure (not checked).
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Sequence

COMMANDS = {"sch": "erc", "pcb": "drc"}
SUFFIXES = {"sch": ".kicad_sch", "pcb": ".kicad_pcb"}
SEXPR_TOKEN = re.compile(r'\s+|"(?:\\.|[^"\\])*"|[()]|[^\s()"]+')
DEPENDENCY_SIDECARS = (".kicad_pro", ".kicad_dru")
MANUAL_REVIEW_REQUIRED = [
    "Project/rule settings loaded correctly, ignored rules and exclusions",
    "All sheets, libraries and project dependencies at the reviewed revision",
    "Schematic/PCB parity (NOT run by this script)",
    "Ratings, power/return paths, mechanical fit and all outline corner radii",
    "Logo/revision presence and legibility in actual fabrication output",
    "Saved zone fills and Gerber/drill output at the reviewed revision",
]


def run(command: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command, capture_output=True, text=True, encoding="utf-8",
        errors="replace", check=False, timeout=timeout,
    )


def command_for(cli: str, kind: str, source: Path, report: Path,
                refill: bool) -> list[str]:
    command = [cli, kind, COMMANDS[kind], "--format", "json",
               "--severity-all", "--exit-code-violations"]
    if kind == "pcb" and refill:
        command.append("--refill-zones")
    # Do not use --save-board, infer a schematic partner, or overwrite inputs.
    return command + ["--output", str(report), str(source)]


def _decode_kicad_string(value: str) -> str:
    """Decode the escapes relevant to a KiCad quoted path."""
    return re.sub(r'\\(["\\])', r'\1', value)


def schematic_sheetfiles(text: str) -> list[str]:
    """Read only direct ``kicad_sch/sheet/property`` Sheetfile values.

    Walk tokens iteratively, retaining just each node's first three atoms.
    Quoted strings are single tokens, so their parentheses and escaped quotes
    cannot introduce fake sheets or terminate a real one.
    """
    stack: list[list[str]] = []
    paths: list[str] = []
    root_closed = False
    position = 0
    while position < len(text):
        match = SEXPR_TOKEN.match(text, position)
        if match is None:
            raise ValueError(f"Invalid schematic s-expression at offset {position}")
        token = match.group()
        position = match.end()
        if token.isspace():
            continue
        if token == "(":
            if root_closed:
                raise ValueError("Multiple schematic s-expression roots")
            stack.append([])
        elif token == ")":
            if not stack or not stack[-1]:
                raise ValueError("Invalid schematic s-expression closing parenthesis")
            node = stack.pop()
            if (len(stack) == 2 and stack[0][:1] == ["kicad_sch"]
                    and stack[1][:1] == ["sheet"]
                    and node[:2] == ["property", '"Sheetfile"']):
                if len(node) != 3 or not node[2].startswith('"'):
                    raise ValueError("Sheetfile must have a quoted path")
                paths.append(_decode_kicad_string(node[2][1:-1]))
            if not stack:
                if node[0] != "kicad_sch":
                    raise ValueError("Expected a kicad_sch s-expression root")
                root_closed = True
        else:
            if not stack:
                raise ValueError("Schematic atom outside an s-expression")
            if len(stack[-1]) < 3:
                stack[-1].append(token)
    if stack or not root_closed:
        raise ValueError("Incomplete schematic s-expression")
    return paths


def dependency_paths(source: Path) -> list[Path]:
    """Return the files that can affect this check, starting at ``source``."""
    dependencies = {source}
    if source.suffix == ".kicad_sch":
        pending = [source]
        while pending:
            schematic = pending.pop()
            text = schematic.read_text(encoding="utf-8")
            for sheetfile in schematic_sheetfiles(text):
                child = (schematic.parent / sheetfile).resolve()
                if child.suffix != ".kicad_sch":
                    raise ValueError(f"Referenced sheet is not a KiCad schematic: {child}")
                if not child.is_file():
                    raise ValueError(f"Referenced sheet does not exist: {child}")
                if child not in dependencies:
                    dependencies.add(child)
                    pending.append(child)

    # These sidecars are loaded by KiCad for the corresponding design. Missing
    # sidecars are intentionally omitted; their appearance during the check is
    # detected when the dependency set is collected again.
    for suffix in DEPENDENCY_SIDECARS:
        sidecar = source.with_suffix(suffix)
        if sidecar.is_file():
            dependencies.add(sidecar)
    return sorted(dependencies, key=str)


def dependency_snapshot(source: Path) -> dict[str, str]:
    return {
        str(path): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in dependency_paths(source)
    }


def verify_dependency_snapshot(source: Path, before: dict[str, str]) -> None:
    after = dependency_snapshot(source)
    before_paths = set(before)
    after_paths = set(after)
    if before_paths != after_paths:
        changed_paths = sorted(before_paths ^ after_paths)
        raise ValueError(
            "Review dependency set changed during review: "
            + ", ".join(changed_paths)
        )
    changed_paths = sorted(path for path in before if before[path] != after[path])
    if changed_paths:
        raise ValueError(
            "Review dependency changed during review: "
            + ", ".join(changed_paths)
        )


def check_entry(number: int, kind: str, source: Path, output: Path) -> dict:
    name = f"{number:03d}-{source.stem}.{COMMANDS[kind]}"
    return {
        "input": str(source), "kind": COMMANDS[kind], "command": None,
        "report": str(output / f"{name}.json"),
        "log": str(output / f"{name}.log"), "status": "NOT_CHECKED",
        "zones": "not_applicable" if kind == "sch" else None,
    }


def write_summary(output: Path, version: str | None, exit_code: int,
                  checks: list[dict], preflight_error: str | None = None) -> None:
    summary = {
        "kicad_version": version,
        "exit_code": exit_code,
        "checks": checks,
        "scope": "Only the explicitly selected ERC/DRC runs; not manufacturing approval.",
        "manual_review_required": MANUAL_REVIEW_REQUIRED,
    }
    if preflight_error is not None:
        summary["preflight_error"] = preflight_error
    (output / "summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8",
    )


def write_preflight_failure(output: Path, version: str | None,
                            checks: list[dict], error: Exception) -> int:
    message = f"NOT_CHECKED: {error}\n"
    for entry in checks:
        entry["error"] = str(error)
        Path(entry["log"]).write_text(
            message + f"Input: {entry['input']}\n", encoding="utf-8",
        )
    (output / "preflight.log").write_text(
        message + "All requested checks were left NOT_CHECKED.\n",
        encoding="utf-8",
    )
    write_summary(output, version, 2, checks, str(error))
    print(message.rstrip(), file=sys.stderr)
    return 2


def review(args: argparse.Namespace) -> int:
    inputs: list[tuple[str, Path]] = []
    for kind, paths in (("sch", args.schematic), ("pcb", args.pcb)):
        for path in paths:
            source = Path(path).resolve(strict=True)
            if not source.is_file() or source.suffix != SUFFIXES[kind]:
                raise ValueError(f"Expected an existing {SUFFIXES[kind]} file: {path}")
            if (kind, source) not in inputs:
                inputs.append((kind, source))

    parent = None
    if args.output_dir is not None:
        parent = Path(args.output_dir).resolve()
        parent.mkdir(parents=True, exist_ok=True)
    # A fresh directory prevents old reports from turning a failed run green.
    output = Path(tempfile.mkdtemp(prefix="board-review-", dir=parent))
    print(f"Reports: {output}")
    checks = [
        check_entry(number, kind, source, output)
        for number, (kind, source) in enumerate(inputs, 1)
    ]
    version_text: str | None = None

    # Keep all input/argument validation above output creation, but persist
    # failures from CLI discovery and capability preflight in the fresh run.
    try:
        cli = shutil.which(args.kicad_cli)
        if cli is None:
            raise ValueError(f"KiCad CLI not found: {args.kicad_cli}")
        version = run([cli, "version"], args.timeout)
        version_text = version.stdout.strip() or None
        if version.returncode != 0 or version_text is None:
            raise ValueError(f"Cannot read KiCad version: {version.stderr.strip()}")

        refill = False
        for kind in sorted({kind for kind, _ in inputs}):
            help_result = run([cli, kind, COMMANDS[kind], "--help"], args.timeout)
            required = ("--format", "--severity-all", "--exit-code-violations")
            if help_result.returncode != 0 or any(
                flag not in help_result.stdout for flag in required
            ):
                raise ValueError(f"Unsupported KiCad {kind} {COMMANDS[kind]} CLI options")
            if kind == "pcb":
                refill = "--refill-zones" in help_result.stdout
                if not refill and not args.zones_prefilled:
                    raise ValueError(
                        "This CLI cannot refill zones. Refill and save ALL zones in a "
                        "review copy first, then explicitly pass --zones-prefilled."
                    )
    except (OSError, ValueError, UnicodeError, subprocess.TimeoutExpired) as exc:
        return write_preflight_failure(output, version_text, checks, exc)

    exit_code = 0
    for entry, (kind, source) in zip(checks, inputs):
        report = Path(entry["report"])
        log = Path(entry["log"])
        command = command_for(cli, kind, source, report, refill)
        entry["command"] = command
        if kind == "pcb":
            entry["zones"] = "refilled_in_memory" if refill else "prefilled_by_caller"
        try:
            dependencies = dependency_snapshot(source)
            entry["dependencies"] = sorted(dependencies)
            entry["dependency_sha256"] = dependencies
            entry["input_sha256"] = dependencies[str(source)]
            result = run(command, args.timeout)
            entry["returncode"] = result.returncode
            log.write_text(result.stdout + "\nSTDERR:\n" + result.stderr, encoding="utf-8")
            if result.returncode not in (0, 5):
                raise ValueError(f"KiCad execution failed (exit {result.returncode})")
            # Do not accept exit 0 without a fresh, readable JSON report.
            data = json.loads(report.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or not data:
                raise ValueError("KiCad report is not a non-empty JSON object")
            verify_dependency_snapshot(source, dependencies)
            entry["status"] = "PASS" if result.returncode == 0 else "FAIL"
            if result.returncode == 5:
                exit_code = max(exit_code, 1)
        except (OSError, ValueError, UnicodeError, subprocess.TimeoutExpired) as exc:
            entry["error"] = str(exc)
            with log.open("a", encoding="utf-8") as handle:
                handle.write(f"ERROR: {exc}\n")
            exit_code = 2
        print(f"{entry['status']}: {source}")
        if "error" in entry:
            print(f"  {entry['error']}", file=sys.stderr)

    write_summary(output, version_text, exit_code, checks)
    print("Manual checklist still required; this is not board approval.")
    return exit_code


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--schematic", action="append", default=[], metavar="ROOT.kicad_sch")
    parser.add_argument("--pcb", action="append", default=[], metavar="BOARD.kicad_pcb")
    parser.add_argument("--kicad-cli", default="kicad-cli", help="Executable name or path")
    parser.add_argument("--output-dir", help="Parent for a fresh report directory (default: temp)")
    parser.add_argument("--timeout", type=int, default=180, help="Per-command timeout in seconds")
    parser.add_argument("--zones-prefilled", action="store_true", help=(
        "For a CLI without --refill-zones: confirm ALL zones were refilled and "
        "saved in a review copy before running. Not a skip option."
    ))
    args = parser.parse_args(argv)
    if not args.schematic and not args.pcb:
        parser.error("Specify at least one --schematic or --pcb; no inputs is NOT a pass")
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    if args.zones_prefilled and not args.pcb:
        parser.error("--zones-prefilled requires --pcb")
    try:
        return review(args)
    except (OSError, ValueError, UnicodeError, subprocess.TimeoutExpired) as exc:
        print(f"NOT_CHECKED: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
