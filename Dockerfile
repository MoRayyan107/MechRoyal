# Mech Royale bot. Build: docker build -t survivor .
# Base image pinned by tag and digest so a rebuild gets the identical Python (3.13.9, Debian bookworm).
FROM python:3.13.9-slim-bookworm@sha256:b685a4fa58bb19d1814d78a1ec0f0208f351452724f78b20212c984d6e124a34
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /bot
# Standard library only: no packages to install.
COPY mechfight.py nav.py losbreak.py bot.py /bot/
# Precompile at build time; the match disk is read-only.
RUN python3 -m compileall -q /bot && chmod -R a+rX /bot
USER 65534:65534
ENTRYPOINT ["python3", "-u", "/bot/bot.py"]
