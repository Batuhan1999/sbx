#!/bin/sh
# HTTPS on the host, run in a release by host-install.sh and sbx-apply tls:
# nginx and Certbot, using
# HTTP-01 for an IP or DNS-01 for a wildcard domain, serving
#   <login_host>.<domain>  sbx-control: the login, the API, /install
#   <service>.<domain>     the operator's services, behind the login
#   everything else        sandboxes, through their runtime's ingress (Embed's client-proxy on 3002)
# Every request to a service or a sandbox passes sbx-control's /gate first:
# previews for anyone signed in, owner-only ports for their owner. nginx
# removes the login cookie, and an sbx token, before a request reaches a
# sandbox, where an agent could read them.
set -eu
eval "$(venv/bin/python -m sbx_control.settings shell)"
# The renewal scripts below run whichever release is active then.
current=$(venv/bin/python -c 'from sbx_control import host; print(host.CURRENT)')
# Where previews go: the ingress of the sandbox's runtime (Embed's client-proxy),
# one upstream per enabled runtime, picked by the kind the gate names.
upstreams=$(venv/bin/python -m sbx_control.ip_previews upstreams)
# An https ingress (a provider's): SNI with the sandbox's host, its certificate verified.
upstream_tls=$(venv/bin/python -m sbx_control.ip_previews upstream-tls)
# A runtime that answers its sandboxes' ports only with their token (E2B
# Cloud's): the gate names it, nginx sends it to that runtime alone.
upstream_token=$(venv/bin/python -m sbx_control.ip_previews upstream-token)
dir=/opt/sbx-tls
install -d "$dir/letsencrypt" "$dir/nginx" /var/cache/sbx-gate
cert="/etc/letsencrypt/live/$domain"

# nginx in its container, started once, then told to read its configuration
# again: here and after every certificate a renewal script gets.
cat > /usr/local/sbin/sbx-tls-nginx <<NGINX
#!/bin/sh
set -eu
# One at a time: the installer and the tls effect may both start it on a
# first install, and both would find no container and create one.
exec 9>/run/sbx-tls-nginx.lock
flock 9
docker inspect sbx-tls-nginx >/dev/null 2>&1 || docker run -d --name sbx-tls-nginx --restart unless-stopped \\
  --network host -v $dir/nginx:/etc/nginx/conf.d:ro -v $dir/letsencrypt:/etc/letsencrypt:ro \\
  -v /var/cache/sbx-gate:/var/cache/nginx nginx:1.29-alpine >/dev/null || docker inspect sbx-tls-nginx >/dev/null
docker exec sbx-tls-nginx nginx -t
docker exec sbx-tls-nginx nginx -s reload
NGINX
chmod 0755 /usr/local/sbin/sbx-tls-nginx

# sbx-control behind nginx, for every server that serves it; /internal/ is
# for callers on this host only, and /gate for nginx's own subrequests (its
# answer may carry a sandbox's traffic token).
cat > "$dir/nginx/control.inc" <<NGINX
client_max_body_size 50m;
include /etc/nginx/conf.d/extensions/*.locations;
location /internal/ { return 404; }
location = /gate { return 404; }
location / {
  proxy_pass http://127.0.0.1:8080;
  proxy_http_version 1.1;
  proxy_set_header Host \$host;
  proxy_set_header X-Forwarded-For \$remote_addr;
  proxy_set_header X-Forwarded-Proto https;
  proxy_set_header Upgrade \$http_upgrade;
  proxy_set_header Connection \$connection_upgrade;
  proxy_buffering off;
  proxy_read_timeout 1d;
}
NGINX

install_renewal_timer() {
  chmod 0755 /usr/local/sbin/sbx-tls-renew
  cat > /etc/systemd/system/sbx-tls-renew.service <<'UNIT'
[Unit]
Description=Issue or renew the installation certificate
Requires=docker.service
After=docker.service network-online.target
[Service]
Type=oneshot
ExecStart=/usr/local/sbin/sbx-tls-renew
UNIT
  cat > /etc/systemd/system/sbx-tls-renew.timer <<'UNIT'
[Unit]
Description=Check the installation certificate twice daily
[Timer]
OnBootSec=2min
OnUnitActiveSec=12h
Persistent=true
[Install]
WantedBy=timers.target
UNIT
  systemctl daemon-reload
  systemctl enable -q --now sbx-tls-renew.timer
  systemctl reset-failed sbx-tls-renew.service 2>/dev/null || true
  systemctl start sbx-tls-renew.service
}

ip_cert=bootstrap
if [ -n "$bootstrap_address" ] && [ -f "$dir/letsencrypt/live/$bootstrap_address/fullchain.pem" ] &&
    openssl x509 -in "$dir/letsencrypt/live/$bootstrap_address/fullchain.pem" -noout -checkend 0 >/dev/null; then
  ip_cert="$bootstrap_address"
fi

# Before an address is chosen, serve setup with a temporary certificate.
# Choosing the IP upgrades that origin to a renewable Let's Encrypt certificate.
if [ -z "$domain" ]; then
  live="$dir/letsencrypt/live/bootstrap"
  if [ ! -f "$live/fullchain.pem" ]; then
    install -d "$live"
    openssl req -x509 -newkey rsa:2048 -nodes -days 365 -subj "/CN=sbx-setup" \
      -keyout "$live/privkey.pem" -out "$live/fullchain.pem" 2>/dev/null
  fi
  cat > "$dir/nginx/default.conf" <<NGINX
map \$http_upgrade \$connection_upgrade { default upgrade; '' close; }
server {
  listen 443 ssl default_server;
  http2 on;
  server_name _;
  ssl_certificate /etc/letsencrypt/live/$ip_cert/fullchain.pem;
  ssl_certificate_key /etc/letsencrypt/live/$ip_cert/privkey.pem;
  include /etc/nginx/conf.d/control.inc;
}
NGINX
  venv/bin/python -m sbx_control.ip_previews nginx "$bootstrap_address" "$ip_cert" > "$dir/nginx/ip-previews.conf"
  rm -f "$dir/nginx/gate.inc"
  /usr/local/sbin/sbx-tls-nginx
  if [ -z "$bootstrap_address" ]; then
    echo "bootstrap: temporary certificate; choose an IP or domain in setup for trusted HTTPS"
    exit 0
  fi
  # Only Certbot listens on port 80, and only while validating the IP. The
  # existing nginx stays on 443 throughout issuance and renewal.
  systemctl restart sbx-host-guard
  cat > /usr/local/sbin/sbx-ip-tls-renew <<RENEW
#!/bin/sh
set -eu
rm -f $dir/ip-certificate-error
if ! docker run --rm --network host -v $dir/letsencrypt:/etc/letsencrypt \\
  -v $dir/certbot-work:/var/lib/letsencrypt -v $dir/certbot-logs:/var/log/letsencrypt \\
  certbot/certbot:v5.4.0 certonly --standalone --preferred-challenges http \\
  --preferred-profile shortlived --ip-address $bootstrap_address --cert-name $bootstrap_address \\
  --keep-until-expiring --agree-tos --register-unsafely-without-email --non-interactive \\
  > $dir/ip-certbot.log 2>&1; then
  tail -n 12 $dir/ip-certbot.log > $dir/ip-certificate-error
  cat $dir/ip-certificate-error >&2
  exit 1
fi
sed -i 's@/live/bootstrap/@/live/$bootstrap_address/@g' $dir/nginx/default.conf
if [ -f $dir/nginx/ip-previews.conf ]; then
  sed -i 's@/live/bootstrap/@/live/$bootstrap_address/@g' $dir/nginx/ip-previews.conf
fi
if [ -f $dir/nginx/bootstrap.conf ]; then
  sed -i 's@/live/bootstrap/@/live/$bootstrap_address/@g' $dir/nginx/bootstrap.conf
fi
if ! /usr/local/sbin/sbx-tls-nginx; then
  echo 'The certificate was issued, but nginx could not load it. Retry certificate setup.' > $dir/ip-certificate-error
  exit 1
fi
openssl x509 -in $dir/letsencrypt/live/$bootstrap_address/fullchain.pem -noout -fingerprint -sha256 \\
  > $dir/ip-certificate.fingerprint
RENEW
  chmod 0755 /usr/local/sbin/sbx-ip-tls-renew
  cat > /usr/local/sbin/sbx-tls-renew <<'RENEW'
#!/bin/sh
set -eu
exec /usr/local/sbin/sbx-ip-tls-renew
RENEW
  install_renewal_timer
  exit 0
fi

rm -f "$dir/nginx/ip-previews.conf"
# Keep the original setup origin reachable while DNS and certificate issuance
# finish. Its certificate stays the one the operator already accepted.
bootstrap="$dir/nginx/bootstrap.conf"
preview_default=default_server
if [ -n "$bootstrap_address" ] && [ -f "$dir/letsencrypt/live/bootstrap/fullchain.pem" ]; then
  preview_default=""
  cat > "$bootstrap" <<NGINX
server {
  listen 443 ssl default_server;
  http2 on;
  server_name $bootstrap_address;
  ssl_certificate /etc/letsencrypt/live/$ip_cert/fullchain.pem;
  ssl_certificate_key /etc/letsencrypt/live/$ip_cert/privkey.pem;
  include /etc/nginx/conf.d/control.inc;
}
NGINX
else
  rm -f "$bootstrap"
fi

cat > "$dir/nginx/gate.inc" <<NGINX
location = /_sbx_gate {
  internal;
  proxy_pass http://127.0.0.1:8080/gate;
  proxy_pass_request_body off;
  proxy_set_header Content-Length "";
  proxy_set_header Host $login;
  proxy_set_header X-Forwarded-Proto https;
  proxy_set_header X-Original-Host \$host;
  proxy_set_header X-Original-Sandbox-Id \$http_e2b_sandbox_id;
  proxy_set_header X-Original-Sandbox-Port \$http_e2b_sandbox_port;
  # A CORS preflight carries no credentials: the gate lets it through to a
  # declared preview, without a login, and names its runtime.
  proxy_set_header X-Original-Method \$sbx_method;
  proxy_set_header Cookie \$http_cookie;
  proxy_set_header Authorization \$http_authorization;
  # nginx cannot purge an entry when a sandbox is deleted or its owner
  # changes: an allow lasts only a page load's burst of requests.
  proxy_cache gate;
  # An answer with a sandbox's traffic token is never written to the cache
  # on disk; nginx still reads the token from it (auth_request_set).
  proxy_no_cache \$upstream_http_x_sbx_upstream_token;
  # The method first: an anonymous preflight's allow never answers a GET.
  proxy_cache_key "\$sbx_method|\$host|\$http_e2b_sandbox_id|\$http_e2b_sandbox_port|\$cookie_sbx_session|\$http_authorization";
  proxy_cache_valid 200 1s;
  proxy_cache_valid 401 403 404 5s;
}
location @login { return 302 https://$login/auth/login?next=https://\$host\$request_uri; }
location @denied {
  default_type text/plain;
  if (\$sbx_gone) { return 404 "Nothing here: the sandbox doesn't exist or is gone, or it doesn't expose this port.\n"; }
  return 403 "Only the sandbox's owner may open this.\n";
}
NGINX

cat > "$dir/nginx/default.conf" <<NGINX
proxy_cache_path /var/cache/nginx/gate keys_zone=gate:2m max_size=20m inactive=5m;
map \$http_upgrade \$connection_upgrade { default upgrade; '' close; }
# The gate names the runtime's host (<port>-<id>) for readable host names.
map \$sbx_upstream \$sbx_target { "" \$host; default \$sbx_upstream; }
$upstreams
# An sbx API token must not reach a sandbox either; an Authorization header the
# gate did not use is the app's own (a JWT, Basic auth) and passes.
map \$sbx_auth \$sandbox_authorization { bearer ""; default \$http_authorization; }
map \$http_cookie \$sandbox_cookie {
  "~^(?<before>.*?);\\s*sbx_session=[^;]*(?<after>.*)\$" "\$before\$after";
  "~^sbx_session=[^;]*;?\\s*(?<after>.*)\$" "\$after";
  default \$http_cookie;
}
ssl_certificate $cert/fullchain.pem;
ssl_certificate_key $cert/privkey.pem;

server {
  listen 443 ssl;
  http2 on;
  server_name $login;
  include /etc/nginx/conf.d/control.inc;
}

# Sandboxes: every other name under the domain.
server {
  listen 443 ssl $preview_default;
  http2 on;
  server_name $domain *.$domain;
  client_max_body_size 0;
  include /etc/nginx/conf.d/gate.inc;
  location / {
    # The gate's subrequest is a GET; it reads the request's method from here.
    set \$sbx_method \$request_method;
    auth_request /_sbx_gate;
    auth_request_set \$sbx_upstream \$upstream_http_x_sbx_upstream;
    auth_request_set \$sbx_auth \$upstream_http_x_sbx_auth;
    auth_request_set \$sbx_gone \$upstream_http_x_sbx_gone;
    auth_request_set \$sbx_runtime \$upstream_http_x_sbx_runtime;
    error_page 401 = @login;
    error_page 403 = @denied;
    proxy_pass \$sbx_ingress;
$upstream_tls
$upstream_token
    proxy_http_version 1.1;
    # The runtime routes on Host; the app learns the host the browser used
    # from X-Forwarded-Host (Rails, Django, Next.js and Phoenix read it).
    proxy_set_header Host \$sbx_target;
    proxy_set_header X-Forwarded-Host \$host;
    proxy_set_header X-Forwarded-Proto https;
    proxy_set_header X-Forwarded-For \$remote_addr;
    proxy_set_header Cookie \$sandbox_cookie;
    proxy_set_header Authorization \$sandbox_authorization;
    proxy_set_header Upgrade \$http_upgrade;
    proxy_set_header Connection \$connection_upgrade;
    proxy_buffering off;
    proxy_read_timeout 1d;
    proxy_send_timeout 1d;
  }
}
NGINX
for service in $services; do
  name=${service%%:*}; upstream=${service#*:}
  cat >> "$dir/nginx/default.conf" <<NGINX

server {
  listen 443 ssl;
  http2 on;
  server_name $name.$domain;
  client_max_body_size 50m;
  include /etc/nginx/conf.d/gate.inc;
  location / {
    set \$sbx_method \$request_method;
    auth_request /_sbx_gate;
    auth_request_set \$sbx_gone \$upstream_http_x_sbx_gone;
    error_page 401 = @login;
    error_page 403 = @denied;
    proxy_pass http://$upstream;
    proxy_http_version 1.1;
    proxy_set_header Host \$host;
    proxy_set_header X-Forwarded-Proto https;
    proxy_set_header Cookie \$sandbox_cookie;
    proxy_set_header Upgrade \$http_upgrade;
    proxy_set_header Connection \$connection_upgrade;
    proxy_buffering off;
  }
}
NGINX
done

# The certificate: DNS-01 through the configured provider, or, with none, a
# self-signed one for a first test (browsers warn, the sbx CLI refuses it).
if [ "$dns" = none ]; then
  live="$dir/letsencrypt/live/$domain"
  if [ ! -f "$live/fullchain.pem" ]; then
    install -d "$live"
    openssl req -x509 -newkey rsa:2048 -nodes -days 365 -subj "/CN=$domain" \
      -addext "subjectAltName=DNS:$domain,DNS:*.$domain" \
      -keyout "$live/privkey.pem" -out "$live/fullchain.pem" 2>/dev/null
  fi
  /usr/local/sbin/sbx-tls-nginx
  exit 0
fi
envfile=""
case "$dns" in
  delegated)
    # This host answers the challenge itself (sbx_control/acme_dns.py, on 53):
    # certbot runs here, in the venv, with the hooks that set and clear the token.
    [ -x venv/bin/certbot ] || venv/bin/pip install -q certbot >/dev/null
    cat > /usr/local/sbin/sbx-tls-renew <<RENEW
#!/bin/sh
set -eu
cd $current
venv/bin/certbot certonly --manual --preferred-challenges dns \\
  --manual-auth-hook 'venv/bin/python -m sbx_control.acme_dns set "\$CERTBOT_VALIDATION"' \\
  --manual-cleanup-hook 'venv/bin/python -m sbx_control.acme_dns clear' \\
  --config-dir $dir/letsencrypt --work-dir $dir/certbot-work --logs-dir $dir/certbot-logs \\
  --cert-name $domain -d $domain -d '*.$domain' --expand --keep-until-expiring --agree-tos -m $email -n
/usr/local/sbin/sbx-tls-nginx
[ ! -x /usr/local/sbin/sbx-ip-tls-renew ] || /usr/local/sbin/sbx-ip-tls-renew
RENEW
    chmod 0755 /usr/local/sbin/sbx-tls-renew
    delegated=1 ;;
  route53)
    # The instance role on EC2; elsewhere (bare metal) an access key from the
    # platform secrets, for an IAM user that may change the zone's
    # _acme-challenge records only.
    plugin="--dns-route53"; creds=""
    key=$(venv/bin/python -m sbx_control.settings secret AWS_ACCESS_KEY_ID 2>/dev/null || true)
    if [ -n "$key" ]; then
      secret=$(venv/bin/python -m sbx_control.settings secret AWS_SECRET_ACCESS_KEY)
      umask 077; printf 'AWS_ACCESS_KEY_ID=%s\nAWS_SECRET_ACCESS_KEY=%s\n' "$key" "$secret" > "$dir/route53.env"; umask 022
      envfile="--env-file $dir/route53.env"
    fi ;;
  cloudflare)
    [ -n "${CLOUDFLARE_API_TOKEN:-}" ] || CLOUDFLARE_API_TOKEN=$(venv/bin/python -m sbx_control.settings secret CLOUDFLARE_API_TOKEN)
    umask 077; printf 'dns_cloudflare_api_token = %s\n' "$CLOUDFLARE_API_TOKEN" > "$dir/letsencrypt/cloudflare.ini"; umask 022
    plugin="--dns-cloudflare"; creds="--dns-cloudflare-credentials /etc/letsencrypt/cloudflare.ini" ;;
  *) echo "acme: $dns is not supported (delegated, route53, cloudflare, none)"; exit 1 ;;
esac
[ -n "${delegated:-}" ] || cat > /usr/local/sbin/sbx-tls-renew <<RENEW
#!/bin/sh
set -eu
docker run --rm --network host $envfile -v $dir/letsencrypt:/etc/letsencrypt certbot/dns-$dns certonly $plugin $creds \\
  --cert-name $domain -d $domain -d '*.$domain' --expand --keep-until-expiring --agree-tos -m $email -n
/usr/local/sbin/sbx-tls-nginx
[ ! -x /usr/local/sbin/sbx-ip-tls-renew ] || /usr/local/sbin/sbx-ip-tls-renew
RENEW
install_renewal_timer
