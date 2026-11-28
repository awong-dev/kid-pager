#!/bin/sh
# First-time configuration of the home stack: writes pager.env and .env next to this file, then
# tells you which ports to forward. It starts nothing and never overwrites an existing file
# without --force. README.md, "Install and first run".
set -eu
cd "$(dirname "$0")"

usage() {
    cat <<'EOF'
Usage: ./setup.sh [options]

  --host NAME          the name pagers, phones and browsers use for this machine,
                       e.g. a dynamic-DNS name (asked for when missing)
  --tls MODE           selfsigned (default), letsencrypt or external
  --email ADDR         letsencrypt: the address Let's Encrypt sends expiry notices to
  --cert-dir DIR       external: a directory with fullchain.pem and privkey.pem that
                       something else keeps renewed
  --path PATH          where the app is served (default /kid-pager; / for the root)
  --web-port N         public port for the web app (default: a random high port)
  --mqtt-port N        public port for pagers (default: a random high port)
  --beam-port N        public port for Soracom Beam pagers; only used with a public
                       certificate (default: a random high port)
  --standard-ports     use 443, 8883 and 8884 instead of random ports
  --force              replace an existing pager.env and .env (the old ones are kept
                       as *.bak)
  -h, --help           this text
EOF
}

die() {
    echo "setup.sh: $*" >&2
    exit 1
}

host="" tls="" email="" cert_dir="" path="" web_port="" mqtt_port="" beam_port=""
standard=0 force=0
while [ $# -gt 0 ]; do
    case "$1" in
    --host) host="${2:?--host needs a value}"; shift 2 ;;
    --tls) tls="${2:?--tls needs a value}"; shift 2 ;;
    --email) email="${2:?--email needs a value}"; shift 2 ;;
    --cert-dir) cert_dir="${2:?--cert-dir needs a value}"; shift 2 ;;
    --path) path="${2:?--path needs a value}"; shift 2 ;;
    --web-port) web_port="${2:?--web-port needs a value}"; shift 2 ;;
    --mqtt-port) mqtt_port="${2:?--mqtt-port needs a value}"; shift 2 ;;
    --beam-port) beam_port="${2:?--beam-port needs a value}"; shift 2 ;;
    --standard-ports) standard=1; shift ;;
    --force) force=1; shift ;;
    -h | --help) usage; exit 0 ;;
    *) usage >&2; die "unknown option: $1" ;;
    esac
done

if [ "$force" = 0 ]; then
    for f in pager.env .env; do
        [ ! -e "$f" ] || die "$f already exists; edit it, or pass --force to start over"
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

if [ -z "$tls" ] && [ "$interactive" = 1 ]; then
    cat <<'EOF'

Which certificate should the web app use?
  selfsigned   made here. Works at once, but every phone and browser must import
               this server's CA before it can install the app or get notifications.
  letsencrypt  fetched and renewed by this stack. Port 80 of this name must reach
               this machine.
  external     a certificate you already keep renewed for this name, for example
               by a dynamic-DNS (DNS-01) script. No port 80 needed.
EOF
    ask tls "Certificate" selfsigned
fi
tls="${tls:-selfsigned}"
case "$tls" in
selfsigned | letsencrypt | external) ;;
*) die "--tls is selfsigned, letsencrypt or external, not $tls" ;;
esac

if [ "$tls" = letsencrypt ] && [ -z "$email" ]; then
    ask email "Email address for Let's Encrypt expiry notices"
    [ -n "$email" ] || die "letsencrypt needs --email ADDR"
fi
if [ "$tls" = external ]; then
    [ -n "$cert_dir" ] || ask cert_dir "Directory holding fullchain.pem and privkey.pem"
    [ -n "$cert_dir" ] || die "external needs --cert-dir DIR"
    [ -d "$cert_dir" ] || die "$cert_dir is not a directory"
    cert_dir="$(cd "$cert_dir" && pwd)"
    for f in fullchain.pem privkey.pem; do
        [ -f "$cert_dir/$f" ] || echo "setup.sh: warning: $cert_dir/$f does not exist yet" >&2
    done
    for f in fullchain.pem privkey.pem; do
        # Docker mounts the directory, so a link that points outside it is dangling in the container.
        if [ -L "$cert_dir/$f" ]; then
            echo "setup.sh: warning: $cert_dir/$f is a symbolic link; if it points outside" \
                "$cert_dir (certbot's live/ does), copy the files instead" >&2
        fi
    done
fi

case "${path:-/kid-pager}" in
/ | /*) ;;
*) die "--path starts with /, like /kid-pager" ;;
esac

port_in_use() {
    if command -v ss >/dev/null 2>&1; then
        ss -Htln 2>/dev/null | grep -Eq "[:.]$1[[:space:]]"
    elif command -v netstat >/dev/null 2>&1; then
        netstat -an 2>/dev/null | grep -i listen | grep -Eq "[:.]$1[[:space:]]"
    else
        return 1
    fi
}

# 20000-32767: above the registered services people usually run, below Linux's range for outgoing
# connections (32768 up), so the port is never taken by one at start.
taken=""
random_port() {
    while :; do
        n=$(od -An -N2 -tu2 /dev/urandom | tr -d ' \n')
        p=$((20000 + n % 12768))
        case " $taken " in *" $p "*) continue ;; esac
        port_in_use "$p" && continue
        taken="$taken $p"
        echo "$p"
        return
    done
}

check_port() { # check_port NAME VALUE
    case "$2" in
    '' | *[!0-9]*) die "$1 is a port number, not $2" ;;
    esac
    [ "$2" -ge 1 ] && [ "$2" -le 65535 ] || die "$1 is a port number from 1 to 65535, not $2"
}

if [ "$standard" = 1 ]; then
    web_port="${web_port:-443}" mqtt_port="${mqtt_port:-8883}" beam_port="${beam_port:-8884}"
fi
taken="$web_port $mqtt_port $beam_port"
# Assigned one at a time: a command substitution cannot update $taken for the next call.
[ -n "$web_port" ] || { web_port=$(random_port); taken="$taken $web_port"; }
[ -n "$mqtt_port" ] || { mqtt_port=$(random_port); taken="$taken $mqtt_port"; }
[ -n "$beam_port" ] || { beam_port=$(random_port); taken="$taken $beam_port"; }
check_port --web-port "$web_port"
check_port --mqtt-port "$mqtt_port"
check_port --beam-port "$beam_port"
if [ "$web_port" = "$mqtt_port" ] || [ "$web_port" = "$beam_port" ] || [ "$mqtt_port" = "$beam_port" ]; then
    die "the web, pager and Beam ports must differ"
fi

if [ "$force" = 1 ]; then
    for f in pager.env .env; do
        [ ! -e "$f" ] || mv "$f" "$f.bak"
    done
fi

{
    echo "# Written by setup.sh. Nothing here is secret: the stack generates every password and key"
    echo "# into data/. pager.env.example explains each value."
    echo "PAGER_HOST=$host"
    echo "TLS_MODE=$tls"
    echo "LETSENCRYPT_EMAIL=$email"
    [ -z "$path" ] || echo "PAGER_PATH=$path"
} >pager.env

{
    echo "# Written by setup.sh; read by docker compose. docker-compose.yml explains each value."
    echo "# The ports the outside world dials. Pagers store the pager port: choose before you"
    echo "# provision any, and forward each one on your router to the same port on this machine."
    echo "PAGER_WEB_PORT=$web_port"
    echo "PAGER_MQTT_PORT=$mqtt_port"
    echo "PAGER_BEAM_PORT=$beam_port"
    if [ "$tls" = letsencrypt ]; then
        echo "# Let's Encrypt checks this name on port 80."
        echo "PAGER_HTTP_BIND=80"
        echo "COMPOSE_PROFILES=letsencrypt"
    fi
    [ "$tls" != external ] || echo "PAGER_CERT_DIR=$cert_dir"
} >.env

app_path="${path:-/kid-pager}"
[ "$app_path" != / ] || app_path=""
url_port=":$web_port"
[ "$web_port" != 443 ] || url_port=""

cat <<EOF

Wrote $(pwd)/pager.env and $(pwd)/.env

Forward these TCP ports on your router to this machine, same port number:
  $web_port   web app, bridge phone, pager downloads
  $mqtt_port   pagers
EOF
case "$tls" in
selfsigned)
    echo "(Soracom Beam pagers need a public certificate: TLS mode letsencrypt or external.)"
    ;;
*)
    echo "  $beam_port   pagers on Soracom Beam (Beam group destination: mqtts://$host:$beam_port)"
    ;;
esac
[ "$tls" != letsencrypt ] || echo "  80      Let's Encrypt's check"
cat <<EOF

Then:
  docker compose up -d
and open https://$host$url_port$app_path/ to create the first admin.

Check from a phone on mobile data, not your Wi-Fi, that pagers can reach you:
  openssl s_client -connect $host:$mqtt_port
EOF
