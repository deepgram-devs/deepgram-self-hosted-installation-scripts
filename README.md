# Deepgram AWS Self-Hosted Setup Script

Interactive setup for running Deepgram self-hosted services on AWS EC2 using Docker/Podman.

Primary script: `deepgram-aws-docker-setup.sh`

## What This Script Does

- Optional EC2 provisioning from local machine:
  - Create or use existing key pair
  - Create or use existing security group
  - Launch instance with auto-detected Ubuntu AMI
  - Copy script to EC2 and continue remotely
- Host bootstrap on Ubuntu EC2:
  - Installs Docker or Podman (interactive choice)
  - Prefers Docker Compose v2 (`docker compose`)
  - Installs NVIDIA drivers + NVIDIA container toolkit (optional)
  - Detects when reboot is required for GPU driver activation
- Deepgram deployment setup:
  - Supports `standard` and `license-proxy` deployment types
  - Supports model profiles: `nova-3`, `flux`, `aura-2`
  - Aura-2 variants: `en`, `es`, `polyglot`
  - Uses Aura-2 specific compose/TOML templates when selected
  - For Flux, enables required TOML flags automatically
  - Downloads `.dg` model files from comma-separated URLs or URL-file input
  - Writes config files and starts containers
- Optional API key persistence:
  - Saves `DEEPGRAM_API_KEY` to `~/.deepgram-self-hosted.env`
  - Adds source lines to shell startup files

## Repository Contents

- `deepgram-aws-docker-setup.sh` - main interactive AWS Docker/Podman setup script
- `deepgram-eks-setup.sh` - WIP

## Requirements

- For local provisioning mode:
  - AWS CLI configured (`aws sts get-caller-identity` works)
  - SSH + SCP installed
- For EC2 host mode:
  - Ubuntu recommended (automation is Ubuntu-focused)
  - `sudo` privileges
  - Internet egress to pull packages/images/models
- Deepgram credentials:
  - Quay credentials with access to required self-hosted images
  - Deepgram self-hosted API key
  - Model `.dg` URLs or a txt file containing model links

## Usage

### 1) Run directly on an EC2 host

```bash
chmod +x ./deepgram-aws-docker-setup.sh
./deepgram-aws-docker-setup.sh --skip-ec2-provision
```

### 2) Run from local machine (provision EC2 first)

```bash
chmod +x ./deepgram-aws-docker-setup.sh
./deepgram-aws-docker-setup.sh
```

Choose:
- `On my local machine (provision EC2 first)` to create/use key pair, security group, and instance
- then copy + execute remotely automatically or manually

## Prompt Tips

- Model URL input accepts:
  - Path to a local file (one URL per line), or
  - Direct comma-separated S3 URLs
- Defaults are shown as `Default: ...`
- Auto-discovered values are shown as `Auto-detected: ...`

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
Or run commands with `sudo` in current session.

### NVIDIA driver not loaded / NVML errors

Symptoms:
- `NVIDIA-SMI has failed because it couldn't communicate with the NVIDIA driver`
- engine startup fails with NVML/driver errors

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
sudo docker logs --tail 200 config-api-1
sudo docker logs --tail 200 config-engin-1
sudo docker logs --tail 200 config-license-proxy-1
```

## Notes

- Script automation is tuned for Ubuntu; other distros may require manual package/runtime adjustments.
