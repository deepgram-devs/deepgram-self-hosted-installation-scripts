from __future__ import annotations

import os
from copy import deepcopy
from pathlib import Path
from typing import Any

import yaml

SECRET_KEYS: tuple[str, ...] = ("registry_username", "registry_password", "api_key")

# Known LLM providers supported by the Helm chart's `global.thirdPartyCredentials`.
# Key is the canonical provider id used in our config; value is the Helm-values
# field name under `global.thirdPartyCredentials`.
LLM_PROVIDER_SECRET_REF_FIELDS: dict[str, str] = {
    "openai": "openAiSecretRef",
    "anthropic": "anthropicSecretRef",
    "groq": "groqSecretRef",
    "elevenlabs": "elevenLabsSecretRef",
    "cartesia": "cartesiaSecretRef",
    "xai": "xaiSecretRef",
    "google": "googleSecretRef",
}

# Voice Agent engine "replicas" entries (Helm `scaling.replicas.engine` keys).
VOICE_AGENT_ENGINE_REPLICA_KEYS: tuple[str, ...] = (
    "agent-speech-to-text",
    "agent-text-to-speech",
    "agent-end-of-turn",
)

# Aura-2 UUIDs vendored from the upstream chart sample:
# https://github.com/deepgram/self-hosted-resources/blob/main/charts/deepgram-self-hosted/samples/05-voice-agent-aws.values.yaml
# If the chart updates these, fetch the latest values from the sample, or extract
# from a running pod with:
#   kubectl logs -l engine-type=agent-text-to-speech -n <namespace> | head -100
DEFAULT_AURA2_UUIDS: dict[str, dict[str, str]] = {
    "english": {
        "t2cUuid": "0ec06c9b-0aa0-44d0-a001-3ec57d32229e",
        "c2aUuid": "2e5096c7-7bf1-435e-bbdd-f673f88d0ebd",
        "cudaVisibleDevices": "0,1",
    },
    "spanish": {
        "t2cUuid": "c053c7a8-7317-4de8-8a50-7e01c54e7ba9",
        "c2aUuid": "04355c1e-8148-478d-9f6c-6a6c54ec3591",
        "cudaVisibleDevices": "2,3",
    },
    "polyglot": {
        "t2cUuid": "04975889-c601-4f80-a02f-0f2f9c22deaf",
        "c2aUuid": "9e94567e-11e7-4619-adbc-d28212194367",
        "cudaVisibleDevices": "2,3",
    },
}


def _default_aura2_language(language: str, enabled: bool = False) -> dict[str, Any]:
    uuids = DEFAULT_AURA2_UUIDS[language]
    return {
        "enabled": enabled,
        "maxBatchSize": 8,
        "t2cUuid": uuids["t2cUuid"],
        "c2aUuid": uuids["c2aUuid"],
        "cudaVisibleDevices": uuids["cudaVisibleDevices"],
    }


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
    },
    "node_groups": {
        "control_plane": {"min": 1, "desired": 1, "max": 3, "instance_type": "t3.large"},
        "engine": {
            "min": 1,
            "desired": 1,
            "max": 8,
            "instance_type": "g6.2xlarge",
            # Voice Agent only: per-engine-pool replica counts.
            "agent_replicas": {key: 1 for key in VOICE_AGENT_ENGINE_REPLICA_KEYS},
        },
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
        # Per-provider API keys (Voice Agent only). Same redaction semantics as
        # the three top-level secret values: stripped to None before write when
        # secrets.mode == "create".
        "llm_provider_api_keys": {},
    },
    "license_proxy": {
        "enabled": False,
    },
    "cluster_autoscaler": {
        "enabled": True,
    },
    "agent": {
        "enabled": False,
    },
    "aura2": {
        "enabled": False,
        "english": _default_aura2_language("english", enabled=False),
        "spanish": _default_aura2_language("spanish", enabled=False),
        "polyglot": _default_aura2_language("polyglot", enabled=False),
    },
    # Voice Agent LLM provider secret refs. Keys are provider ids from
    # LLM_PROVIDER_SECRET_REF_FIELDS; values are K8s secret names. Empty by
    # default — populated by the wizard when type is VOICE_AGENT.
    "third_party_credentials": {},
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
    # Per-provider LLM API keys (Voice Agent) — clear values, preserve provider keys.
    llm_keys = secrets.get("llm_provider_api_keys")
    if isinstance(llm_keys, dict):
        secrets["llm_provider_api_keys"] = {provider: None for provider in llm_keys}
    return sanitized


def extract_secrets(config: dict[str, Any]) -> dict[str, Any]:
    """Return a dict of the three secret values from config (any may be None)."""
    return {key: get_path(config, "secrets", key) for key in SECRET_KEYS}


def extract_llm_provider_api_keys(config: dict[str, Any]) -> dict[str, str | None]:
    """Return a mapping of provider id -> API key value from config (any may be None)."""
    keys = get_path(config, "secrets", "llm_provider_api_keys", default={}) or {}
    if not isinstance(keys, dict):
        return {}
    return {provider: value for provider, value in keys.items()}


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
