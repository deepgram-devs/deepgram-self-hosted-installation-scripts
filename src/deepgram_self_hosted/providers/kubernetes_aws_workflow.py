from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml
from rich.console import Console

from deepgram_self_hosted.config import get_path, load_config, write_config
from deepgram_self_hosted.providers import aws_cli

CLUSTER_CONFIG_FILENAME = "cluster-config.yaml"
HELM_VALUES_FILENAME = "helm-values.yaml"


def plan_from_config(
    config_path: Path,
    output_dir: Path,
    *,
    resolve_aws: bool = False,
) -> tuple[Path, Path]:
    config = load_config(config_path)
    output_dir.mkdir(parents=True, exist_ok=True)

    cluster_config_path = output_dir / CLUSTER_CONFIG_FILENAME
    values_path = output_dir / HELM_VALUES_FILENAME

    cluster_config_path.write_text(render_cluster_config(config))
    values_path.write_text(render_values(config, resolve_aws=resolve_aws))

    return cluster_config_path, values_path


def ensure_efs_from_config(
    config_path: Path,
    *,
    write_back: bool,
    console: Console,
) -> str:
    config = load_config(config_path)
    cluster_name = str(get_path(config, "cluster", "name", default="deepgram-self-hosted-cluster"))
    region = str(get_path(config, "cluster", "region", default="us-west-2"))
    efs_mode = str(get_path(config, "efs", "mode", default="create")).lower()
    configured_efs_id = get_path(config, "efs", "file_system_id")

    console.print(f"Discovering EKS networking for [bold]{cluster_name}[/bold] in {region}...")
    networking = aws_cli.get_eks_networking(cluster_name, region)

    console.print("Ensuring EFS filesystem exists and is available...")
    efs_id = aws_cli.ensure_efs_file_system(
        cluster_name,
        region,
        efs_mode,
        str(configured_efs_id) if configured_efs_id else None,
    )

    console.print("Ensuring NFS access from cluster nodes to EFS...")
    aws_cli.ensure_nfs_ingress(networking.cluster_security_group_id, region)

    console.print("Ensuring EFS mount targets exist in cluster availability zones...")
    created = aws_cli.ensure_mount_targets(
        efs_id,
        networking.subnet_ids,
        networking.cluster_security_group_id,
        region,
    )

    if created:
        console.print(f"Created {len(created)} mount target(s): {', '.join(created)}")
    else:
        console.print("All required mount targets already exist.")

    if write_back:
        config.setdefault("efs", {})["mode"] = "existing"
        config.setdefault("efs", {})["file_system_id"] = efs_id
        write_config(config_path, config)
        console.print(f"Updated config with EFS ID [bold]{efs_id}[/bold]: {config_path}")

    return efs_id


def render_cluster_config(config: dict[str, Any]) -> str:
    cluster_name = str(get_path(config, "cluster", "name", default="deepgram-self-hosted-cluster"))
    region = str(get_path(config, "cluster", "region", default="us-west-2"))
    version = str(get_path(config, "cluster", "kubernetes_version", default="1.33"))
    autoscaler_role = _role_name(cluster_name, "cluster-autoscaler-role")
    efs_csi_role = _role_name(cluster_name, "efs-csi-driver-role")

    node_groups = [
        {
            "name": "control-plane-node-group",
            "minSize": _node_size(config, "control_plane", "min", 1),
            "desiredCapacity": _node_size(config, "control_plane", "desired", 1),
            "maxSize": _node_size(config, "control_plane", "max", 3),
            "instanceType": _instance_type(config, "control_plane", "t3.large"),
            "amiFamily": "AmazonLinux2023",
            "iam": {"withAddonPolicies": {"autoScaler": True}},
            "propagateASGTags": True,
        },
        {
            "name": "engine-node-group",
            "minSize": _node_size(config, "engine", "min", 1),
            "desiredCapacity": _node_size(config, "engine", "desired", 1),
            "maxSize": _node_size(config, "engine", "max", 8),
            "instanceType": _instance_type(config, "engine", "g6.2xlarge"),
            "amiFamily": "AmazonLinux2023",
            "labels": {
                "k8s.deepgram.com/node-type": "engine",
                "k8s.amazonaws.com/accelerator": "nvidia-l4",
            },
            "iam": {"withAddonPolicies": {"efs": True, "autoScaler": True}},
            "taints": [
                {
                    "key": "efs.csi.aws.com/agent-not-ready",
                    "value": "true",
                    "effect": "NoExecute",
                }
            ],
            "propagateASGTags": True,
        },
        {
            "name": "api-node-group",
            "minSize": _node_size(config, "api", "min", 1),
            "desiredCapacity": _node_size(config, "api", "desired", 1),
            "maxSize": _node_size(config, "api", "max", 2),
            "instanceType": _instance_type(config, "api", "c5n.xlarge"),
            "amiFamily": "AmazonLinux2023",
            "labels": {"k8s.deepgram.com/node-type": "api"},
            "iam": {"withAddonPolicies": {"autoScaler": True}},
            "propagateASGTags": True,
        },
    ]

    if get_path(config, "license_proxy", "enabled", default=False):
        node_groups.append(
            {
                "name": "license-proxy-node-group",
                "minSize": _node_size(config, "license_proxy", "min", 0),
                "desiredCapacity": _node_size(config, "license_proxy", "desired", 0),
                "maxSize": _node_size(config, "license_proxy", "max", 2),
                "instanceType": _instance_type(config, "license_proxy", "t3.large"),
                "amiFamily": "AmazonLinux2023",
                "labels": {"k8s.deepgram.com/node-type": "license-proxy"},
                "iam": {"withAddonPolicies": {"autoScaler": True}},
                "propagateASGTags": True,
            }
        )

    document = {
        "apiVersion": "eksctl.io/v1alpha5",
        "kind": "ClusterConfig",
        "metadata": {
            "name": cluster_name,
            "region": region,
            "version": version,
        },
        "iam": {
            "withOIDC": True,
            "serviceAccounts": [
                {
                    "metadata": {
                        "name": "cluster-autoscaler-sa",
                        "namespace": "dg-self-hosted",
                    },
                    "wellKnownPolicies": {"autoScaler": True},
                    "roleName": autoscaler_role,
                    "roleOnly": True,
                },
                {
                    "metadata": {
                        "name": "efs-csi-controller-sa",
                        "namespace": "kube-system",
                    },
                    "wellKnownPolicies": {"efsCSIController": True},
                    "roleName": efs_csi_role,
                    "roleOnly": True,
                },
            ],
        },
        "managedNodeGroups": node_groups,
    }
    return yaml.safe_dump(document, sort_keys=False)


def render_values(
    config: dict[str, Any],
    *,
    cluster_autoscaler_role_arn: str | None = None,
    resolve_aws: bool = False,
) -> str:
    efs_id = get_path(config, "efs", "file_system_id")
    models = _model_urls(config)
    cluster_name = str(get_path(config, "cluster", "name", default="deepgram-self-hosted-cluster"))
    region = str(get_path(config, "cluster", "region", default="us-west-2"))
    service_type = str(get_path(config, "deployment", "service_type", default="ClusterIP"))
    autoscaler_role_name = _role_name(cluster_name, "cluster-autoscaler-role")
    autoscaler_role_arn = _autoscaler_role_arn(
        autoscaler_role_name,
        cluster_autoscaler_role_arn,
        resolve_aws,
    )
    if resolve_aws and not efs_id:
        raise ValueError(
            "efs.file_system_id is required when rendering with --resolve-aws. "
            "Run prepare efs kubernetes aws first."
        )

    document = {
        "global": {
            "pullSecretRef": "dg-regcred",
            "deepgramSecretRef": "dg-self-hosted-api-key",
        },
        "scaling": {
            "replicas": {
                "api": _node_size(config, "api", "desired", 1),
                "engine": _node_size(config, "engine", "desired", 1),
            },
            "auto": {"enabled": False},
        },
        "agent": {"enabled": False},
        "api": {
            "affinity": _node_affinity("api"),
            "resources": {
                "requests": {"memory": "4Gi", "cpu": "2000m"},
                "limits": {"memory": "8Gi", "cpu": "4000m"},
            },
            "service": {"type": service_type},
        },
        "engine": {
            "affinity": _node_affinity("engine"),
            "resources": {
                "requests": {"memory": "28Gi", "cpu": "6000m", "gpu": 1},
                "limits": {"memory": "40Gi", "cpu": "8000m", "gpu": 1},
            },
            "concurrencyLimit": {"activeRequests": None},
            "modelManager": {
                "volumes": {
                    "aws": {
                        "efs": {
                            "enabled": True,
                            "fileSystemId": efs_id,
                            "namePrefix": "dg-models",
                        }
                    }
                },
                "models": {"add": models, "remove": []},
            },
        },
        "licenseProxy": {
            "enabled": bool(get_path(config, "license_proxy", "enabled", default=False)),
            "affinity": _node_affinity("license-proxy"),
            "resources": {
                "requests": {"memory": "6Gi", "cpu": "1500m"},
                "limits": {"memory": "8Gi", "cpu": "2000m"},
            },
            "service": {"type": service_type},
        },
        "cluster-autoscaler": {
            "enabled": bool(
                get_path(config, "cluster_autoscaler", "enabled", default=True)
            ),
            "rbac": {
                "serviceAccount": {
                    "name": "cluster-autoscaler-sa",
                    "annotations": {"eks.amazonaws.com/role-arn": autoscaler_role_arn},
                }
            },
            "autoDiscovery": {"clusterName": cluster_name},
            "awsRegion": region,
        },
        # The AL2023-NVIDIA EKS AMI variant (auto-selected by eksctl for GPU instance
        # types) ships with NVIDIA drivers and the container toolkit pre-installed,
        # so we ask the GPU Operator not to install its own copies. Enable both if
        # you switch to a non-NVIDIA AMI such as plain AL2023 or Ubuntu.
        "gpu-operator": {
            "enabled": True,
            "driver": {"enabled": False},
            "toolkit": {"enabled": False},
        },
    }
    return yaml.safe_dump(document, sort_keys=False)


def _model_urls(config: dict[str, Any]) -> list[str]:
    """Return model URLs from config. Empty list is valid when reusing an EFS volume
    that already has models loaded."""
    urls = get_path(config, "models", "urls", default=[])
    if urls:
        return [str(url) for url in urls]

    deployment_file = get_path(config, "models", "deployment_file")
    if deployment_file:
        text = Path(deployment_file).read_text()
        return sorted(set(re.findall(r"https?://[^\s]+?\.dg", text)))

    return []


def _node_size(config: dict[str, Any], group: str, key: str, default: int) -> int:
    return int(get_path(config, "node_groups", group, key, default=default))


def _instance_type(config: dict[str, Any], group: str, default: str) -> str:
    return str(get_path(config, "node_groups", group, "instance_type", default=default))


def _role_name(cluster_name: str, suffix: str) -> str:
    safe_cluster_name = re.sub(r"[^A-Za-z0-9+=,.@_-]", "-", cluster_name)
    return f"{safe_cluster_name}-{suffix}"


def _autoscaler_role_arn(
    role_name: str,
    provided_role_arn: str | None,
    resolve_aws: bool,
) -> str:
    if provided_role_arn:
        return provided_role_arn
    if resolve_aws:
        return aws_cli.get_role_arn(role_name)
    return f"arn:aws:iam::<account-id>:role/{role_name}"


def _node_affinity(node_type: str) -> dict[str, Any]:
    return {
        "nodeAffinity": {
            "requiredDuringSchedulingIgnoredDuringExecution": {
                "nodeSelectorTerms": [
                    {
                        "matchExpressions": [
                            {
                                "key": "k8s.deepgram.com/node-type",
                                "operator": "In",
                                "values": [node_type],
                            }
                        ]
                    }
                ]
            }
        }
    }
