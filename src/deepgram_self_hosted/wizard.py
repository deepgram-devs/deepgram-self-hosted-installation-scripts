"""Interactive Questionary wizard that builds an EKS deployment config dict.

Returns the same shape as `default_eks_config()` so the result can be passed
straight to `_run_native_setup` (after writing to disk).
"""

from __future__ import annotations

from typing import Any

import questionary

from deepgram_self_hosted.config import default_eks_config, get_path

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
API_INSTANCES = ["c5n.xlarge", "c5.xlarge", "c5.2xlarge", "m5.xlarge", OTHER]
STT_MODEL_PROFILES = ["nova", "flux"]
FLUX_MODELS = ["flux-general-en", "flux-general-multi"]
TTS_ENGINE_INSTANCES = [
    "g6.12xlarge",   # 4x NVIDIA L4
    "g6.24xlarge",
    "g5.12xlarge",   # 4x NVIDIA A10G
    "g5.24xlarge",
    "g4dn.12xlarge",  # 4x NVIDIA T4
    "p4d.24xlarge",  # 8x NVIDIA A100
    OTHER,
]
AURA2_VARIANTS = ["en", "es", "polyglot"]


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
        "Deployment type", ["STT", "TTS"], "STT"
    )
    config["deployment"]["service_type"] = _annotated_select(
        "Service exposure type", ["ClusterIP", "LoadBalancer", "NodePort"], "ClusterIP"
    )
    is_tts = config["deployment"]["type"] == "TTS"
    if is_tts:
        _ask_tts_aura2(config)
    else:
        _ask_stt_model_profile(config)

    _node_group("Control plane", "control_plane", config, GENERAL_INSTANCES)
    if is_tts:
        # Aura-2 needs 2 GPUs per pod, so default to a multi-GPU instance.
        if config["node_groups"]["engine"].get("instance_type") in (None, "g6.2xlarge"):
            config["node_groups"]["engine"]["instance_type"] = "g6.12xlarge"
        _node_group("Engine (multi-GPU)", "engine", config, TTS_ENGINE_INSTANCES)
    else:
        _node_group("Engine (GPU)", "engine", config, ENGINE_INSTANCES)
    _node_group("API", "api", config, API_INSTANCES)

    config["license_proxy"]["enabled"] = questionary.confirm(
        "Enable License Proxy?", default=is_tts
    ).unsafe_ask()
    if config["license_proxy"]["enabled"]:
        _node_group("License Proxy", "license_proxy", config, GENERAL_INSTANCES)

    _ask_efs(config)
    _ask_models(config)
    _ask_secrets(config)

    config["actions"]["dry_run"] = questionary.confirm(
        "Dry run (write artifacts only, do not provision)?", default=False
    ).unsafe_ask()

    return config


def _ask_stt_model_profile(config: dict[str, Any]) -> None:
    profile = _annotated_select(
        "STT model profile",
        STT_MODEL_PROFILES,
        str(get_path(config, "deployment", "model_profile", default="nova")),
    )
    config["deployment"]["model_profile"] = profile
    if profile != "flux":
        return

    questionary.print(
        "Note: run Flux on a dedicated cluster with no other STT models.",
        style="italic",
    )
    config["deployment"].setdefault("flux", {})
    config["deployment"]["flux"]["model_name"] = _annotated_select(
        "Flux model",
        FLUX_MODELS,
        str(get_path(config, "deployment", "flux", "model_name", default="flux-general-en")),
    )

    def _validate_optional_int(value: str) -> bool | str:
        text = value.strip()
        if not text:
            return True
        try:
            int(text)
        except ValueError:
            return "Enter an integer or leave blank."
        return True

    raw = questionary.text(
        "Max concurrent streams (leave blank if non-production)",
        validate=_validate_optional_int,
    ).unsafe_ask().strip()
    config["deployment"]["flux"]["max_streams"] = int(raw) if raw else None


def _ask_tts_aura2(config: dict[str, Any]) -> None:
    questionary.print(
        "Note: Aura-2 needs 2 GPUs per engine pod. The wizard will default the engine "
        "node group to a multi-GPU instance type.",
        style="italic",
    )
    config["deployment"].setdefault("tts", {})
    config["deployment"]["tts"]["variant"] = _annotated_select(
        "Aura-2 language variant",
        AURA2_VARIANTS,
        str(get_path(config, "deployment", "tts", "variant", default="en")),
    )

    def _validate_positive_int(value: str) -> bool | str:
        text = value.strip()
        if not text:
            return "Required."
        try:
            return True if int(text) > 0 else "Must be positive."
        except ValueError:
            return "Enter a positive integer."

    raw = questionary.text(
        "Aura-2 max batch size",
        default=str(get_path(config, "deployment", "tts", "max_batch_size", default=8)),
        validate=_validate_positive_int,
    ).unsafe_ask().strip()
    config["deployment"]["tts"]["max_batch_size"] = int(raw)


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


EDITABLE_FIELDS: list[tuple[str, str]] = [
    ("cluster.name", "Cluster name"),
    ("cluster.region", "Region"),
    ("cluster.kubernetes_version", "Kubernetes version"),
    ("deployment.service_type", "Service type"),
    ("deployment.model_profile", "STT model profile"),
    ("deployment.tts.variant", "Aura-2 language variant"),
    ("deployment.tts.max_batch_size", "Aura-2 max batch size"),
    ("node_groups.control_plane.instance_type", "Control plane instance type"),
    ("node_groups.control_plane.desired", "Control plane desired count"),
    ("node_groups.engine.instance_type", "Engine instance type"),
    ("node_groups.engine.desired", "Engine desired count"),
    ("node_groups.api.instance_type", "API instance type"),
    ("node_groups.api.desired", "API desired count"),
    ("license_proxy.enabled", "License Proxy enabled"),
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
        choices = {
            "engine": ENGINE_INSTANCES,
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
    elif leaf == "model_profile":
        parent[leaf] = _annotated_select(label, STT_MODEL_PROFILES, str(current or "nova"))
    elif leaf == "variant":
        parent[leaf] = _annotated_select(label, AURA2_VARIANTS, str(current or "en"))
    elif leaf == "max_batch_size":
        parent[leaf] = _int_prompt(label, int(current) if current is not None else 8)
    elif leaf in {"enabled", "dry_run"}:
        parent[leaf] = questionary.confirm(label, default=bool(current)).unsafe_ask()
    elif leaf == "desired":
        parent[leaf] = _int_prompt(label, int(current) if current is not None else 1)
    else:
        parent[leaf] = questionary.text(label, default=str(current or "")).unsafe_ask().strip()
