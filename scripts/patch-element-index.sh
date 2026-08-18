#!/usr/bin/env bash
# Patches Element's index.html (stdin -> stdout) for serving via Caddy:
#  - loads /branding/register-redirect.js ahead of the app bundle so the
#    #/register client-side route lands on /join (the captcha-gated signup)
#  - strips Google's recaptcha hosts from the CSP script-src; we never use
#    reCAPTCHA, so the browser must refuse those origins outright
# The deploy workflow regenerates caddy/assets/element-index.html with this
# script on every deploy, so an Element image bump can never drift from the
# served shell.
set -euo pipefail
sed -e 's|<script src="bundles/|<script src="/branding/register-redirect.js"></script><script src="bundles/|' \
    -e 's| https://www.recaptcha.net/recaptcha/ https://www.gstatic.com/recaptcha/||g'
