from __future__ import annotations

import time
from dataclasses import dataclass

from deepgram_self_hosted.runner import run, run_json


@dataclass(frozen=True)
class EksNetworking:
    vpc_id: str
    subnet_ids: list[str]
    cluster_security_group_id: str


def get_eks_networking(cluster_name: str, region: str) -> EksNetworking:
    result, data = run_json(
        ["aws", "eks", "describe-cluster", "--name", cluster_name, "--region", region]
    )
    if not result.ok or not data:
        raise RuntimeError(f"Could not describe EKS cluster {cluster_name}: {_error(result)}")

    resources = data["cluster"]["resourcesVpcConfig"]
    return EksNetworking(
        vpc_id=resources["vpcId"],
        subnet_ids=resources["subnetIds"],
        cluster_security_group_id=resources["clusterSecurityGroupId"],
    )


def get_role_arn(role_name: str) -> str:
    result, data = run_json(["aws", "iam", "get-role", "--role-name", role_name])
    if not result.ok or not data:
        raise RuntimeError(f"Could not describe IAM role {role_name}: {_error(result)}")
    return data["Role"]["Arn"]


def describe_subnet_az(subnet_id: str, region: str) -> str:
    result, data = run_json(
        ["aws", "ec2", "describe-subnets", "--region", region, "--subnet-ids", subnet_id]
    )
    if not result.ok or not data:
        raise RuntimeError(f"Could not describe subnet {subnet_id}: {_error(result)}")
    return data["Subnets"][0]["AvailabilityZone"]


def ensure_efs_file_system(cluster_name: str, region: str, mode: str, efs_id: str | None) -> str:
    if mode == "existing":
        if not efs_id:
            raise ValueError("efs.file_system_id is required when efs.mode is existing")
        wait_for_efs_file_system(efs_id, region)
        return efs_id

    creation_token = f"{cluster_name}-resources"
    existing = find_efs_by_creation_token(creation_token, region)
    if existing:
        wait_for_efs_file_system(existing, region)
        return existing

    result, data = run_json(
        [
            "aws",
            "efs",
            "create-file-system",
            "--performance-mode",
            "generalPurpose",
            "--throughput-mode",
            "bursting",
            "--encrypted",
            "--creation-token",
            creation_token,
            "--tags",
            f"Key=Name,Value={creation_token}",
            f"Key=associated-cluster-name,Value={cluster_name}",
            "--region",
            region,
        ]
    )
    if not result.ok or not data:
        raise RuntimeError(f"Could not create EFS filesystem: {_error(result)}")

    created = data["FileSystemId"]
    wait_for_efs_file_system(created, region)
    return created


def find_efs_by_creation_token(creation_token: str, region: str) -> str | None:
    result, data = run_json(
        [
            "aws",
            "efs",
            "describe-file-systems",
            "--creation-token",
            creation_token,
            "--region",
            region,
        ]
    )
    if not result.ok or not data:
        return None
    file_systems = data.get("FileSystems", [])
    if not file_systems:
        return None
    return file_systems[0].get("FileSystemId")


def wait_for_efs_file_system(efs_id: str, region: str) -> None:
    state = "unknown"
    for _ in range(60):
        result, data = run_json(
            [
                "aws",
                "efs",
                "describe-file-systems",
                "--file-system-id",
                efs_id,
                "--region",
                region,
            ]
        )
        if result.ok and data and data.get("FileSystems"):
            state = data["FileSystems"][0].get("LifeCycleState", "unknown")
            if state == "available":
                return
        time.sleep(5)
    raise TimeoutError(f"Timed out waiting for EFS filesystem {efs_id}. Last state: {state}")


def ensure_nfs_ingress(security_group_id: str, region: str) -> None:
    result = run(
        [
            "aws",
            "ec2",
            "authorize-security-group-ingress",
            "--region",
            region,
            "--group-id",
            security_group_id,
            "--protocol",
            "tcp",
            "--port",
            "2049",
            "--source-group",
            security_group_id,
        ]
    )
    if result.ok:
        return
    if "InvalidPermission.Duplicate" in result.stderr:
        return
    raise RuntimeError(f"Could not authorize NFS ingress: {_error(result)}")


def ensure_mount_targets(
    efs_id: str,
    subnet_ids: list[str],
    security_group_id: str,
    region: str,
) -> list[str]:
    created: list[str] = []
    existing_by_az = mount_target_azs(efs_id, region)

    for subnet_id in subnet_ids:
        az = describe_subnet_az(subnet_id, region)
        if az in existing_by_az:
            continue

        result, data = run_json(
            [
                "aws",
                "efs",
                "create-mount-target",
                "--file-system-id",
                efs_id,
                "--subnet-id",
                subnet_id,
                "--security-groups",
                security_group_id,
                "--region",
                region,
            ]
        )
        if not result.ok or not data:
            raise RuntimeError(
                f"Could not create EFS mount target in {subnet_id}: {_error(result)}"
            )
        mount_target_id = data["MountTargetId"]
        created.append(mount_target_id)
        existing_by_az.add(az)
        wait_for_mount_target(mount_target_id, region)

    return created


def mount_target_azs(efs_id: str, region: str) -> set[str]:
    result, data = run_json(
        ["aws", "efs", "describe-mount-targets", "--file-system-id", efs_id, "--region", region]
    )
    if not result.ok or not data:
        return set()
    return {
        target["AvailabilityZoneName"]
        for target in data.get("MountTargets", [])
        if target.get("AvailabilityZoneName")
    }


def wait_for_mount_target(mount_target_id: str, region: str) -> None:
    state = "unknown"
    for _ in range(60):
        result, data = run_json(
            [
                "aws",
                "efs",
                "describe-mount-targets",
                "--mount-target-id",
                mount_target_id,
                "--region",
                region,
            ]
        )
        if result.ok and data and data.get("MountTargets"):
            state = data["MountTargets"][0].get("LifeCycleState", "unknown")
            if state == "available":
                return
        time.sleep(5)
    raise TimeoutError(
        f"Timed out waiting for EFS mount target {mount_target_id}. Last state: {state}"
    )


def _error(result) -> str:
    return (result.stderr or result.stdout or f"exit code {result.returncode}").strip()
