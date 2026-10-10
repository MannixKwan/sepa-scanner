#!/usr/bin/env bash
# Deploy & manage the SEPA scanner on AWS Lightsail.
#
#   deploy/aws/sepa-aws.sh plan          what would be created + monthly cost (changes nothing)
#   deploy/aws/sepa-aws.sh up            create on first run, otherwise push code/config and restart
#   deploy/aws/sepa-aws.sh credentials   store your IBKR login on the server and start IB Gateway
#   deploy/aws/sepa-aws.sh ibkr-check    test the server's IBKR connection
#   deploy/aws/sepa-aws.sh status        containers, recent log lines, dashboard URL
#   deploy/aws/sepa-aws.sh logs [svc]    follow logs: scanner (default) | ib-gateway | caddy
#   deploy/aws/sepa-aws.sh ssh           shell on the server
#   deploy/aws/sepa-aws.sh allow-ip      re-allow SSH from your current IP (after your home IP changes)
#   deploy/aws/sepa-aws.sh destroy       delete instance, static IP and key pair (asks to confirm)
#
# Settings (env vars): SEPA_REGION (ap-southeast-1), SEPA_NAME (sepa-scanner), SEPA_RAM_GB (4)
set -euo pipefail
cd "$(dirname "$0")/../.."

REGION="${SEPA_REGION:-ap-southeast-1}"
AZ="${SEPA_AZ:-${REGION}a}"
NAME="${SEPA_NAME:-sepa-scanner}"
RAM_GB="${SEPA_RAM_GB:-4}"
KEY_NAME="$NAME-key"
IP_NAME="$NAME-ip"
KEY_FILE="$HOME/.ssh/$NAME.pem"
IP_CACHE="$HOME/.ssh/$NAME.ip"        # remembered after 'up' so day-to-day commands don't need 'aws login'
R=/opt/sepa
ls_() { aws lightsail --region "$REGION" --output text "$@"; }

die() { echo "error: $*" >&2; exit 1; }
need_login() {
  aws sts get-caller-identity --region "$REGION" >/dev/null 2>&1 || die "AWS session missing or expired - run:  aws login"
}
bundle() {  # cheapest active Linux bundle with RAM_GB, excluding IPv6-only bundles
  ls_ get-bundles --query "sort_by(bundles[?ramSizeInGb==\`${RAM_GB}\` && isActive && contains(supportedPlatforms, 'LINUX_UNIX') && !contains(bundleId, 'ipv6')], &price)[0].[bundleId,price,cpuCount,diskSizeInGb]"
}
static_ip() { ls_ get-static-ip --static-ip-name "$IP_NAME" --query staticIp.ipAddress 2>/dev/null || true; }
host_of() { echo "${1//./-}.sslip.io"; }
server_ip() {  # cached address, else ask AWS
  if [ -s "$IP_CACHE" ]; then cat "$IP_CACHE"; return; fi
  need_login; static_ip
}
my_ip() { curl -fsS https://checkip.amazonaws.com | tr -d '[:space:]'; }
SSH_OPTS=(-i "$KEY_FILE" -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 -o ServerAliveInterval=30)
rsh() { ssh "${SSH_OPTS[@]}" "ubuntu@$IP" "$@"; }
rsync_() { rsync -az -e "ssh ${SSH_OPTS[*]}" "$@"; }

firewall() {
  # your ISP/VPN may rotate the public IP within a block, so allow its /24 (override: SEPA_SSH_CIDR)
  local me cidr; me="$(my_ip)"; cidr="${SEPA_SSH_CIDR:-${me%.*}.0/24}"
  ls_ put-instance-public-ports --instance-name "$NAME" --port-infos "[
    {\"fromPort\":22,\"toPort\":22,\"protocol\":\"tcp\",\"cidrs\":[\"$cidr\"],\"cidrListAliases\":[\"lightsail-connect\"]},
    {\"fromPort\":80,\"toPort\":80,\"protocol\":\"tcp\",\"cidrs\":[\"0.0.0.0/0\"],\"ipv6Cidrs\":[\"::/0\"]},
    {\"fromPort\":443,\"toPort\":443,\"protocol\":\"tcp\",\"cidrs\":[\"0.0.0.0/0\"],\"ipv6Cidrs\":[\"::/0\"]}
  ]" >/dev/null
  echo "firewall: 80/443 open; SSH only from $cidr (and the Lightsail browser console)"
}

cmd_plan() {
  need_login
  read -r BID PRICE CPU DISK <<<"$(bundle)"
  [ -n "${BID:-}" ] && [ "$BID" != "None" ] || die "no ${RAM_GB} GB Linux bundle found in $REGION"
  cat <<EOF
Will create in $REGION ($AZ):
  - Lightsail instance '$NAME': Ubuntu 24.04, bundle $BID ($CPU vCPU, ${RAM_GB} GB RAM, ${DISK} GB SSD)
    US\$$PRICE / month (billed hourly; traffic allowance included)
  - Static IP '$IP_NAME' (free while attached to the instance)
  - SSH key pair '$KEY_NAME' (private key saved to $KEY_FILE)
  - Firewall: 80/443 open to the internet (Caddy HTTPS), 22 only from your current IP
Nothing has been created. Run '$0 up' to proceed.
EOF
}

cmd_up() {
  need_login
  [ -f config.yaml ] || die "run from the project (config.yaml not found)"
  [ -f positions.yaml ] || cp positions.example.yaml positions.yaml
  local created=0

  if [ ! -f "$KEY_FILE" ]; then
    if ls_ get-key-pair --key-pair-name "$KEY_NAME" >/dev/null 2>&1; then
      die "key pair $KEY_NAME exists in Lightsail but $KEY_FILE is missing - restore the file or delete the key pair"
    fi
    mkdir -p "$HOME/.ssh"
    (umask 077; ls_ create-key-pair --key-pair-name "$KEY_NAME" --query privateKeyBase64 > "$KEY_FILE")
    chmod 600 "$KEY_FILE"
    echo "created key pair -> $KEY_FILE"
  fi

  if ! ls_ get-instance --instance-name "$NAME" >/dev/null 2>&1; then
    read -r BID PRICE _ _ <<<"$(bundle)"
    echo "creating instance $NAME ($BID, US\$$PRICE/mo) in $AZ ..."
    ls_ create-instances --instance-names "$NAME" --availability-zone "$AZ" --blueprint-id ubuntu_24_04 \
      --bundle-id "$BID" --key-pair-name "$KEY_NAME" --user-data file://deploy/aws/cloud-init.sh \
      --tags key=app,value=sepa-scanner >/dev/null
    created=1
  fi
  printf "waiting for instance to run"
  until [ "$(ls_ get-instance-state --instance-name "$NAME" --query state.name 2>/dev/null)" = "running" ]; do
    printf "."; sleep 5
  done; echo

  if [ -z "$(static_ip)" ]; then
    ls_ allocate-static-ip --static-ip-name "$IP_NAME" >/dev/null
  fi
  if [ "$(ls_ get-static-ip --static-ip-name "$IP_NAME" --query staticIp.attachedTo)" != "$NAME" ]; then
    ls_ attach-static-ip --static-ip-name "$IP_NAME" --instance-name "$NAME" >/dev/null
  fi
  IP="$(static_ip)"; HOST="$(host_of "$IP")"; echo "$IP" > "$IP_CACHE"
  [ "$created" = 1 ] && ssh-keygen -R "$IP" >/dev/null 2>&1 || true
  firewall

  printf "waiting for SSH"
  for _ in $(seq 1 60); do rsh true 2>/dev/null && break; printf "."; sleep 5; done; echo
  echo "waiting for first-boot setup (Docker install, ~2-4 min on a new server) ..."
  rsh 'cloud-init status --wait >/dev/null 2>&1; test -f /opt/sepa/.cloud-init-done' \
    || die "first-boot setup failed - check: ssh in and run 'sudo cat /var/log/cloud-init-output.log'"

  echo "uploading code and config ..."
  rsync_ --delete --exclude __pycache__ minervini requirements.txt deploy "ubuntu@$IP:$R/app/"
  rsync_ deploy/aws/docker-compose.yml deploy/aws/Caddyfile config.yaml positions.yaml "ubuntu@$IP:$R/"
  if ! rsh "test -f $R/data/prices_daily.pkl"; then
    echo "seeding price/fundamentals cache (saves ~10 min on the first scan) ..."
    rsync_ data/prices_daily.pkl data/fundamentals data/sec_tickers.json data/universe.csv "ubuntu@$IP:$R/data/" 2>/dev/null || true
  fi

  # server .env: messaging + SEC contact from your local .env; dashboard token generated once and kept
  local token port
  token="$(rsh "grep -s '^DASHBOARD_TOKEN=' $R/.env | cut -d= -f2-" || true)"
  [ -n "$token" ] || token="$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')"
  port="$(rsh "grep -s '^IBKR_PORT=' $R/.env | cut -d= -f2-" || true)"
  {
    grep -E '^(TELEGRAM_BOT_TOKEN|TELEGRAM_CHAT_ID|CALLMEBOT_PHONE|CALLMEBOT_APIKEY|TWILIO_[A-Z_]+|SEC_USER_AGENT|ACCOUNT_EQUITY)=.+' .env 2>/dev/null \
      | grep -v 'XXXX' || true
    echo "DASHBOARD_URL=https://$HOST/"
    echo "DASHBOARD_TOKEN=$token"
    echo "DOMAIN=$HOST"
    echo "IBKR_PORT=${port:-4003}"
  } | rsh "umask 077; cat > $R/.env"
  rsh "cd $R && touch ib.env secrets/tws_password.txt && chmod 600 ib.env secrets/tws_password.txt"

  echo "building and starting containers (first build ~2-3 min) ..."
  rsh "cd $R && { sudo docker compose up -d --build --remove-orphans scanner caddy \
       && if [ -s secrets/tws_password.txt ]; then sudo docker compose --profile ibkr up -d; fi; } > deploy.log 2>&1; \
       rc=\$?; tail -4 deploy.log; exit \$rc" || die "container start failed - see $R/deploy.log on the server"

  cat <<EOF

Deployed. Dashboard (works from anywhere; the HTTPS certificate takes ~1 minute on first start):
  https://$HOST/?k=$token

The server runs its first full scan now (~3-5 min with the seeded cache).
Next: $0 credentials   (connect IBKR)
EOF
}

cmd_credentials() {
  IP="$(server_ip)"; [ -n "$IP" ] || die "not deployed yet - run: $0 up"
  cat <<'EOF'
Your IBKR login is stored only on your server (/opt/sepa/secrets, readable by you and root there).
Strongly recommended: use a SECOND IBKR username for the server (Client Portal -> Settings ->
Users & Access Rights -> add user, with no trading permissions). With the same username as your
phone/desktop, the sessions log each other out.
EOF
  local U P M PORT
  read -rp "IBKR username: " U
  read -rsp "IBKR password (hidden): " P; echo
  read -rp "Account mode, live or paper [live]: " M; M="${M:-live}"
  case "$M" in live) PORT=4003 ;; paper) PORT=4004 ;; *) die "mode must be live or paper" ;; esac
  printf '%s' "$P" | rsh "umask 077; cat > $R/secrets/tws_password.txt"; unset P
  printf 'TWS_USERID=%s\nTRADING_MODE=%s\n' "$U" "$M" | rsh "umask 077; cat > $R/ib.env"
  rsh "sed -i '/^IBKR_PORT=/d' $R/.env && echo IBKR_PORT=$PORT >> $R/.env && cd $R \
       && sudo docker compose --profile ibkr up -d --force-recreate ib-gateway && sudo docker compose up -d scanner" \
    | tail -3
  echo
  echo ">>> Approve the IB Gateway login on IBKR Mobile now (you have ~3 minutes)."
  echo "    Then check the connection:  $0 ibkr-check"
}

cmd_ibkr_check() {
  IP="$(server_ip)"
  rsh "cd $R && sudo docker compose exec -T scanner python -m minervini ibkr-check"
}

cmd_status() {
  IP="$(server_ip)"; [ -n "$IP" ] || die "not deployed"
  rsh "cd $R && sudo docker compose --profile ibkr ps --format 'table {{.Service}}\t{{.Status}}' \
       && echo && sudo docker compose logs --tail 12 --no-log-prefix scanner"
  echo; echo "Dashboard: https://$(host_of "$IP")/?k=$(rsh "grep '^DASHBOARD_TOKEN=' $R/.env | cut -d= -f2-")"
}

cmd_logs() { IP="$(server_ip)"; rsh -t "cd $R && sudo docker compose --profile ibkr logs -f --tail 100 ${1:-scanner}"; }
cmd_ssh() { IP="$(server_ip)"; ssh "${SSH_OPTS[@]}" -t "ubuntu@$IP" "cd $R; exec bash -l"; }
cmd_allow_ip() { need_login; firewall; }

cmd_destroy() {
  need_login
  echo "This permanently deletes instance '$NAME' (and its data), static IP '$IP_NAME' and key pair '$KEY_NAME' in $REGION."
  read -rp "Type the instance name to confirm: " c
  [ "$c" = "$NAME" ] || die "not confirmed"
  ls_ delete-instance --instance-name "$NAME" >/dev/null || true
  ls_ release-static-ip --static-ip-name "$IP_NAME" >/dev/null || true
  ls_ delete-key-pair --key-pair-name "$KEY_NAME" >/dev/null || true
  rm -f "$KEY_FILE" "$IP_CACHE"
  echo "deleted."
}

case "${1:-}" in
  plan) cmd_plan ;;
  up) cmd_up ;;
  credentials) cmd_credentials ;;
  ibkr-check) cmd_ibkr_check ;;
  status) cmd_status ;;
  logs) shift; cmd_logs "${1:-scanner}" ;;
  ssh) cmd_ssh ;;
  allow-ip) cmd_allow_ip ;;
  destroy) cmd_destroy ;;
  *) sed -n '2,15p' "$0"; exit 1 ;;
esac
