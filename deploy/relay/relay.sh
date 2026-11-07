#!/bin/sh
# Opens a SOCKS proxy on the EC2 server whose traffic leaves from this laptop (D81).
#
# -R with no destination is SSH's reverse dynamic forward: the server's containers connect to
# RELAY_BIND:1080, and this laptop makes the outbound connection. PermitRemoteOpen limits that
# to SportyBet, so nothing else can use the owner's home connection.
set -eu

# Windows bind mounts show every file as world-readable, which ssh refuses for a key.
install -m 600 /run/relay/key.pem /tmp/key.pem

# After a dropped connection the server holds the old tunnel's port for up to 90 seconds
# (sshd-relay.conf). Waiting first keeps restarts from becoming a burst of failed connections.
sleep 20
echo "relay: connecting to ${RELAY_TARGET}, SportyBet only"
exec ssh -N \
  -i /tmp/key.pem \
  -o StrictHostKeyChecking=accept-new \
  -o UserKnownHostsFile=/state/known_hosts \
  -o ServerAliveInterval=30 \
  -o ServerAliveCountMax=3 \
  -o ExitOnForwardFailure=yes \
  -o PermitRemoteOpen=www.sportybet.com:443 \
  -R "${RELAY_BIND}:1080" \
  "${RELAY_TARGET}"
