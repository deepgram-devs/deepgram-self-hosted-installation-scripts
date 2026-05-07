# Deepgram Docker on AWS EC2

Interactive setup for running Deepgram self-hosted services on AWS EC2 using Docker or Podman.

Primary script: `deepgram-aws-docker-setup.sh`

## What This Script Does

- Optional EC2 provisioning from local machine:
  - Detects macOS and goes straight to EC2 provisioning
  - Create a key pair or select an existing key pair from the chosen AWS region
  - Create, select, or reuse an existing security group
  - Launch instance with auto-detected Ubuntu AMI
  - Copy script to EC2 and continue remotely
- Host bootstrap on Ubuntu EC2:
  - Installs Docker or Podman
  - Prefers Docker Compose v2 (`docker compose`)
  - Installs NVIDIA drivers and NVIDIA container toolkit when requested
  - Detects when reboot is required for GPU driver activation
- Deepgram deployment setup:
  - Supports `standard` and `license-proxy` deployment types
  - Supports model profiles: `nova-3`, `flux`, `aura-2`
  - Supports Aura-2 variants: `en`, `es`, `polyglot`
  - Uses Aura-2 specific compose/TOML templates when selected
  - Enables required Flux TOML flags automatically
  - Downloads `.dg` model files from comma-separated URLs or URL-file input
  - Writes config files and starts containers
- Optional API key persistence:
  - Saves `DEEPGRAM_API_KEY` to `~/.deepgram-self-hosted.env`
  - Adds source lines to shell startup files

## Requirements

- For local provisioning mode:
  - [AWS CLI](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html) configured (`aws sts get-caller-identity` works)
  - [OpenSSH](https://www.openssh.com/manual.html) tools: `ssh` and `scp`
- Deepgram credentials:
  - [Self Hosted Quay.io](https://developers.deepgram.com/docs/self-hosted-self-service-tutorial#create-container-image-distribution-credentials) credentials with access to required self-hosted images
  - [Deepgram self-hosted API key](https://developers.deepgram.com/docs/self-hosted-self-service-tutorial#create-a-self-hosted-api-key)
  - Model `.dg` URLs

## Usage

Run directly on an EC2 host:

```bash
cd docker/aws
chmod +x ./deepgram-aws-docker-setup.sh
./deepgram-aws-docker-setup.sh --skip-ec2-provision
```

Run from a local machine and provision EC2 first:

```bash
cd docker/aws
chmod +x ./deepgram-aws-docker-setup.sh
./deepgram-aws-docker-setup.sh
```

Choose `On my local machine (provision EC2 first)` to create or use a key pair, security group, and instance. The script can then copy itself to the EC2 host and continue remotely.

On macOS, the script skips the EC2-host choice because host setup requires Ubuntu with NVIDIA drivers. Running with `--skip-ec2-provision` on macOS fails fast with a message to provision EC2 instead.

## Prompt Tips

- Model URL input accepts a local file path with one URL per line.
- Model URL input also accepts direct comma-separated URLs.
- Defaults are shown as `Default: ...`.
- Auto-discovered values are shown as `Auto-detected: ...`.
- Existing EC2 key pairs and security groups are listed from the AWS region and VPC you select.
- If the default security group name already exists, you can reuse it, enter a different name, or choose another existing group.

## Troubleshooting

### Docker compose command issues

Symptom:
- `unknown shorthand flag: 'f' in -f` when running `docker compose -f ...`

Cause:
- Compose v2 plugin missing; host only has `docker-compose` v1.

Fix:

```bash
sudo apt-get update
sudo apt-get install -y docker-compose-v2
docker compose version
```

### Docker daemon permission denied

Symptom:
- `PermissionError: [Errno 13] Permission denied` for `/var/run/docker.sock`

Fix:

```bash
sudo usermod -aG docker "$USER"
newgrp docker
```

Or run commands with `sudo` in the current session.

### NVIDIA driver not loaded / NVML errors

Symptoms:
- `NVIDIA-SMI has failed because it couldn't communicate with the NVIDIA driver`
- Engine startup fails with NVML/driver errors

Fix:

```bash
sudo reboot
```

Then verify:

```bash
nvidia-smi
```

### Aura-2 not serving traffic

Symptom:
- `Engine not configured to serve Aura-2 traffic`

Checks:
- Ensure Aura-2 compose template is used (`docker-compose.aura-2*.yml`)
- Ensure Aura-2 UUID env vars are present under `services.engine.environment`
- Ensure selected Aura-2 model files match variant (`en`, `es`, or `polyglot`)

### API 401 responses

Symptom:
- API requests return `401 Unauthorized`

Fix:
- Include `Authorization: Token $DEEPGRAM_API_KEY` in manual requests
- Ensure `.env` and/or `~/.deepgram-self-hosted.env` contains a valid key

### License proxy connection refused

Symptom:
- API warns about failed license proxy connection

Checks:
- `license-proxy` container is running
- `license-proxy.toml` and API config use matching protocol/port
- Internal service endpoint is reachable from API container

Useful commands:

```bash
sudo docker compose -f /home/ubuntu/deepgram-self-hosted/config/compose.yml ps
sudo docker logs --tail 200 config_api_1
sudo docker logs --tail 200 config_engine_1
sudo docker logs --tail 200 config_license-proxy_1
```

## Notes

- `g6.2xlarge` has 1 GPU (NVIDIA L4). Use `CUDA_VISIBLE_DEVICES="0"`.
- Script automation is tuned for Ubuntu; other distros may require manual package/runtime adjustments.
