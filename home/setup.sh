#!/bin/sh
# First-time configuration of the home stack behind your own reverse proxy: writes pager.env and
# .env next to this file, then prints what to add to nginx or traefik. It starts nothing and never
# overwrites an existing file without --force. README.md, "Install and first run".
set -eu
cd "$(dirname "$0")"

# Where docker-compose.yml publishes the app servers unless .env says otherwise.
HTTP_LOCAL=18480 MQTTS_LOCAL=18883 MQTT_LOCAL=11883

usage() {
    cat <<'EOF'
Usage: ./setup.sh [options]
       ./setup.sh --print-proxy [--proxy nginx|traefik|both]

  --host NAME          the name pagers, phones and browsers use for this machine,
                       e.g. a dynamic-DNS name (asked for when missing)
  --path PATH          where the app is served (default /kid-pager; / for the root)
  --web-port N         the https port your reverse proxy answers on, as the internet
                       sees it (default 443)
  --mqtt-port N        the pager port as the internet sees it; your router forwards
                       it to port 18883 of this machine (default 18883)
  --beam-port N        a port for pagers on Soracom Beam, which your proxy serves
                       with its public certificate (default: none)
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

host="" path="" web_port="" mqtt_port="" beam_port="" proxy="" upstream=""
force=0 print_only=0
while [ $# -gt 0 ]; do
    case "$1" in
    --host) host="${2:?--host needs a value}"; shift 2 ;;
    --path) path="${2:?--path needs a value}"; shift 2 ;;
    --web-port) web_port="${2:?--web-port needs a value}"; shift 2 ;;
    --mqtt-port) mqtt_port="${2:?--mqtt-port needs a value}"; shift 2 ;;
    --beam-port) beam_port="${2:?--beam-port needs a value}"; shift 2 ;;
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
    [ -n "$beam_port" ] || return 0
    cat <<EOF

For Soracom Beam, at the top level of nginx.conf (not inside http { }; needs
the stream module), with the same certificate as https://$host$url_port:

    stream {
        server {
            listen $beam_port ssl;
            ssl_certificate     /path/to/fullchain.pem;
            ssl_certificate_key /path/to/privkey.pem;
            proxy_timeout 1h;
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
EOF
    [ -n "$beam_port" ] || return 0
    cat <<EOF
    tcp:
      routers:
        kid-pager-beam:
          rule: "HostSNI(\`$host\`)"
          entryPoints: [pager-beam]
          service: kid-pager-beam
          tls:
            certResolver: letsencrypt
      services:
        kid-pager-beam:
          loadBalancer:
            servers:
              - address: "$upstream:$mqtt_local"

For Soracom Beam, also a new entry point in the static configuration:

    entryPoints:
      pager-beam:
        address: ":$beam_port"
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

Router: forward TCP port $mqtt_port to port $mqtts_local of this machine. That is the pager
port; the stack serves it with its own certificate, so it does not go through the proxy.
EOF
    [ -z "$beam_port" ] || echo "Also forward TCP port $beam_port to your proxy, and set the Beam group's destination to mqtts://$host:$beam_port."
    cat <<EOF

Then:
  docker compose up -d
and open https://$host$url_port$app_path/ to create the first admin.

Check from a phone on mobile data, not your Wi-Fi, that pagers can reach you:
  openssl s_client -connect $host:$mqtt_port
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
    beam_port=$(value .env PAGER_BEAM_PORT)
    http_local=$(bind_port PAGER_APP_HTTP_BIND "$HTTP_LOCAL")
    mqtts_local=$(bind_port PAGER_APP_MQTTS_BIND "$MQTTS_LOCAL")
    mqtt_local=$(bind_port PAGER_APP_MQTT_BIND "$MQTT_LOCAL")
    if [ -z "$upstream" ]; then
        bind=$(value .env PAGER_APP_HTTP_BIND)
        case "$bind" in
        *:*) upstream="${bind%:*}" ;;
        esac
    fi
    upstream="${upstream:-127.0.0.1}"
    web_port="${web_port:-443}" mqtt_port="${mqtt_port:-$MQTTS_LOCAL}"
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

web_port="${web_port:-443}" mqtt_port="${mqtt_port:-$MQTTS_LOCAL}"
check_port --web-port "$web_port"
check_port --mqtt-port "$mqtt_port"
[ -z "$beam_port" ] || check_port --beam-port "$beam_port"
if [ "$web_port" = "$mqtt_port" ] || [ "$web_port" = "$beam_port" ] || [ "$mqtt_port" = "$beam_port" ]; then
    die "the web, pager and Beam ports must differ"
fi
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
    echo "# proxy: the web certificate is your reverse proxy's."
    echo "TLS_MODE=proxy"
    [ -z "$path" ] || echo "PAGER_PATH=$path"
} >pager.env

{
    echo "# Written by setup.sh; read by docker compose. docker-compose.yml explains each value."
    echo "# The ports the outside world dials. Pagers store the pager port: choose before you"
    echo "# provision any."
    echo "PAGER_WEB_PORT=$web_port"
    echo "PAGER_MQTT_PORT=$mqtt_port"
    [ -z "$beam_port" ] || echo "PAGER_BEAM_PORT=$beam_port"
    if is_ip "$upstream" && [ "$upstream" != 127.0.0.1 ]; then
        echo "# Where the proxy reaches the relay and the plain broker port."
        echo "PAGER_APP_HTTP_BIND=$upstream:$HTTP_LOCAL"
        echo "PAGER_APP_MQTT_BIND=$upstream:$MQTT_LOCAL"
    fi
} >.env

echo
echo "Wrote $(pwd)/pager.env and $(pwd)/.env"
http_local=$HTTP_LOCAL mqtts_local=$MQTTS_LOCAL mqtt_local=$MQTT_LOCAL
print_proxy
if ! is_ip "$upstream"; then
    cat <<EOF

$upstream is a name, so the app ports still listen on 127.0.0.1 only. If your
proxy cannot reach that, set PAGER_APP_HTTP_BIND and PAGER_APP_MQTT_BIND in .env
(docker-compose.yml).
EOF
fi
