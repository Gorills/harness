# ADR 0083: Offline Android tunnel profiles and per-application VPN

Date: 2026-11-09. Status: implemented; native-device acceptance remains unverified.

## Decision

The human `/tunnels/` workspace can explicitly export the **primary VPS's client
transport access** as one offline QR code. `phone_export` is a separate read-only
privileged-service command. It takes only a profile identifier, holds the service
operation lock, copies an explicit field allowlist and never enters network recovery,
persistence, SSH, process or guard paths, including on failure. The controller checks
profile revision, preparation and pending operations. The authenticated same-origin
POST returns bounded QR pixels with `Cache-Control: no-store`. QR data is absent from
SQLite, observations, SSE, event history and browser storage; closing the dialog clears
its canvas. No public configuration URL or new VPS listener is introduced.

The QR shares existing client credentials. It does **not** create an independently
revocable device identity. This deliberately preserves the currently running desktop
connection and requires no VPS reconfiguration. The dialog states that access is shared.
Removing a profile from Android does not invalidate other saved QR copies. The schema
contains client UUID/public REALITY parameters and Hysteria password/obfuscation/server
certificate; it excludes REALITY private keys, SSH and maintenance credentials. Backup
VPS and desktop domain/LAN exclusions are not included in this first mobile profile.

The `HT1:` envelope contains uncompressed base64url JSON, bounded to2800ASCII characters.
Android rejects other schemes, oversized inputs, unknown/missing fields, invalid IP and
transport values, and invalid certificates. It never fetches a profile from a QR URL or
accepts arbitrary engine configuration. A camera permission is requested only in the
scanner. Profile secrets are encrypted with Android Keystore AES-GCM, in app-private
no-backup storage. No profile credentials or request history are logged.

The Android app uses a native `VpnService` and an explicit package allowlist. Only enabled,
installed selected packages can reach `addAllowedApplication`; self and missing packages
are removed. No TUN is established when zero packages remain. Unselected packages keep
ordinary connectivity. Android enforces VPN policy by UID: packages sharing one UID may
share that policy. Selection is saved explicitly; changing it reestablishes only the
phone's VPN. An empty saved selection stops the phone VPN. This never operates the desktop
network or any VPS service.

The service owns the original Android TUN descriptor; the Go core owns a duplicate. Each
outbound TCP/UDP socket is protected and bound to a non-VPN Android `Network` through a
JNI callback. Platform interface data comes from Android rather than restricted netlink
queries. Native work is serialized on one worker; stop/revoke invalidate queued attempts
and close both descriptor owners. Network changes recreate the engine against a current
underlay. Hysteria2 is tried first and REALITY second, with12second HTTPS probes through
the selected outbound. The green connected state requires a successful authenticated
HTTPS204 response, rather than merely creating a lazy outbound. Failure retains the TUN
while retrying, so selected traffic waits; there is no direct outbound fallback. Android
process termination/force-stop can remove VPN routes. This version does not advertise an
always-on or process-death kill-switch guarantee.

## Build and verification boundary

`android/build.py` builds an arm64-v8a APK for Android8/API26 and newer, targetAPI36,
using the existing patched sing-box source recipe, Go1.26.10, SDK Build Tools36.0.0,
NDK27.1.12297006 and JDK17. There is no Gradle, Compose or gomobile runtime. The source
archive, module graph and checksums are verified before compilation; source patches are
identical to ADR0082. JNI is the only new native boundary. ELF load segments use16KiB
alignment; zip alignment and APK signatures are verified. The signer is supplied
externally and never copied into source or APK. The delivered private development APK
is signed by an ignored workspace-local development key; public distribution/signing is
outside this task.

ZXing Core3.5.4 has no runtime Maven transitives. QR generation uses qrcode8.2 without
image-library extras. Exact-version OSV queries and upstream advisory pages were checked
on2026-11-09; both returned no published matching advisories. Android binary Go scanning
and actual check results belong to the task evidence, not a claim that all defects are
absent. Core source provenance, compiled-module license notices and component versions are
included in the APK. No emulator or phone was used; the operator explicitly chose software
checks first. Camera, Android permission/lifecycle, rendered native UI and actual per-app
routing therefore require subsequent device acceptance. The current desktop VPN and VPS
were not restarted, changed or used by the implementation tests.

Primary references:

- [Android VPN and per-app lists](https://developer.android.com/develop/connectivity/vpn)
- [Android Keystore](https://developer.android.com/privacy-and-security/keystore)
- [16KiB native alignment](https://developer.android.com/guide/practices/page-sizes)
- [NDK revision history](https://developer.android.com/ndk/downloads/revision_history)
- [ZXing3.5.4](https://github.com/zxing/zxing/releases/tag/zxing-3.5.4)
- [ZXing advisories](https://github.com/zxing/zxing/security/advisories)
- [qrcode8.2](https://pypi.org/project/qrcode/8.2/)
- [qrcode advisories](https://github.com/lincolnloop/python-qrcode/security)
- [Go vulnerability database](https://vuln.go.dev/)
