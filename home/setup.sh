#!/bin/sh
# First-time configuration of the home stack behind your own reverse proxy: writes pager.env and
# .env next to this file, then prints what to add to nginx or traefik. It starts nothing and never
# overwrites an existing file without --force. README.md, "Install and first run".
set -eu
cd "$(dirname "$0")"

# Where docker-compose.yml publishes the app servers unless .env says otherwise.
HTTP_LOCAL=18480 MQTT_LOCAL=11883

usage() {
    cat <<'EOF'
Usage: ./setup.sh [options]
       ./setup.sh --print-proxy [--proxy nginx|traefik|both]

  --host NAME          the name pagers, phones and browsers use for this machine,
                       e.g. a dynamic-DNS name (asked for when missing)
  --path PATH          where the app is served (default /kid-pager; / for the root)
  --web-port N         the https port your reverse proxy answers on, as the internet
                       sees it (default 443)
  --mqtt-port N        the port your reverse proxy answers pagers on (MQTT over
                       TLS), as the internet sees it (default 8883)
  --proxy KIND         print configuration for nginx, traefik or both (default both)
  --upstream ADDR      the address your proxy reaches this machine at (default
                       127.0.0.1). An IP address other than 127.0.0.1, such as the
                       Docker bridge 172.17.0.1 for a proxy in a container, also
                       becomes the address the app ports listen on.
  --print-proxy        write nothing; print the proxy configuration for the
                       pager.env and .env that are already here
  --force              replace an existing pager.env and .env (the old ones are kept
                       as *.bak)
  -h, --help           this text
EOF
}

die() {
    echo "setup.sh: $*" >&2
    exit 1
}

host="" path="" web_port="" mqtt_port="" proxy="" upstream=""
force=0 print_only=0
while [ $# -gt 0 ]; do
    case "$1" in
    --host) host="${2:?--host needs a value}"; shift 2 ;;
    --path) path="${2:?--path needs a value}"; shift 2 ;;
    --web-port) web_port="${2:?--web-port needs a value}"; shift 2 ;;
    --mqtt-port) mqtt_port="${2:?--mqtt-port needs a value}"; shift 2 ;;
    --proxy) proxy="${2:?--proxy needs a value}"; shift 2 ;;
    --upstream) upstream="${2:?--upstream needs a value}"; shift 2 ;;
    --print-proxy) print_only=1; shift ;;
    --force) force=1; shift ;;
    -h | --help) usage; exit 0 ;;
    *) usage >&2; die "unknown option: $1" ;;
    esac
done

case "${proxy:-both}" in
nginx | traefik | both) ;;
*) die "--proxy is nginx, traefik or both, not $proxy" ;;
esac

# value FILE KEY: the last KEY=... in FILE, or nothing.
value() {
    [ -f "$1" ] || return 0
    sed -n "s/^$2=//p" "$1" | tail -n 1
}

# An address is an IPv4 literal when it has only digits and dots.
is_ip() {
    case "$1" in
    *[!0-9.]* | '') return 1 ;;
    *) return 0 ;;
    esac
}

print_nginx() {
    cat <<EOF
---- nginx ----------------------------------------------------------------
Inside the server { } block that answers https://$host$url_port (within http { }):

    location $app_path/ {
        proxy_pass http://$upstream:$http_local;    # no path after the port
        proxy_http_version 1.1;
        proxy_set_header Connection "";
        proxy_set_header Host \$http_host;
        proxy_set_header X-Forwarded-For \$remote_addr;
        proxy_set_header X-Forwarded-Proto https;
        proxy_buffering off;                       # the live-update stream
        proxy_read_timeout 1h;
        client_max_body_size 16m;
    }
EOF
    if [ -n "$app_path" ]; then
        echo "    location = $app_path { return 308 $app_path/; }"
    else
        echo "    location /internal/ { return 404; }"
    fi
    cat <<EOF

For pagers, at the top level of nginx.conf (not inside http { }; needs the
stream module), with the certificate of https://$host$url_port:

    stream {
        server {
            listen $mqtt_port ssl;
            ssl_certificate     /path/to/fullchain.pem;
            ssl_certificate_key /path/to/privkey.pem;
            ssl_protocols TLSv1.2 TLSv1.3;         # the pager's modem speaks TLS 1.2
            proxy_timeout 1h;                      # a sleeping pager stays connected
            proxy_pass $upstream:$mqtt_local;
        }
    }
EOF
}

print_traefik() {
    rule="Host(\`$host\`)"
    if [ -n "$app_path" ]; then
        rule="$rule && PathPrefix(\`$app_path\`)"
    else
        rule="$rule && !PathPrefix(\`/internal/\`)"
    fi
    cat <<EOF
---- traefik --------------------------------------------------------------
Dynamic configuration (a file your file provider loads). "websecure" is the
entry point that answers https://$host$url_port and "letsencrypt" your
certificate resolver: use your own names.

    http:
      routers:
        kid-pager:
          rule: "$rule"
          entryPoints: [websecure]
          service: kid-pager
          tls:
            certResolver: letsencrypt
      services:
        kid-pager:
          loadBalancer:
            servers:
              - url: "http://$upstream:$http_local"
    tcp:
      routers:
        kid-pager-mqtt:
          rule: "HostSNI(\`*\`)"
          entryPoints: [pager-mqtt]
          service: kid-pager-mqtt
          tls:
            certResolver: letsencrypt
            domains:
              - main: "$host"
      services:
        kid-pager-mqtt:
          loadBalancer:
            servers:
              - address: "$upstream:$mqtt_local"

and a new entry point for pagers in the static configuration:

    entryPoints:
      pager-mqtt:
        address: ":$mqtt_port"
EOF
}

print_proxy() {
    app_path="${path:-/kid-pager}"
    app_path="${app_path%/}"
    url_port=":$web_port"
    [ "$web_port" != 443 ] || url_port=""
    echo
    echo "Add this to your reverse proxy, then reload it:"
    echo
    case "${proxy:-both}" in
    nginx) print_nginx ;;
    traefik) print_traefik ;;
    both) print_nginx; echo; print_traefik ;;
    esac
    cat <<EOF

Router: forward TCP port $mqtt_port to your proxy, like port $web_port. Pagers on
Soracom Beam use the same port: the Beam group's destination is
mqtts://$host:$mqtt_port.

Then:
  docker compose up -d
and open https://$host$url_port$app_path/ to create the first admin.

Check from a phone on mobile data, not your Wi-Fi, that pagers can reach you:
  openssl s_client -connect $host:$mqtt_port
It must show a certificate for $host from a public authority. The relay reads
it from that same address to tell pagers which authority to trust, and gives
out no setup code until it can.
EOF
}

# The port of a compose bind value such as 127.0.0.1:18480, or the default.
bind_port() { # bind_port KEY DEFAULT
    v=$(value .env "$1")
    [ -n "$v" ] || { echo "$2"; return; }
    echo "${v##*:}"
}

if [ "$print_only" = 1 ]; then
    [ -f pager.env ] || die "there is no pager.env here yet; run ./setup.sh first"
    host=$(value pager.env PAGER_HOST)
    path=$(value pager.env PAGER_PATH)
    web_port=$(value .env PAGER_WEB_PORT)
    mqtt_port=$(value .env PAGER_MQTT_PORT)
    http_local=$(bind_port PAGER_APP_HTTP_BIND "$HTTP_LOCAL")
    mqtt_local=$(bind_port PAGER_APP_MQTT_BIND "$MQTT_LOCAL")
    if [ -z "$upstream" ]; then
        bind=$(value .env PAGER_APP_HTTP_BIND)
        case "$bind" in
        *:*) upstream="${bind%:*}" ;;
        esac
    fi
    upstream="${upstream:-127.0.0.1}"
    web_port="${web_port:-443}" mqtt_port="${mqtt_port:-8883}"
    print_proxy
    exit 0
fi

if [ "$force" = 0 ]; then
    for f in pager.env .env; do
        [ ! -e "$f" ] || die "$f already exists; edit it, pass --force to start over, or --print-proxy to see the proxy configuration again"
    done
fi

interactive=0
[ -t 0 ] && interactive=1

ask() { # ask VAR "question" [default]
    [ "$interactive" = 1 ] || return 0
    if [ -n "${3:-}" ]; then
        printf '%s [%s]: ' "$2" "$3"
    else
        printf '%s: ' "$2"
    fi
    read -r reply || reply=""
    [ -n "$reply" ] || reply="${3:-}"
    eval "$1=\$reply"
}

if [ -z "$host" ]; then
    ask host "Name of this server as the internet sees it (e.g. myhome.dedyn.io)"
fi
[ -n "$host" ] || die "a host name is required: --host NAME"
case "$host" in
*[!A-Za-z0-9.:-]* | *://*) die "the host is a bare name or IP address, not $host" ;;
esac

[ -n "$web_port" ] || ask web_port "The https port your reverse proxy answers on" 443
[ -n "$mqtt_port" ] || ask mqtt_port "The port it should answer pagers on" 8883
[ -n "$proxy" ] || ask proxy "Print configuration for nginx, traefik or both" both
case "${proxy:-both}" in
nginx | traefik | both) ;;
*) die "--proxy is nginx, traefik or both, not $proxy" ;;
esac

case "${path:-/kid-pager}" in
/ | /*) ;;
*) die "--path starts with /, like /kid-pager" ;;
esac

check_port() { # check_port NAME VALUE
    case "$2" in
    '' | *[!0-9]*) die "$1 is a port number, not $2" ;;
    esac
    [ "$2" -ge 1 ] && [ "$2" -le 65535 ] || die "$1 is a port number from 1 to 65535, not $2"
}

web_port="${web_port:-443}" mqtt_port="${mqtt_port:-8883}"
check_port --web-port "$web_port"
check_port --mqtt-port "$mqtt_port"
[ "$web_port" != "$mqtt_port" ] || die "the web and pager ports must differ"
upstream="${upstream:-127.0.0.1}"
case "$upstream" in
*[!A-Za-z0-9.-]*) die "--upstream is a name or an IPv4 address, not $upstream" ;;
esac

if [ "$force" = 1 ]; then
    for f in pager.env .env; do
        [ ! -e "$f" ] || mv "$f" "$f.bak"
    done
fi

{
    echo "# Written by setup.sh. Nothing here is secret: the stack generates every password and key"
    echo "# into data/. pager.env.example explains each value."
    echo "PAGER_HOST=$host"
    echo "# proxy: your reverse proxy terminates TLS for the web app and for pagers."
    echo "TLS_MODE=proxy"
    [ -z "$path" ] || echo "PAGER_PATH=$path"
} >pager.env

{
    echo "# Written by setup.sh; read by docker compose. docker-compose.yml explains each value."
    echo "# The ports your reverse proxy answers on, as the outside world dials them. Pagers store"
    echo "# the pager port: choose before you provision any."
    echo "PAGER_WEB_PORT=$web_port"
    echo "PAGER_MQTT_PORT=$mqtt_port"
    if is_ip "$upstream" && [ "$upstream" != 127.0.0.1 ]; then
        echo "# Where the proxy reaches the relay and the broker."
        echo "PAGER_APP_HTTP_BIND=$upstream:$HTTP_LOCAL"
        echo "PAGER_APP_MQTT_BIND=$upstream:$MQTT_LOCAL"
    fi
} >.env

echo
echo "Wrote $(pwd)/pager.env and $(pwd)/.env"
http_local=$HTTP_LOCAL mqtt_local=$MQTT_LOCAL
print_proxy
if ! is_ip "$upstream"; then
    cat <<EOF

$upstream is a name, so the app ports still listen on 127.0.0.1 only. If your
proxy cannot reach that, set PAGER_APP_HTTP_BIND and PAGER_APP_MQTT_BIND in .env
(docker-compose.yml).
EOF
fi
