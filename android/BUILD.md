# Android APK

The Android app is a small native Java/VpnService shell around the same patched
sing-box engine as the desktop tunnel. `build.py` assembles it with the Android SDK
without Gradle. It never installs the APK or operates the desktop tunnel/VPS.

Requirements: Linuxamd64, JDK17, the exact recipe's Go1.26.10, Android SDK platform36,
Build Tools36.0.0 and NDK27.1.12297006. Build the core from pinned verified source;
do not substitute an upstream prebuilt/AAR, because it omits the bounded TLS/FakeIP
patches. Dependencies are pinned in `build.py`, the root Python lockfile and
`src/harness/tunnel_recipe/engine-source-build-recipe.json`.

Supply an existing private keystore and put its password in
`HARNESS_ANDROID_SIGNING_PASSWORD`; no signing secrets are stored in source.
The default alias is `harness-android`. For development, generate a private key
outside source with `keytool`; preserve it to sign compatible updates. A different
signer requires uninstalling the old APK, which deletes its local profile.

```bash
scripts/dev python android/build.py \
  --sdk /absolute/path/to/Android/Sdk \
  --go /absolute/path/to/go1.26.10/bin/go \
  --work /tmp/harness-android-build-fresh \
  --cache /tmp/harness-android-downloads \
  --out /absolute/path/to/dist/harness-tunnels-android-arm64.apk \
  --keystore /private/path/to/android.jks
```

Use a fresh `--work` directory each run. `--module-cache` and `--go-build-cache` may
reuse normal Go caches; the build verifies modules and the complete locked module
graph. Downloads are bounded and SHA256-verified. The script verifies signer and
zip alignment and emits `<apk>.build.json` with source and native provenance.
The APK contains corresponding notices/licenses and the complete dependency graph.
It targets Android8/API26+ on arm64-v8a; 32bit ARM and x86 are not packaged.

This task's development signer lives under ignored `.harness/android-signing/` with
mode0600 private files. It is deliberately absent from the APK and Git diff.
Do not publish the development signer or use it as a production distribution key.

Software checks cover QR serialization/decoding, read-only service export failure
paths, CSRF/revision checks, negative disclosure/persistence, existing desktop
regressions, Java/DEX/native compilation, ELF16KiB alignment and APK signing.
Native camera, consent/revocation, lifecycle and actual selected/bypassed app traffic
remain unverified until a phone or approved emulator is available.
