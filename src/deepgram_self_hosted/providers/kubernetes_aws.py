from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

import questionary
from rich.console import Console

from deepgram_self_hosted.config import (
    SECRET_KEYS,
    extract_secrets,
    get_path,
    load_config,
    strip_secrets,
    write_config,
)
from deepgram_self_hosted.paths import artifacts_dir_for
from deepgram_self_hosted.providers import aws_cli
from deepgram_self_hosted.providers.kubernetes_aws_workflow import (
    CLUSTER_CONFIG_FILENAME,
    HELM_VALUES_FILENAME,
    _role_name,
    ensure_efs_from_config,
    render_cluster_config,
    render_values,
)
from deepgram_self_hosted.runner import command_exists, run
from deepgram_self_hosted.summary import render_summary
from deepgram_self_hosted.wizard import edit_field, run_eks_wizard, validate_name

SESSION_FILENAME = "session.yaml"
EXPANDED_CLUSTER_CONFIG_FILENAME = "eksctl-expanded-cluster-config.yaml"

SECRET_ENV_VARS: dict[str, str] = {
    "registry_username": "DG_REGISTRY_USERNAME",
    "registry_password": "DG_REGISTRY_PASSWORD",
    "api_key": "DG_API_KEY",
}

DEFAULT_NAMESPACE = "dg-self-hosted"
DEFAULT_RELEASE = "deepgram"
HELM_CHART = "deepgram/deepgram-self-hosted"
HELM_REPO_NAME = "deepgram"
HELM_REPO_URL = "https://deepgram.github.io/self-hosted-resources"
REQUIRED_TOOLS = ("aws", "eksctl", "kubectl", "helm")


def setup(console: Console, *, config_path: Path | None = None) -> None:
    if config_path is not None:
        config = load_config(config_path)
        on_disk_path = config_path
    else:
        try:
            config = run_eks_wizard()
        except KeyboardInterrupt:
            console.print("[yellow]Cancelled.[/yellow]")
            return
        on_disk_path = None

    try:
        decision = _summary_loop(config, console)
    except KeyboardInterrupt:
        console.print("[yellow]Cancelled.[/yellow]")
        return

    if decision == "cancel":
        console.print("Cancelled.")
        return

    secrets_mode = str(get_path(config, "secrets", "mode", default="external")).lower()
    secrets_in_memory = (
        extract_secrets(config) if secrets_mode == "create" else None
    )
    config_to_save = strip_secrets(config) if secrets_in_memory else config

    if on_disk_path is None:
        artifact_dir = _prompt_for_artifact_folder(
            str(get_path(config, "cluster", "name", default="deepgram-self-hosted-cluster")),
            console,
        )
        on_disk_path = artifact_dir / SESSION_FILENAME

    on_disk_path.parent.mkdir(parents=True, exist_ok=True)
    write_config(on_disk_path, config_to_save)
    console.print(f"Wrote config to [bold]{on_disk_path}[/bold] (mode 0600)")
    if secrets_in_memory:
        console.print(
            "[yellow]Credentials kept in memory only; not written to disk.[/yellow] "
            "For non-interactive re-runs, set "
            f"{', '.join(SECRET_ENV_VARS.values())}."
        )

    if decision == "save":
        return

    _run_native_setup(on_disk_path, console, secrets_override=secrets_in_memory)


def _prompt_for_artifact_folder(default_name: str, console: Console) -> Path:
    """Ask for an artifact-folder name, looping until the user accepts or picks a fresh
    folder. Defaults to `<artifacts-root>/<cluster-name>`.

    If the resolved folder already exists with files in it, ask for confirmation
    before overwriting; on decline, re-prompt.
    """
    while True:
        name = questionary.text(
            "Folder name for deployment artifacts "
            "(cluster-config, helm-values, session)",
            default=default_name,
            validate=validate_name,
        ).unsafe_ask().strip()

        folder = artifacts_dir_for(name)
        if folder.exists() and any(folder.iterdir()):
            overwrite = questionary.confirm(
                f"Folder {folder} already exists. Overwrite its contents?",
                default=False,
            ).unsafe_ask()
            if not overwrite:
                console.print("[yellow]Pick a different name.[/yellow]")
                continue
        return folder


def _summary_loop(config: dict[str, Any], console: Console) -> str:
    """Show summary; let the user edit fields. Returns 'deploy', 'save', or 'cancel'."""
    while True:
        render_summary(config, console)
        is_dry_run = bool(get_path(config, "actions", "dry_run", default=False))
        primary = "Render artifacts (dry run)" if is_dry_run else "Deploy"
        action = questionary.select(
            "What next?",
            choices=[primary, "Edit a field", "Save config and exit", "Cancel"],
        ).unsafe_ask()
        if action == primary:
            return "deploy"
        if action == "Save config and exit":
            return "save"
        if action == "Cancel":
            return "cancel"
        edit_field(config)


def status(
    console: Console,
    *,
    namespace: str = DEFAULT_NAMESPACE,
    release: str = DEFAULT_RELEASE,
) -> None:
    console.rule("[bold]Kubernetes Status")
    _print_command(console, ["kubectl", "get", "pods", "-n", namespace, "-o", "wide"])
    _print_command(console, ["kubectl", "get", "svc", "-n", namespace])
    _print_command(console, ["helm", "status", release, "-n", namespace])


def _run_native_setup(
    config_path: Path,
    console: Console,
    *,
    secrets_override: dict[str, Any] | None = None,
) -> None:
    config = load_config(config_path)
    cluster_name = str(get_path(config, "cluster", "name", default="deepgram-self-hosted-cluster"))
    region = str(get_path(config, "cluster", "region", default="us-west-2"))

    _preflight(console)

    artifact_dir = config_path.parent
    artifact_dir.mkdir(parents=True, exist_ok=True)
    cluster_config_path = artifact_dir / CLUSTER_CONFIG_FILENAME
    cluster_config_path.write_text(render_cluster_config(config))
    console.print(f"Wrote cluster config to [bold]{cluster_config_path}[/bold]")

    if get_path(config, "actions", "dry_run", default=False):
        if get_path(config, "actions", "expanded_eksctl_dry_run", default=False):
            expanded = artifact_dir / EXPANDED_CLUSTER_CONFIG_FILENAME
            result = run(
                ["eksctl", "create", "cluster", "-f", str(cluster_config_path), "--dry-run"]
            )
            if not result.ok:
                raise RuntimeError(f"eksctl dry-run failed: {result.stderr.strip()}")
            expanded.write_text(result.stdout)
            console.print(f"Wrote expanded eksctl dry run to [bold]{expanded}[/bold]")
        console.print("Dry run enabled; skipping cluster creation.")
        return

    if not get_path(config, "actions", "continue", default=True):
        console.print("[yellow]actions.continue is false; aborting before provisioning.[/yellow]")
        return

    if get_path(config, "actions", "create_cluster", default=True):
        console.print("Creating EKS cluster (this may take 15-25 minutes)...")
        run(
            ["eksctl", "create", "cluster", "-f", str(cluster_config_path)],
            stream=True,
            check=True,
        )
    else:
        console.print("Skipping cluster creation (actions.create_cluster=false).")

    console.print("Updating kubeconfig...")
    run(
        ["aws", "eks", "update-kubeconfig", "--name", cluster_name, "--region", region],
        check=True,
    )

    ensure_efs_from_config(config_path, write_back=True, console=console)

    console.print("Installing aws-efs-csi-driver addon...")
    csi_role_arn = aws_cli.get_role_arn(_role_name(cluster_name, "efs-csi-driver-role"))
    run(
        [
            "eksctl", "create", "addon",
            "--cluster", cluster_name,
            "--name", "aws-efs-csi-driver",
            "--version", "latest",
            "--service-account-role-arn", csi_role_arn,
            "--region", region,
            "--force",
        ],
        stream=True,
        check=True,
    )

    console.print(f"Ensuring namespace [bold]{DEFAULT_NAMESPACE}[/bold] exists...")
    namespace_result = run(["kubectl", "create", "namespace", DEFAULT_NAMESPACE])
    if not namespace_result.ok and "AlreadyExists" not in namespace_result.stderr:
        raise RuntimeError(
            f"Could not create namespace {DEFAULT_NAMESPACE}: {namespace_result.stderr.strip()}"
        )

    secrets_mode = str(get_path(config, "secrets", "mode", default="external")).lower()
    if secrets_mode == "create":
        creds = _resolve_secrets(config, secrets_override)
        _create_secrets(creds, DEFAULT_NAMESPACE, console)
    else:
        console.print(
            f"[yellow]Skipping secret creation; ensure dg-regcred and "
            f"dg-self-hosted-api-key exist in {DEFAULT_NAMESPACE}.[/yellow]"
        )

    config = load_config(config_path)
    values_path = artifact_dir / HELM_VALUES_FILENAME
    values_path.write_text(render_values(config, resolve_aws=True))
    console.print(f"Wrote Helm values to [bold]{values_path}[/bold]")

    if get_path(config, "actions", "install_helm", default=True):
        _install_or_upgrade_helm(values_path, DEFAULT_NAMESPACE, console)
    else:
        console.print("Skipping Helm install/upgrade (actions.install_helm=false).")

    console.print(f"\nDone. Artifacts are in [bold]{artifact_dir}[/bold]")


def _preflight(console: Console) -> None:
    missing = [tool for tool in REQUIRED_TOOLS if not command_exists(tool)]
    if missing:
        raise RuntimeError(f"Missing required commands: {', '.join(missing)}")

    console.print("Checking AWS credentials...")
    result = run(["aws", "sts", "get-caller-identity"])
    if not result.ok:
        raise RuntimeError("AWS credentials not configured. Run 'aws configure' or set env vars.")


def _resolve_secrets(
    config: dict[str, Any],
    secrets_override: dict[str, Any] | None,
) -> dict[str, str | None]:
    """Pick credentials from (1) in-memory override, (2) env vars, (3) config file."""
    if secrets_override:
        return {key: secrets_override.get(key) for key in SECRET_KEYS}
    return {
        key: os.environ.get(SECRET_ENV_VARS[key]) or get_path(config, "secrets", key)
        for key in SECRET_KEYS
    }


def _create_secrets(
    creds: dict[str, str | None],
    namespace: str,
    console: Console,
) -> None:
    user = creds.get("registry_username")
    password = creds.get("registry_password")
    api_key = creds.get("api_key")
    if not (user and password and api_key):
        env_var_list = ", ".join(SECRET_ENV_VARS.values())
        raise ValueError(
            "secrets.mode is create but registry_username, registry_password, "
            f"or api_key is missing. Re-run the wizard interactively or set "
            f"{env_var_list} before deploying."
        )

    console.print("Creating registry secret dg-regcred...")
    _kubectl_create_or_replace(
        [
            "kubectl", "create", "secret", "docker-registry", "dg-regcred",
            "--docker-server=quay.io",
            f"--docker-username={user}",
            f"--docker-password={password}",
            "--namespace", namespace,
        ],
    )

    console.print("Creating API key secret dg-self-hosted-api-key...")
    _kubectl_create_or_replace(
        [
            "kubectl", "create", "secret", "generic", "dg-self-hosted-api-key",
            f"--from-literal=DEEPGRAM_API_KEY={api_key}",
            "--namespace", namespace,
        ],
    )


def _kubectl_create_or_replace(create_command: list[str]) -> None:
    rendered = subprocess.run(
        [*create_command, "--dry-run=client", "-o", "yaml"],
        capture_output=True,
        text=True,
        check=False,
    )
    if rendered.returncode != 0:
        raise RuntimeError(f"kubectl create dry-run failed: {rendered.stderr.strip()}")

    applied = subprocess.run(
        ["kubectl", "apply", "-f", "-"],
        input=rendered.stdout,
        text=True,
        check=False,
    )
    if applied.returncode != 0:
        raise RuntimeError(f"kubectl apply failed with exit {applied.returncode}")


def _install_or_upgrade_helm(values_path: Path, namespace: str, console: Console) -> None:
    console.print("Adding/updating Helm repo...")
    run(["helm", "repo", "add", HELM_REPO_NAME, HELM_REPO_URL])
    run(["helm", "repo", "update"], check=True)

    status_result = run(["helm", "status", DEFAULT_RELEASE, "-n", namespace])
    helm_action = "upgrade" if status_result.ok else "install"
    console.print(f"Running helm {helm_action}...")
    run(
        [
            "helm", helm_action, DEFAULT_RELEASE, HELM_CHART,
            "-f", str(values_path),
            "-n", namespace,
            "--atomic",
            "--timeout", "1h",
        ],
        stream=True,
        check=True,
    )


def _print_command(console: Console, command: list[str]) -> None:
    console.print(f"\n[bold]$ {' '.join(command)}[/bold]")
    result = run(command)
    if result.stdout.strip():
        console.print(result.stdout)
    if result.stderr.strip():
        console.print(f"[yellow]{result.stderr}[/yellow]")
    if not result.ok:
        console.print(f"[red]exit code {result.returncode}[/red]")
