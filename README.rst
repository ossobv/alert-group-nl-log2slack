Alert Mobile alarm scrape
=========================

Scrapes info from https://alertmobile.alert-group.nl/ and publishes last
alarm notifications to Slack.

Requirements::

    KLANT_NUMMER = E...
    KLANT_CODE = <pass>
    SLACK_WEBHOOK_URL = https://hooks.slack.com/services/T../B../a..
    TIMEZONE = Europe/Amsterdam  # used by Docker image

Building::

    docker build --build-arg=GITVERSION=$(git describe --always) \
        -t $NAMESPACE/alert-group-nl-log2slack .
