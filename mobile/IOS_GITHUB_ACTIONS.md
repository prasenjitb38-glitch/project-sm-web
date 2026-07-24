# Build an iPhone IPA with GitHub Actions

This project includes `.github/workflows/ios-ipa.yml`. It runs on a GitHub-hosted Mac, so a Windows PC can start the iOS build.

## Before the first build

You need an Apple Developer account and an App ID matching `com.example.projectSmMobile` (change this identifier before publishing if you want your own unique App ID). Create either:

- an **Ad Hoc** provisioning profile, after registering the iPhone UDID, to install the IPA on that registered iPhone; or
- an **App Store** provisioning profile, to upload the IPA to App Store Connect/TestFlight.

In the GitHub repository open **Settings → Secrets and variables → Actions → New repository secret**. Add these six secrets:

| Secret name | Value |
| --- | --- |
| `APPLE_TEAM_ID` | Apple Developer Team ID |
| `IOS_CERTIFICATE_BASE64` | Base64 text of the distribution `.p12` certificate |
| `IOS_CERTIFICATE_PASSWORD` | Password used when exporting that `.p12` file |
| `IOS_PROVISIONING_PROFILE_BASE64` | Base64 text of the `.mobileprovision` file |
| `IOS_PROVISIONING_PROFILE_NAME` | Exact provisioning profile name from Apple Developer |
| `IOS_KEYCHAIN_PASSWORD` | Any new long random password for the temporary GitHub build keychain |

Do not upload a certificate or provisioning profile as a normal repository file, and do not share the secret values in chat.

## Run the iOS build

1. Push this project to GitHub.
2. Open the GitHub repository → **Actions** → **Build Project SM iOS IPA**.
3. Choose **Run workflow**.
4. Choose `ad-hoc` for an already registered iPhone, or `app-store` for TestFlight.
5. When the run completes, open it → **Artifacts** → download `Project-SM-iOS-IPA-...`.

An Ad Hoc IPA can only install on iPhones whose UDID is in its provisioning profile. For TestFlight, upload the App Store IPA using Transporter on a Mac or App Store Connect tooling; users then install it from the TestFlight iPhone app.
