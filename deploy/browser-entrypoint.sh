#!/bin/sh
set -eu
: "${VNC_PASSWORD:?Set a dedicated VNC_PASSWORD for the local login console}"
umask 077
mkdir -p /data
x11vnc -storepasswd "$VNC_PASSWORD" /data/.vnc-password >/dev/null 2>&1
Xvfb :99 -screen 0 1440x1000x24 -nolisten tcp >/data/xvfb.log 2>&1 &
sleep 1
openbox >/data/openbox.log 2>&1 &
x11vnc -display :99 -localhost -rfbauth /data/.vnc-password -forever -shared >/data/vnc.log 2>&1 &
websockify --web=/usr/share/novnc/ 6080 localhost:5900 >/data/novnc.log 2>&1 &
exec python -m dreamina_api.browser_worker "$@"
