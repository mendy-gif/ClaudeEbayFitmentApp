# syntax=docker/dockerfile:1
# Transbase (Transaction Software GmbH) as shipped on the BMW ETK 2020-01 disc.
#
# The binaries are ELF 32-bit Intel 80386, linked for GNU/Linux 2.2.5 -- genuinely
# old software. So this image needs a 32-bit runtime, which it gets one of two ways:
#
#   linux/amd64 (default) : 64-bit Debian with i386 multiarch libraries added
#   linux/386             : a natively 32-bit Debian base (pass BASE_IMAGE)
#
# Debian bullseye is chosen for its older glibc; newer bases risk symbol-version
# errors against binaries of this vintage. Nothing from BMW is baked into a
# published image -- transbase_linux.tar.gz is copied from the mounted ISO at build
# time and stays local.
ARG BASE_IMAGE=debian:bullseye-slim
FROM ${BASE_IMAGE}

RUN set -eux; \
    arch="$(dpkg --print-architecture)"; \
    if [ "$arch" != "i386" ]; then dpkg --add-architecture i386; fi; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates procps file; \
    if [ "$arch" = "i386" ]; then \
      apt-get install -y --no-install-recommends libc6 libstdc++6 zlib1g; \
      apt-get install -y --no-install-recommends libncurses6 libtinfo6 || true; \
      apt-get install -y --no-install-recommends libncurses5 libtinfo5 || true; \
    else \
      apt-get install -y --no-install-recommends libc6:i386 libstdc++6:i386 zlib1g:i386; \
      apt-get install -y --no-install-recommends libncurses6:i386 libtinfo6:i386 || true; \
      apt-get install -y --no-install-recommends libncurses5:i386 libtinfo5:i386 || true; \
    fi; \
    rm -rf /var/lib/apt/lists/*

ENV TRANSBASE=/opt/transbase \
    TRANSBASE_SERVICENAMES=2024:2025 \
    PATH=/opt/transbase:$PATH

COPY transbase_linux.tar.gz /tmp/transbase_linux.tar.gz
RUN mkdir -p "$TRANSBASE" \
 && tar -xzf /tmp/transbase_linux.tar.gz -C "$TRANSBASE" \
 && rm /tmp/transbase_linux.tar.gz \
 && chmod +x "$TRANSBASE"/tbadmin "$TRANSBASE"/tbi "$TRANSBASE"/tbserver 2>/dev/null || true

# Keep a pristine copy of the shipped database registry; the entrypoint seeds the
# persistent one from it on first run.
RUN [ -f "$TRANSBASE/dblist.ini" ] && cp "$TRANSBASE/dblist.ini" "$TRANSBASE/dblist.ini.orig" || true

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# /rom  : the extracted rfile* ROM files, mounted read-only
# /data : where the attached database lives (a named volume)
# /out  : where schema dumps are written back to the host
VOLUME ["/data"]
WORKDIR /data
ENTRYPOINT ["/entrypoint.sh"]
CMD ["/bin/bash"]
