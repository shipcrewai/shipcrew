# syntax=docker/dockerfile:1

# Shipply handler image.
# This image packages all Shipply bot handlers, the observability dashboard,
# and the external tools they invoke (omp, gh, bd).
FROM python:3.14-slim

# Pin: Oh My Pi (omp) binary from the pacto-bot-api v0.7.0 linux amd64 release.
# Source URL: https://github.com/covenant-gov/pacto-bot-api/releases/download/v0.7.0/pacto-bot-api_0.7.0_linux_amd64.tar.gz
# Platform: linux amd64.
ARG OMP_VERSION=0.7.0
ARG OMP_URL=https://github.com/covenant-gov/pacto-bot-api/releases/download/v${OMP_VERSION}/pacto-bot-api_${OMP_VERSION}_linux_amd64.tar.gz

# Install build/runtime dependencies.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        git \
        gnupg \
        tar \
    && rm -rf /var/lib/apt/lists/*

# Download and install the omp binary.
# The tarball is expected to contain either an `omp` binary or a
# `pacto-bot-api` binary; the latter is installed as `omp` for compatibility.
RUN set -eux; \
    curl -fsSL -o /tmp/omp.tar.gz "${OMP_URL}"; \
    mkdir -p /tmp/omp; \
    tar -xzf /tmp/omp.tar.gz -C /tmp/omp; \
    if [ -f /tmp/omp/omp ]; then \
        install -m 0755 /tmp/omp/omp /usr/local/bin/omp; \
    elif [ -f /tmp/omp/pacto-bot-api ]; then \
        install -m 0755 /tmp/omp/pacto-bot-api /usr/local/bin/omp; \
    else \
        echo "No omp binary found in ${OMP_URL}"; \
        ls -la /tmp/omp; \
        exit 1; \
    fi; \
    rm -rf /tmp/omp.tar.gz /tmp/omp; \
    omp --version

# Install GitHub CLI (gh) for PR status queries in Forge and Gate 3.
RUN set -eux; \
    curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg | dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg; \
    chmod go+r /usr/share/keyrings/githubcli-archive-keyring.gpg; \
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" > /etc/apt/sources.list.d/github-cli.list; \
    apt-get update; \
    apt-get install -y --no-install-recommends gh; \
    rm -rf /var/lib/apt/lists/*; \
    gh --version

# Install Beads (bd) CLI for Forge's task backend.
# The install script is fetched from the main branch of the steveyegge/beads
# repository. For reproducibility, consider pinning to a specific release once
# the project publishes versioned install URLs.
ENV BD_NON_INTERACTIVE=1
RUN set -eux; \
    curl -fsSL https://raw.githubusercontent.com/steveyegge/beads/main/scripts/install.sh | bash; \
    bd --version

# Install pacto-bot-sdk from the matching source repository.
# The package is not currently published to PyPI, so we install it directly from
# the covenant-gov/pacto-bot-api release tag that matches the pinned daemon/omp
# version (v0.7.0). The Python package lives in the `python/` subdirectory.
ARG PACTO_SDK_VERSION=0.7.0
RUN pip install --no-cache-dir \
    "git+https://github.com/covenant-gov/pacto-bot-api.git@v${PACTO_SDK_VERSION}#subdirectory=python"

# Copy the repository and install the shipply package.
WORKDIR /app
COPY . .
RUN pip install --no-cache-dir -e .

# Default command (overridden by docker-compose.yml per service).
CMD []
