#!/bin/sh
# Validates Settings() once, synchronously, before handing off to the real
# command (uvicorn). This matters specifically because of --workers: uvicorn's
# multiprocess supervisor treats a worker crashing on import as transient and
# just respawns it forever rather than exiting — so a bad production config
# (e.g. app/core/config.py's fail-fast validator rejecting DEBUG=true or a
# placeholder JWT_SECRET_KEY under ENVIRONMENT=production) would otherwise
# loop silently instead of ever visibly failing the container. Running the
# same check here, in the single-process entrypoint, fails loudly with a
# non-zero exit code before that loop ever has a chance to start.
set -e
python -c "from app.core.config import get_settings; get_settings()"
exec "$@"
