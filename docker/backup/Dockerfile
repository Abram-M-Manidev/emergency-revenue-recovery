# ERRS backup image: PostgreSQL 16 client tools + the off-site uploader.
#
# Built FROM the same `postgres:16-alpine` the database runs, so pg_dump and
# pg_restore are exactly the server's version. A client older than the
# server refuses to dump; a newer one can write an archive the server cannot
# restore. Both surface only when a restore is needed, so the match is
# enforced here rather than hoped for.
FROM postgres:16-alpine AS runtime

# A venv, not `pip install --break-system-packages`: the pinned boto3 stays
# isolated from anything Alpine's python3 package brings in.
RUN apk add --no-cache python3 && python3 -m venv /opt/offsite
COPY requirements.txt /tmp/requirements.txt
RUN /opt/offsite/bin/pip install --no-cache-dir -r /tmp/requirements.txt && rm /tmp/requirements.txt

COPY errs_offsite.py /opt/offsite/errs_offsite.py
COPY backup.sh /usr/local/bin/errs-backup.sh
COPY restore.sh /usr/local/bin/errs-restore.sh
RUN printf '#!/bin/sh\nexec /opt/offsite/bin/python /opt/offsite/errs_offsite.py "$@"\n' \
        > /usr/local/bin/errs-offsite \
    && chmod 0755 /usr/local/bin/errs-offsite /usr/local/bin/errs-backup.sh /usr/local/bin/errs-restore.sh

ENTRYPOINT ["/bin/sh", "/usr/local/bin/errs-backup.sh"]

# --- test image: the runtime image plus pytest/ruff/mypy and the suite ------
FROM runtime AS test
COPY requirements-test.txt requirements.txt /tmp/
RUN /opt/offsite/bin/pip install --no-cache-dir -r /tmp/requirements-test.txt
WORKDIR /src
COPY . /src
ENV PATH="/opt/offsite/bin:${PATH}"
ENTRYPOINT []
CMD ["python", "-m", "pytest", "-q"]
