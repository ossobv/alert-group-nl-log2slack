FROM python:3-slim-bullseye

RUN pip install BeautifulSoup4 requests
ENV PYTHONUNBUFFERED=1
COPY alert_group_nl_log2slack.py /srv/
CMD python3 -V && pip freeze && echo '.' && \
    echo 'Starting publish-forever:' && \
    exec python3 /srv/alert_group_nl_log2slack.py publish
