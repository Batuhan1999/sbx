"""nginx's front on 443 (tls.sh): its main configuration, with the stream
block whose front reads each connection's TLS server name without
decrypting (ssl_preread) and the HTTPS server behind it on HTTPS_BACKEND,
which takes the client's address from the PROXY protocol. The stream front
itself and the endpoints' servers are endpoints.nginx's."""
import sys

PORT = 443
HTTPS_BACKEND = "127.0.0.1:8442"        # nginx's HTTPS server (tls.sh: HTTPS_LISTEN), behind the stream front
HTTPS_LISTEN = f"{HTTPS_BACKEND} ssl proxy_protocol"
WORKER_SHUTDOWN = "30s"                 # how long a reload's old workers keep their connections

MAIN_CONF = f"""# nginx's main configuration (tls.sh, tls_front.py): the image's own, the client's
# address from the PROXY protocol (the stream front on 443 passes it to the HTTPS
# server on {HTTPS_BACKEND}), and the stream block (conf.d/*.stream).
user nginx;
worker_processes auto;
# A reload's old workers end their connections after this: a revoked or removed
# endpoint's sessions with them (endpoints), and long previews (they reconnect).
worker_shutdown_timeout {WORKER_SHUTDOWN};
error_log /var/log/nginx/error.log notice;
pid /run/nginx.pid;
events {{
  worker_connections 1024;
}}
http {{
  include /etc/nginx/mime.types;
  default_type application/octet-stream;
  set_real_ip_from 127.0.0.1;
  real_ip_header proxy_protocol;
  log_format main '$remote_addr - $remote_user [$time_local] "$request" '
                  '$status $body_bytes_sent "$http_referer" "$http_user_agent" "$http_x_forwarded_for"';
  access_log /var/log/nginx/access.log main;
  sendfile on;
  keepalive_timeout 65;
  include /etc/nginx/conf.d/*.conf;
}}
stream {{
  include /etc/nginx/conf.d/*.stream;
}}
"""


if __name__ == "__main__":
    if sys.argv[1:] == ["main-conf"]:
        print(MAIN_CONF, end="")
    elif sys.argv[1:] == ["https-listen"]:
        print(HTTPS_LISTEN)
    else:
        raise SystemExit("usage: python -m sbx_control.tls_front main-conf | https-listen")
