#!/usr/bin/env bash
# Run as root on the existing Oracle VPS. Only modifies the separate adviser copy.
set -euo pipefail
ADVISOR_DIR=/home/oracle/oracle-advisor
ORACLE_DIR=/home/oracle/oracle
[ "$(id -u)" = 0 ] || { echo 'Run as root'; exit 1; }
[ -f "$ADVISOR_DIR/advisor/oauth.py" ] || { echo 'Update the adviser copy first'; exit 1; }
[ -f "$ORACLE_DIR/.env" ] || { echo 'Existing Oracle .env not found'; exit 1; }
if systemctl is-active --quiet oracle-advisor-pilot; then
  echo 'Pilot already running; leaving it unchanged.'
  cat "$ADVISOR_DIR/storage/public_url.txt"
  exit 0
fi
install -d -o oracle -g oracle -m 700 "$ADVISOR_DIR/storage" "$ADVISOR_DIR/bin"
case "$(uname -m)" in
  x86_64) ADVISOR_ARCH=amd64 ;;
  aarch64) ADVISOR_ARCH=arm64 ;;
  *) echo 'Unsupported server architecture'; exit 1 ;;
esac
curl --fail --location --proto '=https' --tlsv1.2 \
  "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-$ADVISOR_ARCH" \
  -o "$ADVISOR_DIR/bin/cloudflared.download"
chmod 755 "$ADVISOR_DIR/bin/cloudflared.download"
mv "$ADVISOR_DIR/bin/cloudflared.download" "$ADVISOR_DIR/bin/cloudflared"
chown root:root "$ADVISOR_DIR/bin/cloudflared"
sudo -u oracle "$ADVISOR_DIR/.venv-advisor/bin/pip" install -r "$ADVISOR_DIR/advisor/requirements.txt"
sudo -u oracle bash -c 'cd /home/oracle/oracle-advisor; .venv-advisor/bin/python -m unittest discover -s advisor/tests' >/tmp/oracle-advisor-tests.log 2>&1 || { tail -30 /tmp/oracle-advisor-tests.log; exit 1; }
# Generate locally, never print the secret or put it on a command line.
sudo -u oracle "$ADVISOR_DIR/.venv-advisor/bin/python" - <<'PY'
import os,secrets
from pathlib import Path
os.umask(0o077)
p=Path('/home/oracle/oracle-advisor/.env.advisor')
if not p.exists():
    p.write_text('ORACLE_OWNER_PASSWORD='+secrets.token_urlsafe(48)+'\n')
else:
    from dotenv import dotenv_values
    if len(dotenv_values(p).get('ORACLE_OWNER_PASSWORD') or '')<32:
        raise SystemExit('Existing .env.advisor lacks owner password; left unchanged')
PY
cat > /etc/systemd/system/oracle-advisor-pilot.service <<'UNIT'
[Unit]
Description=Oracle adviser private-data HTTPS pilot (temporary URL)
After=network-online.target
Wants=network-online.target
[Service]
User=oracle
WorkingDirectory=/home/oracle/oracle-advisor
ExecStart=/bin/bash -c 'set -a; source /home/oracle/oracle/.env; source /home/oracle/oracle-advisor/.env.advisor; export ORACLE_DATA_ROOT=/home/oracle/oracle; exec .venv-advisor/bin/python -m advisor.pilot'
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=/home/oracle/oracle-advisor/storage
Restart=no
[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl start oracle-advisor-pilot
# Bounded wait for readiness; no secrets printed.
for ADVISOR_ATTEMPT in $(seq 1 100); do
  if [ -s "$ADVISOR_DIR/storage/public_url.txt" ]; then
    echo 'READY — send only this URL to ChatGPT:'
    cat "$ADVISOR_DIR/storage/public_url.txt"
    exit 0
  fi
  systemctl is-active --quiet oracle-advisor-pilot || break
  sleep 1
done
echo 'Pilot not ready. Diagnostic command: journalctl -u oracle-advisor-pilot -n 30 --no-pager'
exit 1
