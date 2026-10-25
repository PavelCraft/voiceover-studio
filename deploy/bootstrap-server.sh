#!/usr/bin/env bash
set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo "Run with sudo: sudo bash deploy/bootstrap-server.sh" >&2
  exit 1
fi

APP_DIR=/var/www/voiceover-studio
STATE_DIR=/var/lib/voiceover-studio
MEDIA_IMAGE=/var/lib/voiceover-studio-media.ext4
MEDIA_DIR=${STATE_DIR}/media
DEPLOY_USER=deploy

test -d "${APP_DIR}/.git" || { echo "Clone the repository to ${APP_DIR} first" >&2; exit 1; }

apt-get update
apt-get install -y python3-venv python3-pip ffmpeg gettext nginx

install -d -o "${DEPLOY_USER}" -g "${DEPLOY_USER}" "${STATE_DIR}" "${STATE_DIR}/static" "${STATE_DIR}/backups"

if [[ ! -f "${MEDIA_IMAGE}" ]]; then
  truncate -s 7G "${MEDIA_IMAGE}"
  mkfs.ext4 -F -L voiceover-media "${MEDIA_IMAGE}"
fi
install -d "${MEDIA_DIR}"
if ! grep -Fq "${MEDIA_IMAGE} ${MEDIA_DIR}" /etc/fstab; then
  echo "${MEDIA_IMAGE} ${MEDIA_DIR} ext4 loop,nofail,nodev,nosuid 0 0" >> /etc/fstab
fi
mountpoint -q "${MEDIA_DIR}" || mount "${MEDIA_DIR}"
chown "${DEPLOY_USER}:${DEPLOY_USER}" "${MEDIA_DIR}"
install -d -o "${DEPLOY_USER}" -g "${DEPLOY_USER}" "${MEDIA_DIR}/tmp"

if [[ ! -f /etc/voiceover-studio.env ]]; then
  install -m 0640 -o root -g "${DEPLOY_USER}" "${APP_DIR}/deploy/voiceover-studio.env.example" /etc/voiceover-studio.env
  echo "Edit /etc/voiceover-studio.env before starting the services." >&2
fi

if [[ ! -x "${APP_DIR}/.venv/bin/python" ]]; then
  sudo -u "${DEPLOY_USER}" python3 -m venv "${APP_DIR}/.venv"
fi
sudo -u "${DEPLOY_USER}" "${APP_DIR}/.venv/bin/pip" install --upgrade pip
sudo -u "${DEPLOY_USER}" "${APP_DIR}/.venv/bin/pip" install -r "${APP_DIR}/requirements.txt"

install -m 0644 "${APP_DIR}/deploy/voiceover-studio-web.service" /etc/systemd/system/voiceover-studio-web.service
install -m 0644 "${APP_DIR}/deploy/voiceover-studio-worker.service" /etc/systemd/system/voiceover-studio-worker.service
install -m 0644 "${APP_DIR}/deploy/nginx.studio.formacode.dev.conf" /etc/nginx/sites-available/studio.formacode.dev
ln -sfn /etc/nginx/sites-available/studio.formacode.dev /etc/nginx/sites-enabled/studio.formacode.dev

cat >/etc/sudoers.d/voiceover-studio-deploy <<'EOF'
deploy ALL=(root) NOPASSWD: /usr/bin/systemctl restart voiceover-studio-web, /usr/bin/systemctl restart voiceover-studio-worker, /usr/bin/systemctl is-active --quiet voiceover-studio-web, /usr/bin/systemctl is-active --quiet voiceover-studio-worker
EOF
chmod 0440 /etc/sudoers.d/voiceover-studio-deploy
visudo -cf /etc/sudoers.d/voiceover-studio-deploy

systemctl daemon-reload
nginx -t

echo
echo "Bootstrap complete. Next:"
echo "  1. Edit /etc/voiceover-studio.env"
echo "  2. Run initial migrate/collectstatic as deploy"
echo "  3. Enable the two services and reload nginx"
echo "  4. Configure TLS for studio.formacode.dev"
