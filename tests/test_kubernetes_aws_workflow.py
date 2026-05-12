from __future__ import annotations

import pytest
import yaml

from deepgram_self_hosted.config import (
    default_eks_config,
    extract_secrets,
    strip_secrets,
    write_config,
)
from deepgram_self_hosted.providers import aws_cli, kubernetes_aws, kubernetes_aws_workflow
from deepgram_self_hosted.providers.kubernetes_aws_workflow import (
    ensure_efs_from_config,
    plan_from_config,
    render_cluster_config,
    render_values,
)
from deepgram_self_hosted.runner import run


class _QuietConsole:
    def print(self, *args, **kwargs) -> None:
        return None

    def rule(self, *args, **kwargs) -> None:
        return None


def test_cluster_config_omits_license_proxy_node_group_when_disabled() -> None:
    config = default_eks_config()
    config["cluster"]["name"] = "test-cluster"

    rendered = yaml.safe_load(render_cluster_config(config))

    node_group_names = [group["name"] for group in rendered["managedNodeGroups"]]
    assert "license-proxy-node-group" not in node_group_names
    assert rendered["iam"]["serviceAccounts"][0]["roleName"] == (
        "test-cluster-cluster-autoscaler-role"
    )
    assert rendered["iam"]["serviceAccounts"][1]["roleName"] == (
        "test-cluster-efs-csi-driver-role"
    )


def test_cluster_config_includes_license_proxy_node_group_when_enabled() -> None:
    config = default_eks_config()
    config["license_proxy"]["enabled"] = True

    rendered = yaml.safe_load(render_cluster_config(config))

    node_group_names = [group["name"] for group in rendered["managedNodeGroups"]]
    assert "license-proxy-node-group" in node_group_names


def test_values_include_model_urls_and_role_arn() -> None:
    config = default_eks_config()
    config["cluster"]["name"] = "values-cluster"
    config["cluster"]["region"] = "us-east-2"
    config["efs"]["file_system_id"] = "fs-123"
    config["models"]["urls"] = ["https://example.com/model.dg"]

    rendered = yaml.safe_load(
        render_values(
            config,
            cluster_autoscaler_role_arn="arn:aws:iam::123:role/autoscaler",
        )
    )

    assert rendered["engine"]["modelManager"]["volumes"]["aws"]["efs"]["fileSystemId"] == "fs-123"
    assert rendered["engine"]["modelManager"]["models"]["add"] == ["https://example.com/model.dg"]
    assert rendered["cluster-autoscaler"]["awsRegion"] == "us-east-2"
    assert (
        rendered["cluster-autoscaler"]["rbac"]["serviceAccount"]["annotations"][
            "eks.amazonaws.com/role-arn"
        ]
        == "arn:aws:iam::123:role/autoscaler"
    )


def test_plan_resolves_aws_role_arn_when_requested(tmp_path, monkeypatch) -> None:
    config = default_eks_config()
    config["cluster"]["name"] = "resolved-cluster"
    config["efs"]["file_system_id"] = "fs-123"
    config["models"]["urls"] = ["https://example.com/model.dg"]
    config_path = tmp_path / "deployment.yaml"
    output_dir = tmp_path / "artifacts"
    config_path.write_text(yaml.safe_dump(config, sort_keys=False))

    monkeypatch.setattr(
        kubernetes_aws_workflow.aws_cli,
        "get_role_arn",
        lambda role_name: f"arn:aws:iam::123:role/{role_name}",
    )

    _, values_path = plan_from_config(config_path, output_dir, resolve_aws=True)

    rendered = yaml.safe_load(values_path.read_text())
    assert (
        rendered["cluster-autoscaler"]["rbac"]["serviceAccount"]["annotations"][
            "eks.amazonaws.com/role-arn"
        ]
        == "arn:aws:iam::123:role/resolved-cluster-cluster-autoscaler-role"
    )


def test_plan_resolve_aws_requires_efs_id(tmp_path, monkeypatch) -> None:
    config = default_eks_config()
    config["cluster"]["name"] = "missing-efs-cluster"
    config["models"]["urls"] = ["https://example.com/model.dg"]
    config_path = tmp_path / "deployment.yaml"
    output_dir = tmp_path / "artifacts"
    config_path.write_text(yaml.safe_dump(config, sort_keys=False))

    monkeypatch.setattr(
        kubernetes_aws_workflow.aws_cli,
        "get_role_arn",
        lambda role_name: f"arn:aws:iam::123:role/{role_name}",
    )

    with pytest.raises(ValueError, match="efs.file_system_id is required"):
        plan_from_config(config_path, output_dir, resolve_aws=True)


def test_ensure_efs_writes_discovered_id_to_config(tmp_path, monkeypatch) -> None:
    config = default_eks_config()
    config["models"]["urls"] = ["https://example.com/model.dg"]
    config_path = tmp_path / "deployment.yaml"
    config_path.write_text(yaml.safe_dump(config, sort_keys=False))

    class FakeNetworking:
        subnet_ids = ["subnet-1"]
        cluster_security_group_id = "sg-1"

    monkeypatch.setattr(
        kubernetes_aws_workflow.aws_cli,
        "get_eks_networking",
        lambda cluster_name, region: FakeNetworking(),
    )
    monkeypatch.setattr(
        kubernetes_aws_workflow.aws_cli,
        "ensure_efs_file_system",
        lambda cluster_name, region, mode, efs_id: "fs-123",
    )
    monkeypatch.setattr(
        kubernetes_aws_workflow.aws_cli,
        "ensure_nfs_ingress",
        lambda security_group_id, region: None,
    )
    monkeypatch.setattr(
        kubernetes_aws_workflow.aws_cli,
        "ensure_mount_targets",
        lambda efs_id, subnet_ids, security_group_id, region: ["fsmt-1"],
    )

    efs_id = ensure_efs_from_config(config_path, write_back=True, console=_QuietConsole())

    updated = yaml.safe_load(config_path.read_text())
    assert efs_id == "fs-123"
    assert updated["efs"]["mode"] == "existing"
    assert updated["efs"]["file_system_id"] == "fs-123"


def test_ensure_mount_targets_creates_once_per_availability_zone(monkeypatch) -> None:
    create_calls: list[str] = []

    monkeypatch.setattr(aws_cli, "mount_target_azs", lambda efs_id, region: set())
    monkeypatch.setattr(
        aws_cli,
        "describe_subnet_az",
        lambda subnet_id, region: {
            "subnet-a1": "us-east-2a",
            "subnet-a2": "us-east-2a",
            "subnet-b1": "us-east-2b",
        }[subnet_id],
    )
    monkeypatch.setattr(aws_cli, "wait_for_mount_target", lambda mount_target_id, region: None)

    def fake_run_json(command, *, cwd=None):
        create_calls.append(command[command.index("--subnet-id") + 1])
        return (
            type("Result", (), {"ok": True})(),
            {"MountTargetId": f"fsmt-{len(create_calls)}"},
        )

    monkeypatch.setattr(aws_cli, "run_json", fake_run_json)

    created = aws_cli.ensure_mount_targets(
        "fs-123",
        ["subnet-a1", "subnet-a2", "subnet-b1"],
        "sg-123",
        "us-east-2",
    )

    assert create_calls == ["subnet-a1", "subnet-b1"]
    assert created == ["fsmt-1", "fsmt-2"]


def test_native_setup_dry_run_writes_cluster_config_and_skips_provisioning(
    tmp_path, monkeypatch
) -> None:
    config = default_eks_config()
    config["models"]["urls"] = ["https://example.com/model.dg"]
    config["actions"]["dry_run"] = True
    config_path = tmp_path / "deployment.yaml"
    config_path.write_text(yaml.safe_dump(config, sort_keys=False))

    artifact_dir = tmp_path / "artifacts"
    monkeypatch.setattr(kubernetes_aws, "KUBERNETES_AWS_ARTIFACTS_DIR", artifact_dir)
    monkeypatch.setattr(kubernetes_aws, "_preflight", lambda console: None)

    def fail_run(*args, **kwargs):
        raise AssertionError("dry run must not invoke external commands")

    monkeypatch.setattr(kubernetes_aws, "run", fail_run)

    kubernetes_aws._run_native_setup(config_path, _QuietConsole())

    rendered = yaml.safe_load((artifact_dir / "cluster-config.yaml").read_text())
    assert rendered["metadata"]["name"] == "deepgram-self-hosted-cluster"
    # Dry run also writes Helm values so users can diff against the upstream
    # chart sample without deploying.
    assert (artifact_dir / "my-values.yaml").exists()
    values = yaml.safe_load((artifact_dir / "my-values.yaml").read_text())
    assert values["engine"]["modelManager"]["models"]["add"] == [
        "https://example.com/model.dg"
    ]


def test_native_setup_aborts_when_actions_continue_is_false(tmp_path, monkeypatch) -> None:
    config = default_eks_config()
    config["models"]["urls"] = ["https://example.com/model.dg"]
    config["actions"]["continue"] = False
    config_path = tmp_path / "deployment.yaml"
    config_path.write_text(yaml.safe_dump(config, sort_keys=False))

    artifact_dir = tmp_path / "artifacts"
    monkeypatch.setattr(kubernetes_aws, "KUBERNETES_AWS_ARTIFACTS_DIR", artifact_dir)
    monkeypatch.setattr(kubernetes_aws, "_preflight", lambda console: None)

    def fail_run(*args, **kwargs):
        raise AssertionError("must not invoke external commands when continue=false")

    monkeypatch.setattr(kubernetes_aws, "run", fail_run)

    kubernetes_aws._run_native_setup(config_path, _QuietConsole())

    assert (artifact_dir / "cluster-config.yaml").exists()
    assert not (artifact_dir / "my-values.yaml").exists()


def test_render_values_allows_empty_models_when_reusing_efs() -> None:
    config = default_eks_config()
    config["efs"]["mode"] = "existing"
    config["efs"]["file_system_id"] = "fs-existing"
    config["models"]["urls"] = []
    config["models"]["deployment_file"] = None

    rendered = yaml.safe_load(
        render_values(config, cluster_autoscaler_role_arn="arn:aws:iam::123:role/x")
    )

    assert rendered["engine"]["modelManager"]["models"]["add"] == []
    assert rendered["engine"]["modelManager"]["volumes"]["aws"]["efs"]["fileSystemId"] == (
        "fs-existing"
    )


def test_summary_renders_existing_efs_message_when_models_empty(capsys) -> None:
    from rich.console import Console

    from deepgram_self_hosted.summary import render_summary

    config = default_eks_config()
    config["efs"]["mode"] = "existing"
    config["efs"]["file_system_id"] = "fs-existing"
    config["models"]["urls"] = []
    config["models"]["deployment_file"] = None

    console = Console(force_terminal=False, width=120)
    render_summary(config, console)
    out = capsys.readouterr().out

    assert "use models already on EFS" in out
    assert "fs-existing" in out


def test_cluster_config_uses_per_node_group_instance_type_overrides() -> None:
    config = default_eks_config()
    config["node_groups"]["control_plane"]["instance_type"] = "m5.large"
    config["node_groups"]["engine"]["instance_type"] = "g5.4xlarge"
    config["node_groups"]["api"]["instance_type"] = "c5.2xlarge"
    config["license_proxy"]["enabled"] = True
    config["node_groups"]["license_proxy"]["instance_type"] = "t3.xlarge"

    rendered = yaml.safe_load(render_cluster_config(config))
    by_name = {group["name"]: group for group in rendered["managedNodeGroups"]}

    assert by_name["control-plane-node-group"]["instanceType"] == "m5.large"
    assert by_name["engine-node-group"]["instanceType"] == "g5.4xlarge"
    assert by_name["api-node-group"]["instanceType"] == "c5.2xlarge"
    assert by_name["license-proxy-node-group"]["instanceType"] == "t3.xlarge"


def test_cluster_config_falls_back_to_default_instance_types_when_unset() -> None:
    config = default_eks_config()
    for group in config["node_groups"].values():
        group.pop("instance_type", None)

    rendered = yaml.safe_load(render_cluster_config(config))
    by_name = {group["name"]: group for group in rendered["managedNodeGroups"]}

    assert by_name["control-plane-node-group"]["instanceType"] == "t3.large"
    assert by_name["engine-node-group"]["instanceType"] == "g6.2xlarge"
    assert by_name["api-node-group"]["instanceType"] == "c5n.xlarge"


def test_summary_renders_node_groups_and_other_fields(capsys) -> None:
    from rich.console import Console

    from deepgram_self_hosted.summary import render_summary

    config = default_eks_config()
    config["cluster"]["name"] = "summary-cluster"
    config["cluster"]["region"] = "us-east-2"
    config["node_groups"]["engine"]["instance_type"] = "g5.4xlarge"
    config["node_groups"]["engine"]["desired"] = 4
    config["models"]["urls"] = ["https://example.com/a.dg", "https://example.com/b.dg"]
    config["license_proxy"]["enabled"] = True

    console = Console(force_terminal=False, width=120)
    render_summary(config, console)
    out = capsys.readouterr().out

    assert "summary-cluster" in out
    assert "us-east-2" in out
    assert "g5.4xlarge" in out
    assert "License proxy" in out
    assert "2 URL(s)" in out


def test_write_config_chmods_to_owner_only(tmp_path) -> None:
    import os
    import stat

    path = tmp_path / "deployment.yaml"
    write_config(path, {"hello": "world"})

    mode = stat.S_IMODE(os.stat(path).st_mode)
    assert mode == 0o600


def test_strip_secrets_clears_values_but_preserves_mode() -> None:
    config = default_eks_config()
    config["secrets"] = {
        "mode": "create",
        "registry_username": "u",
        "registry_password": "p",
        "api_key": "k",
    }

    sanitized = strip_secrets(config)

    assert sanitized["secrets"]["mode"] == "create"
    assert sanitized["secrets"]["registry_username"] is None
    assert sanitized["secrets"]["registry_password"] is None
    assert sanitized["secrets"]["api_key"] is None
    # Original is untouched.
    assert config["secrets"]["registry_password"] == "p"


def test_extract_secrets_returns_three_keys() -> None:
    config = {"secrets": {"registry_username": "u", "registry_password": "p", "api_key": "k"}}
    assert extract_secrets(config) == {
        "registry_username": "u",
        "registry_password": "p",
        "api_key": "k",
    }


def test_resolve_secrets_prefers_override(monkeypatch) -> None:
    monkeypatch.setenv("DG_REGISTRY_USERNAME", "from-env")
    config = {"secrets": {"registry_username": "from-config"}}
    override = {"registry_username": "from-override", "registry_password": "p", "api_key": "k"}

    creds = kubernetes_aws._resolve_secrets(config, override)
    assert creds["registry_username"] == "from-override"


def test_resolve_secrets_falls_back_to_env_then_config(monkeypatch) -> None:
    monkeypatch.delenv("DG_REGISTRY_USERNAME", raising=False)
    monkeypatch.setenv("DG_REGISTRY_PASSWORD", "env-password")
    monkeypatch.delenv("DG_API_KEY", raising=False)
    config = {
        "secrets": {
            "registry_username": "config-user",
            "registry_password": "config-password",
            "api_key": None,
        }
    }

    creds = kubernetes_aws._resolve_secrets(config, secrets_override=None)
    assert creds["registry_username"] == "config-user"
    assert creds["registry_password"] == "env-password"
    assert creds["api_key"] is None


def test_create_secrets_raises_when_any_credential_missing() -> None:
    with pytest.raises(ValueError, match="api_key is missing"):
        kubernetes_aws._create_secrets(
            {"registry_username": "u", "registry_password": "p", "api_key": None},
            "dg-self-hosted",
            _QuietConsole(),
        )


def test_run_returns_failed_result_when_command_is_missing() -> None:
    result = run(["definitely-not-a-real-deepgram-cli"])

    assert result.returncode == 127
    assert not result.ok
    assert result.stderr == "definitely-not-a-real-deepgram-cli not found on PATH"


def _voice_agent_config() -> dict:
    config = default_eks_config()
    config["deployment"]["type"] = "VOICE_AGENT"
    config["efs"]["file_system_id"] = "fs-voice-agent"
    config["models"]["urls"] = ["https://example.com/eot.dg"]
    config["aura2"]["enabled"] = True
    config["aura2"]["english"]["enabled"] = True
    return config


def test_render_values_voice_agent_emits_agent_block_and_dict_engine_replicas() -> None:
    config = _voice_agent_config()
    config["node_groups"]["engine"]["agent_replicas"] = {
        "agent-speech-to-text": 2,
        "agent-text-to-speech": 3,
        "agent-end-of-turn": 1,
    }

    rendered = yaml.safe_load(
        render_values(config, cluster_autoscaler_role_arn="arn:aws:iam::123:role/x")
    )

    assert rendered["agent"]["enabled"] is True
    assert rendered["scaling"]["replicas"]["engine"] == {
        "agent-speech-to-text": 2,
        "agent-text-to-speech": 3,
        "agent-end-of-turn": 1,
    }
    # Cluster autoscaler is forced off for Voice Agent.
    assert rendered["cluster-autoscaler"]["enabled"] is False
    # Aura-2 English appears with vendored UUIDs.
    assert rendered["aura2"]["enabled"] is True
    assert rendered["aura2"]["english"]["enabled"] is True
    assert rendered["aura2"]["english"]["t2cUuid"]
    assert rendered["aura2"]["english"]["c2aUuid"]


def test_render_values_stt_still_emits_scalar_engine_replicas_and_agent_disabled() -> None:
    config = default_eks_config()
    config["efs"]["file_system_id"] = "fs-stt"
    config["models"]["urls"] = ["https://example.com/nova.dg"]

    rendered = yaml.safe_load(
        render_values(config, cluster_autoscaler_role_arn="arn:aws:iam::123:role/x")
    )

    assert rendered["agent"]["enabled"] is False
    assert isinstance(rendered["scaling"]["replicas"]["engine"], int)
    assert rendered["cluster-autoscaler"]["enabled"] is True
    assert "aura2" not in rendered


def test_render_values_voice_agent_emits_third_party_credentials() -> None:
    config = _voice_agent_config()
    config["third_party_credentials"] = {
        "openai": "openai-api-key",
        "anthropic": "anthropic-api-key",
    }

    rendered = yaml.safe_load(
        render_values(config, cluster_autoscaler_role_arn="arn:aws:iam::123:role/x")
    )

    third_party = rendered["global"]["thirdPartyCredentials"]
    assert third_party["openAiSecretRef"] == "openai-api-key"
    assert third_party["anthropicSecretRef"] == "anthropic-api-key"


def test_render_values_skips_aura2_when_disabled() -> None:
    config = _voice_agent_config()
    config["aura2"]["enabled"] = False

    rendered = yaml.safe_load(
        render_values(config, cluster_autoscaler_role_arn="arn:aws:iam::123:role/x")
    )

    assert "aura2" not in rendered


def test_strip_secrets_nulls_llm_provider_api_keys() -> None:
    config = default_eks_config()
    config["secrets"]["mode"] = "create"
    config["secrets"]["llm_provider_api_keys"] = {
        "openai": "sk-secret",
        "anthropic": "ant-secret",
    }

    sanitized = strip_secrets(config)

    assert sanitized["secrets"]["llm_provider_api_keys"] == {
        "openai": None,
        "anthropic": None,
    }
    # Original untouched.
    assert config["secrets"]["llm_provider_api_keys"]["openai"] == "sk-secret"


def test_resolve_llm_provider_secrets_prefers_override(monkeypatch) -> None:
    monkeypatch.delenv("DG_OPENAI_API_KEY", raising=False)
    config = {
        "third_party_credentials": {"openai": "openai-api-key"},
        "secrets": {"llm_provider_api_keys": {"openai": "config-value"}},
    }

    creds = kubernetes_aws._resolve_llm_provider_secrets(
        config, {"openai": "override-value"}
    )

    assert creds == {"openai": "override-value"}


def test_resolve_llm_provider_secrets_falls_back_to_env_then_config(monkeypatch) -> None:
    monkeypatch.setenv("DG_OPENAI_API_KEY", "env-value")
    monkeypatch.delenv("DG_ANTHROPIC_API_KEY", raising=False)
    config = {
        "third_party_credentials": {
            "openai": "openai-api-key",
            "anthropic": "anthropic-api-key",
        },
        "secrets": {
            "llm_provider_api_keys": {
                "openai": "config-value",
                "anthropic": "anthropic-config",
            }
        },
    }

    creds = kubernetes_aws._resolve_llm_provider_secrets(config, llm_api_keys_override=None)

    assert creds["openai"] == "env-value"
    assert creds["anthropic"] == "anthropic-config"


def test_voice_agent_defaults_match_chart_sample_sizing() -> None:
    """After _apply_voice_agent_defaults, cluster-config sizing should match
    Deepgram's 05-voice-agent-aws.cluster-config.yaml sample."""
    from deepgram_self_hosted.wizard import _apply_voice_agent_defaults

    config = default_eks_config()
    config["deployment"]["type"] = "VOICE_AGENT"
    config["license_proxy"]["enabled"] = True
    _apply_voice_agent_defaults(config)

    rendered = yaml.safe_load(render_cluster_config(config))
    by_name = {group["name"]: group for group in rendered["managedNodeGroups"]}

    engine = by_name["engine-node-group"]
    assert engine["minSize"] == 3
    assert engine["desiredCapacity"] == 3
    assert engine["maxSize"] == 8
    assert engine["instanceType"] == "g6.12xlarge"

    license_proxy = by_name["license-proxy-node-group"]
    assert license_proxy["minSize"] == 1
    assert license_proxy["desiredCapacity"] == 1
    assert license_proxy["maxSize"] == 2


def test_voice_agent_defaults_preserve_user_overrides() -> None:
    """User overrides survive _apply_voice_agent_defaults."""
    from deepgram_self_hosted.wizard import _apply_voice_agent_defaults

    config = default_eks_config()
    config["deployment"]["type"] = "VOICE_AGENT"
    config["node_groups"]["engine"]["min"] = 2
    config["node_groups"]["engine"]["desired"] = 4
    config["node_groups"]["engine"]["instance_type"] = "g5.12xlarge"
    config["node_groups"]["license_proxy"]["min"] = 2
    config["node_groups"]["license_proxy"]["desired"] = 2

    _apply_voice_agent_defaults(config)

    assert config["node_groups"]["engine"]["min"] == 2
    assert config["node_groups"]["engine"]["desired"] == 4
    assert config["node_groups"]["engine"]["instance_type"] == "g5.12xlarge"
    assert config["node_groups"]["license_proxy"]["min"] == 2
    assert config["node_groups"]["license_proxy"]["desired"] == 2


def test_create_llm_provider_secrets_raises_when_key_missing() -> None:
    config = {"third_party_credentials": {"openai": "openai-api-key"}}
    with pytest.raises(ValueError, match="openai"):
        kubernetes_aws._create_llm_provider_secrets(
            config,
            {"openai": None},
            "dg-self-hosted",
            _QuietConsole(),
        )
