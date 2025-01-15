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

Kubernetes deployment::

    ---
    apiVersion: v1
    data:
      # Customer code/number/passwords
      KLANT_CODE: FIXME+FIXME=
      KLANT_NUMMER: FIXME+FIXME=
      # Slack xoxb-... token
      SLACK_API_BEARER: FIXME+FIXME=
      # Users that should not be @-mentioned: "anton john sarah"
      SLACK_NO_MENTION_USERS: FIXME+FIXME=
      # Slack webhook URL: https://hooks.slack.com/services/...
      SLACK_WEBHOOK_URL: FIXME+FIXME=
      # "Europe/Amsterdam"
      TIMEZONE: RXVyb3BlL0Ftc3RlcmRhbQ==
    kind: Secret
    metadata:
      name: alert-group-nl-log2slack-config
    type: Opaque

    ---
    apiVersion: apps/v1
    kind: Deployment
    metadata:
      labels:
        app: alert-group-nl-log2slack
      name: alert-group-nl-log2slack
    spec:
      replicas: 1
      selector:
        matchLabels:
          app: alert-group-nl-log2slack
      template:
        metadata:
          labels:
            app: alert-group-nl-log2slack
        spec:
          containers:
          - env:
            - name: HEALTH_FILE
              value: /run/healthz
            envFrom:
            - secretRef:
                name: alert-group-nl-log2slack-config
            image: harbor.osso.io/ossobv/alert-group-nl-log2slack:v0.1
            imagePullPolicy: IfNotPresent
            livenessProbe:
              exec:
                command:
                - /bin/sh
                - -c
                - |
                  now=$(date +%s)
                  updated=$(stat -c%Y "$HEALTH_FILE" 2>/dev/null || echo 0)
                  if [ $((now - updated)) -gt 900 ]; then
                      echo "No updates in the last $((now - updated)) seconds" >&2
                      exit 1
                  fi
                  exit 0
              failureThreshold: 3
              initialDelaySeconds: 5
              periodSeconds: 5
              successThreshold: 1
              timeoutSeconds: 1
            name: alert-group-nl-log2slack
