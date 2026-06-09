# Cryptography

This document describes every cryptographic primitive Social Home uses,
where each one lives in the code, how keys are stored at rest, what the
wire formats look like, and the path forward to post-quantum (PQ)
cryptography.

> **Status (2026-04-18):** classical crypto is in production shape.
> The hybrid Ed25519 + ML-DSA-65 signature path is wired end-to-end
> behind `Config.federation_sig_suite = "ed25519+mldsa65"` and a
> manually-installed `liboqs-python` package (PyPI rejects the direct
> URL ref so it can't ship as a `socialhome[pq]` extra — see Operator
> checklist below for the install command).
> Further PQ work (ML-KEM for pairing, PQ VAPID) is tracked in
> [§ Post-quantum migration path](#post-quantum-migration-path) below.

## Threat model

Social Home is a federated social network for households. Each
deployment (a household's instance) is a trust boundary. The crypto
layer protects three things:

1. **Authenticity** — a paired peer's admin can prove that a federation
   envelope really came from them. Ed25519 identity signatures today.
2. **Confidentiality** — post bodies, DM contents, space content, and
   pairing key exchanges never appear in plaintext on the network.
   Per-pair AES-256-GCM session keys today.
3. **Forward secrecy (bounded)** — a compromised peer's keys can't
   retroactively decrypt envelopes sent by other pairs. Each pair has
   its own directional keys; the KEK on disk encrypts them at rest.

Explicit non-goals:

* **Deniability / OTR-style forward secrecy** — we don't rotate per-
  message keys. §12.5 DM relay is the closest thing to deniability.
* **Hiding federation metadata from a global adversary** — the GFS
  sees routing metadata by design; only `sealed_sender` hides the
  sender identity from the GFS.
* **Anonymity** — instance IDs are derived from identity public keys
  and are persistent. Users consent to this by accepting the pairing.

The fourth concern that showed up recently — **quantum adversaries
harvesting traffic now to decrypt later** — is what the PQ migration
addresses. See the last section of this document.

## Primitives in use

| Primitive | Algorithm | Key size | Purpose | Source |
|-----------|-----------|----------|---------|--------|
| Identity signatures | Ed25519 | 256-bit | Federation envelopes, user-identity assertions, space config, SDP | `socialhome/crypto.py` |
| Identity signatures (PQ, optional) | ML-DSA-65 (FIPS 204) | 1952-byte pk / 4032-byte sk | Hybrid signature when suite is `ed25519+mldsa65` | `socialhome/federation/pq_signer.py` |
| Key agreement | X25519 (ECDH) | 256-bit | Pairing-time session key derivation | `socialhome/federation/pairing_coordinator.py` |
| Symmetric AEAD | AES-256-GCM | 256-bit | Federation payloads, space content, sealed sender, KEK wrap | `cryptography.hazmat.primitives.ciphers.aead.AESGCM` |
| KDF | HKDF-SHA256 | 32-byte output | Session-key derivation, KEK derivation | `cryptography.hazmat.primitives.kdf.hkdf` |
| Hash | SHA-256 | 256-bit | Instance/space/user ID derivation, token hashing | `hashlib.sha256` |
| MAC | HMAC-SHA256 | 256-bit | Relay path selection (`keyed_hash`) | `socialhome/crypto.py` |
| MAC | HMAC-SHA1 | 160-bit | TURN credential generation (coturn REST API) | `socialhome/routes/calls.py` |
| Password hash | scrypt | N=2^14, r=8, p=1 | Standalone-mode user passwords | `socialhome/platform/standalone/adapter.py` |
| Web Push | VAPID (P-256 ECDSA) | P-256 | Push-notification JWT signing | `socialhome/services/push_service.py` |

### Quantum safety at a glance

- **Quantum-safe today:** AES-256-GCM (Grover ⇒ effective 128-bit),
  SHA-256, HKDF-SHA256, HMAC-SHA256, scrypt.
- **Vulnerable to Shor's algorithm:** Ed25519 (signatures), X25519
  (key agreement), P-256 ECDSA (VAPID).

## Where each primitive is used

**Identity** — every instance mints an Ed25519 keypair at first start
(`identity_bootstrap.py`). The seed is KEK-encrypted and stored in
`instance_identity.identity_private_key`; the public key is the basis
for `instance_id = base32(SHA256(pk)[:20])`. When
`federation_sig_suite = "ed25519+mldsa65"`, a second (ML-DSA-65) key is
minted and stored in `pq_private_key` / `pq_public_key`.

**Federation envelopes** — `FederationService.send_event` builds an
envelope, AES-256-GCM-encrypts the payload under the per-pair session
key, and signs the envelope bytes with every algorithm in the peer's
negotiated `sig_suite`. See `federation/encoder.py`. The wire format is:

```json
{
  "msg_id":            "<uuid4>",
  "event_type":        "dm_message",
  "from_instance":     "<own instance_id>",
  "to_instance":       "<peer instance_id>",
  "timestamp":         "2026-04-18T12:34:56+00:00",
  "encrypted_payload": "<b64url(nonce)>:<b64url(ct+tag)>",
  "space_id":          null,
  "proto_version":     1,
  "sig_suite":         "ed25519" | "ed25519+mldsa65",
  "signatures": {
    "ed25519":  "<b64url Ed25519 sig>",
    "mldsa65":  "<b64url ML-DSA-65 sig>"
  }
}
```

The `signatures` map's key set is enforced to equal the algorithms
parsed from `sig_suite`. Verification is **AND across all algorithms**
(see `encoder.verify_signatures_all`): an attacker must break every
algorithm in the suite, not just one.

**Redelivery re-signs (fresh `timestamp`, same `msg_id`).** The §24.11
pipeline rejects any envelope whose `timestamp` is outside ±300 s. A
queued event redelivered from the outbox days later would fail that gate,
so `FederationService.resign_for_redelivery` rebuilds the envelope with a
fresh `timestamp` and re-signs it (same `msg_id`, same `encrypted_payload`,
same `sig_suite`) on every retry — otherwise structural / security events
in `NEVER_DROP` (bans, key revocations, `SPACE_DISSOLVED`) would be silently
lost to any peer offline more than 5 minutes. The `msg_id` is preserved, so
the receiver's replay cache still dedupes a redelivery whose `2xx` ack was
lost. Because re-signed retries pass the timestamp gate, the replay-dedup
window (`REPLAY_CACHE_WINDOW`, 24 h) is deliberately sized to outlast the
outbox's max jittered redelivery interval (~5.2 h) so a lost-ack retry is
caught by replay rather than applied twice. The re-sign uses the sender's
own signing key — an attacker who captures the bytes still cannot move the
timestamp window themselves (the signature covers `timestamp`).

**Pairing** — `PairingCoordinator.initiate/accept/confirm` runs a QR-
flow with an ephemeral X25519 ECDH. Two directional keys fall out via
HKDF-SHA256; each is AES-256-GCM-wrapped under the local KEK and
stored in `remote_instances.key_self_to_remote` /
`key_remote_to_self`. The QR payload carries `sig_suite` and
optionally `pq_identity_pk`; the receiver negotiates the intersection
(`crypto_suite.negotiate`) so a classical peer talking to a hybrid peer
settles on classical for that pair.

**Space content** — `SpaceContentEncryption` in
`services/space_crypto_service.py` owns an AES-256-GCM key per space
per epoch. Epochs rotate on membership change; old keys are retained
for historical decryption. `space_id` is the GCM AAD so a key lifted
from one space can't decrypt another.

**Sealed sender** (`federation/sealed_sender.py`) — for GFS-routed
events the sender's instance ID is separately encrypted under the
space key, so a passive GFS operator sees `space_id` + `epoch` for
routing and nothing else. Because a public/global space's content key
is shared widely, encryption alone proves only that the sealer held
the key — not *who* sealed it. So every envelope also carries an
`outer_signature`: the sender Ed25519-signs a canonical,
domain-separated message binding `space_id` + `epoch` + both
ciphertexts + `aead_suite`, using its **identity** seed (never the
shared space key). The recipient resolves the decrypted
`sender_instance_id` to that instance's registered identity pubkey,
checks `derive_instance_id(pubkey) == sender_instance_id` (binds
key↔id, 160-bit, mirrors the #596 mesh-route fix), then verifies the
signature. A key-holder forging content as another member, or a GFS
substituting/dropping a sealed blob, is detected and rejected
(`SealedSenderAuthError`); an envelope with no `outer_signature` is
rejected by `from_dict` (fail-closed — no unauthenticated path).

**Routed-envelope seal** (`federation/routed_crypto.py`) — for
multi-hop `SPACE_ROUTED` events the inner payload is sealed with a
per-route ephemeral X25519+HKDF directional key (HKDF info
``socialhome/space_routed/{origin-to-target,target-to-origin}``).
The target generates a fresh ephemeral on each `SPACE_FIND_ROUTE`
probe and ships the public via `SPACE_ROUTE_FOUND`; the origin
generates its own ephemeral on `send_routed` and stashes the priv
for the matching reply. Relays only ever see the opaque ciphertext.
The wire carries a `kem_suite` field (today only `"x25519"`) so a
future hybrid (`x25519+mlkem768`, Phase 2) is a suite-bump rather
than a wire-format break — see [`protocol/spaces.md` → "Mesh
routing"](protocol/spaces.md#mesh-routing-space_routed).

**Space content key delivery** (`services/space_crypto_service.py`)
ships the per-space AES-256-GCM key to a new remote member via the
§D1b invite/redeem envelope. The payload wears a `key_suite` field
(today only `"aesgcm-256"`) so future variants (e.g. ChaCha20-Poly1305
for low-power receivers, or a PQ-protected wrapping when Phase 2
lands a hybrid asymmetric channel for it) are wire-additive. AES-256
is Grover-resistant on the symmetric side, so the PQ migration here
is about strengthening the *delivery channel*, not the AEAD primitive.
Receivers reject unknown `key_suite` values rather than fall back —
see `apply_space_content_key_from_metadata` for the validator.

**Delegated-admin signing-seed delivery** (`SPACE_ADMIN_KEY_SHARE`,
`services/space_service.py`, v_22) — when the owner opts into
`SpaceFeatures.delegated_admin_authority`, the space's Ed25519 signing
*seed* (the private half of `identity_public_key`) is shipped to a
remote admin household so it can sign space-authority events offline.
The seed rides the encrypted peer-pair path (the directional session
key, same AES-256-GCM channel as every other federation event to that
peer) addressed to that one household — **never broadcast**. The
payload carries a `seed_suite` field (today only `"ed25519-seed"`,
validated against `SUPPORTED_SEED_SUITES`, rejected-not-defaulted on
unknown) so a future hybrid PQ signing key is a wire-additive suite
bump. The receiver fails closed — it accepts the seed only from the
authentic owner instance into a space whose *local* copy has
delegation enabled, and only when the b64url payload decodes to
exactly 32 bytes. See `_share_admin_signing_seed` (sender) /
`PrivateSpaceInviteHandler._on_admin_key_share` (receiver).

**Sealed-sender envelope** (`federation/sealed_sender.py`) wears the
``aead_suite`` field (today only ``"aesgcm-256"``) on its wire shape;
receivers reject unknown values via ``UnsupportedAeadSuite``. The
suite-tag retrofit promised in earlier revisions of this doc is now
shipped — every cryptographic wire format in the federation surface
carries a ``*_suite`` identifier (signatures, mesh KEM, content-key
delivery, sealed-sender AEAD). A future ChaCha20-Poly1305 or
PQ-protected variant is a wire-additive change.

**Binary media chunks** (`federation/media_framing.py`,
`federation/encoder.py:encrypt_bytes`) — media on the `fed-media-v1`
DataChannel ships the raw chunk as `nonce(12) ‖ AES-256-GCM(ct+tag)`,
**not base64**. The chunk's wrapping header is an ordinary signed
federation envelope, so origin auth + replay are the §24.11 envelope
guarantees unchanged. The binary payload is bound to that signed
envelope by a `chunk_sha256` field — the b64url SHA-256 of the
**plaintext** chunk — carried *inside* the AES-GCM-encrypted,
signature-covered envelope metadata. The receiver verifies
`sha256(plaintext) == chunk_sha256` (constant-time) after decryption,
so the hash commits to the exact bytes written to disk; tampering with
the payload fails the hash, tampering with the hash fails the GCM tag
or the signature. The metadata carries a `media_aead_suite` tag (today
only `"aesgcm-256"`, validated against `SUPPORTED_MEDIA_AEAD_SUITES`,
rejected-not-defaulted on unknown). **Nonce budget:** both the metadata
AEAD and the chunk AEAD draw a fresh random 96-bit nonce under the same
directional session key already used for every JSON envelope to that
peer. Chunking raises the messages-per-key count (a 200 MiB video ≈ 400
chunks × 2 GCM ops), but stays ~7–8 orders of magnitude under the NIST
SP 800-38D random-nonce birthday bound (~2³² messages/key) for any
realistic peer lifetime — never introduce a deterministic/counter nonce
that could collide with this random-nonce stream. See
[`protocol/media.md`](protocol/media.md).

**Binary app frames** (`federation/app_framing.py`) — app messages on the
`fed-app-v1` DataChannel (capability v_17) follow the same binary wire layout
as the media channel: `nonce(12) ‖ AES-256-GCM(ct+tag)` in the payload bytes,
wrapping header is a signed federation envelope, payload is bound to the header
by a `payload_sha256` field carried inside the encrypted envelope metadata
(identical pattern to `chunk_sha256` on the media channel).  Constants:
`APP_AEAD_SUITE_AESGCM_256 = "aesgcm-256"`, validated against
`SUPPORTED_APP_AEAD_SUITES`; unknown suites raise `UnsupportedAppAeadSuite`
with no default fallback.  Payload ceiling: 1 MiB (tighter than the 4 MiB
media ceiling — app messages are chess moves and whiteboard deltas, not media
blobs).  The JSON `APP_MESSAGE` event fallback uses the same per-pair AES-256-GCM
session key as every other federation envelope (no additional symmetric key
material is needed).  See [`protocol/apps.md`](protocol/apps.md).

**WebRTC SDP signing** (`federation/sdp_signing.py`) — Ed25519
signature over `<sdp_type>:<sdp>` so a MITM can't swap DTLS endpoints.

**Standalone auth** — `StandaloneAdapter` hashes passwords with scrypt
and embeds parameters in the stored hash: `scrypt$16384$8$1$<salt
hex>$<hash hex>`. Parameters can be bumped without a schema change.

## Key storage

### Database

| Table | Column | Content | Wrapped? | AAD |
|-------|--------|---------|----------|-----|
| `instance_identity` | `identity_private_key` | 32-byte Ed25519 seed | KEK AES-256-GCM | — |
| `instance_identity` | `identity_public_key` | 32-byte Ed25519 public key (hex) | no | — |
| `instance_identity` | `pq_private_key` | ML-DSA-65 secret key | KEK AES-256-GCM | — |
| `instance_identity` | `pq_public_key` | ML-DSA-65 public key (hex) | no | — |
| `instance_identity` | `routing_secret` | 32-byte HMAC key (hex) | no *(local-only, never transmitted)* | — |
| `remote_instances` | `key_self_to_remote` | 32-byte session key | KEK AES-256-GCM | — |
| `remote_instances` | `key_remote_to_self` | 32-byte session key | KEK AES-256-GCM | — |
| `remote_instances` | `remote_identity_pk` | Peer Ed25519 public key (hex) | no | — |
| `remote_instances` | `remote_pq_identity_pk` | Peer ML-DSA-65 public key (hex) | no | — |
| `remote_instances` | `sig_suite` | Negotiated per-peer suite | no | — |
| `space_keys` | `content_key_hex` | 32-byte space content key | KEK AES-256-GCM | `space_id` |
| `pending_pairings` | `own_dh_sk` | X25519 ephemeral secret | KEK AES-256-GCM | — |
| `platform_users` | `password_hash` | `scrypt$N$r$p$salt$hash` | self-contained (params + salt embedded) | — |
| `api_tokens` | `token_hash` | `sha256(token)` | no | — |

### Filesystem

| Path | Content | Permissions |
|------|---------|-------------|
| `{data_dir}/.kek_salt` | 32-byte random salt (input to KEK HKDF) | `0600` |
| `{data_dir}/.vapid_private.pem` | P-256 ECDSA private key (PKCS8 PEM) | `0600` |
| `{data_dir}/.vapid_public.txt` | P-256 public key (base64url uncompressed point) | `0644` |

The KEK itself is never stored — it's re-derived from the salt on each
startup via `KeyManager.from_data_dir`. Passphrase-mode deployments
use `KeyManager.from_passphrase(passphrase, salt)`; losing the
passphrase bricks the instance's existing wrapped keys.

## Derivation chains

- `KEK = HKDF-SHA256(salt, length=32, info=b"socialhome/kek/from-data-dir")`
- Pairing directional keys (post-ECDH):
  - `key_self_to_remote = HKDF-SHA256(shared_secret, info=b"socialhome/session/self-to-remote")`
  - `key_remote_to_self = HKDF-SHA256(shared_secret, info=b"socialhome/session/remote-to-self")`
- ID derivation (public, deterministic, no secret):
  - `instance_id = base32(SHA256(identity_pk)[:20]).lower()`
  - `space_id    = base32(SHA256(space_pk)[:20]).lower()`
  - `user_id     = base32(SHA256(instance_pk ‖ 0x00 ‖ username)[:20]).lower()`

## Post-quantum migration path

### Threat: harvest-now-decrypt-later

A quantum adversary can record federation traffic today and decrypt it
years later once a fault-tolerant quantum computer exists. What's at
risk depends on the primitive:

| Primitive | Risk tomorrow | Mitigation in v1 |
|-----------|---------------|------------------|
| Ed25519 identity signatures | Forgeable — attacker can impersonate any peer in replays | **Hybrid Ed25519+ML-DSA-65** (this doc) |
| X25519 pairing ECDH | Shared secret recoverable; all historical envelopes readable | Phase 2 — ML-KEM-768 for pairing |
| AES-256-GCM payload | Safe (Grover ⇒ ~128-bit) | No change needed |
| P-256 VAPID | VAPID JWT forgeable | Phase 3 — pending Web Push spec update |

### The suite contract

The wire-format building block is the `sig_suite` field. Its grammar:
`<algo>` or `<algo>+<algo>+…`, where each `<algo>` appears in
`socialhome/federation/crypto_suite.KNOWN_ALGORITHMS`. Current
registry:

- `ed25519` — classical.
- `mldsa65` — ML-DSA-65 (FIPS 204, NIST security level 3).

A hybrid envelope's `signatures` map must contain exactly one entry
per algorithm in the suite. Verification is AND — missing or invalid
entries reject the envelope.

Extending the suite with a third algorithm (e.g. SLH-DSA for
long-term archival signatures) is a three-step change:

1. Add the identifier to `KNOWN_ALGORITHMS` + `SUPPORTED_SUITES`.
2. Add a signer class alongside `PqSigner`.
3. Extend `FederationEncoder.sign_envelope_all` /
   `verify_signatures_all` to dispatch on the new identifier.

No wire-format or schema change is needed — the `signatures` map
grows naturally.

### Phase 1 — hybrid signatures (done)

- Config: `federation_sig_suite = "ed25519+mldsa65"`.
- Library: `liboqs-python` via the `pq` optional extra.
- Identity: on startup, `ensure_instance_identity` mints an ML-DSA-65
  keypair and persists the secret KEK-encrypted in
  `instance_identity.pq_private_key`. On an existing deployment that
  later enables hybrid, the bootstrap upgrades the row in place.
- Pairing: QR payload carries `pq_identity_pk` + `sig_suite`. The
  receiver runs `crypto_suite.negotiate`; a classical peer paired
  with a hybrid peer runs classical for that pair.
- Wire: every outbound envelope emits the `signatures` map with the
  per-peer suite's algorithms. Every inbound envelope is rejected
  unless every entry verifies.

### Phase 2 — post-quantum key agreement

Not done in this repo yet. The work is to introduce a `kem_suite`
field to the pairing QR payload and replace X25519 with a hybrid
X25519+ML-KEM-768 KEM (Signal's PQXDH-style). Directional keys would
be derived from the concatenated X25519 + ML-KEM shared secrets
through HKDF. The AES-256-GCM-wrapped session keys on the
`remote_instances` rows stay — only their derivation changes.

### Phase 3 — Web Push VAPID

Blocked on the IETF: Web Push (RFC 8292) currently mandates ECDSA
P-256 for VAPID. When a PQ signature variant lands in the spec,
`services/push_service.py`'s `load_or_create_vapid` grows a PQ branch.
Until then we rely on the transport (browser → push server) being
TLS-protected.

### Phase 4 — retire classical halves

Only once every peer in the federation has rotated to a hybrid suite.
Retirement is a flag on `Config` (`federation_require_pq = True`)
rather than a wire-format change — hybrid receivers start rejecting
classical envelopes. The admin UI should warn about any paired peers
still on `sig_suite = "ed25519"` before the flag is flipped.

## Operator checklist

To enable the hybrid signature suite on a deployment:

1. Install `liboqs-python` next to the existing `socialhome` install:
   `pip install 'oqs @ git+https://github.com/open-quantum-safe/liboqs-python@0.10.0'`
   (PyPI doesn't accept direct URL refs so we can't bundle this as a
   `socialhome[pq]` extra; install it manually). Requires the native
   `liboqs` C library on the host.
2. Set `federation_sig_suite = "ed25519+mldsa65"` in `socialhome.toml`
   (or `SH_FEDERATION_SIG_SUITE=ed25519+mldsa65` as an env var).
3. Restart. `identity_bootstrap` detects the suite change and mints
   the ML-DSA keypair on startup.
4. Re-pair any existing paired peers. Newly paired peers automatically
   negotiate the hybrid suite if they also have it enabled.

`liboqs-python` must be compatible with the host's liboqs C library —
mismatched versions will fail at import. Test this end to end in a
staging deployment before flipping a production instance.

## References

- **NIST FIPS 203** (ML-KEM, standardises CRYSTALS-Kyber).
- **NIST FIPS 204** (ML-DSA, standardises CRYSTALS-Dilithium).
- **NIST FIPS 205** (SLH-DSA, standardises SPHINCS+).
- **Signal "PQXDH"** (2023) — the canonical hybrid KEM protocol
  reference.
- **liboqs-python** — <https://github.com/open-quantum-safe/liboqs-python>.
- **RFC 8292** (VAPID for Web Push).
- **RFC 5869** (HKDF).
- **RFC 7914** (scrypt).
