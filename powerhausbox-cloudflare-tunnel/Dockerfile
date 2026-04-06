ARG BUILD_FROM
FROM ${BUILD_FROM}

ARG BUILD_ARCH

SHELL ["/bin/ash", "-eo", "pipefail", "-c"]

RUN apk add --no-cache bash curl jq python3 py3-flask py3-yaml \
    py3-aiohttp \
    openssh ttyd tmux bash-completion nano \
    nginx \
    && case "${BUILD_ARCH}" in \
        aarch64) CF_ARCH="arm64" ;; \
        amd64) CF_ARCH="amd64" ;; \
        armv7) CF_ARCH="arm" ;; \
        *) echo "Unsupported architecture: ${BUILD_ARCH}" && exit 1 ;; \
    esac \
    && curl -fsSL "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-${CF_ARCH}" -o /usr/local/bin/cloudflared \
    && chmod +x /usr/local/bin/cloudflared

COPY run.sh /run.sh
COPY rootfs/ /
COPY integration/ /opt/powerhausbox/integration/

RUN chmod +x /run.sh

CMD ["/run.sh"]
