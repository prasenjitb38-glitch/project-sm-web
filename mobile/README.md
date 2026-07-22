# Project SM Mobile (Android + iPhone)

This Flutter app opens the same Project SM interface in a mobile-native shell, so
watchlist, charts and API behaviour remain shared with the web and Windows app.

## One prerequisite: public HTTPS server

Android and iPhone cannot reach the Flask server bundled inside the Windows app.
The default release server is `https://project-sm-web.onrender.com`. Do not use a
local `localhost` address for a release app.

## Create the platform folders

Install Flutter, then run these commands from this `mobile` folder:

```powershell
flutter create --platforms=android,ios .
flutter pub get
```

In `android/app/src/main/AndroidManifest.xml`, add this line immediately inside the
`<manifest>` element:

```xml
<uses-permission android:name="android.permission.INTERNET" />
```

## Android APK

```powershell
flutter build apk --release
```

The APK will be in `build/app/outputs/flutter-apk/app-release.apk`.

## iPhone app

On a Mac with Xcode installed:

```bash
flutter build ipa --release
```

Use HTTPS in production. This avoids iOS App Transport Security exceptions and keeps
login and market traffic protected.
