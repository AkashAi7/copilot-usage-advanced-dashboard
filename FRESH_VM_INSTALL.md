# Fresh Linux VM Installation Guide

Complete setup guide for deploying GitHub Copilot Usage Advanced Dashboard on a fresh Linux VM.

## Prerequisites

- Fresh Ubuntu 20.04/22.04/24.04 VM
- SSH access to the VM
- GitHub Personal Access Token (PAT) with scopes: `manage_billing:copilot`, `read:org`, `read:enterprise`
- GitHub Organization or Enterprise with Copilot Business/Enterprise enabled

> **Enterprise users:** Your `ORGANIZATION_SLUGS` must use the `standalone:<slug>` prefix.
> For example, if your enterprise slug is `NegD`, set `ORGANIZATION_SLUGS=standalone:NegD`.

---

## Quick Start (Automated)

### Option 1: One-Click Setup Script

```bash
# Clone repository
git clone https://github.com/AkashAi7/copilot-usage-advanced-dashboard.git
cd copilot-usage-advanced-dashboard

# Make script executable and run
chmod +x setup.sh
./setup.sh
```

The script will:
- ✅ Check and install prerequisites (Docker, Docker Compose)
- ✅ Collect GitHub credentials interactively
- ✅ Generate `.env` configuration
- ✅ Build and start all containers
- ✅ Perform health checks
- ✅ Display access URLs with local and public IPs

**Command-line flags** (skip interactive prompts):
```bash
# For a GitHub Organization:
./setup.sh --pat ghp_YOUR_TOKEN --org your-org-name --interval 1 --tz America/New_York

# For a GitHub Enterprise (use standalone: prefix):
./setup.sh --pat ghp_YOUR_TOKEN --org standalone:your-enterprise-slug --interval 1 --tz America/New_York
```

---

## Manual Installation (Step-by-Step)

If you prefer manual control or the automated script fails:

### Step 1: Update System
```bash
sudo apt update && sudo apt upgrade -y
```

### Step 2: Install Docker
```bash
# Install Docker
sudo apt install -y docker.io

# Add current user to docker group
sudo usermod -aG docker $USER

# Log out and log back in to apply group changes
exit
# SSH back in
```

### Step 3: Install Docker Compose
```bash
# Download Docker Compose v2
sudo curl -SL https://github.com/docker/compose/releases/latest/download/docker-compose-linux-x86_64 -o /usr/local/bin/docker-compose

# Make executable
sudo chmod +x /usr/local/bin/docker-compose

# Create plugin directory and symlink
sudo mkdir -p /usr/local/lib/docker/cli-plugins
sudo ln -sf /usr/local/bin/docker-compose /usr/local/lib/docker/cli-plugins/docker-compose

# Verify installation
docker compose version
```

### Step 4: Clone Repository
```bash
cd ~
git clone https://github.com/AkashAi7/copilot-usage-advanced-dashboard.git
cd copilot-usage-advanced-dashboard
```

### Step 5: Configure Environment
```bash
# Create .env file
cat > .env << 'EOF'
GITHUB_PAT=ghp_YOUR_GITHUB_PAT_HERE
ORGANIZATION_SLUGS=your-organization-name
EXECUTION_INTERVAL_HOURS=1
ELASTICSEARCH_URL=http://elasticsearch:9200
EOF

# Replace with your actual values
nano .env
```

**Environment Variables:**
- `GITHUB_PAT`: Your GitHub Personal Access Token
- `ORGANIZATION_SLUGS`: Your GitHub organization or enterprise slug:
  - Single org: `myOrg`
  - Multiple orgs: `myOrg1,myOrg2`
  - **GitHub Enterprise slug** (most common for enterprise PATs): `standalone:your-enterprise-slug`
    > Example: if your enterprise is `NegD`, set `ORGANIZATION_SLUGS=standalone:NegD`
- `EXECUTION_INTERVAL_HOURS`: How often to fetch data (default: 1)
- `ELASTICSEARCH_URL`: Keep as `http://elasticsearch:9200`

### Step 6: Create Dashboard Placeholder
```bash
# Create empty JSON file to prevent Docker volume bug
echo '{}' > user_advance_metrics_dashboard.json
```

### Step 7: Start Services
```bash
# Build and start containers
docker compose up --build -d

# Check container status
docker compose ps
```

Expected output:
```
NAME            STATUS          PORTS
elasticsearch   Up (health: starting)   0.0.0.0:9200->9200/tcp
grafana         Up (healthy)            0.0.0.0:3000->80/tcp
cpuad-updater   Up (health: starting)
init-grafana    Started
```

### Step 8: Configure Azure NSG/Firewall

**Azure Portal:**
1. Go to your VM → Networking → Network settings
2. Add inbound security rule:
   - **Port:** 3000
   - **Protocol:** TCP
   - **Source:** Any (or your IP for security)
   - **Action:** Allow
   - **Priority:** 1000
   - **Name:** Allow-Grafana-3000

**Azure CLI:**
```bash
az network nsg rule create \
  --resource-group YOUR_RG \
  --nsg-name YOUR_NSG \
  --name Allow-Grafana-3000 \
  --priority 1000 \
  --destination-port-ranges 3000 \
  --access Allow \
  --protocol Tcp
```

**UFW (VM firewall):**
```bash
sudo ufw allow 3000/tcp
sudo ufw status
```

### Step 9: Access Dashboard

Get your public IP:
```bash
curl -s https://api.ipify.org
```

Open browser:
- **URL:** `http://YOUR_PUBLIC_IP:3000`
- **Username:** `admin`
- **Password:** `copilot`

---

## Verification & Monitoring

### Check Container Health
```bash
docker compose ps
```

### Monitor Data Fetching
```bash
# Watch live logs
docker compose logs -f cpuad-updater

# Look for messages like:
# [INFO] Fetched Copilot usage for org: your-org-name
# [INFO] Successfully indexed data to Elasticsearch
```

### Force Immediate Data Fetch
```bash
docker compose restart cpuad-updater
docker compose logs -f cpuad-updater
```

### Check Elasticsearch Health
```bash
curl http://localhost:9200/_cluster/health?pretty
```

### Check Individual Container Logs
```bash
docker compose logs grafana
docker compose logs elasticsearch
docker compose logs init-grafana
```

---

## Troubleshooting

### Issue: Permission Denied Errors
```bash
sudo chown -R $USER:$USER ~/copilot-usage-advanced-dashboard
```

### Issue: Port Already in Use
```bash
# Check what's using port 3000 or 9200
sudo ss -tlnp | grep 3000
sudo ss -tlnp | grep 9200

# Stop conflicting containers
docker compose down
docker rm -f $(docker ps -aq)

# Restart Docker if needed
sudo systemctl restart docker
```

### Issue: Container Keeps Restarting
```bash
# Check logs for errors
docker compose logs [container-name]

# Common fixes:
# 1. Verify .env has correct GITHUB_PAT
# 2. Check ORGANIZATION_SLUGS spelling
# 3. Ensure PAT has correct scopes
```

### Issue: No Data in Dashboard
1. Wait for first fetch cycle (EXECUTION_INTERVAL_HOURS)
2. Check cpuad-updater logs for errors
3. Verify organization has Copilot seats assigned
4. Check GitHub PAT has `manage_billing:copilot` scope

### Issue: Grafana Login Loop
```bash
# Reset Grafana admin password
docker compose exec grafana grafana-cli admin reset-admin-password copilot
```

---

## Management Commands

### Stop Services
```bash
docker compose down
```

### Restart Services
```bash
docker compose restart
```

### View All Logs
```bash
docker compose logs
```

### Reset Everything (Delete All Data)
```bash
docker compose down -v
```

### Update to Latest Version
```bash
git pull origin main
docker compose up --build -d
```

---

## Architecture

**Services:**
- **Elasticsearch** (port 9200): Data storage
- **Grafana** (port 3000): Dashboard UI
- **cpuad-updater**: Fetches GitHub Copilot metrics every N hours
- **init-grafana**: One-time setup for Grafana data sources

**Data Flow:**
1. `cpuad-updater` fetches metrics from GitHub Copilot API
2. Data is indexed into Elasticsearch
3. Grafana queries Elasticsearch and displays dashboards
4. Process repeats every `EXECUTION_INTERVAL_HOURS`

**Volumes:**
- `data`: Elasticsearch data (persistent)
- `logs`: Elasticsearch logs
- `grafana`: Grafana configuration and dashboards

---

## Security Best Practices

1. **Never commit `.env` file** - Contains sensitive tokens
2. **Restrict NSG rules** - Allow only your IP, not `0.0.0.0/0`
3. **Change default Grafana password** - Edit `docker-compose.yml` before first run
4. **Rotate GitHub PAT regularly** - Create new tokens periodically
5. **Use HTTPS** - Set up reverse proxy (nginx/Caddy) with SSL certificate
6. **Keep updated** - Run `git pull` and rebuild containers regularly

---

## Advanced Configuration

### Change Grafana Port
Edit `docker-compose.yml`:
```yaml
grafana:
  ports:
    - "3000:80"  # Change 3000 to your desired port
```

### Run on Different Schedule
Edit `.env`:
```bash
EXECUTION_INTERVAL_HOURS=6  # Fetch every 6 hours
```

### Monitor Multiple Organizations
Edit `.env`:
```bash
# Multiple orgs:
ORGANIZATION_SLUGS=org1,org2,org3

# Enterprise + org mixed:
ORGANIZATION_SLUGS=standalone:your-enterprise,org2
```

### GitHub Enterprise Setup
If you have a GitHub Enterprise account (not just an org), your slug must use the `standalone:` prefix:
```bash
ORGANIZATION_SLUGS=standalone:your-enterprise-slug
```
This tells the fetcher to use the Enterprise API endpoints (`/enterprises/...`) instead of the org-level ones (`/orgs/...`). Your PAT must have `read:enterprise` scope.

### Increase Memory Limits
Edit `docker-compose.yml`:
```yaml
elasticsearch:
  mem_limit: 2g  # Increase from 1g
  cpus: 2        # Increase from 1
```

---

## Getting Help

- **GitHub Issues:** https://github.com/AkashAi7/copilot-usage-advanced-dashboard/issues
- **Original Project:** https://github.com/satomic/copilot-usage-advanced-dashboard
- **Docker Docs:** https://docs.docker.com/
- **Grafana Docs:** https://grafana.com/docs/

---

## Quick Reference

| Component | Port | Credentials | Endpoint |
|-----------|------|-------------|----------|
| Grafana | 3000 | admin / copilot | http://YOUR_IP:3000 |
| Elasticsearch | 9200 | None | http://YOUR_IP:9200 |

**Useful URLs:**
- Grafana Login: `http://YOUR_IP:3000/login`
- Elasticsearch Health: `http://YOUR_IP:9200/_cluster/health`
- Elasticsearch Indices: `http://YOUR_IP:9200/_cat/indices?v`

---

## License

See [LICENSE](LICENSE) file for details.
