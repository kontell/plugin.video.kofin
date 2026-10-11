#!/bin/sh
# Runs ON the box: switch Kodi's database log component on for N seconds
# and save the log lines written meanwhile.   sqltrace.sh <out-file> [seconds]
OUT="$1"; SECS="${2:-45}"
U=/storage/.kodi/userdata; L=/storage/.kodi/temp/kodi.log
PASS=$(sed -n "s/.*services.webserverpassword[^>]*>\([^<]*\)<.*/\1/p" $U/guisettings.xml)
rpc() { curl -s -m 10 -u "kodi:$PASS" -H "Content-Type: application/json" -d "$1" http://localhost:8080/jsonrpc; echo; }
A=$(wc -l < $L)
rpc '{"jsonrpc":"2.0","id":1,"method":"Settings.SetSettingValue","params":{"setting":"debug.setextraloglevel","value":[131072]}}'
rpc '{"jsonrpc":"2.0","id":1,"method":"Settings.SetSettingValue","params":{"setting":"debug.extralogging","value":true}}'
sleep "$SECS"
rpc '{"jsonrpc":"2.0","id":1,"method":"Settings.SetSettingValue","params":{"setting":"debug.extralogging","value":false}}'
rpc '{"jsonrpc":"2.0","id":1,"method":"Settings.SetSettingValue","params":{"setting":"debug.setextraloglevel","value":[]}}'
B=$(wc -l < $L)
sed -n "${A},${B}p" $L | sed -E "s/api_key=[0-9a-f]+/api_key=X/g" > "$OUT"
wc -l "$OUT"
