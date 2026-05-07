from __future__ import annotations

from rich.console import Console

from deepgram_self_hosted.paths import DOCKER_AWS_DIR, DOCKER_AWS_SCRIPT
from deepgram_self_hosted.runner import run


def setup(console: Console, *, skip_ec2_provision: bool = False) -> None:
    if not DOCKER_AWS_SCRIPT.exists():
        raise FileNotFoundError(f"Missing Docker AWS script: {DOCKER_AWS_SCRIPT}")

    command = [str(DOCKER_AWS_SCRIPT)]
    if skip_ec2_provision:
        command.append("--skip-ec2-provision")

    console.print(f"Running [bold]{' '.join(command)}[/bold]")
    result = run(command, cwd=DOCKER_AWS_DIR, stream=True)
    raise SystemExit(result.returncode)
