# Copilot Usage Advanced Dashboard — Standalone Enterprise VM Deployment Guide

This guide covers deploying the Copilot Usage Advanced Dashboard on a Linux VM for **Standalone (Enterprise)** Copilot setups where you only have an enterprise slug (no individual organization).

---

## Quick Install (One Command)

SSH into your VM and run:

```bash
curl -sL https://raw.githubusercontent.com/AkashAi7/copilot-usage-advanced-dashboard/main/scripts/install.sh | bash
```

Or if you already cloned the repo:

```bash
cd ~/copilot-usage-advanced-dashboard
GITHUB_PAT=ghp_your_token ORGANIZATION_SLUGS=standalone:your-enterprise-slug bash scripts/install.sh
```

The script will:
- Install Docker and dependencies
- Configure kernel settings
- Write a production-ready `docker-compose.yml` (Grafana on port **3000**)
- Start all containers and wait for data collection
- Verify all 7 indexes are populated
- Print the Grafana URL with credentials

If you prefer manual setup, follow the steps below.

---

## Table of Contents

- [Prerequisites](#prerequisites)
- [Architecture](#architecture)
- [Step 1 — Provision a Linux VM](#step-1--provision-a-linux-vm)
- [Step 2 — SSH into the VM](#step-2--ssh-into-the-vm)
- [Step 3 — Install Docker](#step-3--install-docker)
- [Step 4 — Configure Kernel Settings](#step-4--configure-kernel-settings)
- [Step 5 — Clone the Repository](#step-5--clone-the-repository)
- [Step 6 — Create the .env File](#step-6--create-the-env-file)
- [Step 7 — Create Dashboard Placeholder](#step-7--create-dashboard-placeholder)
- [Step 8 — Open Firewall Port](#step-8--open-firewall-port)
- [Step 9 — Start the Stack](#step-9--start-the-stack)
- [Step 10 — Verify the Run](#step-10--verify-the-run)
- [Step 11 — Verify Elasticsearch Indexes](#step-11--verify-elasticsearch-indexes)
- [Step 12 — Access Grafana](#step-12--access-grafana)
- [Networking — Azure NSG Configuration](#networking--azure-nsg-configuration)
- [Troubleshooting](#troubleshooting)

---

## Prerequisites

| Requirement | Details |
|---|---|
| **VM** | Ubuntu 22.04+ recommended, 16 GB RAM, 2+ vCPUs |
| **GitHub PAT** | Personal Access Token with `manage_billing:copilot`, `read:enterprise`, `read:org` scopes. [Create Token](https://github.com/settings/tokens) |
| **Enterprise Slug** | Your GitHub Enterprise slug (from `github.com/enterprises/<slug>`) |
| **Ports** | `3000` (Grafana) and `22` (SSH) must be open |

---

## Architecture

```
┌─────────────────────────────────────────────────────┐
│                     Linux VM                        │
│                                                     │
│  ┌──────────────┐  ┌──────────┐  ┌──────────────┐  │
│  │Elasticsearch │  │  Grafana  │  │cpuad-updater │  │
│  │  :9200       │  │  :3000   │  │  (hourly)    │  │
│  └──────┬───────┘  └────┬─────┘  └──────┬───────┘  │
│         │               │               │          │
│         └───────────────┴───────────────┘          │
│              Docker Compose Network                 │
└─────────────────────────────────────────────────────┘
         │                                    │
         │ Read data                          │ Fetch data
         ▼                                    ▼
    Grafana Dashboard              GitHub Enterprise API
```

---

## Step 1 — Provision a Linux VM

Create a VM in your cloud provider:

- **Azure**: Create a Standard_D2s_v3 (2 vCPU, 8 GB) or Standard_D4s_v3 (4 vCPU, 16 GB)
- **AWS**: t3.large or t3.xlarge
- **GCP**: e2-standard-2 or e2-standard-4

> Minimum 8 GB RAM is required. 16 GB is recommended.

---

## Step 2 — SSH into the VM

```bash
ssh your-user@<VM_PUBLIC_IP>
```

---

## Step 3 — Install Docker

```bash
sudo apt update && sudo apt install -y docker.io docker-compose-v2 git jq
sudo systemctl enable docker && sudo systemctl start docker
```

Add your user to the docker group (avoids needing `sudo` for every docker command):

```bash
sudo usermod -aG docker $USER
newgrp docker
```

Verify:

```bash
docker version
```

---

## Step 4 — Configure Kernel Settings

Elasticsearch requires `vm.max_map_count` to be at least 262144:

```bash
sudo sysctl -w vm.max_map_count=262144
echo "vm.max_map_count=262144" | sudo tee -a /etc/sysctl.conf
```

---

## Step 5 — Clone the Repository

```bash
cd ~
git clone https://github.com/AkashAi7/copilot-usage-advanced-dashboard.git
cd copilot-usage-advanced-dashboard
```

---

## Step 6 — Create the .env File

```bash
cat > .env <<EOF
GITHUB_PAT=ghp_your_token_here
ORGANIZATION_SLUGS=standalone:your-enterprise-slug
EXECUTION_INTERVAL_HOURS=1
EOF
```

**Important:**
- Replace `ghp_your_token_here` with your actual GitHub Personal Access Token
- Replace `your-enterprise-slug` with your actual enterprise slug (e.g., `NegD`)
- The `standalone:` prefix is **required** — it tells the app to use Enterprise APIs instead of Organization APIs

### Slug Format Reference

| Format | Description |
|---|---|
| `myOrg1` | Single organization |
| `myOrg1,myOrg2` | Multiple organizations |
| `standalone:myEnterprise` | Enterprise-only (standalone) |
| `myOrg1,standalone:myEnterprise` | Mixed org + enterprise |

---

## Step 7 — Create Dashboard Placeholder

Docker Compose mounts `user_advance_metrics_dashboard.json`. If the file is missing, Docker creates it as a directory which breaks Grafana:

```bash
[ -f user_advance_metrics_dashboard.json ] || echo '{}' > user_advance_metrics_dashboard.json
```

Also ensure the provisioning directory exists:

```bash
mkdir -p grafana-provisioning/dashboards
```

---

## Step 8 — Open Firewall Port

```bash
# Linux firewall (if ufw is active)
sudo ufw allow 3000/tcp 2>/dev/null; true
```

For cloud provider firewall, see [Networking — Azure NSG Configuration](#networking--azure-nsg-configuration) below.

---

## Step 9 — Start the Stack

```bash
docker compose up -d --build
```

Expected output:

```
✔ Container elasticsearch  Healthy    61.5s
✔ Container cpuad-updater  Started    61.6s
✔ Container grafana        Started     0.6s
✔ Container init-grafana   Started     0.3s
```

> Note: Elasticsearch takes ~60 seconds to become healthy. The `cpuad-updater` will wait automatically.

---

## Step 10 — Verify the Run

Watch the updater logs:

```bash
docker logs -f cpuad-updater
```

Wait for the full sequence:

```
Elasticsearch is up and running
Created index: copilot_seat_info_settings
Created index: copilot_seat_assignments
Created index: copilot_usage_total
Created index: copilot_usage_breakdown
Created index: copilot_usage_breakdown_chat
Created index: copilot_user_metrics
Created index: copilot_user_adoption
Processing Copilot seat info & settings for Standalone: ...
Processing Copilot seat assignments for Standalone: ...
Processing Copilot user metrics for Standalone: ...
Processing Copilot usage data for Standalone: ...
-----------------Finished Successfully-----------------
Sleeping for 1 hour(s) until next run...
```

Press `Ctrl+C` to exit the log view.

---

## Step 11 — Verify Elasticsearch Indexes

```bash
for idx in copilot_seat_info_settings copilot_seat_assignments copilot_usage_total copilot_usage_breakdown copilot_usage_breakdown_chat copilot_user_metrics copilot_user_adoption; do
  echo -n "$idx: "; curl -s http://localhost:9200/$idx/_count | jq .count
done
```

Expected output (all non-zero):

```
copilot_seat_info_settings: 1
copilot_seat_assignments: 394
copilot_usage_total: 2008
copilot_usage_breakdown: 10550
copilot_usage_breakdown_chat: 2148
copilot_user_metrics: 3360
copilot_user_adoption: 11
```

---

## Step 12 — Access Grafana

Open in your browser:

```
http://<VM_PUBLIC_IP>:3000
```

| Field | Value |
|---|---|
| **Username** | `admin` |
| **Password** | `copilot` |

### Dashboard Panels

Set the time range to **Last 90 days** or **Last 6 months** to see full history.

| Panel Section | Data Source Index | What It Shows |
|---|---|---|
| **Organization** | `copilot_usage_total` | Acceptance rate, suggestions, lines of code |
| **Teams** | `copilot_usage_breakdown` | Team-level comparisons |
| **Languages** | `copilot_usage_breakdown` | Language-level usage stats |
| **Editors** | `copilot_usage_breakdown` | Editor-level usage stats |
| **Copilot Chat** | `copilot_usage_breakdown_chat` | Chat turns, acceptances, active users |
| **Seat Analysis** | `copilot_seat_info_settings` + `copilot_seat_assignments` | Seat allocation, inactive users |
| **Breakdown Heatmap** | `copilot_usage_breakdown` | Language × Editor matrix |
| **User Metrics** | `copilot_user_metrics` + `copilot_user_adoption` | Per-user analytics, Top 10 leaderboard |

---

## Networking — Azure NSG Configuration

If your VM is on Azure, you need an inbound NSG rule for port 3000.

### Check for two NSGs

Azure VMs can have NSGs at **two levels** — both must allow port 3000:

1. **Subnet-level NSG**: Azure Portal → VM → Networking → look at the subnet's NSG
2. **NIC-level NSG**: Azure Portal → VM → Networking → click the Network Interface → Network security group

### Add the rule

In each NSG that exists:

- **Source**: Any (or your IP for security)
- **Destination port**: 3000
- **Protocol**: TCP
- **Action**: Allow
- **Priority**: 1001 (or any number < 65000)

### Azure CLI

```bash
az network nsg rule create \
  --resource-group YOUR_RG \
  --nsg-name YOUR_NSG \
  --name Allow-Grafana-3000 \
  --priority 1001 \
  --destination-port-ranges 3000 \
  --access Allow \
  --protocol Tcp
```

### Verify from the VM

```bash
PUBLIC_IP=$(curl -s ifconfig.me)
echo "Public IP: $PUBLIC_IP"
curl -m 5 http://$PUBLIC_IP:3000/api/health
```

If `localhost` works but the public IP doesn't → NSG is blocking.

---

## Troubleshooting

### Full Clean Reinstall (Nuclear Option)

If things aren't working, wipe everything and start fresh:

```bash
# Stop and remove all containers + volumes
cd ~/copilot-usage-advanced-dashboard
sudo docker compose down -v --remove-orphans

# Remove all Docker images for this project
sudo docker rmi $(sudo docker images --filter "reference=copilot-usage-advanced-dashboard*" -q) 2>/dev/null; true

# Delete the repo entirely
cd ~
rm -rf copilot-usage-advanced-dashboard

# Fresh clone
git clone https://github.com/AkashAi7/copilot-usage-advanced-dashboard.git
cd copilot-usage-advanced-dashboard

# Kernel tuning
sudo sysctl -w vm.max_map_count=262144
grep -q 'vm.max_map_count' /etc/sysctl.conf || echo 'vm.max_map_count=262144' | sudo tee -a /etc/sysctl.conf

# Create .env
cat > .env <<EOF
GITHUB_PAT=ghp_your_token_here
ORGANIZATION_SLUGS=standalone:your-enterprise-slug
EXECUTION_INTERVAL_HOURS=1
EOF

# Dashboard placeholder
echo '{}' > user_advance_metrics_dashboard.json
mkdir -p grafana-provisioning/dashboards

# Build and start
sudo docker compose up -d --build

# Watch logs (wait for "Finished Successfully")
sudo docker compose logs -f cpuad-updater --tail=50
```

After `"Finished Successfully"` appears, verify indexes:

```bash
for idx in copilot_seat_info_settings copilot_seat_assignments copilot_usage_total copilot_usage_breakdown copilot_usage_breakdown_chat copilot_user_metrics copilot_user_adoption; do
  echo -n "$idx: "; curl -s http://localhost:9200/$idx/_count | jq .count
done
```

All counts should be non-zero. Then open `http://<VM_IP>:3000` (admin / copilot).

### Elasticsearch exits with code 137 (OOM)

```bash
# Check memory
free -h

# Ensure memory lock is disabled in elasticsearch.yml
grep memory_lock src/elasticsearch/elasticsearch.yml
# Must show: bootstrap.memory_lock: false

# Increase container memory limit in docker-compose.yml
# mem_limit: 2g (minimum for ES)
```

### Elasticsearch 503 — No shard available

Existing indexes were created with replicas. Fix:

```bash
for index in copilot_seat_info_settings copilot_seat_assignments copilot_usage_total copilot_usage_breakdown copilot_usage_breakdown_chat copilot_user_metrics copilot_user_adoption; do
  curl -X PUT http://localhost:9200/$index/_settings \
    -H "Content-Type: application/json" \
    -d '{"index":{"number_of_replicas":0}}'
done
```

Or nuclear option — delete all data and start fresh:

```bash
docker compose down
docker volume rm copilot-usage-advanced-dashboard_data copilot-usage-advanced-dashboard_logs
docker compose up -d --build
```

### cpuad-updater can't resolve `elasticsearch`

```bash
# Full restart to recreate Docker network
docker compose down && docker compose up -d --build
```

### Grafana reachable on localhost but not public IP

```bash
# Verify port is listening
ss -tlnp | grep 3000

# Check cloud firewall (Azure NSG / AWS Security Group / GCP Firewall)
# Both subnet-level AND NIC-level NSGs must allow port 3000
```

### Force a fresh data fetch (don't wait 1 hour)

```bash
docker restart cpuad-updater && docker logs -f cpuad-updater
```

### Validate GitHub API access

```bash
# Check teams
curl -s -H "Authorization: Bearer YOUR_PAT" -H "Accept: application/vnd.github+json" \
  https://api.github.com/enterprises/YOUR_SLUG/teams | jq '.[].slug'

# Check metrics
curl -s -H "Authorization: Bearer YOUR_PAT" -H "Accept: application/vnd.github+json" \
  "https://api.github.com/enterprises/YOUR_SLUG/copilot/metrics" | jq '.[0].date'
```

### No Data in Dashboard
1. Wait for first fetch cycle (check `EXECUTION_INTERVAL_HOURS` in `.env`)
2. Check cpuad-updater logs: `docker logs cpuad-updater`
3. Verify organization has Copilot seats assigned
4. Verify PAT has `manage_billing:copilot` + `read:enterprise` scopes
5. Set Grafana time range to **Last 90 days** or wider

### Grafana Login Loop
```bash
docker compose exec grafana grafana-cli admin reset-admin-password copilot
```

---

## Summary of All Commands (Quick Reference)

```bash
# 1. SSH
ssh user@<VM_IP>

# 2. Install
sudo apt update && sudo apt install -y docker.io docker-compose-v2 git jq
sudo systemctl enable docker && sudo systemctl start docker
sudo usermod -aG docker $USER && newgrp docker

# 3. Kernel
sudo sysctl -w vm.max_map_count=262144
echo "vm.max_map_count=262144" | sudo tee -a /etc/sysctl.conf

# 4. Clone
cd ~ && git clone https://github.com/AkashAi7/copilot-usage-advanced-dashboard.git
cd copilot-usage-advanced-dashboard

# 5. Configure
cat > .env <<EOF
GITHUB_PAT=ghp_your_token
ORGANIZATION_SLUGS=standalone:your-enterprise-slug
EXECUTION_INTERVAL_HOURS=1
EOF

# 6. Placeholder
[ -f user_advance_metrics_dashboard.json ] || echo '{}' > user_advance_metrics_dashboard.json

# 7. Start
docker compose up -d --build

# 8. Watch
docker logs -f cpuad-updater

# 9. Verify
for idx in copilot_seat_info_settings copilot_seat_assignments copilot_usage_total copilot_usage_breakdown copilot_usage_breakdown_chat copilot_user_metrics copilot_user_adoption; do
  echo -n "$idx: "; curl -s http://localhost:9200/$idx/_count | jq .count
done

# 10. Open browser: http://<VM_IP>:3000 (admin/copilot)
```

---

## Management Commands

| Action | Command |
|--------|---------|
| Stop services | `docker compose down` |
| Restart services | `docker compose restart` |
| View all logs | `docker compose logs` |
| Reset everything | `docker compose down -v` |
| Update to latest | `git pull origin main && docker compose up --build -d` |
| Force data fetch | `docker restart cpuad-updater && docker logs -f cpuad-updater` |

---

## Security Best Practices

1. **Never commit `.env` file** — Contains sensitive tokens
2. **Restrict NSG rules** — Allow only your IP, not `0.0.0.0/0`
3. **Change default Grafana password** — Edit `docker-compose.yml` before first run
4. **Rotate GitHub PAT regularly** — Create new tokens periodically
5. **Use HTTPS** — Set up reverse proxy (nginx/Caddy) with SSL certificate
6. **Keep updated** — Run `git pull` and rebuild containers regularly
