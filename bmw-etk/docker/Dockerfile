# Transbase (Transaction Software GmbH) as shipped on the BMW ETK 2020-01 disc.
#
# The binaries are 2022-era x86_64 Linux builds, so this image is pinned to
# linux/amd64 and runs under emulation on Apple Silicon. Debian bullseye is
# chosen for its older glibc (2.31), which suits binaries of that vintage;
# newer bases risk symbol-version errors.
#
# Nothing from BMW is baked into a published image -- transbase_linux.tar.gz is
# copied from the mounted ISO at build time and stays local.
FROM --platform=linux/amd64 debian:bullseye-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      libstdc++6 libncurses6 ca-certificates procps file \
 && rm -rf /var/lib/apt/lists/*

ENV TRANSBASE=/opt/transbase \
    TRANSBASE_SERVICENAMES=2024:2025 \
    PATH=/opt/transbase:$PATH

COPY transbase_linux.tar.gz /tmp/transbase_linux.tar.gz
RUN mkdir -p "$TRANSBASE" \
 && tar -xzf /tmp/transbase_linux.tar.gz -C "$TRANSBASE" \
 && rm /tmp/transbase_linux.tar.gz \
 && chmod +x "$TRANSBASE"/tbadmin "$TRANSBASE"/tbi "$TRANSBASE"/tbserver 2>/dev/null || true

# /rom  : the extracted rfile* ROM files, mounted read-only
# /data : where the attached database lives (a named volume)
VOLUME ["/data"]
WORKDIR /data
CMD ["/bin/bash"]
