# Rally Bot

Discord bot for managing game rallies with voice countdown features.

## Features

- 🏰 Create Keep Rallies with detailed info
- 👑 Create Seat of Power (SOP) Rallies
- 🎙️ Voice countdown timers (Bomb & Rolling rallies)
- 👥 Participant tracking and roster management
- 📢 Automatic temporary voice channel creation

## Commands

### Rally Management
- `/rally keep` - Create a Keep Rally (opens form)
- `/rally sop` - Create a Seat of Power Rally

### Voice Countdown
- `/type_of_rally bomb <duration>` - Start bomb rally countdown (5m, 10m, 30m, 1h)
- `/type_of_rally rolling <gap>` - Start rolling rally countdown (5s, 10s, 15s, 30s)

### Utility
- `/stay` - Keep bot in voice channel
- `/leave` - Disconnect bot from voice

## Deployment on Railway

1. Fork/clone this repository
2. Sign up at [Railway](https://railway.app)
3. Create new project from GitHub repo
4. Add environment variables (see below)
5. Deploy!

## Required Environment Variables

Set these in Railway dashboard:

```
DISCORD_BOT_TOKEN=your_bot_token_here
ENABLE_VOICE=True
TEMP_VC_CATEGORY_ID=your_category_id (optional)
HITTERS_ROLE_NAME=hitters
DELETE_VC_IF_EMPTY_AFTER_SECS=300
```

Audio file URLs (already configured):
```
AUDIO_5M_BOMB=https://storage.googleapis.com/rallybot/5minbombcomplete.mp3
AUDIO_10M_BOMB=https://storage.googleapis.com/rallybot/10minbomb.mp3
AUDIO_EXPLAIN_BOMB=https://storage.googleapis.com/rallybot/explainbombrally.mp3
AUDIO_5S_ROLL=https://storage.googleapis.com/rallybot/5secondgaps.mp3
AUDIO_10S_ROLL=https://storage.googleapis.com/rallybot/10secondgaps.mp3
AUDIO_15S_ROLL=https://storage.googleapis.com/rallybot/15secondgaps.mp3
AUDIO_30S_ROLL=https://storage.googleapis.com/rallybot/30secondgaps.mp3
```

## Setup Instructions

### Get Your Discord Bot Token
1. Go to [Discord Developer Portal](https://discord.com/developers/applications)
2. Create a new application (or select existing)
3. Go to "Bot" section
4. Click "Reset Token" and copy it
5. Enable "Message Content Intent" and "Server Members Intent"
6. Save changes

### Get Your Category ID (Optional)
1. Enable Developer Mode in Discord (Settings > Advanced > Developer Mode)
2. Right-click on the category you want to use for rally VCs
3. Click "Copy ID"

### Invite Bot to Server
1. In Discord Developer Portal, go to OAuth2 > URL Generator
2. Select scopes: `bot`, `applications.commands`
3. Select permissions: 
   - Manage Channels
   - Connect
   - Speak
   - Move Members
   - Send Messages
   - Embed Links
   - Attach Files
   - Read Message History
   - Use Slash Commands
4. Copy the generated URL and open it in browser
5. Select your server and authorize

## Tech Stack

- Python 3.12
- discord.py 2.4.0
- Docker
- ffmpeg (for voice)

## License

MIT
