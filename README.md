# aih-plugin-cloudflared

AIH `network.connector` plugin for **Cloudflare Tunnel**.

- Connector id: `cloudflared`
- Transport: outbound tunnel (never reported as P2P direct)
- Operations: `install`, `uninstall`, `configure`, `enable`, `disable`, `probe`
- Version is reported, never enforced. `engines.aih` is wide (`>=0.0.0`).

## What it does

- Detects `cloudflared` and reports its version.
- Stores the desired state under `$HOME/.ai_home/plugin-state/network.cloudflared/` (0600).
- `enable` starts `cloudflared tunnel --no-autoupdate run` in its own process group.
  - Token is passed through `TUNNEL_TOKEN`, never through argv.
  - Credentials-file mode is also supported.
- `disable` stops the tunnel (SIGTERM, then SIGKILL).
- `install` / `uninstall` use Homebrew when available; otherwise they return an
  explicit message instead of pretending success.

## Install

```sh
# Build and install (unsigned during development)
aih plugin pack . cloudflared-0.1.0.aih-plugin
aih plugin install ./cloudflared-0.1.0.aih-plugin
aih plugin enable network.cloudflared --accept-permissions
```

For a distributed build, sign it with an Ed25519 release key:

```sh
aih plugin keygen --out keys
aih plugin pack . cloudflared-0.1.0.aih-plugin \
  --signing-key keys/private.pem --key-id aih-plugins
aih plugin trust keys/public.pem --key-id aih-plugins
aih plugin install ./cloudflared-0.1.0.aih-plugin --require-signed
```

Never commit `keys/private.pem`.

## Configure

The WebUI sends `configure` input to the plugin. Accepted JSON:

```json
{
  "tunnelToken": "<cloudflare tunnel token>",
  "credentialsFile": "/path/to/credentials.json",
  "hostname": "aih.example.com",
  "serviceUrl": "http://127.0.0.1:9527"
}
```

`tunnelToken` is marked as a secret field; status responses never echo it.

## Boundaries

- No Electron, WebView, CDP, or stealth.
- A running outbound tunnel is reported as `outbound-tunnel`; it is not
  evidence of direct P2P connectivity.
- `install` does not use `sudo`; Homebrew installs run as the current user.
