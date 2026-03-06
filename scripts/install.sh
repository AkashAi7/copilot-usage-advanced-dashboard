#!/usr/bin/env bash
# =============================================================================
# Copilot Usage Advanced Dashboard — One-Command Installer
# =============================================================================
# Usage (one-liner from any machine):
#   curl -sL https://raw.githubusercontent.com/AkashAi7/copilot-usage-advanced-dashboard/main/scripts/install.sh | bash
#
# Or with env vars pre-set:
#   GITHUB_PAT=ghp_xxx ORGANIZATION_SLUGS=standalone:myEnterprise bash scripts/install.sh
# =============================================================================

set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; RESET='\033[0m'

info()    { echo -e "${CYAN}[INFO]${RESET}  $*"; }
success() { echo -e "${GREEN}[ OK ]${RESET}  $*"; }
warn()    { echo -e "${YELLOW}[WARN]${RESET}  $*"; }
die()     { echo -e "${RED}[FAIL]${RESET} $*" >&2; exit 1; }

REPO_URL="https://github.com/AkashAi7/copilot-usage-advanced-dashboard.git"
INSTALL_DIR="$HOME/copilot-usage-advanced-dashboard"

echo ""
echo -e "${BOLD}${CYAN}"
echo "  ┌─────────────────────────────────────────────────────┐"
echo "  │   Copilot Usage Advanced Dashboard — Installer      │"
echo "  └─────────────────────────────────────────────────────┘"
echo -e "${RESET}"

# ── 1. Install system dependencies ───────────────────────────────────────────
info "Installing system dependencies..."
sudo apt update -qq
sudo apt install -y docker.io docker-compose-v2 git jq curl
sudo systemctl enable docker && sudo systemctl start docker
sudo usermod -aG docker "$USER" 2>/dev/null || true
success "System dependencies installed"

# ── 2. Kernel tuning ─────────────────────────────────────────────────────────
info "Configuring kernel settings for Elasticsearch..."
CURRENT=$(cat /proc/sys/vm/max_map_count 2>/dev/null || echo 0)
if (( CURRENT < 262144 )); then
  sudo sysctl -w vm.max_map_count=262144
  grep -q 'vm.max_map_count' /etc/sysctl.conf 2>/dev/null \
    || echo 'vm.max_map_count=262144' | sudo tee -a /etc/sysctl.conf >/dev/null
fi
success "vm.max_map_count = 262144"

# ── 3. Clone or update repo ──────────────────────────────────────────────────
if [[ -d "$INSTALL_DIR/.git" ]]; then
  info "Repository already exists — pulling latest..."
  cd "$INSTALL_DIR"
  git pull --ff-only origin main
else
  info "Cloning repository..."
  git clone "$REPO_URL" "$INSTALL_DIR"
  cd "$INSTALL_DIR"
fi
success "Repository ready at $INSTALL_DIR"

# ── 4. Collect credentials ───────────────────────────────────────────────────
if [[ -z "${GITHUB_PAT:-}" ]]; then
  echo ""
  echo -e "  ${BOLD}GitHub Personal Access Token${RESET}"
  echo -e "  Required scopes: ${YELLOW}manage_billing:copilot${RESET}, ${YELLOW}read:enterprise${RESET}, ${YELLOW}read:org${RESET}"
  echo -e "  Create one at: https://github.com/settings/tokens"
  echo ""
  read -rsp "  Enter your GitHub PAT: " GITHUB_PAT; echo ""
  [[ -z "$GITHUB_PAT" ]] && die "GITHUB_PAT cannot be empty."
fi

if [[ -z "${ORGANIZATION_SLUGS:-}" ]]; then
  echo ""
  echo -e "  ${BOLD}Organization / Enterprise Slug${RESET}"
  echo -e "  For Enterprise: ${YELLOW}standalone:your-enterprise-slug${RESET}"
  echo -e "  For Org:        ${YELLOW}your-org-name${RESET}"
  echo ""
  read -rp "  Enter slug: " ORGANIZATION_SLUGS
  [[ -z "$ORGANIZATION_SLUGS" ]] && die "ORGANIZATION_SLUGS cannot be empty."
fi

EXECUTION_INTERVAL_HOURS="${EXECUTION_INTERVAL_HOURS:-1}"

# ── 5. Write .env ────────────────────────────────────────────────────────────
cat > .env <<EOF
GITHUB_PAT=${GITHUB_PAT}
ORGANIZATION_SLUGS=${ORGANIZATION_SLUGS}
EXECUTION_INTERVAL_HOURS=${EXECUTION_INTERVAL_HOURS}
EOF
success ".env written"

# ── 6. Dashboard placeholder ─────────────────────────────────────────────────
[[ -f user_advance_metrics_dashboard.json ]] || echo '{}' > user_advance_metrics_dashboard.json
mkdir -p grafana-provisioning/dashboards

# ── 7. Build & start ─────────────────────────────────────────────────────────
info "Building and starting containers (first run takes a few minutes)..."
sudo docker compose up -d --build

# ── 8. Wait for health ───────────────────────────────────────────────────────
info "Waiting for Elasticsearch to become healthy..."
for i in $(seq 1 60); do
  if curl -sf http://localhost:9200/_cluster/health >/dev/null 2>&1; then
    success "Elasticsearch is healthy"
    break
  fi
  sleep 5
  echo -n "."
done
echo ""

info "Waiting for Grafana to become healthy..."
for i in $(seq 1 30); do
  if curl -sf http://localhost:3000/api/health >/dev/null 2>&1; then
    success "Grafana is healthy"
    break
  fi
  sleep 5
  echo -n "."
done
echo ""

# ── 9. Wait for first data run ───────────────────────────────────────────────
info "Waiting for cpuad-updater to finish first data fetch (this may take 2-5 minutes)..."
for i in $(seq 1 120); do
  if sudo docker logs cpuad-updater 2>&1 | grep -q "Finished Successfully"; then
    success "First data fetch completed!"
    break
  fi
  sleep 5
  echo -n "."
done
echo ""

# ── 10. Verify indexes ───────────────────────────────────────────────────────
info "Verifying Elasticsearch indexes..."
ALL_OK=true
for idx in copilot_seat_info_settings copilot_seat_assignments copilot_usage_total copilot_usage_breakdown copilot_usage_breakdown_chat copilot_user_metrics copilot_user_adoption; do
  COUNT=$(curl -s "http://localhost:9200/${idx}/_count" 2>/dev/null | jq '.count // 0' 2>/dev/null || echo 0)
  if (( COUNT > 0 )); then
    success "$idx: $COUNT documents"
  else
    warn "$idx: 0 documents (may populate on next fetch cycle)"
    ALL_OK=false
  fi
done

# ── 11. Print summary ────────────────────────────────────────────────────────
PUBLIC_IP=$(curl -sf --max-time 3 https://api.ipify.org 2>/dev/null || curl -sf --max-time 3 https://ifconfig.me 2>/dev/null || echo "")
LOCAL_IP=$(hostname -I 2>/dev/null | awk '{print $1}' || echo "localhost")

echo ""
echo -e "${BOLD}${GREEN}  ╔══════════════════════════════════════════════════════════════╗"
echo    "  ║  Setup Complete!                                            ║"
echo    "  ╠══════════════════════════════════════════════════════════════╣"
printf "  ║  Grafana (local):   http://%-33s║\n" "${LOCAL_IP}:3000"
if [[ -n "$PUBLIC_IP" ]]; then
printf "  ║  Grafana (public):  http://%-33s║\n" "${PUBLIC_IP}:3000"
fi
echo    "  ║  Username:          admin                                    ║"
echo    "  ║  Password:          copilot                                  ║"
echo -e "  ╚══════════════════════════════════════════════════════════════╝${RESET}"
echo ""
if [[ -n "$PUBLIC_IP" ]]; then
  echo -e "  ${YELLOW}Note:${RESET} Make sure port ${BOLD}3000${RESET} is open in your firewall/NSG rules."
fi
echo -e "  ${YELLOW}Logs:${RESET} sudo docker compose logs -f cpuad-updater"
echo ""
