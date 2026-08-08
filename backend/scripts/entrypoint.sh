#!/bin/sh
# Start the API server, deciding first whether X-Forwarded-For may be believed.
#
# That decision is the whole reason this file exists rather than an inline
# `sh -c` in the Containerfile: it is what stands between the per-IP rate
# limiters and a client that dictates its own limiting key, and a control nobody
# can execute is a control nobody can check. `tests/infra/test_entrypoint.py` runs
# this file with `sh` and asserts each of the three branches below.
#
# See the Containerfile for the measurement that motivated it.
set -eu

if [ "${FORWARDED_ALLOW_IPS:-}" = '*' ]; then
	echo "FORWARDED_ALLOW_IPS=* lets any client that reaches this port forge its own address through a header, which makes every per-IP rate limit and every IP-based decision in the stack meaningless. Name the proxy's address instead, or leave the variable unset. Refusing to start." >&2
	exit 1
fi

if [ -n "${FORWARDED_ALLOW_IPS:-}" ]; then
	# A proxy has been named. Believe the headers, and only from that peer.
	set -- --proxy-headers --forwarded-allow-ips "$FORWARDED_ALLOW_IPS"
else
	# Nobody has been named, so nobody verifies the header: read the real peer
	# address and nothing else. Explicit rather than omitted, because uvicorn's
	# own default for --proxy-headers is *on*.
	set -- --no-proxy-headers
fi

exec uvicorn chaudron.api.main:app \
	--host 0.0.0.0 \
	--port "${CHAUDRON_PORT:-8000}" \
	--no-access-log \
	"$@"
