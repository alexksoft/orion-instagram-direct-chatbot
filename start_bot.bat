@echo off
REM Start the chatbot server and Cloudflare tunnel in two separate windows.
REM The tunnel gives the server a public HTTPS URL that Instagram can reach.
REM
REM IMPORTANT: The tunnel URL changes every time you restart!
REM After starting, copy the new URL from the tunnel window and update it in:
REM   Meta Developer Console → Your App → Webhooks → Callback URL
REM   (set it to: https://your-new-url.trycloudflare.com/webhooks/instagram)

start cmd /k "cd /d %~dp0 && .venv\Scripts\activate && python run.py"
