"""Render a deployment-config summary as Rich tables."""

from __future__ import annotations

from typing import Any

from rich import box
from rich.console import Console
from rich.table import Table

from deepgram_self_hosted.config import get_path

NODE_GROUPS: list[tuple[str, str, str]] = [
    ("control_plane", "Control plane", "t3.large"),
    ("engine", "Engine (GPU)", "g6.2xlarge"),
    ("api", "API", "c5n.xlarge"),
]


def render_summary(config: dict[str, Any], console: Console) -> None:
    console.rule("[bold]Deployment summary")
    console.print(_cluster_table(config))
    console.print(_node_group_table(config))
    console.print(_other_table(config))


def _cluster_table(config: dict[str, Any]) -> Table:
    table = Table(title="Cluster", show_header=False, box=box.SIMPLE)
    table.add_column(style="bold")
    table.add_column()
    table.add_row("Name", str(get_path(config, "cluster", "name", default="")))
    table.add_row("Region", str(get_path(config, "cluster", "region", default="")))
    table.add_row(
        "Kubernetes version", str(get_path(config, "cluster", "kubernetes_version", default=""))
    )
    deployment_type = str(get_path(config, "deployment", "type", default=""))
    table.add_row("Deployment type", deployment_type)
    table.add_row("Service type", str(get_path(config, "deployment", "service_type", default="")))
    if deployment_type.upper() == "STT":
        profile = str(get_path(config, "deployment", "model_profile", default="nova")).lower()
        if profile == "flux":
            model_name = str(
                get_path(config, "deployment", "flux", "model_name", default="flux-general-en")
            )
            max_streams = get_path(config, "deployment", "flux", "max_streams")
            streams_label = f"max_streams={max_streams}" if max_streams is not None else "no limit"
            table.add_row("Model profile", f"flux ({model_name}, {streams_label})")
        else:
            table.add_row("Model profile", profile)
    return table


def _node_group_table(config: dict[str, Any]) -> Table:
    table = Table(title="Node groups", header_style="bold", box=box.SIMPLE)
    for col in ("Group", "Instance type", "Min", "Desired", "Max"):
        table.add_column(col)

    groups = list(NODE_GROUPS)
    if get_path(config, "license_proxy", "enabled", default=False):
        groups.append(("license_proxy", "License proxy", "t3.large"))

    for key, label, default_instance in groups:
        table.add_row(
            label,
            str(get_path(config, "node_groups", key, "instance_type", default=default_instance)),
            str(get_path(config, "node_groups", key, "min", default=0)),
            str(get_path(config, "node_groups", key, "desired", default=0)),
            str(get_path(config, "node_groups", key, "max", default=0)),
        )
    return table


def _other_table(config: dict[str, Any]) -> Table:
    table = Table(title="Other", show_header=False, box=box.SIMPLE)
    table.add_column(style="bold")
    table.add_column()

    efs_mode = str(get_path(config, "efs", "mode", default="create"))
    efs_id = get_path(config, "efs", "file_system_id")
    table.add_row("EFS", f"{efs_mode}" + (f" ({efs_id})" if efs_id else ""))

    table.add_row("Secrets", str(get_path(config, "secrets", "mode", default="external")))
    table.add_row(
        "License Proxy",
        "enabled" if get_path(config, "license_proxy", "enabled", default=False) else "disabled",
    )

    deployment_file = get_path(config, "models", "deployment_file")
    urls = get_path(config, "models", "urls", default=[]) or []
    if deployment_file:
        table.add_row("Models", f"from file: {deployment_file}")
    elif urls:
        table.add_row("Models", f"{len(urls)} URL(s)")
    else:
        table.add_row("Models", "use models already on EFS")

    table.add_row(
        "Dry run",
        "yes" if get_path(config, "actions", "dry_run", default=False) else "no",
    )
    return table
