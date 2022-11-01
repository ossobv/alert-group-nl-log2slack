FROM python:3-slim-bullseye
LABEL maintainer="OSSO"
LABEL description="Scrape alertmobile.alert-group.nl and publish to Slack"
LABEL dockerfile-vcs="https://github.com/ossobv/alert-group-nl-log2slack"

ENV PYTHONUNBUFFERED=1
RUN pip install BeautifulSoup4 requests
COPY alert_group_nl_log2slack.py /srv/
CMD python3 -V && pip freeze && echo '.' && \
    echo 'Starting publish-forever...' && \
    exec python3 /srv/alert_group_nl_log2slack.py publish
