#!/bin/sh
# Wi-Fi watchdog for Valentina. Run every minute from the arduino user's crontab:
#   * * * * * /home/arduino/ArduinoApps/unoq-doorbell/scripts/wifi-watchdog.sh
#
# Pings the default gateway (the FRITZ!Box). After consecutive failures it
# repairs the link without root (NetworkManager here has auth-polkit=false):
#   3 min: reconnect the Wi-Fi connection (nmcli con up)
#   6 min: turn the Wi-Fi radio off and on
#   then:  reconnect again every 10 min
# On 2026-10-12, after a move to the porch (another mesh satellite), Valentina
# could only be reached from some devices until it was power-cycled.
# Log: journalctl -t wifi-watchdog
set -u
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin  # cron's PATH lacks sbin

# The Wi-Fi connection to repair: WIFI_CONNECTION, else the active Wi-Fi
# connection, else the last one remembered while it was up.
SAVED="${STATE%.failures}.connection"
CONN="${WIFI_CONNECTION:-$(nmcli -t -f NAME,TYPE con show --active 2>/dev/null | awk -F: '$2 == "802-11-wireless" {print $1; exit}')}"
if [ -n "$CONN" ]; then
    echo "$CONN" >"$SAVED"
else
    CONN=$(cat "$SAVED" 2>/dev/null)
fi
STATE="${STATE:-/tmp/wifi-watchdog.failures}"
TAG=wifi-watchdog

gateway="${GATEWAY:-}"  # override for testing
[ -z "$gateway" ] && gateway=$(ip -4 route show default 2>/dev/null | awk '/dev wlan/ {print $3; exit}')
[ -z "$gateway" ] && gateway=$(ip -4 route show default 2>/dev/null | awk '{print $3; exit}')

if [ -n "$gateway" ] && ping -c 3 -W 3 "$gateway" >/dev/null 2>&1; then
    if [ -s "$STATE" ] && [ "$(cat "$STATE")" -gt 0 ]; then
        logger -t "$TAG" "gateway $gateway reachable again after $(cat "$STATE") failed checks"
    fi
    echo 0 >"$STATE"
    exit 0
fi

failures=$(( $(cat "$STATE" 2>/dev/null || echo 0) + 1 ))
echo "$failures" >"$STATE"
logger -t "$TAG" "gateway ${gateway:-<none>} unreachable ($failures)"

if [ -z "$CONN" ]; then
    logger -t "$TAG" "no Wi-Fi connection name known; set WIFI_CONNECTION"
elif [ "$failures" -eq 3 ] || { [ "$failures" -gt 6 ] && [ $(( failures % 10 )) -eq 0 ]; }; then
    logger -t "$TAG" "reconnecting '$CONN'"
    nmcli con up "$CONN" >/dev/null 2>&1 || logger -t "$TAG" "nmcli con up failed"
elif [ "$failures" -eq 6 ]; then
    logger -t "$TAG" "cycling the Wi-Fi radio"
    nmcli radio wifi off && sleep 5 && nmcli radio wifi on
fi
