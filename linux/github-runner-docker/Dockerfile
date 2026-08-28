FROM ubuntu:24.04

ARG TARGETARCH
ARG RUNNER_VERSION=""

ENV DEBIAN_FRONTEND=noninteractive
ENV RUNNER_HOME=/actions-runner

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        git \
        jq \
        sudo \
        tar \
        gzip \
        docker.io \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --uid 1001 --shell /bin/bash runner \
    && echo 'runner ALL=(ALL) NOPASSWD:ALL' >/etc/sudoers.d/runner \
    && chmod 440 /etc/sudoers.d/runner \
    && mkdir -p "${RUNNER_HOME}" "${RUNNER_HOME}/_work" \
    && chown -R runner:runner "${RUNNER_HOME}"

WORKDIR ${RUNNER_HOME}

RUN set -eux; \
    arch="${TARGETARCH:-$(dpkg --print-architecture)}"; \
    case "$arch" in \
      amd64) runner_arch=x64 ;; \
      arm64) runner_arch=arm64 ;; \
      arm) runner_arch=arm ;; \
      *) echo "Unsupported architecture: $arch" >&2; exit 1 ;; \
    esac; \
    version="${RUNNER_VERSION}"; \
    if [ -z "$version" ]; then \
      version="$(curl -fsSL https://api.github.com/repos/actions/runner/releases/latest | jq -r '.tag_name' | sed 's/^v//')"; \
    fi; \
    curl -fsSL \
      "https://github.com/actions/runner/releases/download/v${version}/actions-runner-linux-${runner_arch}-${version}.tar.gz" \
      -o /tmp/actions-runner.tar.gz; \
    tar xzf /tmp/actions-runner.tar.gz -C "${RUNNER_HOME}"; \
    rm -f /tmp/actions-runner.tar.gz; \
    "${RUNNER_HOME}/bin/installdependencies.sh"; \
    chown -R runner:runner "${RUNNER_HOME}"

COPY entrypoint.sh /usr/local/bin/runner-entrypoint.sh
RUN chmod 0755 /usr/local/bin/runner-entrypoint.sh

USER runner

ENTRYPOINT ["/usr/local/bin/runner-entrypoint.sh"]
