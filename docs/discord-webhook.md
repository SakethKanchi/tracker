# Discord webhook dashboard

Push the same Claude + Grok usage tree to a Discord channel. The poller posts
one message, then edits it on each cycle — no bot token, no slash commands.

## 1. Create a webhook

1. Open Discord → channel settings → **Integrations** → **Webhooks**
2. **New Webhook** → copy the URL  
   (`https://discord.com/api/webhooks/<id>/<token>`)

## 2. Configure tracker

```bash
mkdir -p ~/.config/tracker
cat > ~/.config/tracker/webhook.json <<'EOF'
{
  "url": "https://discord.com/api/webhooks/YOUR_ID/YOUR_TOKEN",
  "interval_sec": 300
}
EOF
chmod 600 ~/.config/tracker/webhook.json
```

| Field | Meaning | Default |
|-------|---------|---------|
| `url` | Discord webhook URL | required |
| `interval_sec` | Seconds between force-sync + edit | `300` |

## 3. Run once (smoke test)

```bash
tracker webhook --once
```

You should see a usage embed appear in the channel. Message id is saved to
`~/.local/share/tracker/webhook_message_id` so restarts edit the same message.

## 4. Run as a user service (optional)

```ini
# ~/.config/systemd/user/tracker-webhook.service
[Unit]
Description=tracker Discord usage webhook
After=network-online.target

[Service]
Type=simple
ExecStart=%h/.local/bin/tracker webhook
Restart=on-failure
RestartSec=30

[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now tracker-webhook.service
journalctl --user -u tracker-webhook -f
```

## Notes

- Each cycle force-syncs all accounts (`collect_all(..., force=True)`), so
  Discord stays fresher than a plain `tracker list` cache hit.
- If the message was deleted in Discord, the next cycle posts a new one and
  updates the saved message id.
- Keep `webhook.json` private — the URL is a secret bearer credential.
