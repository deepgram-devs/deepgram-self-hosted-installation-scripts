from __future__ import annotations

import os
from copy import deepcopy
from pathlib import Path
from typing import Any

import yaml

SECRET_KEYS: tuple[str, ...] = ("registry_username", "registry_password", "api_key")

DEFAULT_EKS_CONFIG: dict[str, Any] = {
    "target": "kubernetes/aws",
    "cluster": {
        "name": "deepgram-self-hosted-cluster",
        "region": "us-west-2",
        "kubernetes_version": "1.33",
    },
    "deployment": {
        "type": "STT",
        "service_type": "ClusterIP",
        "model_profile": "nova",
        "flux": {
            "max_streams": None,
            "model_name": "flux-general-en",
        },
        "tts": {
            "variant": "en",
            "max_batch_size": 8,
        },
    },
    "node_groups": {
        "control_plane": {"min": 1, "desired": 1, "max": 3, "instance_type": "t3.large"},
        "engine": {"min": 1, "desired": 1, "max": 8, "instance_type": "g6.2xlarge"},
        "api": {"min": 1, "desired": 1, "max": 2, "instance_type": "c5n.xlarge"},
        "license_proxy": {"min": 0, "desired": 0, "max": 2, "instance_type": "t3.large"},
    },
    "models": {
        "urls": [],
        "deployment_file": None,
    },
    "efs": {
        "mode": "create",
        "file_system_id": None,
    },
    "secrets": {
        "mode": "external",
        "registry_username": None,
        "registry_password": None,
        "api_key": None,
    },
    "license_proxy": {
        "enabled": False,
    },
    "cluster_autoscaler": {
        "enabled": True,
    },
    "actions": {
        "dry_run": False,
        "expanded_eksctl_dry_run": False,
        "continue": True,
        "create_cluster": True,
        "install_helm": True,
    },
}


def default_eks_config() -> dict[str, Any]:
    return deepcopy(DEFAULT_EKS_CONFIG)


def load_config(path: Path) -> dict[str, Any]:
    with path.open() as file:
        data = yaml.safe_load(file)
    if not isinstance(data, dict):
        raise ValueError(f"Config must be a YAML mapping: {path}")
    return data


def write_config(path: Path, config: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as file:
        yaml.safe_dump(config, file, sort_keys=False)
    os.chmod(path, 0o600)


def strip_secrets(config: dict[str, Any]) -> dict[str, Any]:
    """Return a deepcopy with secret values cleared. Mode and other fields are preserved."""
    sanitized = deepcopy(config)
    secrets = sanitized.setdefault("secrets", {})
    for key in SECRET_KEYS:
        if key in secrets:
            secrets[key] = None
    return sanitized


def extract_secrets(config: dict[str, Any]) -> dict[str, Any]:
    """Return a dict of the three secret values from config (any may be None)."""
    return {key: get_path(config, "secrets", key) for key in SECRET_KEYS}


def clone_eks_config(
    source: dict[str, Any],
    *,
    cluster_name: str | None = None,
    region: str | None = None,
    output_efs_mode: str | None = None,
) -> dict[str, Any]:
    cloned = deepcopy(source)
    if cluster_name:
        cloned.setdefault("cluster", {})["name"] = cluster_name
    if region:
        cloned.setdefault("cluster", {})["region"] = region
    if output_efs_mode:
        cloned.setdefault("efs", {})["mode"] = output_efs_mode
        if output_efs_mode == "create":
            cloned.setdefault("efs", {})["file_system_id"] = None
    return cloned


def get_path(config: dict[str, Any], *path: str, default: Any = None) -> Any:
    current: Any = config
    for part in path:
        if not isinstance(current, dict) or part not in current:
            return default
        current = current[part]
    return current
