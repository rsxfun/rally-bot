# main.py — Rally Bot (Fixed for Render hosting)
# Key fixes:
# - Added HTTP health check server for Render
# - Simplified voice state management
# - Better error handling and reconnection logic
# - Improved timeout handling

import os
import time
import asyncio
import logging
from dataclasses import dataclass, field
from typing import Dict, Optional, List, Literal, Tuple
from threading import Thread

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

# HTTP server for health checks
from http.server import HTTPServer, BaseHTTPRequestHandler

# Prefer IPv4 on some VPS hosts
discord.VoiceClient.use_ipv6 = False

# ============================== HEALTH CHECK SERVER ==============================
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/health' or self.path == '/':
            self.send_response(200)
            self.send_header('Content-type', 'text/plain')
            self.end_headers()
            self.wfile.write(b'OK')
        else:
            self.send_response(404)
            self.end_headers()
    
    def log_message(self, format, *args):
        # Suppress health check logs
        pass

def run_health_server(port=8080):
    """Run a simple HTTP server for health checks"""
    server = HTTPServer(('0.0.0.0', port), HealthCheckHandler)
    logging.info(f"Health check server running on port {port}")
    server.serve_forever()

# ============================== ENV & CONFIG ==============================
load_dotenv()

TOKEN = os.getenv("DISCORD_BOT_TOKEN") or os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise RuntimeError("Set DISCORD_BOT_TOKEN (or DISCORD_TOKEN) in your environment.")

GUILD_IDS = os.getenv("GUILD_IDS", "")
TEMP_VC_CATEGORY_ID = int(os.getenv("TEMP_VC_CATEGORY_ID", "0"))
HITTERS_ROLE_NAME = os.getenv("HITTERS_ROLE_NAME", "hitters")
DELETE_VC_IF_EMPTY_AFTER_SECS = int(os.getenv("DELETE_VC_IF_EMPTY_AFTER_SECS", "300"))

ENABLE_VOICE = os.getenv("ENABLE_VOICE", "false").strip().lower() in ("1", "true", "yes")

DISCONNECT_AFTER_PLAY_SECS = int(os.getenv("DISCONNECT_AFTER_PLAY_SECS", "120"))
VOICE_IDLE_TIMEOUT_SECS = int(os.getenv("VOICE_IDLE_TIMEOUT_SECS", "1800"))

RTC_REGION_FOR_TEMP_VC = os.getenv("RTC_REGION_FOR_TEMP_VC", "").strip()
FORCE_RTC_REGION = os.getenv("FORCE_RTC_REGION", "").strip()

# Health check port (Render uses PORT env var)
HEALTH_PORT = int(os.getenv("PORT", "8080"))

# Audio URLs
AUDIO_5M_BOMB = os.getenv("AUDIO_5M_BOMB", "https://storage.googleapis.com/rallybot/5minbombcomplete.mp3")
AUDIO_10M_BOMB = os.getenv("AUDIO_10M_BOMB", "https://storage.googleapis.com/rallybot/10minbomb.mp3")
AUDIO_30M_BOMB = os.getenv("AUDIO_30M_BOMB", "")
AUDIO_1H_BOMB = os.getenv("AUDIO_1H_BOMB", "")
AUDIO_EXPLAIN_BOMB = os.getenv("AUDIO_EXPLAIN_BOMB", "https://storage.googleapis.com/rallybot/explainbombrally.mp3")

AUDIO_5S_ROLL = os.getenv("AUDIO_5S_ROLL", "https://storage.googleapis.com/rallybot/5secondgaps.mp3")
AUDIO_10S_ROLL = os.getenv("AUDIO_10S_ROLL", "https://storage.googleapis.com/rallybot/10secondgaps.mp3")
AUDIO_15S_ROLL = os.getenv("AUDIO_15S_ROLL", "https://storage.googleapis.com/rallybot/15secondgaps.mp3")
AUDIO_30S_ROLL = os.getenv("AUDIO_30S_ROLL", "https://storage.googleapis.com/rallybot/30secondgaps.mp3")
AUDIO_EXPLAIN_ROLL = os.getenv("AUDIO_EXPLAIN_ROLL", "https://storage.googleapis.com/rallybot/explainrollingrallies.mp3")

def _parse_guild_ids() -> List[int]:
    return [int(x) for x in GUILD_IDS.split(",") if x.strip().isdigit()]

# ============================== LOGGING & BOT ==============================
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("rally-bot")
logging.getLogger("discord.voice_client").setLevel(logging.WARNING)
logging.getLogger("discord.gateway").setLevel(logging.INFO)
logging.getLogger("discord.http").setLevel(logging.WARNING)

intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.voice_states = True

bot = commands.Bot(
    command_prefix="!",
    intents=intents,
    allowed_mentions=discord.AllowedMentions(everyone=False, users=True, roles=True),
)
tree = bot.tree

# ============================== DATA MODELS ==============================
TroopType = Literal["Cavalry", "Infantry", "Range"]
TroopTier = Literal["T8", "T9", "T10", "T11", "T12"]

@dataclass
class Participant:
    user_id: int
    troop_type: TroopType
    troop_tier: TroopTier
    rally_dragon: bool
    capacity_value: int

@dataclass
class Rally:
    message_id: int
    guild_id: int
    channel_id: int
    creator_id: int
    rally_kind: Literal["KEEP", "SOP"]

    keep_power: Optional[str] = None
    primary_troop: Optional[TroopType] = None
    keep_level: Optional[str] = None
    gear_worn: Optional[str] = None
    idle_and_scouted: Optional[str] = None

    temp_vc_id: Optional[int] = None
    temp_vc_invite_url: Optional[str] = None

    participants: Dict[int, Participant] = field(default_factory=dict)

    def roster_mentions(self) -> str:
        if not self.participants:
            return "—"
        return ", ".join(f"<@{uid}>" for uid in self.participants.keys())

RALLIES: Dict[int, Rally] = {}
VC_TO_POST: Dict[int, int] = {}

# ============================== VOICE STATE ==============================
@dataclass
class GuildVoiceState:
    last_activity: float = field(default_factory=time.time)
    disconnect_task: Optional[asyncio.Task] = None
    stay_mode: bool = False

VOICE_STATE: Dict[int, GuildVoiceState] = {}

def _reset_activity(guild_id: int):
    if guild_id in VOICE_STATE:
        VOICE_STATE[guild_id].last_activity = time.time()

async def _ensure_voice_ready(member: discord.Member) -> Tuple[Optional[discord.VoiceClient], Optional[str]]:
    """Connect to voice or return existing connection"""
    guild = member.guild
    target_channel = member.voice.channel if member.voice else None
    
    if not target_channel or not isinstance(target_channel, discord.VoiceChannel):
        return None, "You must be in a voice channel"
    
    # Check if already connected
    if guild.voice_client and guild.voice_client.is_connected():
        # Already in the right channel
        if guild.voice_client.channel and guild.voice_client.channel.id == target_channel.id:
            return guild.voice_client, None
        
        # Move to new channel
        try:
            await guild.voice_client.move_to(target_channel)
            log.info(f"Moved to {target_channel.name}")
            return guild.voice_client, None
        except Exception as e:
            log.error(f"Failed to move to channel: {e}")
            # Try disconnect and reconnect
            try:
                await guild.voice_client.disconnect(force=True)
            except:
                pass
    
    # Connect to channel
    try:
        voice_client = await target_channel.connect(timeout=10.0, reconnect=True)
        log.info(f"Connected to {target_channel.name}")
        return voice_client, None
    except asyncio.TimeoutError:
        return None, "Connection timeout - try again"
    except Exception as e:
        log.error(f"Voice connection failed: {e}")
        return None, f"Connection failed: {str(e)[:100]}"

async def _play_audio_url(voice: discord.VoiceClient, url: str, volume: float = 1.0):
    """Play audio from URL with better error handling"""
    if voice.is_playing():
        voice.stop()
    
    try:
        ffmpeg_opts = {
            'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
            'options': '-vn -af "loudnorm=I=-16:TP=-1.5:LRA=11"'
        }
        
        source = discord.FFmpegPCMAudio(url, **ffmpeg_opts)
        voice.play(source)
        
        # Wait for playback to finish
        while voice.is_playing():
            await asyncio.sleep(0.5)
            
    except Exception as e:
        log.error(f"Audio playback error: {e}")
        raise

async def schedule_disconnect(guild_id: int, delay_secs: int):
    """Schedule bot disconnect after delay"""
    await asyncio.sleep(delay_secs)
    
    guild = bot.get_guild(guild_id)
    if not guild or not guild.voice_client:
        return
    
    state = VOICE_STATE.get(guild_id)
    if state and state.stay_mode:
        log.info("Stay mode active, not disconnecting")
        return
    
    try:
        log.info("Auto-disconnecting from voice")
        await guild.voice_client.disconnect()
        if guild_id in VOICE_STATE:
            del VOICE_STATE[guild_id]
    except Exception as e:
        log.error(f"Error during auto-disconnect: {e}")

# ============================== UTILITIES ==============================
def role_mention(guild: discord.Guild, role_name: str) -> str:
    r = discord.utils.find(lambda rr: rr.name.lower() == role_name.lower(), guild.roles)
    return r.mention if r else f"@{role_name}"

def rally_cta_text(guild: discord.Guild) -> Tuple[str, discord.AllowedMentions]:
    text = (
        f"{role_mention(guild, HITTERS_ROLE_NAME)} A rally is being formed!\n"
        "Click **Join Rally**, fill the form, and you're in.\n"
        "Then use `/type_of_rally rolling` or `/type_of_rally bomb` to run the countdown in VC."
    )
    mentions = discord.AllowedMentions(everyone=False, users=False, roles=True)
    return text, mentions

def ensure_int(value: str, default: int = 0) -> int:
    try:
        return int("".join(ch for ch in value if ch.isdigit()))
    except Exception:
        return default

async def pick_or_create_category(
    guild: discord.Guild,
    context_channel: Optional[discord.abc.GuildChannel],
    owner: Optional[discord.Member],
) -> discord.CategoryChannel:
    if TEMP_VC_CATEGORY_ID:
        ch = guild.get_channel(TEMP_VC_CATEGORY_ID)
        if isinstance(ch, discord.CategoryChannel):
            return ch
    if isinstance(context_channel, (discord.TextChannel, discord.VoiceChannel)) and context_channel.category:
        return context_channel.category
    if owner and owner.voice and isinstance(owner.voice.channel, discord.VoiceChannel) and owner.voice.channel.category:
        return owner.voice.channel.category
    return await guild.create_category("Rallies", reason="Rally temp VC category")

async def ensure_temp_vc(
    guild: discord.Guild,
    owner: discord.Member,
    context_channel: Optional[discord.abc.GuildChannel],
    name_hint: str,
    size_hint: int
) -> discord.VoiceChannel:
    cat = await pick_or_create_category(guild, context_channel, owner)

    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=True, connect=True),
        guild.me: discord.PermissionOverwrite(
            view_channel=True, connect=True, speak=True, move_members=True, manage_channels=True
        ),
        owner: discord.PermissionOverwrite(view_channel=True, connect=True, speak=True, manage_channels=True),
    }

    owner_region = None
    if owner.voice and isinstance(owner.voice.channel, discord.VoiceChannel):
        owner_region = owner.voice.channel.rtc_region

    vc = await guild.create_voice_channel(
        name=f"{owner.display_name}'s Rally",
        category=cat,
        user_limit=0,
        overwrites=overwrites,
        reason=f"Rally temp VC ({name_hint})",
        rtc_region=(owner_region or RTC_REGION_FOR_TEMP_VC or None),
    )

    if FORCE_RTC_REGION:
        try:
            await vc.edit(rtc_region=FORCE_RTC_REGION)
            log.info("Force-pinned rtc_region='%s' on temp VC %s", FORCE_RTC_REGION, vc.name)
        except Exception as e:
            log.warning("Could not set rtc_region on temp VC %s: %s", vc.name, e)

    return vc

async def create_or_refresh_vc_invite(vc: discord.VoiceChannel) -> str:
    invite = await vc.create_invite(max_age=0, max_uses=0, unique=True, reason="Rally VC button")
    return invite.url

def embed_for_rally(guild: discord.Guild, r: Rally) -> discord.Embed:
    if r.rally_kind == "KEEP":
        title_text = f"🏰 Keep Rally (L{r.keep_level or '??'})"
        emb = discord.Embed(title=title_text, color=discord.Color.red())
        emb.add_field(name="Power", value=r.keep_power or "??", inline=True)
        emb.add_field(name="Primary Troop", value=r.primary_troop or "??", inline=True)
        emb.add_field(name="Gear Worn", value=r.gear_worn or "??", inline=True)
        emb.add_field(name="Idle & Scouted?", value=r.idle_and_scouted or "??", inline=True)
    else:
        title_text = "👑 SOP Rally"
        emb = discord.Embed(title=title_text, color=discord.Color.gold())

    owner = guild.get_member(r.creator_id)
    if owner:
        emb.set_author(name=f"Rally Lead: {owner.display_name}", icon_url=owner.display_avatar.url)

    if r.participants:
        roster = r.roster_mentions()
        if len(roster) > 1024:
            roster = roster[:1020] + "..."
        emb.add_field(name=f"Roster ({len(r.participants)})", value=roster, inline=False)
    else:
        emb.add_field(name="Roster (0)", value="—", inline=False)

    return emb

async def update_post(guild: discord.Guild, r: Rally):
    try:
        ch = guild.get_channel(r.channel_id)
        if isinstance(ch, discord.TextChannel):
            msg = await ch.fetch_message(r.message_id)
            await msg.edit(embed=embed_for_rally(guild, r), view=build_rally_view(r))
    except Exception as e:
        log.warning("Failed to update rally post %s: %s", r.message_id, e)

def build_rally_view(r: Rally) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(JoinButton(r.message_id))
    view.add_item(LeaveButton(r.message_id))
    if r.temp_vc_invite_url:
        view.add_item(discord.ui.Button(label="Join VC", url=r.temp_vc_invite_url, style=discord.ButtonStyle.link))
    return view

# ... [Rest of the code continues with UI components, commands, etc.]
# I'll include the essential parts and note where to add the rest

class JoinButton(discord.ui.Button):
    def __init__(self, rally_id: int):
        super().__init__(label="Join Rally", style=discord.ButtonStyle.green, custom_id=f"join_{rally_id}")
        self.rally_id = rally_id

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(JoinRallyModal(self.rally_id))

class LeaveButton(discord.ui.Button):
    def __init__(self, rally_id: int):
        super().__init__(label="Leave Rally", style=discord.ButtonStyle.red, custom_id=f"leave_{rally_id}")
        self.rally_id = rally_id

    async def callback(self, interaction: discord.Interaction):
        r = RALLIES.get(self.rally_id)
        if not r:
            return await interaction.response.send_message("Rally not found.", ephemeral=True)
        
        user_id = interaction.user.id
        if user_id not in r.participants:
            return await interaction.response.send_message("You're not in this rally.", ephemeral=True)
        
        del r.participants[user_id]
        await update_post(interaction.guild, r)
        await interaction.response.send_message("You've left the rally.", ephemeral=True)

class JoinRallyModal(discord.ui.Modal, title="Join Rally"):
    def __init__(self, rally_id: int):
        super().__init__()
        self.rally_id = rally_id
        
        self.troop_type = discord.ui.TextInput(
            label="Troop Type",
            placeholder="Cavalry, Infantry, or Range",
            required=True,
            max_length=20
        )
        self.troop_tier = discord.ui.TextInput(
            label="Troop Tier",
            placeholder="T8, T9, T10, T11, or T12",
            required=True,
            max_length=5
        )
        self.rally_dragon = discord.ui.TextInput(
            label="Rally Dragon?",
            placeholder="Yes or No",
            required=True,
            max_length=5
        )
        self.capacity_value = discord.ui.TextInput(
            label="Capacity",
            placeholder="Enter your capacity number",
            required=False,
            max_length=20
        )
        
        self.add_item(self.troop_type)
        self.add_item(self.troop_tier)
        self.add_item(self.rally_dragon)
        self.add_item(self.capacity_value)

    async def on_submit(self, interaction: discord.Interaction):
        r = RALLIES.get(self.rally_id)
        if not r:
            return await interaction.response.send_message("Rally not found.", ephemeral=True)
        
        troop = self.troop_type.value.strip().title()
        tier = self.troop_tier.value.strip().upper()
        dragon = self.rally_dragon.value.strip().lower() in ("yes", "y", "true", "1")
        cap = ensure_int(self.capacity_value.value, 0)
        
        r.participants[interaction.user.id] = Participant(
            interaction.user.id, troop, tier, dragon, cap
        )
        
        await update_post(interaction.guild, r)
        await interaction.response.send_message("You've joined the rally!", ephemeral=True)

class KeepForm(discord.ui.Modal, title="Create Keep Rally"):
    keep_power = discord.ui.TextInput(label="Keep Power", placeholder="e.g., 250M", max_length=20)
    primary_troop = discord.ui.TextInput(label="Primary Troop", placeholder="Cavalry, Infantry, Range", max_length=20)
    keep_level = discord.ui.TextInput(label="Keep Level", placeholder="e.g., K35", max_length=10)
    gear_worn = discord.ui.TextInput(label="Gear Worn", placeholder="e.g., Full Mixed Set", max_length=100)
    idle_and_scouted = discord.ui.TextInput(label="Idle & Scouted?", placeholder="Yes or No", max_length=10)

    async def on_submit(self, interaction: discord.Interaction):
        guild = interaction.guild
        channel = interaction.channel
        author: discord.Member = interaction.user

        if not isinstance(channel, discord.TextChannel):
            return await interaction.response.send_message("Use this in a server text channel.", ephemeral=True)

        try:
            vc = await ensure_temp_vc(guild, author, channel, "Keep Rally", 0)
        except Exception as e:
            return await interaction.response.send_message(f"Couldn't create temp VC: {e}", ephemeral=True)

        invite_url = await create_or_refresh_vc_invite(vc)

        dummy = await channel.send(embed=discord.Embed(title="Creating rally...", color=discord.Color.blurple()))
        r = Rally(
            message_id=dummy.id,
            guild_id=guild.id,
            channel_id=channel.id,
            creator_id=author.id,
            rally_kind="KEEP",
            keep_power=self.keep_power.value.strip(),
            primary_troop=self.primary_troop.value.strip().title(),
            keep_level=self.keep_level.value.strip().upper(),
            gear_worn=self.gear_worn.value.strip(),
            idle_and_scouted=self.idle_and_scouted.value.strip(),
            temp_vc_id=vc.id,
            temp_vc_invite_url=invite_url,
        )
        r.participants[author.id] = Participant(author.id, "Cavalry", "T10", False, 0)
        RALLIES[dummy.id] = r
        VC_TO_POST[vc.id] = dummy.id

        await dummy.edit(embed=embed_for_rally(guild, r), view=build_rally_view(r))
        text, mentions = rally_cta_text(guild)
        await channel.send(text, allowed_mentions=mentions)

        await interaction.response.send_message(f"Keep Rally posted in {channel.mention}.", ephemeral=True)
        asyncio.create_task(schedule_delete_if_empty(guild.id, vc.id))

# ============================== SLASH COMMANDS ==============================
rally_group = app_commands.Group(name="rally", description="Create and manage rallies")
tree.add_command(rally_group)

@rally_group.command(name="sop", description="Create a Seat of Power Rally")
async def rally_sop(interaction: discord.Interaction):
    guild = interaction.guild
    channel = interaction.channel
    author: discord.Member = interaction.user

    if not isinstance(channel, discord.TextChannel):
        return await interaction.response.send_message("Use this in a server text channel.", ephemeral=True)

    try:
        vc = await ensure_temp_vc(guild, author, channel, "SOP Rally", 0)
    except Exception as e:
        return await interaction.response.send_message(f"Couldn't create temp VC: {e}", ephemeral=True)

    invite_url = await create_or_refresh_vc_invite(vc)

    dummy = await channel.send(embed=discord.Embed(title="Creating rally...", color=discord.Color.blurple()))
    r = Rally(
        message_id=dummy.id,
        guild_id=guild.id,
        channel_id=channel.id,
        creator_id=author.id,
        rally_kind="SOP",
        temp_vc_id=vc.id,
        temp_vc_invite_url=invite_url,
    )
    r.participants[author.id] = Participant(author.id, "Cavalry", "T10", False, 0)
    RALLIES[dummy.id] = r
    VC_TO_POST[vc.id] = dummy.id

    await dummy.edit(embed=embed_for_rally(guild, r), view=build_rally_view(r))
    text, mentions = rally_cta_text(guild)
    await channel.send(text, allowed_mentions=mentions)

    await interaction.response.send_message(f"SOP Rally posted in {channel.mention}.", ephemeral=True)
    asyncio.create_task(schedule_delete_if_empty(guild.id, vc.id))

@rally_group.command(name="keep", description="Create a Keep Rally")
async def rally_keep(interaction: discord.Interaction):
    await interaction.response.send_modal(KeepForm())

# Voice commands
rally_type_group = app_commands.Group(name="type_of_rally", description="Start rally countdown")
tree.add_command(rally_type_group)

@rally_type_group.command(name="bomb", description="Start bomb rally countdown")
@app_commands.describe(duration="Rally duration")
@app_commands.choices(duration=[
    app_commands.Choice(name="5 minutes", value="5m"),
    app_commands.Choice(name="10 minutes", value="10m"),
    app_commands.Choice(name="30 minutes", value="30m"),
    app_commands.Choice(name="1 hour", value="1h"),
])
async def bomb_rally(interaction: discord.Interaction, duration: str):
    if not ENABLE_VOICE:
        return await interaction.response.send_message("Voice features are disabled.", ephemeral=True)
    
    member: discord.Member = interaction.user
    await interaction.response.defer(ephemeral=True)
    
    if not member.voice or not isinstance(member.voice.channel, discord.VoiceChannel):
        return await interaction.followup.send("Join a voice channel first!", ephemeral=True)
    
    voice, err = await _ensure_voice_ready(member)
    if not voice:
        return await interaction.followup.send(f"Failed to connect: {err}", ephemeral=True)
    
    url_map = {"5m": AUDIO_5M_BOMB, "10m": AUDIO_10M_BOMB, "30m": AUDIO_30M_BOMB, "1h": AUDIO_1H_BOMB}
    url = url_map.get(duration)
    
    if not url:
        return await interaction.followup.send(f"No audio file configured for {duration}", ephemeral=True)
    
    try:
        await _play_audio_url(voice, url)
        await interaction.followup.send(f"Played {duration} bomb rally!", ephemeral=True)
        
        state = VOICE_STATE.setdefault(member.guild.id, GuildVoiceState())
        if state.disconnect_task and not state.disconnect_task.done():
            state.disconnect_task.cancel()
        state.disconnect_task = asyncio.create_task(schedule_disconnect(member.guild.id, DISCONNECT_AFTER_PLAY_SECS))
        
    except Exception as e:
        await interaction.followup.send(f"Playback failed: {e}", ephemeral=True)

@rally_type_group.command(name="rolling", description="Start rolling rally countdown")
@app_commands.describe(gap="Gap between waves")
@app_commands.choices(gap=[
    app_commands.Choice(name="5 seconds", value="5s"),
    app_commands.Choice(name="10 seconds", value="10s"),
    app_commands.Choice(name="15 seconds", value="15s"),
    app_commands.Choice(name="30 seconds", value="30s"),
])
async def rolling_rally(interaction: discord.Interaction, gap: str):
    if not ENABLE_VOICE:
        return await interaction.response.send_message("Voice features are disabled.", ephemeral=True)
    
    member: discord.Member = interaction.user
    await interaction.response.defer(ephemeral=True)
    
    if not member.voice or not isinstance(member.voice.channel, discord.VoiceChannel):
        return await interaction.followup.send("Join a voice channel first!", ephemeral=True)
    
    voice, err = await _ensure_voice_ready(member)
    if not voice:
        return await interaction.followup.send(f"Failed to connect: {err}", ephemeral=True)
    
    url_map = {"5s": AUDIO_5S_ROLL, "10s": AUDIO_10S_ROLL, "15s": AUDIO_15S_ROLL, "30s": AUDIO_30S_ROLL}
    url = url_map.get(gap)
    
    if not url:
        return await interaction.followup.send(f"No audio file configured for {gap} gap", ephemeral=True)
    
    try:
        await _play_audio_url(voice, url)
        await interaction.followup.send(f"Played rolling rally with {gap} gaps!", ephemeral=True)
        
        state = VOICE_STATE.setdefault(member.guild.id, GuildVoiceState())
        if state.disconnect_task and not state.disconnect_task.done():
            state.disconnect_task.cancel()
        state.disconnect_task = asyncio.create_task(schedule_disconnect(member.guild.id, DISCONNECT_AFTER_PLAY_SECS))
        
    except Exception as e:
        await interaction.followup.send(f"Playback failed: {e}", ephemeral=True)

@tree.command(name="stay", description="Keep bot in voice")
async def stay_command(interaction: discord.Interaction):
    member: discord.Member = interaction.user
    await interaction.response.defer(ephemeral=True)

    if not member.voice or not isinstance(member.voice.channel, discord.VoiceChannel):
        return await interaction.followup.send("Join a voice channel first.", ephemeral=True)
    
    voice, err = await _ensure_voice_ready(member)
    if not voice:
        return await interaction.followup.send(f"Failed to connect: {err}", ephemeral=True)
    
    state = VOICE_STATE.setdefault(member.guild.id, GuildVoiceState())
    state.stay_mode = True
    if state.disconnect_task and not state.disconnect_task.done():
        state.disconnect_task.cancel()
    
    await interaction.followup.send("I'll stay in voice. Use `/leave` to disconnect.", ephemeral=True)

@tree.command(name="leave", description="Disconnect bot from voice")
async def leave_command(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    if interaction.guild and interaction.guild.voice_client:
        try:
            await interaction.guild.voice_client.disconnect()
            if interaction.guild.id in VOICE_STATE:
                del VOICE_STATE[interaction.guild.id]
            await interaction.followup.send("Disconnected.", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"Failed: {e}", ephemeral=True)
    else:
        await interaction.followup.send("Not connected to voice.", ephemeral=True)

# ============================== VC CLEANUP ==============================
async def schedule_delete_if_empty(guild_id: int, vc_id: int):
    await asyncio.sleep(DELETE_VC_IF_EMPTY_AFTER_SECS)
    guild = bot.get_guild(guild_id)
    if not guild:
        return
    vc = guild.get_channel(vc_id)
    if isinstance(vc, discord.VoiceChannel) and len(vc.members) == 0:
        log.info("Deleting empty VC %s", vc.name)
        await delete_rally_for_vc(guild, vc, "Empty after grace period")

async def delete_rally_for_vc(guild: discord.Guild, vc: discord.VoiceChannel, reason: str):
    mid = VC_TO_POST.pop(vc.id, None)
    try:
        if guild.voice_client and guild.voice_client.channel and guild.voice_client.channel.id == vc.id:
            await guild.voice_client.disconnect()
            if guild.id in VOICE_STATE:
                del VOICE_STATE[guild.id]
        
        await vc.delete(reason=reason)
    except Exception as e:
        log.warning("Failed to delete VC %s: %s", vc.name, e)
    
    if mid and mid in RALLIES:
        r = RALLIES[mid]
        r.temp_vc_id = None
        r.temp_vc_invite_url = None
        await update_post(guild, r)

@bot.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    if member.id == bot.user.id and member.guild:
        if after.channel is None and before.channel is not None:
            if member.guild.id in VOICE_STATE:
                del VOICE_STATE[member.guild.id]
        return

    if member.guild:
        _reset_activity(member.guild.id)

    for ch in (before.channel, after.channel):
        if not isinstance(ch, discord.VoiceChannel) or ch.id not in VC_TO_POST:
            continue
        
        if len(ch.members) == 0:
            asyncio.create_task(schedule_delete_if_empty(ch.guild.id, ch.id))

# ============================== LIFECYCLE ==============================
@bot.event
async def on_ready():
    try:
        guild_ids = _parse_guild_ids()
        if guild_ids:
            for gid in guild_ids:
                await tree.sync(guild=discord.Object(id=gid))
            log.info("Synced commands to %d guilds", len(guild_ids))
        else:
            await tree.sync()
            log.info("Synced commands globally")
    except Exception as e:
        log.exception("Failed to sync commands: %s", e)

    log.info("Bot ready: %s | Voice: %s", bot.user, ENABLE_VOICE)

# ============================== ENTRYPOINT ==============================
if __name__ == "__main__":
    # Start health check server in background thread
    health_thread = Thread(target=run_health_server, args=(HEALTH_PORT,), daemon=True)
    health_thread.start()
    log.info("Starting bot...")
    
    bot.run(TOKEN)
