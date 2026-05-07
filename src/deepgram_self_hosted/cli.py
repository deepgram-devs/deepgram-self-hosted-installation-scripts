from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from deepgram_self_hosted import __version__
from deepgram_self_hosted.config import (
    clone_eks_config,
    default_eks_config,
    load_config,
    write_config,
)
from deepgram_self_hosted.providers import docker_aws, kubernetes_aws, kubernetes_aws_workflow

console = Console()
app = typer.Typer(
    help="Deepgram self-hosted setup tooling.",
    no_args_is_help=True,
    invoke_without_command=True,
)

setup_app = typer.Typer(help="Run setup workflows.", no_args_is_help=True)
setup_kubernetes_app = typer.Typer(help="Run Kubernetes setup workflows.", no_args_is_help=True)
setup_docker_app = typer.Typer(help="Run Docker setup workflows.", no_args_is_help=True)

plan_app = typer.Typer(help="Render deployment plans and artifacts.", no_args_is_help=True)
plan_kubernetes_app = typer.Typer(help="Render Kubernetes deployment plans.", no_args_is_help=True)

prepare_app = typer.Typer(help="Prepare deployment dependencies.", no_args_is_help=True)
prepare_efs_app = typer.Typer(help="Prepare EFS storage dependencies.", no_args_is_help=True)
prepare_efs_kubernetes_app = typer.Typer(
    help="Prepare Kubernetes EFS dependencies.",
    no_args_is_help=True,
)
repair_app = typer.Typer(help="Deprecated aliases for prepare commands.", no_args_is_help=True)
repair_efs_app = typer.Typer(help="Deprecated EFS aliases.", no_args_is_help=True)
repair_efs_kubernetes_app = typer.Typer(
    help="Deprecated Kubernetes EFS aliases.",
    no_args_is_help=True,
)

status_app = typer.Typer(help="Show deployment status.", no_args_is_help=True)
status_kubernetes_app = typer.Typer(help="Show Kubernetes deployment status.", no_args_is_help=True)

config_app = typer.Typer(help="Create and clone reusable deployment configs.", no_args_is_help=True)
config_init_app = typer.Typer(help="Create reusable deployment configs.", no_args_is_help=True)
config_init_kubernetes_app = typer.Typer(
    help="Create reusable Kubernetes deployment configs.",
    no_args_is_help=True,
)


@app.callback()
def root(
    version: Annotated[
        bool,
        typer.Option("--version", help="Show version and exit.", is_eager=True),
    ] = False,
) -> None:
    if version:
        console.print(f"dg-self-hosted {__version__}")
        raise typer.Exit()


@setup_kubernetes_app.command("aws")
def setup_kubernetes_aws(
    config: Annotated[
        Path | None,
        typer.Option(
            "--config",
            "-c",
            help="Reusable YAML config that drives a native Python EKS setup. "
            "Without --config, the interactive shell script is run instead.",
        ),
    ] = None,
) -> None:
    """Run the AWS EKS setup workflow."""
    try:
        kubernetes_aws.setup(console, config_path=config)
    except (RuntimeError, ValueError, FileNotFoundError) as error:
        console.print(f"[red]EKS setup failed:[/red] {error}")
        raise typer.Exit(1) from error


@setup_docker_app.command("aws")
def setup_docker_aws(
    skip_ec2_provision: Annotated[
        bool,
        typer.Option("--skip-ec2-provision", help="Run directly on an existing EC2 host."),
    ] = False,
) -> None:
    """Run the existing interactive AWS Docker setup script."""
    docker_aws.setup(console, skip_ec2_provision=skip_ec2_provision)


@plan_kubernetes_app.command("aws")
def plan_kubernetes_aws(
    config: Annotated[
        Path,
        typer.Option(
            "--config",
            "-c",
            help="Reusable YAML config to render.",
        ),
    ],
    output_dir: Annotated[
        Path,
        typer.Option("--output-dir", "-o", help="Directory for rendered artifacts."),
    ] = Path("kubernetes/aws/artifacts"),
    resolve_aws: Annotated[
        bool,
        typer.Option(
            "--resolve-aws/--offline",
            help="Resolve AWS-discovered values such as IAM role ARNs.",
        ),
    ] = False,
) -> None:
    """Render EKS cluster config and Helm values from Python without applying them."""
    try:
        cluster_config, values = kubernetes_aws_workflow.plan_from_config(
            config,
            output_dir,
            resolve_aws=resolve_aws,
        )
    except (RuntimeError, ValueError) as error:
        console.print(f"[red]Could not render Kubernetes/AWS plan:[/red] {error}")
        raise typer.Exit(1) from error
    console.print(f"Wrote cluster config to [bold]{cluster_config}[/bold]")
    console.print(f"Wrote Helm values to [bold]{values}[/bold]")


def _prepare_efs_kubernetes_aws(config: Path, write_config_file: bool) -> None:
    efs_id = kubernetes_aws_workflow.ensure_efs_from_config(
        config,
        write_back=write_config_file,
        console=console,
    )
    console.print(f"EFS is ready: [bold]{efs_id}[/bold]")


@prepare_efs_kubernetes_app.command("aws")
def prepare_efs_kubernetes_aws(
    config: Annotated[
        Path,
        typer.Option(
            "--config",
            "-c",
            help="Reusable YAML config with cluster and EFS settings.",
        ),
    ],
    write_config_file: Annotated[
        bool,
        typer.Option(
            "--write-config/--no-write-config",
            help="Write the discovered or created EFS ID back to the config.",
        ),
    ] = True,
) -> None:
    """Ensure EFS, NFS ingress, and mount targets are ready for AWS EKS."""
    _prepare_efs_kubernetes_aws(config, write_config_file)


@repair_efs_kubernetes_app.command("aws", hidden=True)
def repair_efs_kubernetes_aws(
    config: Annotated[
        Path,
        typer.Option(
            "--config",
            "-c",
            help="Reusable YAML config with cluster and EFS settings.",
        ),
    ],
    write_config_file: Annotated[
        bool,
        typer.Option(
            "--write-config/--no-write-config",
            help="Write the discovered or created EFS ID back to the config.",
        ),
    ] = True,
) -> None:
    """Deprecated alias for prepare efs kubernetes aws."""
    console.print(
        "[yellow]repair efs kubernetes aws is deprecated; "
        "use prepare efs kubernetes aws instead.[/yellow]"
    )
    _prepare_efs_kubernetes_aws(config, write_config_file)


@status_kubernetes_app.command("aws")
def status_kubernetes_aws(
    namespace: Annotated[
        str,
        typer.Option("--namespace", help="Kubernetes namespace."),
    ] = "dg-self-hosted",
    release: Annotated[str, typer.Option("--release", help="Helm release name.")] = "deepgram",
) -> None:
    """Show pod, service, and Helm status for AWS EKS deployments."""
    kubernetes_aws.status(console, namespace=namespace, release=release)


@config_init_kubernetes_app.command("aws")
def config_init_aws(
    output: Annotated[
        Path,
        typer.Option("--output", "-o", help="Path for the generated YAML config."),
    ] = Path("kubernetes/aws/deepgram.kubernetes.aws.yaml"),
    cluster_name: Annotated[
        str,
        typer.Option("--cluster-name", help="Default EKS cluster name."),
    ] = "deepgram-self-hosted-cluster",
    region: Annotated[
        str,
        typer.Option("--region", help="Default AWS region."),
    ] = "us-west-2",
) -> None:
    """Write a reusable Kubernetes/AWS deployment config."""
    config = default_eks_config()
    config["cluster"]["name"] = cluster_name
    config["cluster"]["region"] = region
    write_config(output, config)
    console.print(f"Wrote config to [bold]{output}[/bold]")


@config_app.command("clone")
def config_clone(
    source: Annotated[
        Path,
        typer.Option("--from", help="Existing config to clone."),
    ],
    output: Annotated[
        Path,
        typer.Option("--output", "-o", help="Path for the cloned config."),
    ],
    cluster_name: Annotated[
        str | None,
        typer.Option("--cluster-name", help="Cluster name for the cloned config."),
    ] = None,
    region: Annotated[
        str | None,
        typer.Option("--region", help="AWS region for the cloned config."),
    ] = None,
    efs_mode: Annotated[
        str | None,
        typer.Option("--efs-mode", help="Set cloned efs.mode, usually create or existing."),
    ] = "create",
) -> None:
    """Clone a reusable config, overriding values that usually change per deployment."""
    config = clone_eks_config(
        load_config(source),
        cluster_name=cluster_name,
        region=region,
        output_efs_mode=efs_mode,
    )
    write_config(output, config)
    console.print(f"Wrote cloned config to [bold]{output}[/bold]")


setup_app.add_typer(setup_kubernetes_app, name="kubernetes")
setup_app.add_typer(setup_docker_app, name="docker")
plan_app.add_typer(plan_kubernetes_app, name="kubernetes")
prepare_efs_app.add_typer(prepare_efs_kubernetes_app, name="kubernetes")
prepare_app.add_typer(prepare_efs_app, name="efs")
repair_efs_app.add_typer(repair_efs_kubernetes_app, name="kubernetes")
repair_app.add_typer(repair_efs_app, name="efs")
status_app.add_typer(status_kubernetes_app, name="kubernetes")
config_init_app.add_typer(config_init_kubernetes_app, name="kubernetes")

app.add_typer(setup_app, name="setup")
app.add_typer(plan_app, name="plan")
app.add_typer(prepare_app, name="prepare")
app.add_typer(repair_app, name="repair")
app.add_typer(status_app, name="status")
config_app.add_typer(config_init_app, name="init")
app.add_typer(config_app, name="config")
