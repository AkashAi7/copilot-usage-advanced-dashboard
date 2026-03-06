#!/usr/bin/env bash
# =============================================================================
# Copilot Usage Advanced Dashboard - One-Click Setup
# =============================================================================
# Usage:
#   ./setup.sh                          # interactive prompts
#   ./setup.sh --pat ghp_xxx --org myOrg
#   GITHUB_PAT=ghp_xxx ORGANIZATION_SLUGS=myOrg ./setup.sh
# =============================================================================

set -euo pipefail

# ── Colors ────────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; RESET='\033[0m'

info()    { echo -e "${CYAN}[INFO]${RESET}  $*"; }
success() { echo -e "${GREEN}[OK]${RESET}    $*"; }
warn()    { echo -e "${YELLOW}[WARN]${RESET}  $*"; }
error()   { echo -e "${RED}[ERROR]${RESET} $*" >&2; exit 1; }
header()  { echo -e "\n${BOLD}${CYAN}══ $* ══${RESET}"; }

# ── Parse CLI args ────────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
  case $1 in
    --pat|-p)   GITHUB_PAT="${2:-}"; shift 2 ;;
    --org|-o)   ORGANIZATION_SLUGS="${2:-}"; shift 2 ;;
    --interval) EXECUTION_INTERVAL_HOURS="${2:-1}"; shift 2 ;;
    --tz)       TZ_VALUE="${2:-GMT}"; shift 2 ;;
    --help|-h)
      echo "Usage: $0 [--pat <token>] [--org <slug>] [--interval <hours>] [--tz <tz>]"
      echo ""
      echo "  --pat       GitHub Personal Access Token (manage_billing:copilot, read:org, read:enterprise)"
      echo "  --org       GitHub Org slug(s), comma-separated."
      echo "              Use 'standalone:<slug>' for Enterprise or Copilot Standalone slugs."
      echo "              e.g. --org standalone:NegD  OR  --org myOrg1,myOrg2"
      echo "  --interval  Data fetch interval in hours (default: 1)"
      echo "  --tz        Timezone, e.g. America/New_York (default: GMT)"
      exit 0 ;;
    *) error "Unknown argument: $1. Run '$0 --help' for usage." ;;
  esac
done

# ── Banner ────────────────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}${CYAN}"
echo "  ┌─────────────────────────────────────────────────────┐"
echo "  │   GitHub Copilot Usage Advanced Dashboard           │"
echo "  │   One-Click Setup                                   │"
echo "  └─────────────────────────────────────────────────────┘"
echo -e "${RESET}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# ── 1. Prerequisites check & auto-install ────────────────────────────────────
header "Checking prerequisites"

# Docker
if ! command -v docker &>/dev/null; then
  warn "Docker not found. Installing Docker..."
  
  # Detect OS
  if [[ -f /etc/os-release ]]; then
    . /etc/os-release
    OS=$ID
  else
    error "Cannot detect OS. Please install Docker manually: https://docs.docker.com/engine/install/"
  fi
  
  case $OS in
    ubuntu|debian)
      info "Installing Docker on $OS..."
      sudo apt update -qq
      sudo apt install -y docker.io
      sudo systemctl start docker
      sudo systemctl enable docker
      sudo usermod -aG docker $USER
      success "Docker installed successfully"
      warn "You need to log out and back in for Docker group changes to take effect"
      warn "Or run: newgrp docker"
      ;;
    centos|rhel|fedora)
      info "Installing Docker on $OS..."
      sudo yum install -y docker
      sudo systemctl start docker
      sudo systemctl enable docker
      sudo usermod -aG docker $USER
      success "Docker installed successfully"
      ;;
    *)
      error "Unsupported OS: $OS. Please install Docker manually: https://docs.docker.com/engine/install/"
      ;;
  esac
fi
success "Docker found: $(docker --version | head -1)"

# Docker Compose (v2 plugin or v1 standalone)
if docker compose version &>/dev/null 2>&1; then
  COMPOSE_CMD="docker compose"
  success "Docker Compose (v2 plugin) found: $(docker compose version --short)"
elif command -v docker-compose &>/dev/null; then
  COMPOSE_CMD="docker-compose"
  success "Docker Compose (v1) found: $(docker-compose --version | head -1)"
else
  warn "Docker Compose not found. Installing Docker Compose v2..."
  
  info "Downloading Docker Compose..."
  sudo curl -SL https://github.com/docker/compose/releases/latest/download/docker-compose-linux-x86_64 -o /usr/local/bin/docker-compose
  sudo chmod +x /usr/local/bin/docker-compose
  
  # Create plugin directory and symlink
  sudo mkdir -p /usr/local/lib/docker/cli-plugins
  sudo ln -sf /usr/local/bin/docker-compose /usr/local/lib/docker/cli-plugins/docker-compose
  
  if docker compose version &>/dev/null 2>&1; then
    COMPOSE_CMD="docker compose"
    success "Docker Compose installed: $(docker compose version --short)"
  else
    COMPOSE_CMD="docker-compose"
    success "Docker Compose installed: $(/usr/local/bin/docker-compose --version)"
  fi
fi

# Docker daemon running? (use sudo since group membership may not be active yet)
if ! sudo docker info &>/dev/null; then
  warn "Docker daemon is not running. Starting it..."
  sudo systemctl start docker 2>/dev/null || sudo service docker start 2>/dev/null || error "Could not start Docker daemon. Please start it manually."
  sleep 3
  if ! sudo docker info &>/dev/null; then
    error "Docker daemon failed to start. Check: sudo systemctl status docker"
  fi
  success "Docker daemon started"
fi
success "Docker daemon is running"

# Check if we need sudo for docker commands (group membership not active yet)
USE_SUDO=""
if ! docker info &>/dev/null 2>&1; then
  if sudo docker info &>/dev/null 2>&1; then
    USE_SUDO="sudo"
    warn "Docker group membership not active yet. Using 'sudo' for docker commands."
    warn "After setup completes, log out and back in to use docker without sudo."
  fi
fi

# Update COMPOSE_CMD with sudo if needed
if [[ -n "$USE_SUDO" ]]; then
  COMPOSE_CMD="$USE_SUDO $COMPOSE_CMD"
fi

# ── 2. Collect required inputs ────────────────────────────────────────────────
header "Configuration"

# GITHUB_PAT
if [[ -z "${GITHUB_PAT:-}" ]]; then
  echo -e "  ${BOLD}GitHub Personal Access Token${RESET}"
  echo -e "  Required scopes: ${YELLOW}manage_billing:copilot${RESET}, ${YELLOW}read:org${RESET}, ${YELLOW}read:enterprise${RESET}"
  echo -e "  Create one at: https://github.com/settings/tokens"
  echo ""
  read -rsp "  Enter your GitHub PAT: " GITHUB_PAT
  echo ""
  [[ -z "$GITHUB_PAT" ]] && error "GITHUB_PAT cannot be empty."
fi
success "GitHub PAT: ${GITHUB_PAT:0:8}…(hidden)"

# ORGANIZATION_SLUGS
if [[ -z "${ORGANIZATION_SLUGS:-}" ]]; then
  echo ""
  echo -e "  ${BOLD}GitHub Organization / Enterprise Slug(s)${RESET}"
  echo -e "  Examples:"
  echo -e "    Single org:              ${YELLOW}myOrg${RESET}"
  echo -e "    Multiple orgs:           ${YELLOW}myOrg1,myOrg2${RESET}"
  echo -e "    Enterprise slug:         ${YELLOW}standalone:myEnterprise${RESET}"
  echo -e "    Copilot Standalone:      ${YELLOW}standalone:mySlug${RESET}"
  echo -e "  ${CYAN}Tip:${RESET} Use the 'standalone:' prefix for GitHub Enterprise slugs"
  echo -e "       (e.g. if your enterprise is 'NegD', enter: standalone:NegD)"
  echo ""
  read -rp "  Enter org slug(s): " ORGANIZATION_SLUGS
  [[ -z "$ORGANIZATION_SLUGS" ]] && error "ORGANIZATION_SLUGS cannot be empty."
fi
success "Organization(s): $ORGANIZATION_SLUGS"

EXECUTION_INTERVAL_HOURS="${EXECUTION_INTERVAL_HOURS:-1}"
TZ_VALUE="${TZ_VALUE:-GMT}"
success "Fetch interval: every ${EXECUTION_INTERVAL_HOURS}h | Timezone: $TZ_VALUE"

# ── 3. Write .env file ────────────────────────────────────────────────────────
header "Writing .env"

ENV_FILE="$SCRIPT_DIR/.env"

cat > "$ENV_FILE" <<EOF
# Generated by setup.sh - $(date -u '+%Y-%m-%d %H:%M:%S UTC')
# Copilot Usage Advanced Dashboard

# ── Required ──────────────────────────────────────────────────────────────────
GITHUB_PAT=${GITHUB_PAT}
ORGANIZATION_SLUGS=${ORGANIZATION_SLUGS}

# ── Execution ─────────────────────────────────────────────────────────────────
EXECUTION_INTERVAL_HOURS=${EXECUTION_INTERVAL_HOURS}
TZ=${TZ_VALUE}

# ── Elasticsearch (Docker Compose internal URLs - do not change) ───────────────
ELASTICSEARCH_URL=http://elasticsearch:9200
EOF

success ".env written to $ENV_FILE"

# ── 4. Ensure dashboard JSON placeholder exists ───────────────────────────────
# docker-compose mounts user_advance_metrics_dashboard.json; if missing Docker
# creates it as a directory which breaks Grafana.
DASHBOARD_JSON="$SCRIPT_DIR/user_advance_metrics_dashboard.json"
if [[ ! -f "$DASHBOARD_JSON" ]]; then
  warn "user_advance_metrics_dashboard.json not found - creating empty placeholder."
  echo '{}' > "$DASHBOARD_JSON"
  success "Placeholder created: $DASHBOARD_JSON"
fi

# ── 5. Ensure grafana-provisioning/dashboards dir exists ──────────────────────
mkdir -p "$SCRIPT_DIR/grafana-provisioning/dashboards"

# ── 6. Pull / build and start ─────────────────────────────────────────────────
header "Starting services (this may take a few minutes on first run)"

info "Building images and starting all containers in detached mode…"
$COMPOSE_CMD up --build -d

# ── 7. Health-check loop ──────────────────────────────────────────────────────
header "Waiting for services to become healthy"

wait_for_url() {
  local name="$1" url="$2" max_wait="${3:-120}" interval=5
  local elapsed=0
  info "Waiting for $name at $url …"
  while ! curl -sf "$url" &>/dev/null; do
    if (( elapsed >= max_wait )); then
      warn "$name did not become healthy within ${max_wait}s. Check: $COMPOSE_CMD logs $name"
      return 1
    fi
    sleep $interval
    elapsed=$(( elapsed + interval ))
    echo -n "."
  done
  echo ""
  success "$name is up!"
}

wait_for_url "Elasticsearch" "http://localhost:9200/_cluster/health" 180
wait_for_url "Grafana"       "http://localhost:3000/api/health"       180

# ── 8. Detect IP addresses ───────────────────────────────────────────────────
header "Detecting access URLs"

# Public IP (works on most cloud VMs; falls back silently)
PUBLIC_IP=""
for svc in "https://api.ipify.org" "https://checkip.amazonaws.com" "https://ifconfig.me"; do
  PUBLIC_IP=$(curl -sf --max-time 3 "$svc" 2>/dev/null | tr -d '[:space:]') && break
done

# Local/private IP
LOCAL_IP=$(hostname -I 2>/dev/null | awk '{print $1}' || ip route get 1 2>/dev/null | awk '{print $7; exit}')

GRAFANA_LOCAL="http://${LOCAL_IP:-localhost}:3000"
ES_LOCAL="http://${LOCAL_IP:-localhost}:9200"

if [[ -n "$PUBLIC_IP" ]]; then
  GRAFANA_PUBLIC="http://${PUBLIC_IP}:3000"
  ES_PUBLIC="http://${PUBLIC_IP}:9200"
  success "Public IP : $PUBLIC_IP"
fi
success "Local IP  : ${LOCAL_IP:-localhost}"

# ── 9. Print summary ──────────────────────────────────────────────────────────
header "Setup complete!"

echo ""
echo -e "${BOLD}  Services running:${RESET}"
$COMPOSE_CMD ps --format "table {{.Name}}\t{{.Status}}\t{{.Ports}}" 2>/dev/null || $COMPOSE_CMD ps
echo ""
echo -e "${BOLD}${GREEN}  ╔══════════════════════════════════════════════════════════════╗"
echo    "  ║  Access your dashboard                                      ║"
echo    "  ╠══════════════════════════════════════════════════════════════╣"
printf "  ║  Grafana (local):   %-40s║\n" "${GRAFANA_LOCAL}"
if [[ -n "$PUBLIC_IP" ]]; then
printf "  ║  Grafana (public):  %-40s║\n" "${GRAFANA_PUBLIC}"
fi
echo    "  ║  Username:          admin                                    ║"
echo    "  ║  Password:          copilot                                  ║"
echo    "  ╠══════════════════════════════════════════════════════════════╣"
printf "  ║  Elasticsearch:     %-40s║\n" "${ES_LOCAL}"
echo -e "  ╚══════════════════════════════════════════════════════════════╝${RESET}"
echo ""
if [[ -n "$PUBLIC_IP" ]]; then
  echo -e "  ${YELLOW}Note:${RESET} Make sure port ${BOLD}3000${RESET} is open in your VM/firewall/security-group rules."
fi
echo -e "  ${YELLOW}Note:${RESET} Data will appear after the first fetch (~${EXECUTION_INTERVAL_HOURS}h)."
echo -e "  ${YELLOW}Note:${RESET} Run '${COMPOSE_CMD} logs -f cpuad-updater' to watch live progress."
echo ""
echo -e "  To stop:    ${CYAN}${COMPOSE_CMD} down${RESET}"
echo -e "  To restart: ${CYAN}${COMPOSE_CMD} up -d${RESET}"
echo -e "  To reset:   ${CYAN}${COMPOSE_CMD} down -v${RESET}  (deletes all data)"
echo ""
