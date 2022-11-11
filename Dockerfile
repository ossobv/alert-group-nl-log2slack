FROM python:3-slim-bullseye
LABEL maintainer="OSSO"
LABEL description="Scrape alertmobile.alert-group.nl and publish to Slack"
LABEL dockerfile-vcs="https://github.com/ossobv/alert-group-nl-log2slack"

ENV PYTHONUNBUFFERED=1
RUN pip install BeautifulSoup4 requests phpserialize
COPY alert_group_nl_log2slack.py /srv/
ARG GITVERSION
RUN echo "$GITVERSION (built $(date +%Y-%m-%d))" >/srv/version
CMD python3 -V && pip freeze && echo '.' && \
    echo 'Starting publish-forever...' $(cat /srv/version) && \
    ln -sf /usr/share/zoneinfo/${TIMEZONE:-Etc/UTC} /etc/localtime && \
    test -s /etc/localtime && \
    exec python3 /srv/alert_group_nl_log2slack.py publish
