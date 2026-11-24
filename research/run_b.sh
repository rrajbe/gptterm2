#!/bin/sh
# Phase 1 test B (inside the container): mitmdump + a SECOND, separate Chromium (own profile,
# CDP :9223) that uses it as proxy. --ignore-certificate-errors applies to this test instance only;
# the main Chromium (:9222, /data/chrome-profile) is untouched.
set -e
OUT=/tmp/p1/results
mkdir -p $OUT
rm -f $OUT/B-mitm-* $OUT/B-flows.mitm
/tmp/mitmvenv/bin/mitmdump -q -s /tmp/p1/mitm_capture.py --listen-host 127.0.0.1 -p 8080 \
  -w $OUT/B-flows.mitm --set capdir=$OUT > /tmp/p1/mitmdump.log 2>&1 &
MITM=$!
sleep 4
rm -rf /tmp/chrome-mitm
/usr/bin/chromium --user-data-dir=/tmp/chrome-mitm --remote-debugging-port=9223 --remote-debugging-address=127.0.0.1 \
  --proxy-server=http://127.0.0.1:8080 --ignore-certificate-errors --disable-quic \
  --no-first-run --no-default-browser-check --disable-background-timer-throttling \
  --disable-renderer-backgrounding --disable-backgrounding-occluded-windows --password-store=basic \
  --window-position=0,0 --window-size=1280,900 about:blank > /tmp/p1/chromium-mitm.log 2>&1 &
CHR=$!
for i in $(seq 1 50); do curl -fs 127.0.0.1:9223/json/version >/dev/null && break; sleep 0.3; done
python /tmp/p1/p1test.py --port 9223 --tag B "Name one planet of the solar system, just the name." || true
sleep 2
kill $CHR; sleep 2; kill $MITM; wait $MITM 2>/dev/null || true
echo "chromium pid $CHR and mitmdump pid $MITM stopped"
