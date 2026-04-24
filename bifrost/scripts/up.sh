#!/bin/bash
# Fix macOS tun interface: set gateway as peer address.
# OpenVPN CLI sets peer=self which breaks routing.
# The Mac app sets peer=gateway which works.
# This runs AFTER ifconfig but BEFORE routes are added.
/sbin/ifconfig "$dev" "$ifconfig_local" "$route_vpn_gateway" \
    netmask "$ifconfig_netmask" mtu "$tun_mtu" up

# Set DNS via `networksetup` on the primary hardware service.
# Rationale: scutil writes to State:/Network/Service/<id>/DNS bind the
# resolver to that service's if_index. With redirect-gateway, DNS traffic
# goes via utunN, so mDNSResponder marks the entry Not Reachable and
# refuses to query. networksetup writes to Setup:/... and mDNSResponder
# queries it through whatever route(4) has, which after the VPN is up is
# the tun. This is also what ~/.openfortivpn/connect.sh does.

PRIMARY_IFACE=$(/usr/sbin/scutil <<< "show State:/Network/Global/IPv4" \
  | /usr/bin/awk '/PrimaryInterface/ {print $3}')

SERVICE=$(/usr/sbin/networksetup -listallhardwareports \
  | /usr/bin/awk -v iface="$PRIMARY_IFACE" '
      /^Hardware Port:/ { port = substr($0, index($0, ":") + 2); sub(/^ +/, "", port) }
      /^Device:/ { if ($2 == iface) { print port; exit } }
  ')

if [ -n "$SERVICE" ]; then
  # Snapshot the pre-VPN DNS so down.sh can restore it (even if it was Empty).
  /usr/sbin/networksetup -getdnsservers "$SERVICE" > /tmp/bifrost_dns.$$ 2>/dev/null
  /bin/mv -f /tmp/bifrost_dns.$$ /tmp/bifrost_dns_backup
  /usr/bin/printf '%s' "$SERVICE" > /tmp/bifrost_dns_service

  DNS_SERVERS=${BIFROST_EXTERNAL_DNS:-"8.8.8.8 1.1.1.1"}
  /usr/sbin/networksetup -setdnsservers "$SERVICE" ${DNS_SERVERS}
fi

# Flush DNS cache
/usr/bin/dscacheutil -flushcache
/usr/bin/killall -HUP mDNSResponder 2>/dev/null
