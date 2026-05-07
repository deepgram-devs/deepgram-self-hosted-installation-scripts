# Deepgram Self-Hosted Setup Scripts

Setup tooling for running Deepgram self-hosted services across deployment targets and cloud providers. The Kubernetes path is driven by an interactive Python wizard (`uv run dg-self-hosted setup kubernetes aws`); the Docker path uses an interactive shell script.

## Layout

```text
.
├── docker/
│   └── aws/
│       ├── README.md
│       └── deepgram-aws-docker-setup.sh
├── kubernetes/
│   └── aws/
│       └── README.md
└── src/deepgram_self_hosted/        # Python CLI + EKS wizard
```

## Deployment Paths

- [Kubernetes on AWS EKS](kubernetes/aws/README.md) — Python wizard creates or uses an EKS cluster, configures EFS, generates Helm values, and deploys the Deepgram self-hosted chart.
- [Docker on AWS EC2](docker/aws/README.md) — provisions or configures an Ubuntu EC2 host, installs Docker/Podman, and runs Deepgram self-hosted containers.

## Local CLI

This repo includes a `uv`-managed Python CLI that runs locally without installing the package globally.

```bash
uv run dg-self-hosted --help
```

You can also use the local wrapper:

```bash
./dg-self-hosted --help
```

Common commands:

```bash
# Interactive wizard for AWS EKS (asks for cluster, node groups, models, EFS, secrets, etc.)
uv run dg-self-hosted setup kubernetes aws

# Re-run a saved deployment config (shows summary, lets you edit, then deploys)
uv run dg-self-hosted setup kubernetes aws --config deployments/stt-2.yaml

# Run the existing interactive AWS EC2 Docker/Podman setup script
uv run dg-self-hosted setup docker aws

# Generate a reusable Kubernetes/AWS base config without going through the wizard
uv run dg-self-hosted config init kubernetes aws --output deployments/base.yaml

# Prepare EFS for the EKS cluster and write the EFS ID back to the config
uv run dg-self-hosted prepare efs kubernetes aws --config deployments/stt-2.yaml

# Render Helm values without touching AWS
uv run dg-self-hosted plan kubernetes aws --config deployments/stt-2.yaml --resolve-aws

# Show current Kubernetes pods, services, and Helm release status
uv run dg-self-hosted status kubernetes aws
```

The Kubernetes/AWS workflow runs natively in Python — it shells out to `aws`, `eksctl`, `kubectl`, and `helm` directly, so no shell wrapper is involved.

## The EKS Wizard

`setup kubernetes aws` (no `--config`) opens an interactive Questionary wizard with curated dropdowns for region, Kubernetes version, and per-node-group instance type (engine GPU instances, API c5n family, general-purpose for control-plane and license-proxy). Each dropdown shows `(default)` next to the suggested value and offers an `Other (enter custom)` entry for off-list values. Required fields (Quay credentials, API key, existing-EFS ID, deployment-file path) are enforced with non-empty validators.

After the wizard collects answers it shows a Rich summary table (Cluster / Node groups / Other) and a four-way menu:

- **Deploy** — write the config to disk and run the full deployment. When `Dry run` is on, this option is renamed to **Render artifacts (dry run)**.
- **Edit a field** — pick any of the most-edited fields (region, instance types, replica counts, license-proxy toggle, dry-run toggle, etc.) and re-prompt for it. The summary refreshes after each edit.
- **Save config and exit** — write the config but don't deploy.
- **Cancel** — discard.

If you also choose `Use existing EFS`, the model picker offers a third option, **Skip (use models already on EFS)**, which leaves `models.add` empty in the rendered Helm values so the model-manager re-uses what's already on the volume.

## Reusing Configs

For repeat deployments, generate a base Kubernetes/AWS config once:

```bash
uv run dg-self-hosted config init kubernetes aws --output deployments/base.yaml
```

Edit the generated YAML to add either `models.urls` or `models.deployment_file`, plus any node sizing, `instance_type` overrides, or action defaults you want to reuse. The full schema is the dict produced by `default_eks_config()` — every node group accepts `instance_type`, `min`, `desired`, and `max`.

Clone it for a new cluster:

```bash
uv run dg-self-hosted config clone \
  --from deployments/base.yaml \
  --output deployments/stt-2.yaml \
  --cluster-name stt-2 \
  --region us-east-2
```

Deploy from the cloned config:

```bash
uv run dg-self-hosted setup kubernetes aws --config deployments/stt-2.yaml
```

`setup ... --config X` loads the YAML, shows the same summary loop the wizard uses, and lets you edit fields before deploying. Any edits you make are written back to the same path before deploy, so re-runs stay in sync with what was actually applied.

Render Kubernetes artifacts without touching AWS:

```bash
uv run dg-self-hosted plan kubernetes aws \
  --config deployments/stt-2.yaml \
  --output-dir deployments/stt-2-artifacts
```

The Python `plan` command owns artifact rendering. By default it runs offline and may leave AWS-discovered values, such as the cluster-autoscaler role ARN, as placeholders.

Prepare EFS after the EKS cluster exists:

```bash
uv run dg-self-hosted prepare efs kubernetes aws --config deployments/stt-2.yaml
```

This Python-native step discovers the EKS VPC/subnets, creates or verifies EFS, ensures NFS ingress, creates missing mount targets, and writes the EFS ID back to the config by default.

After `prepare efs`, render complete values by resolving AWS-discovered fields:

```bash
uv run dg-self-hosted plan kubernetes aws \
  --config deployments/stt-2.yaml \
  --output-dir deployments/stt-2-artifacts \
  --resolve-aws
```

`--resolve-aws` reads the final IAM role ARN from AWS and requires `efs.file_system_id` to already be present in the config. It does not create or modify AWS resources.

## Secrets Handling

Generated configs default to `secrets.mode: external`, so the deployment expects Kubernetes secrets (`dg-regcred` and `dg-self-hosted-api-key`) to already exist in the `dg-self-hosted` namespace.

If you choose `Create in-cluster secrets` in the wizard, the tooling does not write your credentials to disk:

- The wizard collects them in memory only.
- Before the config is saved, the three secret fields (`secrets.registry_username`, `secrets.registry_password`, `secrets.api_key`) are stripped from the saved YAML — `secrets.mode: create` is preserved, but the values are written as `null`.
- At deploy time, credentials are resolved in this order:
  1. The in-memory values from the wizard (current run).
  2. Environment variables: `DG_REGISTRY_USERNAME`, `DG_REGISTRY_PASSWORD`, `DG_API_KEY`.
  3. Values present in the YAML (only useful if you hand-edited them in).
- If none of the above provide all three values, the deploy aborts with a message naming the env vars to set.

Every config written by `dg-self-hosted` (wizard, `config init`, `config clone`, `prepare efs` write-back) is `chmod 0600` — owner-only on disk.

## Shared Requirements

- [`uv`](https://docs.astral.sh/uv/) for running the local Python CLI
- [Python 3.11+](https://docs.python.org/3/) managed by `uv`
- [AWS CLI](https://docs.aws.amazon.com/cli/latest/userguide/cli-chap-welcome.html) configured (`aws sts get-caller-identity` works) — both paths
- For EKS (Kubernetes path): [`eksctl`](https://eksctl.io/), [`kubectl`](https://kubernetes.io/docs/tasks/tools/#kubectl), [`helm`](https://helm.sh/docs/intro/install/)
- [Deepgram self-hosted API key](https://developers.deepgram.com/docs/self-hosted-introduction)
- [Quay](https://docs.projectquay.io/) credentials with access to required Deepgram self-hosted images
- Deepgram model `.dg` URLs provided for your self-hosted deployment
- Cloud-provider permissions for the selected deployment path

## Python Package Docs

The local CLI dependencies are managed by `uv` from [pyproject.toml](pyproject.toml):

- [`Typer`](https://typer.tiangolo.com/) — CLI command framework
- [`Questionary`](https://questionary.readthedocs.io/) — interactive prompts and dropdowns for the wizard
- [`Rich`](https://rich.readthedocs.io/) — terminal formatting and the summary tables
- [`PyYAML`](https://pyyaml.org/wiki/PyYAMLDocumentation) — YAML config parsing/rendering
- [`pytest`](https://docs.pytest.org/) — tests
- [`Ruff`](https://docs.astral.sh/ruff/) — linting

## Generated Files

The Kubernetes wizard always writes artifacts to `kubernetes/aws/artifacts/` (relative to the repo root), regardless of the directory you ran the CLI from:

- `cluster-config.yaml` — Deepgram-style `eksctl` cluster config
- `eksctl-expanded-cluster-config.yaml` — optional expanded `eksctl --dry-run` output
- `my-values.yaml` — Helm values rendered after EFS provisioning
- `session.yaml` — default save path when the wizard runs without `--config`

The Docker path writes its compose/TOML output into `$HOME/deepgram-self-hosted/` on the target host.

`artifacts/` and ad-hoc test output under `test-leah/` are gitignored, along with local env/secret files.

## Adding Providers

Use this structure for future work:

```text
docker/<provider>/
kubernetes/<provider>/
```

Each provider directory should contain:

- A provider-specific setup script (Docker path) or be wired into the Python wizard (Kubernetes path)
- A local `README.md`
- Any provider-specific templates or support files

Keep the root README as a navigation and shared-prerequisites document. Put provider-specific commands, prompts, and troubleshooting in the provider README.
