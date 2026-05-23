# TURN setup for production WebRTC

## When you need this

SocialHome's federation transport (server-to-server WebRTC), the
sync DataChannel, and the SPA's calls / live highlights all use
WebRTC. WebRTC's standard path is **direct peer-to-peer over UDP**.
On the open internet, that path works for ~70% of household pairs
out of the box (cone NAT, IPv4 reflexive). The other ~30% need a
relay because of:

* **Symmetric NAT** — the household's NAT picks a different port
  for every destination, so the address the STUN server reports
  doesn't match what the peer would have to dial.
* **Strict residential / corporate firewalls** — outbound UDP
  blocked entirely; WebRTC must fall back to TCP-on-443 via TURN.
* **CGNAT** — the ISP NATs you behind a shared address; no inbound
  UDP arrives at your machine ever.

When you see federation peers stuck on transport `https` even after
the 30 s settle window, or the log shows
``juice: Connectivity timer expired`` and the PeerConnection
transitions to ``failed`` without a DataChannel ever opening,
**that's the symptom**. STUN alone can't rescue these cases — only
a relay (TURN) can.

The federation transport will log a one-shot warning at startup if
no TURN is configured:

```
WARNING:socialhome.webrtc_ice:WebRTC: no TURN server configured.
STUN alone can't traverse symmetric NAT or strict firewalls; if
federation peers fail to establish RTC (transport stays on 'https'
indefinitely), deploy a TURN server (coturn is easy; see
docs/operations/turn.md) and set webrtc_turn_url +
webrtc_turn_secret (or webrtc_turn_user/cred) in socialhome.toml.
```

That's your signal to set this up.

## Recipe: coturn + HMAC time-limited credentials

[coturn](https://github.com/coturn/coturn) is the reference TURN
server. ~3 MB binary, runs anywhere, supports the TURN-REST
HMAC-credential scheme SocialHome's `webrtc_turn_secret` mode
expects.

Pick a public hostname + a TLS cert (Let's Encrypt is fine). Then:

```
# /etc/turnserver.conf
listening-port=3478
tls-listening-port=5349
external-ip=<your.public.ip>
realm=<your.public.hostname>
fingerprint
lt-cred-mech
use-auth-secret
static-auth-secret=<long-random-string>
# TLS
cert=/etc/letsencrypt/live/<host>/fullchain.pem
pkey=/etc/letsencrypt/live/<host>/privkey.pem
# Lock down — TURN open to the world is a relay-spam risk.
# 200 Mbps cap is generous for a small household.
total-quota=200
bps-capacity=200000
no-loopback-peers
no-multicast-peers
log-file=/var/log/turnserver/turnserver.log
no-stdout-log
```

Open 3478/tcp+udp + 5349/tcp+udp on your firewall. Ports 49152-65535
UDP need to be open too for the actual relay channels (or pin a
narrower range via `min-port` / `max-port`).

Then on **every** SocialHome instance, set:

```toml
# socialhome.toml
[network]
webrtc_stun_url = "stun:<your.public.hostname>:3478"
webrtc_turn_url = "turn:<your.public.hostname>:3478"
webrtc_turn_secret = "<same long-random-string as coturn's static-auth-secret>"
webrtc_turn_ttl_seconds = 3600
```

…or via env vars `SH_WEBRTC_STUN_URL`, `SH_WEBRTC_TURN_URL`,
`SH_WEBRTC_TURN_SECRET`, `SH_WEBRTC_TURN_TTL_SECONDS`.

Restart the SocialHome process. The next federation handshake will
include a TURN candidate; a household that can't pair-check
directly will allocate a relay channel on coturn and pair through
that.

## How HMAC time-limited credentials work

coturn's `--use-auth-secret` mode expects each client to present a
username of the form ``<expiry>:<user_id>`` (where ``expiry`` is a
Unix timestamp) and a password that's `base64(HMAC-SHA1(secret,
username))`. The server recomputes the HMAC and checks that
``expiry`` is in the future. Credentials thus expire — a leaked
TURN credential is bounded by ``webrtc_turn_ttl_seconds`` (default
3600 = 1 h).

SocialHome derives these credentials on demand:

* **SPA users** (calls, live highlights) — `user_id` is the
  authenticated user's id; credentials are issued via
  `GET /api/calls/ice-servers` (auth-required).
* **Federation transport** (server-to-server) — `user_id` is the
  local instance's `instance_id`. Credentials are derived once at
  startup and refreshed on subsequent transport rebuilds (after a
  FAILED PC, after operator-pushed ICE updates via
  `PUT /api/ha/integration/ice-servers`).

Both surfaces use the same shared secret (`webrtc_turn_secret`).
You do **not** need separate secrets per instance — the
`expiry:user_id` scheme already binds each credential to its
consumer.

If you'd rather use static long-lived credentials (e.g. a hosted
TURN provider that only supports username/password), set
`webrtc_turn_user` / `webrtc_turn_cred` instead. The HMAC path
wins when both are configured.

## Testing

Quick sanity check that TURN is reachable + authenticating:

```bash
# Get a fresh credential the way SocialHome would
python -c "
from socialhome.webrtc_ice import make_turn_credential
u, p = make_turn_credential('<your secret>', 'test-user', ttl_seconds=600)
print('username:', u)
print('credential:', p)
"

# Try a TURN allocate against your server using those creds.
# coturn ships with ``turnutils_uclient`` for this. From the coturn
# install:
turnutils_uclient -u <username> -w <credential> <your.public.hostname>
```

If the allocate succeeds, the federation transport's TURN
candidates will also work. If it fails with `401 Unauthorized`,
double-check that ``static-auth-secret`` on the server side
matches ``webrtc_turn_secret`` in your TOML byte-for-byte.

## Privacy / abuse notes

* **Don't expose a TURN server without authentication.** Open
  relays are scraped within minutes and used to NAT-traverse out
  of your network for various flavours of abuse. coturn's
  `--use-auth-secret` (the recipe above) gates every allocation
  behind a fresh HMAC.
* **TURN sees the encrypted DTLS stream, not the plaintext.** The
  SocialHome federation envelope sealed by `SpaceContentEncryption`
  / `routed_crypto` is still end-to-end encrypted between
  households; the TURN operator just sees opaque relayed bytes.
  Same guarantee applies to the SPA's calls (SRTP keyed by DTLS).
* **A self-hosted TURN box logs allocations.** Consider rotating
  `static-auth-secret` periodically and tuning coturn's
  `--log-file` retention to your privacy policy.

## Spec references

- coturn TURN-REST: <https://github.com/coturn/coturn/wiki/TURN-REST-API>
- WebRTC ICE: [RFC 8445](https://datatracker.ietf.org/doc/html/rfc8445)
- TURN: [RFC 5766](https://datatracker.ietf.org/doc/html/rfc5766)
