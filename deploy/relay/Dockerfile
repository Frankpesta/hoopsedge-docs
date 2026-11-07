# The SportyBet relay that runs on the owner's laptop (D81). Only an SSH client.
FROM alpine:3.20
RUN apk add --no-cache openssh-client
COPY relay.sh /relay.sh
RUN chmod 755 /relay.sh
ENTRYPOINT ["/relay.sh"]
