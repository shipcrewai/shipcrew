# syntax=docker/dockerfile:1

# Shipply handler image.
# This image packages all Shipply bot handlers, the observability dashboard,
# and the external tools they invoke (omp, gh, bd).
FROM python:3.14-slim

# Create a non-root user for runtime.
RUN useradd -m -s /bin/bash botuser

# Install system dependencies and tools available via apt (requires root).
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        git \
        gnupg \
        tar \
        build-essential \
        cmake \
        clang \
        lld \
        libclang-dev \
        procps \
        #wget \
        jq \
        pkg-config \
        #socat \
        #mkcert \
    && rm -rf /var/lib/apt/lists/* /var/cache/apt/archives /tmp/* /var/tmp/*

# Install tools that only ship as release binaries (requires root).
ARG GH_VERSION=2.96.0
#ARG WEBSOCAT_VERSION=1.14.1
RUN set -eux; \
    ARCH=$(dpkg --print-architecture); \
    \
    # gh CLI
    curl -fsSL -o /tmp/gh.tar.gz "https://github.com/cli/cli/releases/download/v${GH_VERSION}/gh_${GH_VERSION}_linux_${ARCH}.tar.gz"; \
    tar -xzf /tmp/gh.tar.gz -C /tmp; \
    mv "/tmp/gh_${GH_VERSION}_linux_${ARCH}/bin/gh" /usr/local/bin/; \
    rm -rf /tmp/gh*;
#     
#     # websocat
#     case "$ARCH" in \
#         arm64)  WEBSOCAT_ARCH="aarch64-unknown-linux-musl" ;; \
#         amd64)  WEBSOCAT_ARCH="x86_64-unknown-linux-musl" ;; \
#         *) echo "Unsupported architecture: $ARCH"; exit 1 ;; \
#     esac; \
#     curl -fsSL -o /usr/local/bin/websocat "https://github.com/vi/websocat/releases/download/v${WEBSOCAT_VERSION}/websocat.${WEBSOCAT_ARCH}"; \
#     chmod +x /usr/local/bin/websocat; 

# Install omp and beads via their official installers (requires root for /usr/local/bin).
RUN set -eux; \
    export PI_INSTALL_DIR=/usr/local/bin; \
    curl -fsSL https://omp.sh/install | sh; \
    curl -fsSL https://raw.githubusercontent.com/gastownhall/beads/main/scripts/install.sh | bash

# Switch to botuser for language runtimes and project install.
USER botuser

# Install nvm, Node.js 24, and pnpm via corepack.
ENV NVM_DIR="/home/botuser/.nvm"
ENV PATH="/home/botuser/.nvm/versions/node/current/bin:/home/botuser/.cargo/bin:/home/botuser/.local/bin:${PATH}"
RUN bash -c 'set -eux; \
    export NVM_DIR=/home/botuser/.nvm; \
    curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.5/install.sh | bash; \
    [ -s "$NVM_DIR/nvm.sh" ] && \. "$NVM_DIR/nvm.sh"; \
    nvm install 24; \
    nvm alias default 24; \
    ln -s "$NVM_DIR/versions/node/$(nvm current)" "$NVM_DIR/versions/node/current"; \
    corepack enable pnpm; \
    node -v; \
    pnpm -v'

# Install Rust toolchain.
RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --default-toolchain stable --profile minimal

# Install pacto-bot-sdk from the matching source repository.
# The package is not currently published to PyPI, so we install it directly from
# the covenant-gov/pacto-bot-api repository. The default ref is `main`; override
# at build time with `--build-arg PACTO_SDK_VERSION=<ref>`. The Python package
# lives in the `python/` subdirectory.
ARG PACTO_SDK_VERSION=main
RUN pip install --user --no-cache-dir \
    "git+https://github.com/covenant-gov/pacto-bot-api.git@${PACTO_SDK_VERSION}#subdirectory=python"

# Copy the repository and install the shipply package.
WORKDIR /app
COPY --chown=botuser:botuser . .
RUN pip install --user --no-cache-dir -e .

# Runtime runs as botuser.
CMD []
