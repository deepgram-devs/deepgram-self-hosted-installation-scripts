from __future__ import annotations

from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = Path(__file__).resolve().parents[2]

DOCKER_AWS_DIR = REPO_ROOT / "docker" / "aws"
KUBERNETES_AWS_DIR = REPO_ROOT / "kubernetes" / "aws"
KUBERNETES_AWS_ARTIFACTS_DIR = KUBERNETES_AWS_DIR / "artifacts"

DOCKER_AWS_SCRIPT = DOCKER_AWS_DIR / "deepgram-aws-docker-setup.sh"
