Alert Mobile alarm scrape
=========================

Scrapes info from https://alertmobile.alert-group.nl/ and publishes last
alarm notifications to Slack.

Requirements::

    KLANT_NUMMER = E...
    KLANT_CODE = <pass>
    SLACK_WEBHOOK_URL = https://hooks.slack.com/services/T../B../a..
    TIMEZONE = Europe/Amsterdam  # used by Docker image
    HEALTH_FILE = <file_path>

Building::

    docker build --build-arg=GITVERSION=$(git describe --always) \
        -t $NAMESPACE/alert-group-nl-log2slack .

Health checks::

    #!/bin/sh
    now=$(date +%s)
    updated=$(stat -c%Y "$HEALTH_FILE" 2>/dev/null || echo 0)
    if [ $((now - updated)) -gt 900 ]; then
        echo "No updates in the last $((now - updated)) seconds" >&2
        exit 1
    fi
    exit 0 
