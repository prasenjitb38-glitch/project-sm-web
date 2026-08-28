# Mirae Asset Sharekhan (SKAPI) connection

Project SM is prepared for Sharekhan as a secure, server-side broker connection.

## 1. Activate the API

Open the Mirae Asset Sharekhan Trading API page with your active Sharekhan account. Create an API application and note its **API Key** and **Secure Key**. Enable TOTP in Sharekhan first, because login requires OTP/TOTP.

## 2. Add the secrets to the server

For local testing, set these as environment variables before starting Project SM:

```powershell
$env:SHAREKHAN_API_KEY = "your-api-key"
$env:SHAREKHAN_SECURE_KEY = "your-secure-key"
python app.py
```

For the deployed Render service, add the same names in **Render Dashboard → project-sm-web → Environment → Environment Variables**, then redeploy.

Never put either value in `app.py`, `index.html`, GitHub, or the mobile app.

## 3. Select it in Project SM

Open **Settings → Broker API Connection → Mirae Asset Sharekhan (SKAPI)**. Project SM will show whether the server has the required secure configuration.

After the credentials are available, the final live session is authorised through Sharekhan's own OTP/TOTP screen. This deliberate step protects the trading account; Project SM does not collect or store the Sharekhan password or OTP.
