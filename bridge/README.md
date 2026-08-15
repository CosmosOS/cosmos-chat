# mautrix-discord bridge setup

The bridge generates its own default config on first run — we then edit a few
fields and let it generate the appservice registration for Synapse.
All commands run on the VPS as the `chat` user, from the repo directory.

## 1. Generate the default config

```bash
docker compose run --rm bridge
# exits after writing /data/config.yaml (inside the cosmos-chat_bridge-data volume)
```

## 2. Copy it out, edit, copy it back

```bash
docker run --rm -v cosmos-chat_bridge-data:/data -v "$PWD/bridge":/out alpine \
    cp /data/config.yaml /out/config.yaml
# … edit bridge/config.yaml (see checklist below) …
docker run --rm -v cosmos-chat_bridge-data:/data -v "$PWD/bridge":/out alpine \
    cp /out/config.yaml /data/config.yaml
```

### Config checklist

- `homeserver.address`: `http://synapse:8008`
- `homeserver.domain`: `gocosmos.org`
- `appservice.address`: `http://bridge:<port>` — keep the default `port` from the
  generated file and make the hostname `bridge`
- `appservice.database`: (in `database.uri` on newer versions)
  `postgres://mautrix_discord:<POSTGRES_BRIDGE_PASSWORD from .env>@postgres/mautrix_discord?sslmode=disable`
- `bridge.permissions`:
  ```yaml
  permissions:
      "*": relay
      "gocosmos.org": user
      "@<your-admin-user>:gocosmos.org": admin
  ```
- Enable the relay/webhook section so Matrix users appear natively on Discord.

## 3. Generate the registration and give it to Synapse

```bash
docker compose run --rm bridge   # now generates /data/registration.yaml
docker run --rm -v cosmos-chat_bridge-data:/data -v "$PWD/synapse":/out alpine \
    cp /data/registration.yaml /out/discord-registration.yaml
```

Then:
1. In `compose.yml`, uncomment the `discord-registration.yaml` mount under `synapse`.
2. In `synapse/homeserver.yaml`, uncomment `app_service_config_files`.
3. `docker compose up -d` (restarts Synapse, starts the bridge).

## 4. Connect to Discord and bridge the guild

Create a bot in the [Discord developer portal](https://discord.com/developers/applications)
(scopes: `bot`; permissions: read messages/history, send messages, manage webhooks;
enable the *Message Content* and *Server Members* intents). Invite it to the
CosmosOS guild.

In Matrix, DM `@discordbot:gocosmos.org`:

```
login-token bot <BOT_TOKEN>
guilds status            # list guilds the bot sees
guilds bridge <guild-id> --entire   # or bridge selected channels only
```

Registration/config contain secrets (`as_token`, `hs_token`, bot token) —
they are gitignored; never commit them.
