#!/usr/bin/env bash
set -euo pipefail

# Deepgram Self-Hosted on AWS EKS interactive setup
# This script guides you through provisioning an EKS cluster, EFS, secrets, and Helm deployment.

SCRIPT_VERSION="0.1.0"

# ---------- helpers ----------

say() {
  printf "%b\n" "$*"
}

die() {
  say "ERROR: $*" >&2
  exit 1
}

require_cmd() {
  local cmd="$1"
  command -v "$cmd" >/dev/null 2>&1 || die "Missing required command: $cmd"
}

confirm() {
  local prompt="$1"
  local default="$2" # y or n
  local response
  while true; do
    if [[ "$default" == "y" ]]; then
      read -r -p "$prompt [Y/n]: " response || true
      response=${response:-Y}
    else
      read -r -p "$prompt [y/N]: " response || true
      response=${response:-N}
    fi
    case "$response" in
      [Yy]*) return 0 ;;
      [Nn]*) return 1 ;;
      *) say "Please answer y or n." ;;
    esac
  done
}

prompt() {
  local var_name="$1"
  local prompt_text="$2"
  local default_value="$3"
  local value
  if [[ -n "$default_value" ]]; then
    read -r -p "$prompt_text [$default_value]: " value || true
    value=${value:-$default_value}
  else
    read -r -p "$prompt_text: " value || true
  fi
  printf -v "$var_name" "%s" "$value"
}

prompt_choice() {
  local var_name="$1"
  local prompt_text="$2"
  shift 2
  local choices=("$@")
  local choice
  while true; do
    say "$prompt_text"
    local i=1
    for c in "${choices[@]}"; do
      say "  $i) $c"
      i=$((i+1))
    done
    read -r -p "Select 1-${#choices[@]}: " choice || true
    if [[ "$choice" =~ ^[0-9]+$ ]] && (( choice >= 1 && choice <= ${#choices[@]} )); then
      printf -v "$var_name" "%s" "${choices[$((choice-1))]}"
      return 0
    fi
    say "Invalid choice. Try again."
  done
}

prompt_list() {
  local var_name="$1"
  local prompt_text="$2"
  say "$prompt_text"
  say "Enter one per line. Submit a blank line to finish."
  local items=()
  while true; do
    read -r -p "> " line || true
    if [[ -z "$line" ]]; then
      break
    fi
    items+=("$line")
  done
  if (( ${#items[@]} == 0 )); then
    die "At least one entry is required."
  fi
  printf -v "$var_name" "%s" "${items[*]}"
}

json_array_from_space_list() {
  local list="$1"
  local json="["
  local first=1
  # shellcheck disable=SC2206
  local items=($list)
  for item in "${items[@]}"; do
    if [[ $first -eq 0 ]]; then
      json+=", "
    fi
    json+="\"$item\""
    first=0
  done
  json+="]"
  echo "$json"
}

# ---------- preflight ----------

say "Deepgram Self-Hosted on AWS EKS Setup (v$SCRIPT_VERSION)"

require_cmd aws
require_cmd eksctl
require_cmd kubectl
require_cmd helm
require_cmd jq

say "Checking AWS credentials..."
aws sts get-caller-identity >/dev/null 2>&1 || die "AWS credentials not configured. Run 'aws configure' or set env vars."

# ---------- inputs ----------

prompt CLUSTER_NAME "EKS cluster name" "deepgram-self-hosted-cluster"
prompt AWS_REGION "AWS region" "us-west-2"
prompt_choice DEPLOYMENT_TYPE "Select deployment type" "STT" "TTS"

prompt K8S_VERSION "Kubernetes version" "1.33"

prompt CONTROL_PLANE_MIN "Control plane nodegroup min size" "1"
prompt CONTROL_PLANE_DESIRED "Control plane nodegroup desired size" "1"
prompt CONTROL_PLANE_MAX "Control plane nodegroup max size" "3"

prompt ENGINE_MIN "Engine nodegroup min size" "1"
prompt ENGINE_DESIRED "Engine nodegroup desired size" "1"
prompt ENGINE_MAX "Engine nodegroup max size" "8"

prompt API_MIN "API nodegroup min size" "1"
prompt API_DESIRED "API nodegroup desired size" "1"
prompt API_MAX "API nodegroup max size" "2"

LICENSE_PROXY_MIN="0"
LICENSE_PROXY_DESIRED="0"
LICENSE_PROXY_MAX="2"

prompt_list MODEL_LINKS_LIST "Enter Deepgram model URLs (.dg)"

prompt_choice EFS_MODE "EFS storage" "Create new EFS" "Use existing EFS"
if [[ "$EFS_MODE" == "Use existing EFS" ]]; then
  prompt EFS_ID "EFS filesystem ID (fs-xxxx)" ""
  [[ -n "$EFS_ID" ]] || die "EFS filesystem ID is required."
fi

prompt_choice SECRET_MODE "Kubernetes secrets" "Create in-cluster secrets" "Use external secret store"
if [[ "$SECRET_MODE" == "Create in-cluster secrets" ]]; then
  prompt DG_DOCKER_USER "Deepgram registry username" ""
  prompt DG_DOCKER_PASS "Deepgram registry password" ""
  prompt DG_API_KEY "Deepgram self-hosted API key" ""
  [[ -n "$DG_DOCKER_USER" && -n "$DG_DOCKER_PASS" && -n "$DG_API_KEY" ]] || die "Registry creds and API key required."
fi

prompt_choice SERVICE_TYPE "Service exposure type" "ClusterIP" "LoadBalancer" "NodePort"

if confirm "Dry run mode (generate config only)?" "n"; then
  DRY_RUN="true"
else
  DRY_RUN="false"
fi

if confirm "Enable License Proxy now?" "n"; then
  LICENSE_PROXY_ENABLED="true"
  prompt LICENSE_PROXY_MIN "License proxy nodegroup min size" "0"
  prompt LICENSE_PROXY_DESIRED "License proxy nodegroup desired size" "0"
  prompt LICENSE_PROXY_MAX "License proxy nodegroup max size" "2"
else
  LICENSE_PROXY_ENABLED="false"
fi

say "\nSummary"
say "Cluster: $CLUSTER_NAME"
say "Region: $AWS_REGION"
say "Deployment type: $DEPLOYMENT_TYPE"
say "Control plane nodegroup: min=$CONTROL_PLANE_MIN desired=$CONTROL_PLANE_DESIRED max=$CONTROL_PLANE_MAX"
say "Engine nodegroup: min=$ENGINE_MIN desired=$ENGINE_DESIRED max=$ENGINE_MAX"
say "API nodegroup: min=$API_MIN desired=$API_DESIRED max=$API_MAX"
say "Models: $MODEL_LINKS_LIST"
say "EFS: $EFS_MODE ${EFS_ID:-}"
say "Secrets: $SECRET_MODE"
say "Service: $SERVICE_TYPE"
say "Dry run: $DRY_RUN"
say "License Proxy: $LICENSE_PROXY_ENABLED"

confirm "Continue?" "y" || die "Aborted."

# ---------- provisioning ----------

WORKDIR="$(pwd)"
ARTIFACT_DIR="$WORKDIR/artifacts"
mkdir -p "$ARTIFACT_DIR"

say "\nCreating eksctl cluster config..."
CLUSTER_CONFIG="$ARTIFACT_DIR/cluster-config.yaml"

cat > "$CLUSTER_CONFIG" <<YAML
apiVersion: eksctl.io/v1alpha5
kind: ClusterConfig

metadata:
  name: ${CLUSTER_NAME}
  region: ${AWS_REGION}
  version: "${K8S_VERSION}"

iam:
  withOIDC: true
  serviceAccounts:
    - metadata:
        name: cluster-autoscaler-sa
        namespace: dg-self-hosted
      wellKnownPolicies:
        autoScaler: true
      roleName: cluster-autoscaler-role
      roleOnly: true
    - metadata:
        name: efs-csi-controller-sa
        namespace: kube-system
      wellKnownPolicies:
        efsCSIController: true
      roleName: efs-csi-driver-role
      roleOnly: true

managedNodeGroups:
  - name: control-plane-node-group
    minSize: ${CONTROL_PLANE_MIN}
    desiredCapacity: ${CONTROL_PLANE_DESIRED}
    maxSize: ${CONTROL_PLANE_MAX}
    instanceType: t3.large
    amiFamily: Ubuntu2204
    iam:
      withAddonPolicies:
        autoScaler: true
    propagateASGTags: true
  - name: engine-node-group
    minSize: ${ENGINE_MIN}
    desiredCapacity: ${ENGINE_DESIRED}
    maxSize: ${ENGINE_MAX}
    instanceType: g6.2xlarge
    amiFamily: AmazonLinux2023
    labels:
      k8s.deepgram.com/node-type: engine
      k8s.amazonaws.com/accelerator: nvidia-l4
    iam:
      withAddonPolicies:
        efs: true
        autoScaler: true
    taints:
      - key: efs.csi.aws.com/agent-not-ready
        value: "true"
        effect: NoExecute
    propagateASGTags: true
  - name: api-node-group
    minSize: ${API_MIN}
    desiredCapacity: ${API_DESIRED}
    maxSize: ${API_MAX}
    instanceType: c5n.xlarge
    amiFamily: AmazonLinux2023
    labels:
      k8s.deepgram.com/node-type: api
    iam:
      withAddonPolicies:
        autoScaler: true
    propagateASGTags: true
  - name: license-proxy-node-group
    minSize: ${LICENSE_PROXY_MIN}
    desiredCapacity: ${LICENSE_PROXY_DESIRED}
    maxSize: ${LICENSE_PROXY_MAX}
    instanceType: t3.large
    amiFamily: AmazonLinux2023
    labels:
      k8s.deepgram.com/node-type: license-proxy
    iam:
      withAddonPolicies:
        autoScaler: true
    propagateASGTags: true
YAML

say "Cluster config written to $CLUSTER_CONFIG"

if [[ "$DRY_RUN" == "true" ]]; then
  say "Dry run enabled. Generated official-style cluster config only."
  exit 0
fi

if confirm "Create EKS cluster now?" "y"; then
  eksctl create cluster -f "$CLUSTER_CONFIG"
else
  say "Skipping cluster creation. Ensure cluster exists before proceeding."
fi

say "\nSetting kubectl context..."
aws eks update-kubeconfig --name "$CLUSTER_NAME" --region "$AWS_REGION"

say "\nDiscovering VPC and subnet info..."
VPC_ID=$(aws eks describe-cluster --name "$CLUSTER_NAME" --region "$AWS_REGION" | jq -r '.cluster.resourcesVpcConfig.vpcId')
[[ -n "$VPC_ID" && "$VPC_ID" != "null" ]] || die "Could not determine VPC ID."
SUBNET_IDS=$(aws ec2 describe-subnets --filters "Name=vpc-id,Values=$VPC_ID" | jq -r '.Subnets[].SubnetId')
[[ -n "$SUBNET_IDS" ]] || die "Could not list subnets in VPC."

if [[ "$EFS_MODE" == "Create new EFS" ]]; then
  say "\nCreating EFS filesystem..."
  EFS_ID=$(aws efs create-file-system --performance-mode generalPurpose --throughput-mode bursting --region "$AWS_REGION" | jq -r '.FileSystemId')
  [[ -n "$EFS_ID" && "$EFS_ID" != "null" ]] || die "Failed to create EFS."

  say "Creating EFS mount targets..."
  for subnet in $SUBNET_IDS; do
    aws efs create-mount-target --file-system-id "$EFS_ID" --subnet-id "$subnet" >/dev/null
  done
fi

say "Using EFS ID: $EFS_ID"

say "\nInstalling EFS CSI driver addon..."
aws eks create-addon --cluster-name "$CLUSTER_NAME" --addon-name aws-efs-csi-driver --region "$AWS_REGION" >/dev/null 2>&1 || true
aws eks wait addon-active --cluster-name "$CLUSTER_NAME" --addon-name aws-efs-csi-driver --region "$AWS_REGION"

say "\nCreating namespace dg-self-hosted..."
kubectl create namespace dg-self-hosted >/dev/null 2>&1 || true
kubectl config set-context --current --namespace=dg-self-hosted >/dev/null

# ---------- secrets ----------

if [[ "$SECRET_MODE" == "Create in-cluster secrets" ]]; then
  say "\nCreating registry secret..."
  kubectl create secret docker-registry dg-regcred \
    --docker-server=quay.io \
    --docker-username="$DG_DOCKER_USER" \
    --docker-password="$DG_DOCKER_PASS" \
    --namespace dg-self-hosted \
    --dry-run=client -o yaml | kubectl apply -f -

  say "Creating API key secret..."
  kubectl create secret generic dg-self-hosted-api-key \
    --from-literal=deepgram-api-key="$DG_API_KEY" \
    --namespace dg-self-hosted \
    --dry-run=client -o yaml | kubectl apply -f -
else
  say "Skipping secret creation. Ensure secrets exist: dg-regcred, dg-self-hosted-api-key"
fi

# ---------- helm values ----------

say "\nPreparing Helm values..."
VALUES_FILE="$ARTIFACT_DIR/my-values.yaml"
MODEL_LINKS_JSON=$(json_array_from_space_list "$MODEL_LINKS_LIST")

cat > "$VALUES_FILE" <<YAML
global:
  pullSecretRef: dg-regcred
  deepgramSecretRef: dg-self-hosted-api-key

engine:
  modelManager:
    volumes:
      aws:
        efs:
          fileSystemId: ${EFS_ID}
    models:
      links: ${MODEL_LINKS_JSON}

scaling:
  replicas:
    api: ${API_DESIRED}
    engine: ${ENGINE_DESIRED}

service:
  type: ${SERVICE_TYPE}
YAML

if [[ "$LICENSE_PROXY_ENABLED" == "true" ]]; then
  cat >> "$VALUES_FILE" <<YAML

licenseProxy:
  enabled: true
YAML
fi

say "Values file written to $VALUES_FILE"

# ---------- deploy ----------

if confirm "Install/upgrade Deepgram Helm chart now?" "y"; then
  helm repo add deepgram https://deepgram.github.io/helm-charts/ >/dev/null 2>&1 || true
  helm repo update

  if helm status deepgram -n dg-self-hosted >/dev/null 2>&1; then
    say "Upgrading existing release..."
    helm upgrade deepgram deepgram/deepgram-self-hosted -f "$VALUES_FILE" -n dg-self-hosted --timeout 1h
  else
    say "Installing new release..."
    helm install deepgram deepgram/deepgram-self-hosted -f "$VALUES_FILE" -n dg-self-hosted --timeout 1h
  fi
fi

# ---------- smoke test ----------

if confirm "Run a basic smoke test?" "y"; then
  say "Running smoke test (this may take a few minutes)..."
  kubectl run dg-smoke --image=curlimages/curl:8.5.0 --restart=Never -n dg-self-hosted -- \
    sh -c "curl -s -o /tmp/audio.wav https://static.deepgram.com/examples/deep-learning-podcast.wav && \
    curl -s -X POST \"http://deepgram-api-external:8080/v1/listen\" \
      -H \"Authorization: Token ${DG_API_KEY:-REDACTED}\" \
      --data-binary @/tmp/audio.wav" || true
  kubectl logs dg-smoke -n dg-self-hosted || true
  kubectl delete pod dg-smoke -n dg-self-hosted >/dev/null 2>&1 || true
fi

say "\nDone. Artifacts are in $ARTIFACT_DIR"

say "\nNext steps:"
say "- Verify pods: kubectl get pods -n dg-self-hosted"
say "- If using LoadBalancer: kubectl get svc -n dg-self-hosted"
say "- Configure License Proxy for production"
