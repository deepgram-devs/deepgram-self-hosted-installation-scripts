"""Interactive Questionary wizard that builds an EKS deployment config dict.

Returns the same shape as `default_eks_config()` so the result can be passed
straight to `_run_native_setup` (after writing to disk).
"""

from __future__ import annotations

from typing import Any

import questionary

from deepgram_self_hosted.config import (
    LLM_PROVIDER_SECRET_REF_FIELDS,
    VOICE_AGENT_ENGINE_REPLICA_KEYS,
    default_eks_config,
    get_path,
)

OTHER = "Other (enter custom)"
DEFAULT_TAG = " (default)"

REGIONS = [
    "us-west-1", "us-west-2", "us-east-1", "us-east-2",
    "eu-west-1", "eu-central-1",
    "ap-northeast-1", "ap-southeast-1",
    OTHER,
]
K8S_VERSIONS = ["1.33", "1.32", "1.31", "1.30", OTHER]
GENERAL_INSTANCES = ["t3.large", "t3.xlarge", "m5.large", "m5.xlarge", OTHER]
ENGINE_INSTANCES = [
    "g6.2xlarge",   # NVIDIA L4
    "g6.4xlarge",
    "g6.12xlarge",
    "g5.2xlarge",   # NVIDIA A10G
    "g5.4xlarge",
    "g4dn.2xlarge",  # NVIDIA T4
    "p4d.24xlarge",  # NVIDIA A100
    OTHER,
]
# Engine instance choices restricted to multi-GPU types for Voice Agent + Aura-2,
# which assigns `cudaVisibleDevices: "0,1"` and (optionally) "2,3" per language.
ENGINE_INSTANCES_MULTI_GPU = [
    "g6.12xlarge",   # 4x NVIDIA L4
    "g6.24xlarge",
    "g6.48xlarge",
    "g5.12xlarge",   # 4x NVIDIA A10G
    "g5.24xlarge",
    "g5.48xlarge",
    "p4d.24xlarge",  # 8x NVIDIA A100
    OTHER,
]
API_INSTANCES = ["c5n.xlarge", "c5.xlarge", "c5.2xlarge", "m5.xlarge", OTHER]

DEPLOYMENT_TYPES = ["STT", "TTS", "VOICE_AGENT"]


def _annotated_select(message: str, choices: list[str], default: str) -> str:
    """Render a select where the default choice is suffixed with ' (default)'."""
    if default in choices:
        display = [f"{c}{DEFAULT_TAG}" if c == default else c for c in choices]
        selected = f"{default}{DEFAULT_TAG}"
    else:
        display = list(choices)
        selected = choices[0]
    answer = questionary.select(message, choices=display, default=selected).unsafe_ask()
    return answer.removesuffix(DEFAULT_TAG)


def _select_with_other(message: str, choices: list[str], default: str) -> str:
    answer = _annotated_select(message, choices, default if default in choices else OTHER)
    if answer == OTHER:
        return questionary.text(f"{message} (custom)", default=default).unsafe_ask().strip()
    return answer


def _int_prompt(message: str, default: int) -> int:
    while True:
        raw = questionary.text(message, default=str(default)).unsafe_ask().strip()
        try:
            return int(raw)
        except ValueError:
            questionary.print("Please enter an integer.", style="fg:#ff5555")


def _required_text(message: str) -> str:
    return questionary.text(
        message,
        validate=lambda value: True if value.strip() else "Required.",
    ).unsafe_ask().strip()


def _required_password(message: str) -> str:
    return questionary.password(
        message,
        validate=lambda value: True if value else "Required.",
    ).unsafe_ask()


def _node_group(
    label: str,
    config_key: str,
    config: dict[str, Any],
    instance_choices: list[str],
) -> None:
    questionary.print(f"\n{label} node group", style="bold")
    config["node_groups"][config_key]["instance_type"] = _select_with_other(
        f"  {label} instance type",
        instance_choices,
        get_path(config, "node_groups", config_key, "instance_type", default=instance_choices[0]),
    )
    config["node_groups"][config_key]["min"] = _int_prompt(
        f"  {label} min size",
        get_path(config, "node_groups", config_key, "min", default=1),
    )
    config["node_groups"][config_key]["desired"] = _int_prompt(
        f"  {label} desired size",
        get_path(config, "node_groups", config_key, "desired", default=1),
    )
    config["node_groups"][config_key]["max"] = _int_prompt(
        f"  {label} max size",
        get_path(config, "node_groups", config_key, "max", default=3),
    )


def run_eks_wizard() -> dict[str, Any]:
    """Interactively build an EKS deployment config. Raises KeyboardInterrupt on cancel."""
    config = default_eks_config()

    config["cluster"]["name"] = questionary.text(
        "Cluster name", default=config["cluster"]["name"]
    ).unsafe_ask().strip()
    config["cluster"]["region"] = _select_with_other(
        "AWS region", REGIONS, config["cluster"]["region"]
    )
    config["cluster"]["kubernetes_version"] = _select_with_other(
        "Kubernetes version", K8S_VERSIONS, config["cluster"]["kubernetes_version"]
    )
    config["deployment"]["type"] = _annotated_select(
        "Deployment type", DEPLOYMENT_TYPES, "STT"
    )
    is_voice_agent = config["deployment"]["type"] == "VOICE_AGENT"
    if is_voice_agent:
        _apply_voice_agent_defaults(config)
        _ask_aura2(config)

    config["deployment"]["service_type"] = _annotated_select(
        "Service exposure type", ["ClusterIP", "LoadBalancer", "NodePort"], "ClusterIP"
    )

    _node_group("Control plane", "control_plane", config, GENERAL_INSTANCES)
    engine_choices = (
        ENGINE_INSTANCES_MULTI_GPU
        if is_voice_agent and _aura2_any_enabled(config)
        else ENGINE_INSTANCES
    )
    _node_group("Engine", "engine", config, engine_choices)
    if is_voice_agent:
        _ask_engine_agent_replicas(config)
    _node_group("API", "api", config, API_INSTANCES)

    config["license_proxy"]["enabled"] = questionary.confirm(
        "Enable License Proxy?", default=False
    ).unsafe_ask()
    if config["license_proxy"]["enabled"]:
        _node_group("License Proxy", "license_proxy", config, GENERAL_INSTANCES)

    _ask_efs(config)
    _ask_models(config)
    _ask_secrets(config)
    if is_voice_agent:
        _ask_third_party_credentials(config)

    config["actions"]["dry_run"] = questionary.confirm(
        "Dry run (write artifacts only, do not provision)?", default=False
    ).unsafe_ask()

    return config


def _ask_models(config: dict[str, Any]) -> None:
    using_existing_efs = (
        str(get_path(config, "efs", "mode", default="create")).lower() == "existing"
    )
    skip_choice = "Skip (use models already on EFS)"
    choices = ["Enter URLs manually", "Load from deployment .txt file"]
    if using_existing_efs:
        choices = [skip_choice, *choices]

    mode = _annotated_select("Model URLs", choices, choices[0])

    if mode == skip_choice:
        config["models"]["urls"] = []
        config["models"]["deployment_file"] = None
        return

    if mode == "Enter URLs manually":
        urls: list[str] = []
        questionary.print(
            "Enter one .dg URL per line. Submit a blank line to finish.", style="italic"
        )
        while True:
            line = questionary.text("URL").unsafe_ask().strip()
            if not line:
                if urls or using_existing_efs:
                    break
                questionary.print("At least one URL is required.", style="fg:#ff5555")
                continue
            urls.append(line)
        config["models"]["urls"] = urls
        config["models"]["deployment_file"] = None
    else:
        config["models"]["urls"] = []
        config["models"]["deployment_file"] = _required_text("Path to deployment .txt")


def _ask_efs(config: dict[str, Any]) -> None:
    mode = _annotated_select(
        "EFS storage", ["Create new EFS", "Use existing EFS"], "Create new EFS"
    )
    if mode == "Use existing EFS":
        config["efs"]["mode"] = "existing"
        config["efs"]["file_system_id"] = _required_text("EFS filesystem ID (fs-...)")
    else:
        config["efs"]["mode"] = "create"
        config["efs"]["file_system_id"] = None


def _ask_secrets(config: dict[str, Any]) -> None:
    mode = _annotated_select(
        "Kubernetes secrets",
        ["Use external secret store", "Create in-cluster secrets"],
        "Use external secret store",
    )
    if mode == "Create in-cluster secrets":
        config["secrets"]["mode"] = "create"
        config["secrets"]["registry_username"] = _required_text(
            "Self hosted distribution credentials username"
        )
        config["secrets"]["registry_password"] = _required_password(
            "Self hosted distribution credentials password"
        )
        config["secrets"]["api_key"] = _required_password("Deepgram self-hosted API key")
    else:
        config["secrets"]["mode"] = "external"


def _apply_voice_agent_defaults(config: dict[str, Any]) -> None:
    """Align in-memory defaults with the upstream voice-agent AWS chart sample.

    Mirrors `samples/05-voice-agent-aws.cluster-config.yaml`: engine 3/3/8 on
    g6.12xlarge, license-proxy 1/1/2 (only applied if the user enables it
    later). Only bumps fields still at their global default so user-customized
    values from a loaded config are preserved.
    """
    engine = config["node_groups"]["engine"]
    if int(engine.get("min", 1)) == 1:
        engine["min"] = 3
    if int(engine.get("desired", 1)) == 1:
        engine["desired"] = 3
    if str(engine.get("instance_type", "")) == "g6.2xlarge":
        engine["instance_type"] = "g6.12xlarge"

    license_proxy = config["node_groups"]["license_proxy"]
    if int(license_proxy.get("min", 0)) == 0:
        license_proxy["min"] = 1
    if int(license_proxy.get("desired", 0)) == 0:
        license_proxy["desired"] = 1

    # Keep the saved config aligned with what render_values will produce so the
    # YAML on disk reads the same as the deployed Helm values.
    config.setdefault("agent", {})["enabled"] = True
    config.setdefault("cluster_autoscaler", {})["enabled"] = False


def _ask_aura2(config: dict[str, Any]) -> None:
    """Prompt for Aura-2 TTS settings. UUIDs default to the chart sample's values.

    See config.DEFAULT_AURA2_UUIDS for the upstream source and the kubectl log
    extraction fallback.
    """
    aura2 = config.setdefault("aura2", {})
    enable_aura2 = questionary.confirm(
        "Enable Aura-2 TTS for the Voice Agent?", default=False
    ).unsafe_ask()
    aura2["enabled"] = enable_aura2
    if not enable_aura2:
        for language in ("english", "spanish", "polyglot"):
            aura2.setdefault(language, {})["enabled"] = False
        return

    for language, default_enabled in (
        ("english", True),
        ("spanish", False),
        ("polyglot", False),
    ):
        lang_cfg = aura2.setdefault(language, {})
        enabled = questionary.confirm(
            f"  Enable Aura-2 {language.capitalize()}?", default=default_enabled
        ).unsafe_ask()
        lang_cfg["enabled"] = enabled
        if not enabled:
            continue
        lang_cfg["t2cUuid"] = questionary.text(
            f"    {language.capitalize()} t2cUuid",
            default=str(lang_cfg.get("t2cUuid", "")),
        ).unsafe_ask().strip()
        lang_cfg["c2aUuid"] = questionary.text(
            f"    {language.capitalize()} c2aUuid",
            default=str(lang_cfg.get("c2aUuid", "")),
        ).unsafe_ask().strip()
        lang_cfg["cudaVisibleDevices"] = questionary.text(
            f"    {language.capitalize()} cudaVisibleDevices",
            default=str(lang_cfg.get("cudaVisibleDevices", "")),
        ).unsafe_ask().strip()


def _aura2_any_enabled(config: dict[str, Any]) -> bool:
    aura2 = get_path(config, "aura2", default={}) or {}
    if not aura2.get("enabled"):
        return False
    return any(
        bool(get_path(aura2, lang, "enabled", default=False))
        for lang in ("english", "spanish", "polyglot")
    )


def _ask_engine_agent_replicas(config: dict[str, Any]) -> None:
    """Prompt for the three Voice Agent engine replica counts."""
    questionary.print("\nVoice Agent engine replica counts", style="bold")
    agent_replicas = config["node_groups"]["engine"].setdefault(
        "agent_replicas", {key: 1 for key in VOICE_AGENT_ENGINE_REPLICA_KEYS}
    )
    for key in VOICE_AGENT_ENGINE_REPLICA_KEYS:
        agent_replicas[key] = _int_prompt(
            f"  {key} replicas",
            int(agent_replicas.get(key, 1)),
        )


def _ask_third_party_credentials(config: dict[str, Any]) -> None:
    """Collect LLM provider K8s secret refs (and API keys if mode=create).

    Mirrors the model-URL prompt: one entry per line of the form
    `provider=secret-ref`, blank line to finish. Skips entirely if the user
    submits a blank line first.
    """
    providers = sorted(LLM_PROVIDER_SECRET_REF_FIELDS.keys())
    questionary.print(
        "\nLLM provider credentials (Voice Agent)", style="bold"
    )
    questionary.print(
        "Enter one provider credential per line in the form "
        f"`provider=secret-ref` (provider one of: {', '.join(providers)}). "
        "Submit a blank line to finish.",
        style="italic",
    )

    creds: dict[str, str] = {}
    while True:
        line = questionary.text("provider=secret-ref").unsafe_ask().strip()
        if not line:
            break
        if "=" not in line:
            questionary.print(
                "Expected `provider=secret-ref`. Try again.", style="fg:#ff5555"
            )
            continue
        provider, _, secret_ref = line.partition("=")
        provider = provider.strip().lower()
        secret_ref = secret_ref.strip()
        if provider not in LLM_PROVIDER_SECRET_REF_FIELDS:
            questionary.print(
                f"Unknown provider `{provider}`. Known: {', '.join(providers)}.",
                style="fg:#ff5555",
            )
            continue
        if not secret_ref:
            questionary.print(
                "Secret ref cannot be empty. Try again.", style="fg:#ff5555"
            )
            continue
        creds[provider] = secret_ref

    config["third_party_credentials"] = creds

    if str(get_path(config, "secrets", "mode", default="external")).lower() != "create":
        return

    api_keys = config["secrets"].setdefault("llm_provider_api_keys", {})
    for provider in creds:
        api_keys[provider] = _required_password(
            f"  {provider} API key (held in memory only)"
        )


EDITABLE_FIELDS: list[tuple[str, str]] = [
    ("cluster.name", "Cluster name"),
    ("cluster.region", "Region"),
    ("cluster.kubernetes_version", "Kubernetes version"),
    ("deployment.type", "Deployment type"),
    ("deployment.service_type", "Service type"),
    ("node_groups.control_plane.instance_type", "Control plane instance type"),
    ("node_groups.control_plane.desired", "Control plane desired count"),
    ("node_groups.engine.instance_type", "Engine instance type"),
    ("node_groups.engine.desired", "Engine desired count"),
    ("node_groups.api.instance_type", "API instance type"),
    ("node_groups.api.desired", "API desired count"),
    ("license_proxy.enabled", "License Proxy enabled"),
    ("aura2.enabled", "Aura-2 enabled (Voice Agent)"),
    ("aura2.english.enabled", "Aura-2 English enabled"),
    ("aura2.spanish.enabled", "Aura-2 Spanish enabled"),
    ("aura2.polyglot.enabled", "Aura-2 Polyglot enabled"),
    ("actions.dry_run", "Dry run"),
]


def edit_field(config: dict[str, Any]) -> None:
    """Prompt the user to pick one editable field and re-prompt for its value."""
    label_to_path = {label: path for path, label in EDITABLE_FIELDS}
    label = questionary.select("Edit which field?", choices=list(label_to_path)).unsafe_ask()
    path = label_to_path[label].split(".")

    parent = config
    for part in path[:-1]:
        parent = parent.setdefault(part, {})
    leaf = path[-1]

    current = parent.get(leaf)

    if leaf == "instance_type":
        group = path[1]
        is_voice_agent = (
            str(get_path(config, "deployment", "type", default="STT")).upper()
            == "VOICE_AGENT"
        )
        engine_choices = (
            ENGINE_INSTANCES_MULTI_GPU
            if is_voice_agent and _aura2_any_enabled(config)
            else ENGINE_INSTANCES
        )
        choices = {
            "engine": engine_choices,
            "api": API_INSTANCES,
        }.get(group, GENERAL_INSTANCES)
        parent[leaf] = _select_with_other(label, choices, str(current or choices[0]))
    elif leaf == "region":
        parent[leaf] = _select_with_other(label, REGIONS, str(current or "us-west-2"))
    elif leaf == "kubernetes_version":
        parent[leaf] = _select_with_other(label, K8S_VERSIONS, str(current or "1.33"))
    elif leaf == "service_type":
        parent[leaf] = _annotated_select(
            label, ["ClusterIP", "LoadBalancer", "NodePort"], str(current or "ClusterIP")
        )
    elif path == ["deployment", "type"]:
        parent[leaf] = _annotated_select(
            label, DEPLOYMENT_TYPES, str(current or "STT")
        )
    elif leaf in {"enabled", "dry_run"}:
        parent[leaf] = questionary.confirm(label, default=bool(current)).unsafe_ask()
    elif leaf == "desired":
        parent[leaf] = _int_prompt(label, int(current) if current is not None else 1)
    else:
        parent[leaf] = questionary.text(label, default=str(current or "")).unsafe_ask().strip()
