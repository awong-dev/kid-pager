# Firestore + Auth emulator image. firebase-tools (>=14) needs a JRE >= 21, which Debian bookworm
# lacks, so Eclipse Temurin 21 comes from Adoptium's apt repo.
FROM node:20-bookworm-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends wget gnupg ca-certificates \
    && wget -qO - https://packages.adoptium.net/artifactory/api/gpg/key/public \
        | gpg --dearmor -o /etc/apt/trusted.gpg.d/adoptium.gpg \
    && echo "deb https://packages.adoptium.net/artifactory/deb $(awk -F= '/^VERSION_CODENAME/{print $2}' /etc/os-release) main" \
        > /etc/apt/sources.list.d/adoptium.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends temurin-21-jre \
    && apt-get purge -y wget gnupg \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

RUN npm install -g firebase-tools

WORKDIR /srv

# A `demo-` project id needs no credentials or `firebase login`.
COPY firebase.json .firebaserc firestore.rules firestore.indexes.json ./

EXPOSE 8080 9099 4000

CMD ["firebase", "emulators:start", "--only", "firestore,auth", "--project", "demo-pager"]
