from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class CommandResult:
    command: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def command_exists(command: str) -> bool:
    return shutil.which(command) is not None


def run(
    command: list[str],
    *,
    cwd: Path | None = None,
    check: bool = False,
    stream: bool = False,
) -> CommandResult:
    try:
        if stream:
            completed = subprocess.run(command, cwd=cwd, check=False)
            result = CommandResult(tuple(command), completed.returncode, "", "")
        else:
            completed = subprocess.run(
                command,
                cwd=cwd,
                check=False,
                capture_output=True,
                text=True,
            )
            result = CommandResult(
                tuple(command),
                completed.returncode,
                completed.stdout,
                completed.stderr,
            )
    except FileNotFoundError:
        result = CommandResult(tuple(command), 127, "", f"{command[0]} not found on PATH")

    if check and not result.ok:
        command_text = " ".join(command)
        raise RuntimeError(f"Command failed ({result.returncode}): {command_text}\n{result.stderr}")

    return result


def run_with_input(
    command: list[str],
    input_text: str,
    *,
    cwd: Path | None = None,
) -> CommandResult:
    completed = subprocess.run(
        command,
        cwd=cwd,
        input=input_text,
        check=False,
        text=True,
    )
    return CommandResult(tuple(command), completed.returncode, "", "")


def run_json(command: list[str], *, cwd: Path | None = None) -> tuple[CommandResult, Any | None]:
    result = run(command, cwd=cwd)
    if not result.ok or not result.stdout.strip():
        return result, None
    try:
        return result, json.loads(result.stdout)
    except json.JSONDecodeError:
        return result, None
