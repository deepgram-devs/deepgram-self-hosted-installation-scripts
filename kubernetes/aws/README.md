# Deepgram Kubernetes on AWS EKS

Interactive wizard for running Deepgram self-hosted services on AWS EKS, driven by the `dg-self-hosted` Python CLI. The previous shell script (`deepgram-eks-setup.sh`) has been removed — its logic now lives in `src/deepgram_self_hosted/providers/kubernetes_aws.py` and the wizard in `src/deepgram_self_hosted/wizard.py`.

## What This Workflow Does

- Collects deployment options interactively (or loads a saved YAML config)
- Renders a Deepgram-style `eksctl` cluster config to `artifacts/<folder>/cluster-config.yaml`
- Optionally creates the EKS cluster via `eksctl create cluster`
- Creates or reuses encrypted EFS storage, ensures NFS ingress, and creates missing mount targets per AZ
- Installs the EFS CSI driver addon using the IAM role that `eksctl` provisioned
- Creates the `dg-self-hosted` namespace
- Creates Kubernetes secrets in-cluster (only if you opt in; credentials are not written to disk)
- Renders Helm values to `artifacts/<folder>/helm-values.yaml`
- Installs or upgrades the Deepgram self-hosted Helm chart

## Requirements

- [AWS CLI](https://docs.aws.amazon.com/cli/latest/userguide/cli-chap-welcome.html) configured (`aws sts get-caller-identity` works)
- [`eksctl`](https://eksctl.io/) for EKS cluster creation
- [`kubectl`](https://kubernetes.io/docs/tasks/tools/#kubectl) for Kubernetes operations
- [`helm`](https://helm.sh/docs/intro/install/) for chart installation and upgrades
- AWS permissions to create EKS, IAM roles, EFS, EFS mount targets, and related EC2 networking resources
- Deepgram credentials:
  - [Self Hosted Quay.io](https://developers.deepgram.com/docs/self-hosted-self-service-tutorial#create-container-image-distribution-credentials) credentials with access to required self-hosted images
  - [Deepgram self-hosted API key](https://developers.deepgram.com/docs/self-hosted-self-service-tutorial#create-a-self-hosted-api-key)
  - Model `.dg` URLs, or a Deepgram deployment `.txt` file containing them

## Usage

Fresh interactive wizard:

```bash
uv run dg-self-hosted setup kubernetes aws
```

Re-run from a saved config (the wizard's summary loop opens before deploy so you can edit fields):

```bash
uv run dg-self-hosted setup kubernetes aws --config deployments/stt-2.yaml
```

The wizard collects, in order:

1. Cluster name (text)
2. AWS region (dropdown with `(default)` markers + `Other (enter custom)`)
3. Kubernetes version (dropdown)
4. Deployment type (`STT` or `TTS`)
5. Service exposure type (`ClusterIP`, `LoadBalancer`, or `NodePort`)
6. Per-node-group settings for **Control plane**, **Engine (GPU)**, and **API**:
   - Instance type (curated dropdown — engine list is GPU instances; control-plane and API have their own curated lists; each supports `Other`)
   - Min / desired / max size (integer validators)
7. License Proxy enable + (if enabled) its node-group settings
8. EFS storage mode — `Create new EFS` or `Use existing EFS` (existing requires a non-empty `fs-...` ID)
9. Model URL source — `Enter URLs manually`, `Load from deployment .txt file`, or (only when EFS is existing) `Skip (use models already on EFS)`
10. Kubernetes secrets mode — `Use external secret store` (default) or `Create in-cluster secrets`
11. Dry run toggle

After the wizard collects answers, the summary screen renders three Rich tables (Cluster / Node groups / Other) and shows a four-way menu:

- **Deploy** — write the config and run the full deployment.
- **Render artifacts (dry run)** — replaces "Deploy" when `Dry run` is on; writes only the artifacts and exits.
- **Edit a field** — pick from the most-edited fields (region, instance types, replica counts, license-proxy / dry-run toggles, etc.) and re-prompt.
- **Save config and exit** — write the config but don't deploy.
- **Cancel** — discard.

Before deploy, the (possibly edited) config is written to disk:

- With `--config X`, back to the same path. Rendered `cluster-config.yaml` and `helm-values.yaml` land next to it (in `Path(X).parent`).
- Without `--config`, the wizard asks for a folder name (defaulting to the cluster name) and creates `kubernetes/aws/artifacts/<folder>/` with `session.yaml`, `cluster-config.yaml`, and `helm-values.yaml` inside. If the folder already exists with files in it, you'll be asked to confirm before overwriting.

Every saved config is written with file mode `0600` (owner-only).

## Dry Run

Two ways to enable it:

1. From the wizard, answer **yes** to `Dry run (write artifacts only, do not provision)?`. The summary's primary action becomes **Render artifacts (dry run)**.
2. From a saved config, set `actions.dry_run: true` and run `setup kubernetes aws --config X`.

Generated files are written under `kubernetes/aws/artifacts/<folder>/`:

- `cluster-config.yaml` — Deepgram-style `eksctl` cluster config
- `eksctl-expanded-cluster-config.yaml` — optional expanded `eksctl --dry-run` output (set `actions.expanded_eksctl_dry_run: true` in the config to write this)
- `helm-values.yaml` — Helm values, only written after EFS provisioning succeeds (so it's absent in dry-run mode)
- `session.yaml` — the wizard's saved config (driver YAML for re-runs)

## Full Deployment

For a full EKS deployment, leave dry-run off and pick **Deploy**. The wizard creates or reuses the cluster, configures EFS, installs the EFS CSI addon, creates secrets (if mode is `create`), and runs `helm install` or `helm upgrade`.

Equivalent Helm command if you want to apply the rendered values manually:

```bash
helm install deepgram deepgram/deepgram-self-hosted \
  -f artifacts/<folder>/helm-values.yaml \
  --namespace dg-self-hosted \
  --atomic \
  --timeout 1h
```

The chart is fetched from the Deepgram self-hosted Helm repository:

```bash
helm repo add deepgram https://deepgram.github.io/self-hosted-resources
```

## Credentials Without Persisting Secrets

If you pick `Create in-cluster secrets`, the wizard collects Quay username/password and your Deepgram self-hosted API key but **does not write them to the saved YAML**. They're held in memory for the current deploy, and the saved config has `secrets.mode: create` with `null` values for the three secret fields.

For non-interactive re-runs (e.g., automation that loads a saved config), set the credentials via environment variables before calling the CLI:

```bash
export DG_REGISTRY_USERNAME=...
export DG_REGISTRY_PASSWORD=...
export DG_API_KEY=...
uv run dg-self-hosted setup kubernetes aws --config deployments/stt-2.yaml
```

Resolution order at deploy time: in-memory wizard input → env vars → values in the YAML. If none of those provide all three, the deploy aborts with a message naming the env vars.

## Prompt Tips

- The instance-type dropdowns offer curated AWS instances for each role (engine = GPU; control-plane / license-proxy = general-purpose; API = c5n family). `Other (enter custom)` lets you type any instance type, including ones not in the list.
- Defaults in dropdowns are tagged with `(default)` next to the value.
- License Proxy is optional; toggling it on reveals its node-group questions and adds a `license-proxy-node-group` to the rendered cluster config.
- "Use existing EFS" expects an `fs-...` ID and adds a "Skip (use models already on EFS)" option to the model picker, so you can deploy without re-listing models that the model-manager already downloaded onto the volume.

## Troubleshooting

### Dry run shows extra fields

Symptom:
- `artifacts/<folder>/eksctl-expanded-cluster-config.yaml` contains many defaults that are not present in Deepgram's sample config

Cause:
- `eksctl create cluster --dry-run` normalizes and expands defaults. This is expected.

Fix:
- Compare Deepgram-style input against `artifacts/<folder>/cluster-config.yaml`
- Use `artifacts/<folder>/eksctl-expanded-cluster-config.yaml` only to inspect what `eksctl` will derive internally

### EFS CSI addon role missing

Symptom:
- Script fails while installing `aws-efs-csi-driver`
- Error mentions an EFS CSI IAM role

Checks:
- The cluster was created from `artifacts/<folder>/cluster-config.yaml`
- `eksctl` successfully created IAM service accounts
- AWS IAM contains the cluster-scoped EFS CSI role printed in the script summary, for example `<cluster-name>-efs-csi-driver-role`

### Engine pod stuck in ContainerCreating

Symptom:
- `deepgram-engine` is stuck in `ContainerCreating`
- Pod events include `FailedMount`
- Mount output says `Failed to resolve "fs-...efs.<region>.amazonaws.com"`

Cause:
- The EFS filesystem does not have mount targets in the EKS VPC availability zones, or the mount target security group does not allow NFS from cluster nodes.

Checks:

```bash
kubectl describe pod -n dg-self-hosted <engine-pod-name>
aws efs describe-mount-targets --file-system-id <fs-id> --region <region>
```

Fix:
- Re-run the setup script after this update. It now verifies mount targets and NFS access for both new and existing EFS filesystems.

### Helm chart install fails

Useful checks:

```bash
kubectl get pods -n dg-self-hosted
kubectl describe pod -n dg-self-hosted <pod-name>
helm status deepgram -n dg-self-hosted
kubectl get events -n dg-self-hosted --sort-by=.lastTimestamp
```
