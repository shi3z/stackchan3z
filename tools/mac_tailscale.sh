#!/bin/zsh
# Mac-side Tailscale plumbing for the M5Stack (which cannot run Tailscale itself).
#   ./mac_tailscale.sh expose                    : publish the board (via the stackchan-forward service on 127.0.0.1:8080)
#                                                  to the tailnet at https://<this-mac>.<tailnet>.ts.net:8443/
#   ./mac_tailscale.sh unexpose                  : remove that
#   sudo ./mac_tailscale.sh nat                  : make this Mac a NAT gateway LAN -> tailnet (100.64.0.0/10)
#                                                  then on the board:  gw <mac-lan-ip>
#   sudo ./mac_tailscale.sh nat-off
TS=/Applications/Tailscale.app/Contents/MacOS/Tailscale
LAN_NET="${LAN_NET:-192.168.1.0/24}"   # your LAN, for the optional NAT mode
TS_IF=$(ifconfig | awk '/^utun/{i=$1} /inet 100\./{print i; exit}' | tr -d ':')
case "$1" in
  expose)   $TS serve --bg --https=8443 "http://127.0.0.1:8080" && $TS serve status ;;
  unexpose) $TS serve --https=8443 off; $TS serve status ;;
  nat)      [ "$(id -u)" = 0 ] || { echo "run with sudo"; exit 1; }
            sysctl -w net.inet.ip.forwarding=1
            cat > /etc/pf.anchors/stackchan <<PF
nat on $TS_IF from $LAN_NET to 100.64.0.0/10 -> ($TS_IF)
pass in quick on en0 from $LAN_NET to 100.64.0.0/10
pass out quick on $TS_IF from $LAN_NET to 100.64.0.0/10
PF
            pfctl -a com.apple/stackchan -f /etc/pf.anchors/stackchan
            pfctl -e 2>/dev/null; pfctl -a com.apple/stackchan -s nat ;;
  nat-off)  pfctl -a com.apple/stackchan -F all; sysctl -w net.inet.ip.forwarding=0 ;;
  *) sed -n 2,9p "$0" ;;
esac
