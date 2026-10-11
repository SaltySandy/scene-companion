# Debug signing keystore

`debug.keystore` is the **debug-only** Android keystore used by `assembleDebug`.

It exists so every cloud/local debug APK is signed with the same certificate and
`adb install -r` can replace the Pixel install in place.

- Alias: `androiddebugkey`
- Store / key password: `android` (Android debug defaults)
- Wired in `app/build.gradle.kts` → `signingConfigs.debug`

Do **not** use this keystore for a Play / release build.
