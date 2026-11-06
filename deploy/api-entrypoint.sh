#!/bin/sh
set -eu
umask 077

case "${LOGIN_BROWSER_HEADLESS:-true}" in
    true) ;;
    false)
        # The login UI streams frames through the authenticated admin API.
        # Xvfb needs no VNC server, TCP listener or separately exposed port.
        export DISPLAY=:99
        mkdir -p /app/outputs/managed-browsers
        Xvfb "$DISPLAY" -screen 0 1440x1000x24 -nolisten tcp \
            >/app/outputs/managed-browsers/xvfb.log 2>&1 &
        xvfb_pid=$!
        attempts=0
        while [ ! -S /tmp/.X11-unix/X99 ]; do
            if ! kill -0 "$xvfb_pid" 2>/dev/null; then
                echo 'Xvfb could not start. Inspect the private xvfb.log file.' >&2
                exit 1
            fi
            attempts=$((attempts + 1))
            if [ "$attempts" -ge 50 ]; then
                kill -TERM "$xvfb_pid" 2>/dev/null || true
                echo 'Xvfb readiness timed out.' >&2
                exit 1
            fi
            sleep 0.1
        done
        ;;
    *)
        echo 'LOGIN_BROWSER_HEADLESS must be true or false.' >&2
        exit 1
        ;;
esac

exec "$@"
