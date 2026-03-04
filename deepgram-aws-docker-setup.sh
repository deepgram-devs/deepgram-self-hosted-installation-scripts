#!/usr/bin/env bash
set -euo pipefail

SCRIPT_VERSION="0.2.0"
BASE_URL="https://raw.githubusercontent.com/deepgram/self-hosted-resources/refs/heads/main"
SKIP_EC2_PROVISION="false"

for arg in "$@"; do
  case "$arg" in
    --skip-ec2-provision)
      SKIP_EC2_PROVISION="true"
      ;;
    *)
      ;;
  esac
done

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

append_line_if_missing() {
  local file="$1"
  local line="$2"
  touch "$file"
  if ! grep -Fqx "$line" "$file"; then
    printf "\n%s\n" "$line" >> "$file"
  fi
}

persist_api_key_for_user() {
  local api_key="$1"
  local env_file="$HOME/.deepgram-self-hosted.env"
  local source_line='[ -f "$HOME/.deepgram-self-hosted.env" ] && . "$HOME/.deepgram-self-hosted.env"'

  cat > "$env_file" <<EOF_ENV_FILE
export DEEPGRAM_API_KEY="$api_key"
EOF_ENV_FILE
  chmod 600 "$env_file"

  append_line_if_missing "$HOME/.bashrc" "$source_line"
  append_line_if_missing "$HOME/.profile" "$source_line"
}

read_user_input() {
  local var_name="$1"
  local prompt_text="$2"
  local silent="${3:-false}"
  local input_value=""

  if [[ -r /dev/tty ]]; then
    printf "%s" "$prompt_text" > /dev/tty
    if [[ "$silent" == "true" ]]; then
      IFS= read -r -s input_value < /dev/tty || true
      printf "\n" > /dev/tty
    else
      IFS= read -r input_value < /dev/tty || true
    fi
  else
    if [[ "$silent" == "true" ]]; then
      IFS= read -r -s input_value || true
      printf "\n" >&2
    else
      IFS= read -r input_value || true
    fi
  fi

  printf -v "$var_name" "%s" "$input_value"
}

run_privileged() {
  if [[ "$EUID" -eq 0 ]]; then
    "$@"
  else
    require_cmd sudo
    sudo "$@"
  fi
}

confirm() {
  local prompt="$1"
  local default="$2" # y or n
  local response
  while true; do
    if [[ "$default" == "y" ]]; then
      read_user_input response "$prompt [Y/n]: "
      response="${response:-Y}"
    else
      read_user_input response "$prompt [y/N]: "
      response="${response:-N}"
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
  local default_value="${3:-}"
  local default_label="${4:-Default}"
  local value
  if [[ -n "$default_value" ]]; then
    read_user_input value "$prompt_text [$default_label: $default_value]: "
    value="${value:-$default_value}"
  else
    read_user_input value "$prompt_text: "
  fi
  printf -v "$var_name" "%s" "$value"
}

prompt_required() {
  local var_name="$1"
  local prompt_text="$2"
  local value
  while true; do
    read_user_input value "$prompt_text: "
    if [[ -n "$value" ]]; then
      printf -v "$var_name" "%s" "$value"
      return 0
    fi
    say "This value is required."
  done
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
      i=$((i + 1))
    done
    read_user_input choice "Select 1-${#choices[@]}: "
    if [[ "$choice" =~ ^[0-9]+$ ]] && (( choice >= 1 && choice <= ${#choices[@]} )); then
      printf -v "$var_name" "%s" "${choices[$((choice - 1))]}"
      return 0
    fi
    say "Invalid choice. Try again."
  done
}

prompt_hidden() {
  local var_name="$1"
  local prompt_text="$2"
  local value
  read_user_input value "$prompt_text: " "true"
  printf -v "$var_name" "%s" "$value"
}

download_file() {
  local url="$1"
  local out="$2"
  if command -v curl >/dev/null 2>&1; then
    curl -fsSL "$url" -o "$out"
    return 0
  fi
  if command -v wget >/dev/null 2>&1; then
    wget -qO "$out" "$url"
    return 0
  fi
  die "Need either curl or wget installed."
}

download_models() {
  local links_file="$1"
  local models_dir="$2"

  while IFS= read -r url || [[ -n "$url" ]]; do
    [[ -z "$url" ]] && continue
    [[ "$url" =~ ^# ]] && continue
    local name
    name="$(basename "$url")"
    say "Downloading $name"
    download_file "$url" "$models_dir/$name"
  done < "$links_file"
}

download_first_available() {
  local out="$1"
  shift
  local candidates=("$@")
  local candidate
  for candidate in "${candidates[@]}"; do
    if download_file "$BASE_URL/$candidate" "$out"; then
      say "Using template: $candidate"
      return 0
    fi
  done
  return 1
}

is_http_url() {
  local value="$1"
  [[ "$value" =~ ^https?:// ]]
}

nvidia_ready() {
  command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi >/dev/null 2>&1
}

trim_whitespace() {
  local value="$1"
  value="${value#"${value%%[![:space:]]*}"}"
  value="${value%"${value##*[![:space:]]}"}"
  printf "%s" "$value"
}

portable_sed_inplace() {
  local expression="$1"
  local file="$2"
  sed -i.bak "$expression" "$file"
  rm -f "${file}.bak"
}

upsert_toml_bool() {
  local file="$1"
  local section="$2"
  local key="$3"
  local value="$4" # true|false

  local tmp
  tmp="$(mktemp)"

  awk -v section="$section" -v key="$key" -v value="$value" '
    BEGIN {
      in_section = 0
      section_seen = 0
      key_set = 0
    }
    {
      line = $0
      if (line ~ /^[[:space:]]*\[[^]]+\][[:space:]]*$/) {
        if (in_section && !key_set) {
          print key " = " value
          key_set = 1
        }
        in_section = 0
        if (line == "[" section "]") {
          in_section = 1
          section_seen = 1
        }
      }

      if (in_section && line ~ "^[[:space:]]*" key "[[:space:]]*=") {
        print key " = " value
        key_set = 1
        next
      }

      print line
    }
    END {
      if (in_section && !key_set) {
        print key " = " value
        key_set = 1
      }
      if (!section_seen) {
        print ""
        print "[" section "]"
        print key " = " value
      }
    }
  ' "$file" > "$tmp"

  mv "$tmp" "$file"
}

upsert_engine_env_var() {
  local compose_file="$1"
  local key="$2"
  local value="$3"
  local tmp
  tmp="$(mktemp)"

  awk -v target_key="$key" -v target_val="$value" '
    BEGIN {
      in_engine = 0
      in_engine_env = 0
      inserted = 0
      key_seen = 0
    }
    {
      line = $0

      if (line ~ /^  [a-zA-Z0-9_-]+:[[:space:]]*$/) {
        if (in_engine_env && !inserted && !key_seen) {
          print "      " target_key ": \"" target_val "\""
          inserted = 1
        }
        in_engine = (line ~ /^  engine:[[:space:]]*$/)
        in_engine_env = 0
      }

      if (in_engine && line ~ /^    environment:[[:space:]]*$/) {
        in_engine_env = 1
      } else if (in_engine_env && line ~ /^    [a-zA-Z0-9_-]+:[[:space:]]*$/) {
        if (!inserted && !key_seen) {
          print "      " target_key ": \"" target_val "\""
          inserted = 1
        }
        in_engine_env = 0
      }

      if (in_engine_env && line ~ "^[[:space:]]{6}" target_key ":[[:space:]]*") {
        print "      " target_key ": \"" target_val "\""
        key_seen = 1
        next
      }

      print line
    }
    END {
      if (in_engine_env && !inserted && !key_seen) {
        print "      " target_key ": \"" target_val "\""
      }
    }
  ' "$compose_file" > "$tmp"

  mv "$tmp" "$compose_file"
}

compose_up_cmd() {
  local runtime="$1"
  if [[ "$runtime" == "docker" ]]; then
    if docker compose version >/dev/null 2>&1; then
      echo "docker compose"
      return 0
    fi
    if command -v docker-compose >/dev/null 2>&1; then
      echo "docker-compose"
      return 0
    fi
    die "Docker detected but neither 'docker compose' nor 'docker-compose' is available."
  fi

  if command -v podman-compose >/dev/null 2>&1; then
    echo "podman-compose"
    return 0
  fi
  die "Podman detected but 'podman-compose' is missing."
}

detect_ubuntu() {
  [[ -f /etc/os-release ]] || return 1
  # shellcheck disable=SC1091
  source /etc/os-release
  [[ "${ID:-}" == "ubuntu" ]]
}

install_runtime_ubuntu() {
  local runtime="$1"
  run_privileged apt-get update
  run_privileged apt-get install -y ca-certificates curl wget gnupg lsb-release jq

  if [[ "$runtime" == "docker" ]]; then
    run_privileged apt-get install -y docker.io
    if run_privileged apt-get install -y docker-compose-v2; then
      say "Installed Docker Compose v2 package (docker-compose-v2)."
    elif run_privileged apt-get install -y docker-compose-plugin; then
      say "Installed Docker Compose v2 package (docker-compose-plugin)."
    else
      say "Docker Compose v2 package unavailable; falling back to docker-compose v1."
      run_privileged apt-get install -y docker-compose
    fi
    run_privileged systemctl enable --now docker
    local target_user="${SUDO_USER:-$USER}"
    run_privileged usermod -aG docker "$target_user" || true
  else
    run_privileged apt-get install -y podman podman-compose
  fi
}

install_nvidia_stack_ubuntu() {
  local runtime="$1"

  run_privileged apt-get update
  run_privileged apt-get install -y ubuntu-drivers-common pciutils

  if ! command -v nvidia-smi >/dev/null 2>&1; then
    say "Installing NVIDIA drivers using ubuntu-drivers autoinstall..."
    run_privileged ubuntu-drivers autoinstall || true
  fi

  run_privileged mkdir -p /usr/share/keyrings
  if command -v curl >/dev/null 2>&1; then
    curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
      | run_privileged gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
    curl -fsSL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
      | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
      | run_privileged tee /etc/apt/sources.list.d/nvidia-container-toolkit.list >/dev/null
  else
    die "curl is required to configure NVIDIA container toolkit repository."
  fi

  run_privileged apt-get update
  run_privileged apt-get install -y nvidia-container-toolkit

  if [[ "$runtime" == "docker" ]] && command -v docker >/dev/null 2>&1; then
    run_privileged nvidia-ctk runtime configure --runtime=docker
    run_privileged systemctl restart docker
  elif [[ "$runtime" == "podman" ]] && command -v podman >/dev/null 2>&1; then
    run_privileged mkdir -p /etc/cdi
    run_privileged nvidia-ctk cdi generate --output=/etc/cdi/nvidia.yaml || true
  fi

  if command -v nvidia-smi >/dev/null 2>&1; then
    say "NVIDIA driver check:"
    nvidia-smi | head -n 5 || true
  else
    say "NVIDIA driver still not active. A reboot is usually required after driver install."
  fi
}

get_default_ubuntu_ami() {
  local region="$1"
  aws ssm get-parameter \
    --name /aws/service/canonical/ubuntu/server/22.04/stable/current/amd64/hvm/ebs-gp3/ami-id \
    --region "$region" \
    --query 'Parameter.Value' \
    --output text
}

get_latest_noble24_gp3_ami() {
  local region="$1"
  aws ec2 describe-images \
    --owners 099720109477 \
    --region "$region" \
    --filters \
      "Name=name,Values=ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-amd64-server-*" \
      "Name=state,Values=available" \
    --query 'sort_by(Images,&CreationDate)[-1].ImageId' \
    --output text
}

get_latest_ubuntu_ami_by_name() {
  local region="$1"
  local series="$2" # jammy|noble
  aws ec2 describe-images \
    --owners 099720109477 \
    --region "$region" \
    --filters \
      "Name=name,Values=ubuntu/images/hvm-ssd*/ubuntu-$series-*-amd64-server-*" \
      "Name=state,Values=available" \
    --query 'sort_by(Images,&CreationDate)[-1].ImageId' \
    --output text
}

resolve_default_ubuntu_ami() {
  local region="$1"
  local ami

  # Prefer Ubuntu 24.04 Noble gp3 AMIs first.
  ami="$(get_latest_noble24_gp3_ami "$region" 2>/dev/null || true)"
  if [[ -n "$ami" && "$ami" != "None" ]]; then
    echo "$ami"
    return 0
  fi

  ami="$(get_default_ubuntu_ami "$region" 2>/dev/null || true)"
  if [[ -n "$ami" && "$ami" != "None" ]]; then
    echo "$ami"
    return 0
  fi

  ami="$(get_latest_ubuntu_ami_by_name "$region" "jammy" 2>/dev/null || true)"
  if [[ -n "$ami" && "$ami" != "None" ]]; then
    echo "$ami"
    return 0
  fi

  ami="$(get_latest_ubuntu_ami_by_name "$region" "noble" 2>/dev/null || true)"
  if [[ -n "$ami" && "$ami" != "None" ]]; then
    echo "$ami"
    return 0
  fi

  return 1
}

provision_ec2_instance() {
  require_cmd aws
  require_cmd ssh
  require_cmd scp

  local region
  prompt region "AWS region" "us-east-2"

  aws sts get-caller-identity >/dev/null 2>&1 || die "AWS credentials are not configured."

  local key_mode key_name key_path
  prompt_choice key_mode "EC2 key pair" "Use existing key pair" "Create new key pair"

  if [[ "$key_mode" == "Use existing key pair" ]]; then
    prompt_required key_name "Existing key pair name"
    prompt_required key_path "Local path to PEM private key"
    [[ -f "$key_path" ]] || die "PEM file not found: $key_path"
  else
    prompt key_name "New key pair name" "deepgram-self-hosted-$(date +%Y%m%d%H%M%S)"
    prompt key_path "Where to save PEM file" "./${key_name}.pem"
    aws ec2 create-key-pair --key-name "$key_name" --region "$region" --query 'KeyMaterial' --output text > "$key_path"
    chmod 400 "$key_path"
    say "Created key pair and wrote $key_path"
  fi

  local sg_mode sg_id vpc_id sg_name ssh_cidr
  prompt_choice sg_mode "Security group" "Use existing security group" "Create new security group"
  if [[ "$sg_mode" == "Use existing security group" ]]; then
    prompt_required sg_id "Existing security group ID (sg-...)"
  else
    vpc_id="$(aws ec2 describe-vpcs --region "$region" --filters Name=isDefault,Values=true --query 'Vpcs[0].VpcId' --output text)"
    prompt vpc_id "VPC ID" "$vpc_id" "Auto-detected"
    prompt sg_name "Security group name" "deepgram-self-hosted-sg"
    sg_id="$(aws ec2 create-security-group --group-name "$sg_name" --description "Deepgram self-hosted access" --vpc-id "$vpc_id" --region "$region" --query GroupId --output text)"
    prompt ssh_cidr "SSH ingress CIDR" "0.0.0.0/0"
    aws ec2 authorize-security-group-ingress --group-id "$sg_id" --protocol tcp --port 22 --cidr "$ssh_cidr" --region "$region" || true
    if confirm "Open HTTPS (443) to the internet?" "n"; then
      aws ec2 authorize-security-group-ingress --group-id "$sg_id" --protocol tcp --port 443 --cidr 0.0.0.0/0 --region "$region" || true
    fi
  fi

  local ami_default ami_id instance_type disk_size instance_name
  ami_default="$(resolve_default_ubuntu_ami "$region" 2>/dev/null || true)"
  if [[ -n "$ami_default" ]]; then
    prompt ami_id "Ubuntu AMI ID" "$ami_default" "Auto-detected"
  else
    prompt_required ami_id "AMI ID (could not auto-detect; enter manually)"
  fi

  prompt instance_type "Instance type" "g6.2xlarge"
  prompt disk_size "Root volume size (GiB)" "200"
  prompt instance_name "Instance Name tag" "deepgram-self-hosted"

  local instance_id
  instance_id="$(aws ec2 run-instances \
    --region "$region" \
    --image-id "$ami_id" \
    --instance-type "$instance_type" \
    --key-name "$key_name" \
    --security-group-ids "$sg_id" \
    --block-device-mappings "[{\"DeviceName\":\"/dev/sda1\",\"Ebs\":{\"VolumeSize\":$disk_size,\"VolumeType\":\"gp3\",\"DeleteOnTermination\":true}}]" \
    --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=$instance_name}]" \
    --query 'Instances[0].InstanceId' \
    --output text)"

  say "Launched EC2 instance: $instance_id"
  say "Waiting for instance to enter running state..."
  aws ec2 wait instance-running --region "$region" --instance-ids "$instance_id"

  local public_dns public_ip
  public_dns="$(aws ec2 describe-instances --region "$region" --instance-ids "$instance_id" --query 'Reservations[0].Instances[0].PublicDnsName' --output text)"
  public_ip="$(aws ec2 describe-instances --region "$region" --instance-ids "$instance_id" --query 'Reservations[0].Instances[0].PublicIpAddress' --output text)"

  local ssh_user
  prompt ssh_user "SSH user" "ubuntu"

  say ""
  say "EC2 is ready."
  say "Instance ID: $instance_id"
  say "Public DNS: $public_dns"
  say "Public IP: $public_ip"
  say "SSH command: ssh -i $key_path $ssh_user@$public_dns"

  if confirm "Copy this script to the instance and start setup over SSH now?" "y"; then
    local remote_script="~/deepgram-aws-docker-setup.sh"
    scp -i "$key_path" -o StrictHostKeyChecking=accept-new "$0" "$ssh_user@$public_dns:$remote_script"
    ssh -i "$key_path" -o StrictHostKeyChecking=accept-new "$ssh_user@$public_dns" \
      "chmod +x $remote_script && $remote_script --skip-ec2-provision"
  else
    say "Run this next:"
    say "scp -i $key_path $0 $ssh_user@$public_dns:~/deepgram-aws-docker-setup.sh"
    say "ssh -i $key_path $ssh_user@$public_dns 'chmod +x ~/deepgram-aws-docker-setup.sh && ~/deepgram-aws-docker-setup.sh --skip-ec2-provision'"
  fi
}

main() {
  say "Deepgram Self-Hosted AWS Docker/Podman Setup (v$SCRIPT_VERSION)"

  require_cmd sed
  require_cmd awk
  require_cmd grep
  require_cmd basename
  require_cmd dirname

  if [[ "$SKIP_EC2_PROVISION" != "true" ]]; then
    local run_mode
    prompt_choice run_mode "Where are you running this script?" "On an EC2 host" "On my local machine (provision EC2 first)"
    if [[ "$run_mode" == "On my local machine (provision EC2 first)" ]]; then
      provision_ec2_instance
      return 0
    fi
  fi

  local runtime
  prompt_choice runtime "Select container runtime" "docker" "podman"

  if ! command -v "$runtime" >/dev/null 2>&1; then
    say "$runtime is not installed."
    if confirm "Install $runtime packages now? (Ubuntu only)" "y"; then
      detect_ubuntu || die "Automatic runtime installation currently supports Ubuntu only."
      install_runtime_ubuntu "$runtime"
      say "Runtime installation completed."
    else
      die "Install $runtime and rerun."
    fi
  fi

  local compose_cmd
  compose_cmd="$(compose_up_cmd "$runtime")"
  if [[ "$runtime" == "docker" && "$compose_cmd" == "docker-compose" ]]; then
    if detect_ubuntu && confirm "Install Docker Compose v2 plugin now?" "y"; then
      run_privileged apt-get update
      run_privileged apt-get install -y docker-compose-v2 || run_privileged apt-get install -y docker-compose-plugin || true
      compose_cmd="$(compose_up_cmd "$runtime")"
    fi
  fi
  local docker_daemon_prefix=""
  if [[ "$runtime" == "docker" ]] && ! docker info >/dev/null 2>&1; then
    docker_daemon_prefix="sudo "
    say "Docker daemon requires elevated access in this session; using sudo for compose/ps."
  fi

  if nvidia_ready; then
    say "NVIDIA driver check:"
    nvidia-smi | head -n 5 || true
  else
    if command -v nvidia-smi >/dev/null 2>&1; then
      say "Warning: nvidia-smi exists but NVIDIA driver is not loaded yet."
    else
      say "Warning: nvidia-smi not found."
    fi
    if confirm "Install NVIDIA drivers + container toolkit now? (Ubuntu only)" "y"; then
      detect_ubuntu || die "Automatic NVIDIA setup currently supports Ubuntu only."
      install_nvidia_stack_ubuntu "$runtime"
    else
      say "Continuing without installing NVIDIA stack. GPU inference may fail."
    fi
  fi

  local deploy_type
  prompt_choice deploy_type "Choose deployment type" "standard" "license-proxy"

  local model_profile
  prompt_choice model_profile "Choose primary model profile" "nova-3" "flux" "aura-2"

  local aura_variant="none"
  local aura_t2c_uuid=""
  local aura_c2a_uuid=""
  local aura_batch_size=""
  local aura_cuda_devices=""
  if [[ "$model_profile" == "aura-2" ]]; then
    prompt_choice aura_variant "Aura-2 language profile" "en" "es" "polyglot"
    case "$aura_variant" in
      "en")
        aura_t2c_uuid="15ef8614-52cb-4cd3-a641-d68249c15d53"
        aura_c2a_uuid="2e5096c7-7bf1-435e-bbdd-f673f88d0ebd"
        ;;
      "es")
        aura_t2c_uuid="5d53d105-c6a4-47f5-b670-61adb6e8a880"
        aura_c2a_uuid="4d5c93ad-9e20-4ebf-a1f0-0fb88ac73ef5"
        ;;
      "polyglot")
        aura_t2c_uuid="04975889-c601-4f80-a02f-0f2f9c22deaf"
        aura_c2a_uuid="9e94567e-11e7-4619-adbc-d28212194367"
        ;;
    esac
    prompt aura_batch_size "Aura-2 max batch size" "8"
    prompt aura_cuda_devices "CUDA_VISIBLE_DEVICES for Aura-2 engine" "0"
    if [[ "$deploy_type" == "license-proxy" ]]; then
      say "Aura-2 currently uses standard deployment templates; overriding deployment type to standard."
      deploy_type="standard"
    fi
  fi
  if [[ "$model_profile" == "flux" ]]; then
    say "Flux note: run Flux on a dedicated host/instance with no other STT/TTS models."
  fi

  local project_dir
  prompt project_dir "Project directory" "$HOME/deepgram-self-hosted"
  mkdir -p "$project_dir/config" "$project_dir/models"

  local api_key
  prompt_required api_key "Enter DEEPGRAM_API_KEY (self-hosted API key)"
  if confirm "Persist DEEPGRAM_API_KEY for this user shell sessions?" "n"; then
    persist_api_key_for_user "$api_key"
    say "Saved DEEPGRAM_API_KEY to $HOME/.deepgram-self-hosted.env and linked it from shell profile files."
  fi

  if confirm "Log in to quay.io now?" "y"; then
    local quay_user quay_pass
    prompt_required quay_user "Quay username"
    prompt_hidden quay_pass "Quay password"
    if [[ "$runtime" == "docker" ]]; then
      printf "%s" "$quay_pass" | ${docker_daemon_prefix}docker login quay.io -u "$quay_user" --password-stdin
    else
      printf "%s" "$quay_pass" | podman login quay.io -u "$quay_user" --password-stdin
    fi
  fi

  local compose_src
  if [[ "$model_profile" == "aura-2" ]]; then
    if [[ "$aura_variant" == "polyglot" ]]; then
      compose_src="docker/docker-compose.aura-2-polyglot.yml"
    else
      compose_src="docker/docker-compose.aura-2.yml"
    fi
  elif [[ "$runtime" == "docker" ]]; then
    compose_src="docker/docker-compose.$deploy_type.yml"
  else
    compose_src="podman/podman-compose.$deploy_type.yml"
  fi

  local deploy_dir deploy_prefix
  deploy_dir="${deploy_type//-/_}_deploy"
  deploy_prefix="$project_dir/config"

  say "Downloading Deepgram templates..."
  download_file "$BASE_URL/$compose_src" "$deploy_prefix/compose.yml"

  local api_template engine_template
  case "$model_profile" in
    "nova-3"|"flux")
      api_template="common/$deploy_dir/api.toml"
      engine_template="common/$deploy_dir/engine.toml"
      ;;
    "aura-2")
      if [[ "$aura_variant" == "en" ]]; then
        api_template="common/standard_deploy/api.aura-2-en.toml"
        engine_template="common/standard_deploy/engine.aura-2-en.toml"
      elif [[ "$aura_variant" == "es" ]]; then
        api_template="common/standard_deploy/api.aura-2-es.toml"
        engine_template="common/standard_deploy/engine.aura-2-es.toml"
      else
        # Polyglot naming can vary by release; try common variants.
        if ! download_first_available "$deploy_prefix/api.toml" \
          "common/standard_deploy/api.aura-2-polyglot.toml" \
          "common/standard_deploy/api.aura-2-multilingual.toml"; then
          die "Could not find Aura-2 polyglot API TOML template in self-hosted-resources."
        fi
        if ! download_first_available "$deploy_prefix/engine.toml" \
          "common/standard_deploy/engine.aura-2-polyglot.toml" \
          "common/standard_deploy/engine.aura-2-multilingual.toml"; then
          die "Could not find Aura-2 polyglot Engine TOML template in self-hosted-resources."
        fi
      fi
      ;;
  esac

  if [[ -n "${api_template:-}" ]]; then
    download_file "$BASE_URL/$api_template" "$deploy_prefix/api.toml"
  fi
  if [[ -n "${engine_template:-}" ]]; then
    download_file "$BASE_URL/$engine_template" "$deploy_prefix/engine.toml"
  fi

  if [[ "$deploy_type" == "license-proxy" ]]; then
    download_file "$BASE_URL/common/$deploy_dir/license-proxy.toml" "$deploy_prefix/license-proxy.toml"
  fi

  portable_sed_inplace 's#/path/to/\(api\|engine\|license-proxy\).toml#./\1.toml#g' "$deploy_prefix/compose.yml"
  portable_sed_inplace 's#/path/to/models#../models#g' "$deploy_prefix/compose.yml"

  if [[ "$model_profile" == "aura-2" ]]; then
    upsert_engine_env_var "$deploy_prefix/compose.yml" "IMPELLER_AURA2_MAX_BATCH_SIZE" "$aura_batch_size"
    upsert_engine_env_var "$deploy_prefix/compose.yml" "IMPELLER_AURA2_T2C_UUID" "$aura_t2c_uuid"
    upsert_engine_env_var "$deploy_prefix/compose.yml" "IMPELLER_AURA2_C2A_UUID" "$aura_c2a_uuid"
    upsert_engine_env_var "$deploy_prefix/compose.yml" "CUDA_VISIBLE_DEVICES" "$aura_cuda_devices"
    say "Enabled Aura-2 engine env vars in compose.yml for $aura_variant profile."
  fi

  if [[ "$model_profile" == "flux" ]]; then
    upsert_toml_bool "$deploy_prefix/api.toml" "features" "listen_v2" "true"
    upsert_toml_bool "$deploy_prefix/engine.toml" "flux" "enabled" "true"
    say "Enabled Flux settings in TOML: [features].listen_v2=true and [flux].enabled=true"
  fi

  local model_source links_file
  prompt model_source "Model URL source (file path or direct URL(s), comma-separated)" "$project_dir/model_links.txt"

  if [[ -f "$model_source" ]]; then
    links_file="$model_source"
  else
    local inline_links_file
    inline_links_file="$project_dir/model_links.inline.txt"
    : > "$inline_links_file"

    local token raw
    IFS=',' read -r -a raw <<< "$model_source"
    for token in "${raw[@]}"; do
      token="$(trim_whitespace "$token")"
      [[ -z "$token" ]] && continue
      if ! is_http_url "$token"; then
        die "Model source is not a file and includes a non-URL token: $token"
      fi
      printf "%s\n" "$token" >> "$inline_links_file"
    done

    if [[ ! -s "$inline_links_file" ]]; then
      die "No model URLs provided. Supply a file path or URL(s)."
    fi
    links_file="$inline_links_file"
  fi

  download_models "$links_file" "$project_dir/models"

  cat > "$deploy_prefix/.env" <<EOF_ENV
DEEPGRAM_API_KEY=$api_key
EOF_ENV
  chmod 600 "$deploy_prefix/.env"

  say ""
  say "Generated files:"
  say "  $deploy_prefix/compose.yml"
  say "  $deploy_prefix/api.toml"
  say "  $deploy_prefix/engine.toml"
  if [[ "$deploy_type" == "license-proxy" ]]; then
    say "  $deploy_prefix/license-proxy.toml"
  fi
  say "  $deploy_prefix/.env"
  say "  $project_dir/models/"

  if ! nvidia_ready; then
    say ""
    say "NVIDIA driver is not active yet. Starting engine will fail until GPU driver loads."
    if confirm "Reboot now to load NVIDIA kernel modules?" "y"; then
      say "Rebooting now. Reconnect and rerun:"
      say "  $0 --skip-ec2-provision"
      run_privileged reboot
      exit 0
    fi
    if ! confirm "Continue anyway (engine container is likely to fail)?" "n"; then
      say "Stopping before container startup. Reboot and rerun:"
      say "  $0 --skip-ec2-provision"
      exit 0
    fi
  fi

  if ! confirm "Start Deepgram containers now?" "y"; then
    say "Setup complete. Start later with:"
    say "cd \"$deploy_prefix\" && set -a && source .env && set +a && ${docker_daemon_prefix}$compose_cmd -f compose.yml up -d"
    exit 0
  fi

  (
    cd "$deploy_prefix"
    set -a
    source .env
    set +a
    ${docker_daemon_prefix}$compose_cmd -f compose.yml up -d
  )

  say ""
  say "Containers started. Current status:"
  if [[ "$runtime" == "docker" ]]; then
    ${docker_daemon_prefix}docker ps --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}'
  else
    podman ps --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}'
  fi

  say "Done."
}

main "$@"
