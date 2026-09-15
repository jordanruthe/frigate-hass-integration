# Fork: client certificate + forward-auth support

This fork of [blakeblackshear/frigate-hass-integration](https://github.com/blakeblackshear/frigate-hass-integration)
lets the integration reach a Frigate instance that sits behind a reverse proxy
requiring a TLS client certificate, including SSO forward-auth setups such as
Authentik with a certificate-based login flow.

## How it works

When a client certificate and key are configured (Settings → Devices & services
→ Frigate → Reconfigure, or during setup):

- Every request to Frigate — API calls, snapshots, WebRTC offers and the proxied
  clips/recordings/live views — presents the certificate.
- If the proxy redirects to an Authentik flow (`/if/flow/<slug>/`), the
  integration drives it through Authentik's flow executor API
  (`/api/v3/flows/executor/<slug>/`). A flow that authenticates the user from the
  certificate needs no interaction, so it redirects back to Frigate and the
  forward-auth session cookie is kept and sent on every request.
- When the session expires the proxy redirects again; the API client logs in
  again and retries.

If the flow stops at an interactive stage (for example the certificate CN does
not map to a user), setup fails with a "Forward-auth login failed" error in the
log naming the stage.

The certificate must be PEM, and the key unencrypted PEM. Convert a PKCS#12
bundle with:

```sh
openssl pkcs12 -in client.p12 -clcerts -nokeys -out client.crt
openssl pkcs12 -in client.p12 -nocerts -nodes -out client.key
```

MQTT is unchanged: Home Assistant still needs the same broker as Frigate.

## Releases

`.github/workflows/sync-upstream.yml` runs daily, on pushes to `master` and on
demand, using only `GITHUB_TOKEN`:

1. Finds the latest upstream release and skips if the current `master` commit
   was already released on top of it.
2. Cherry-picks the fork commits (those on `master` that upstream does not
   have) onto that release tag in the runner and runs the test suite. Conflicts
   open an issue; fix by rebasing `master` onto the tag and pushing.
3. Publishes `<upstream tag>-mtls.<n>`, tagged at the `master` commit, with the
   integration as `frigate.zip` (HACS installs it via `zip_release`).

Nothing is pushed by CI, so `master` only changes when you push it.
