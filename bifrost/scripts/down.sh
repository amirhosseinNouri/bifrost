#!/bin/bash
# Restore the pre-VPN DNS for the primary hardware service.
SERVICE=$(/bin/cat /tmp/bifrost_dns_service 2>/dev/null)
if [ -n "$SERVICE" ]; then
  if [ -s /tmp/bifrost_dns_backup ] \
     && ! /usr/bin/grep -q "There aren't any DNS Servers" /tmp/bifrost_dns_backup; then
    # shellcheck disable=SC2046
    /usr/sbin/networksetup -setdnsservers "$SERVICE" $(/bin/cat /tmp/bifrost_dns_backup)
  else
    /usr/sbin/networksetup -setdnsservers "$SERVICE" Empty
  fi
  /bin/rm -f /tmp/bifrost_dns_service /tmp/bifrost_dns_backup
fi

# Legacy scutil path cleanup (harmless if absent).
/usr/sbin/scutil <<EOF
remove State:/Network/Service/bifrost/DNS
remove State:/Network/Service/bifrost/OriginalDNS
remove State:/Network/Service/bifrost/PrimaryServiceID
EOF
/bin/rm -f /tmp/bifrost_psid

# Flush DNS cache
/usr/bin/dscacheutil -flushcache
/usr/bin/killall -HUP mDNSResponder 2>/dev/null
