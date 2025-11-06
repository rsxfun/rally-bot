# main.py — Rally Bot (Fixed voice connection issues)
# Key fixes:
# - Simplified voice state management
# - Fixed race conditions in connect/disconnect
# - Better error handling for audio playback
# - Removed aggressive cooldown system

import os
import time
import asyncio
import logging
from dataclasses import dataclass, field
from typing import Dict, Optional, List, Literal, Tuple

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

# Prefer IPv4 on some VPS hosts
discord.VoiceClient.use_ipv6 = False

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
logging.getLogger("discord.voice_client").setLevel(logging.DEBUG)
logging.getLogger("discord.gateway").setLevel(logging.INFO)

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
    title = "🏰 Keep Rally" if r.rally_kind == "KEEP" else "🛡️ Seat of Power Rally"
    e = discord.Embed(title=title, color=discord.Color.blurple())

    creator = guild.get_member(r.creator_id)
    e.add_field(name="Host", value=(creator.mention if creator else f"<@{r.creator_id}>"), inline=True)

    if r.rally_kind == "KEEP":
        e.add_field(name="Power Level of Keep", value=r.keep_power or "—", inline=True)
        e.add_field(name="Primary Troop Type", value=r.primary_troop or "—", inline=True)
        e.add_field(name="Keep Level", value=r.keep_level or "—", inline=True)
        e.add_field(name="Gear Worn", value=r.gear_worn or "—", inline=True)
        e.add_field(name="Idle / Scouted", value=r.idle_and_scouted or "—", inline=True)

    if r.temp_vc_id:
        ch = guild.get_channel(r.temp_vc_id)
        if isinstance(ch, discord.VoiceChannel):
            e.add_field(name="Voice Channel", value=ch.mention, inline=False)

    e.add_field(name="Roster", value=r.roster_mentions(), inline=False)
    return e

async def dm_join_info(member: discord.Member, r: Rally):
    if not r.temp_vc_id:
        return
    vc = member.guild.get_channel(r.temp_vc_id)
    if not isinstance(vc, discord.VoiceChannel):
        return
    invite = await vc.create_invite(max_age=3600, max_uses=1, unique=True, reason="Rally user join")
    text = f"You joined the **{r.rally_kind} Rally**.\nVoice: {invite.url}"
    try:
        dm = await member.create_dm()
        await dm.send(text)
    except discord.Forbidden:
        pass

async def update_post(guild: discord.Guild, r: Rally):
    ch = guild.get_channel(r.channel_id)
    if not isinstance(ch, discord.TextChannel):
        return
    try:
        msg = await ch.fetch_message(r.message_id)
    except Exception:
        return
    await msg.edit(embed=embed_for_rally(guild, r), view=build_rally_view(r))

# ============================== VOICE HELPERS (SIMPLIFIED) ==============================
def _try_load_opus() -> bool:
    if discord.opus.is_loaded():
        log.debug("Opus already loaded.")
        return True
    for name in ("opus", "libopus.so.0", "libopus-0.dll", "libopus"):
        try:
            discord.opus.load_opus(name)
            log.info("Opus loaded successfully using '%s'.", name)
            return True
        except Exception as e:
            log.debug("Failed to load Opus using '%s': %s", name, e)
            continue
    log.error("Failed to load Opus. Voice playback will not work. Ensure libopus is installed and accessible.")
    return False

# Simplified voice state tracking
class GuildVoiceState:
    def __init__(self):
        self.last_activity: float = time.time()
        self.disconnect_task: Optional[asyncio.Task] = None
        self.stay_mode: bool = False  # If True, don't auto-disconnect

VOICE_STATE: Dict[int, GuildVoiceState] = {}

def _reset_activity(guild_id: int):
    """Reset activity timer for a guild."""
    state = VOICE_STATE.setdefault(guild_id, GuildVoiceState())
    state.last_activity = time.time()
    
    # Cancel existing disconnect task
    if state.disconnect_task and not state.disconnect_task.done():
        state.disconnect_task.cancel()
        state.disconnect_task = None
    
    # Don't create new task if in stay mode
    if not state.stay_mode:
        state.disconnect_task = asyncio.create_task(_auto_disconnect_task(guild_id))

async def _auto_disconnect_task(guild_id: int):
    """Task that handles auto-disconnect after inactivity."""
    try:
        await asyncio.sleep(VOICE_IDLE_TIMEOUT_SECS)
        
        guild = bot.get_guild(guild_id)
        if not guild or not guild.voice_client:
            return
        
        vc_client = guild.voice_client
        if not vc_client.is_connected():
            return
        
        # Check if there are non-bot users in the VC
        if isinstance(vc_client.channel, discord.VoiceChannel):
            non_bot_members = [m for m in vc_client.channel.members if not m.bot]
            if len(non_bot_members) > 0:
                log.info("Users still present in VC for guild %s, extending timeout", guild_id)
                _reset_activity(guild_id)  # Reset and continue
                return
        
        # No users, disconnect
        log.info("Auto-disconnect triggered for guild %s due to inactivity", guild_id)
        await vc_client.disconnect(force=False)
        
        # Clean up state
        if guild_id in VOICE_STATE:
            del VOICE_STATE[guild_id]
            
    except asyncio.CancelledError:
        log.debug("Auto-disconnect task cancelled for guild %s", guild_id)
    except Exception as e:
        log.error("Error in auto-disconnect task for guild %s: %s", guild_id, e)

async def _ensure_voice_ready(member: discord.Member) -> Tuple[Optional[discord.VoiceClient], Optional[str]]:
    """Connect to voice and return the client, or return an error message."""
    
    if not ENABLE_VOICE:
        return None, "Voice playback is disabled (ENABLE_VOICE=false)."
    
    if not member.voice or not isinstance(member.voice.channel, discord.VoiceChannel):
        return None, "Join a voice channel first."
    
    # Ensure Opus is loaded
    if not _try_load_opus():
        return None, "Opus library not available. Check bot logs."
    
    vc_target: discord.VoiceChannel = member.voice.channel
    
    # Check permissions
    perms = vc_target.permissions_for(vc_target.guild.me)
    missing = []
    if not perms.connect:
        missing.append("Connect")
    if not perms.speak:
        missing.append("Speak")
    if missing:
        return None, f"I need {', '.join(missing)} permission in **{vc_target.name}**."
    
    try:
        voice = member.guild.voice_client
        
        # If already connected
        if voice and voice.is_connected():
            if voice.channel.id != vc_target.id:
                log.info("Moving bot from %s to %s", voice.channel.name, vc_target.name)
                await voice.move_to(vc_target)
            _reset_activity(member.guild.id)
            return voice, None
        
        # Connect fresh
        log.info("Connecting to VC '%s' in guild '%s'", vc_target.name, member.guild.name)
        voice = await vc_target.connect(timeout=30.0, reconnect=True, self_deaf=True)
        
        # Wait for connection
        for i in range(30):
            if voice.is_connected():
                log.info("Successfully connected to '%s' after %d attempts", vc_target.name, i + 1)
                _reset_activity(member.guild.id)
                return voice, None
            await asyncio.sleep(0.5)
        
        # Timeout
        log.error("Connection timeout for guild %s", member.guild.id)
        return None, "Connection timed out. Try again."
        
    except Exception as e:
        log.exception("Voice connection failed for guild %s: %s", member.guild.id, e)
        return None, f"Connection failed: {str(e)}"

async def play_audio_in_member_vc(member: discord.Member, url: str) -> Tuple[bool, str]:
    """Play audio file in the member's VC."""
    
    if not url:
        return False, "No audio URL configured for this option."
    
    voice, err = await _ensure_voice_ready(member)
    if not voice:
        return False, err or "Could not connect to voice."
    
    try:
        # Stop any current playback
        if voice.is_playing():
            voice.stop()
            await asyncio.sleep(0.5)  # Give it a moment
        
        log.info("Starting playback: %s", url)
        
        # Create audio source with better options
        audio = discord.FFmpegPCMAudio(
            url,
            before_options='-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
            options='-vn'
        )
        
        def after_play(error):
            if error:
                log.error("Playback error: %s", error)
            else:
                log.info("Playback finished successfully")
            
            # Schedule post-play disconnect
            async def disconnect_after_delay():
                await asyncio.sleep(DISCONNECT_AFTER_PLAY_SECS)
                guild = bot.get_guild(member.guild.id)
                if guild and guild.voice_client and guild.voice_client.is_connected():
                    if not guild.voice_client.is_playing():
                        # Check for users
                        if isinstance(guild.voice_client.channel, discord.VoiceChannel):
                            non_bot = [m for m in guild.voice_client.channel.members if not m.bot]
                            if len(non_bot) == 0:
                                log.info("Disconnecting after playback (no users)")
                                await guild.voice_client.disconnect()
                                if guild.id in VOICE_STATE:
                                    del VOICE_STATE[guild.id]
            
            asyncio.create_task(disconnect_after_delay())
        
        voice.play(audio, after=after_play)
        _reset_activity(member.guild.id)
        return True, "Playback started!"
        
    except Exception as e:
        log.exception("Playback error: %s", e)
        if "ffmpeg" in str(e).lower():
            return False, "FFmpeg not found. Ensure it's installed: `sudo apt-get install ffmpeg`"
        return False, f"Playback failed: {str(e)}"

# ============================== VIEWS & MODALS ==============================
class JoinRallyModal(discord.ui.Modal, title="Join Rally"):
    troop_type = discord.ui.TextInput(label="Troop Type (Cavalry / Infantry / Range)", required=True, max_length=16)
    troop_tier = discord.ui.TextInput(label="Troop Tier (T8 / T9 / T10 / T11 / T12)", required=True, max_length=4)
    rally_dragon = discord.ui.TextInput(label="Rally Dragon (Yes/No)", required=True, max_length=8)
    capacity = discord.ui.TextInput(label="Rally Capacity (number)", required=True, max_length=12, placeholder="e.g. 550000")

    def __init__(self, rally_mid: int, sop: bool):
        super().__init__()
        self.rally_mid = rally_mid
        self.sop = sop

    async def on_submit(self, interaction: discord.Interaction):
        r = RALLIES.get(self.rally_mid)
        if not r:
            return await interaction.response.send_message("This rally no longer exists.", ephemeral=True)

        tt = self.troop_type.value.strip().title()
        if tt not in ("Cavalry", "Infantry", "Range"):
            return await interaction.response.send_message("Troop Type must be Cavalry, Infantry, or Range.", ephemeral=True)
        tier = self.troop_tier.value.strip().upper()
        if tier not in ("T8", "T9", "T10", "T11", "T12"):
            return await interaction.response.send_message("Troop Tier must be one of T8/T9/T10/T11/T12.", ephemeral=True)
        dragon = self.rally_dragon.value.strip().lower().startswith("y")
        capacity_num = ensure_int(self.capacity.value, 0)

        r.participants[interaction.user.id] = Participant(
            user_id=interaction.user.id,
            troop_type=tt,
            troop_tier=tier,
            rally_dragon=dragon,
            capacity_value=capacity_num
        )

        await dm_join_info(interaction.user, r)
        await update_post(interaction.guild, r)
        await interaction.response.send_message("You're on the roster. Check DMs for VC info.", ephemeral=True)

def build_rally_view(r: Rally) -> discord.ui.View:
    class RallyView(discord.ui.View):
        def __init__(self, rally_mid: int):
            super().__init__(timeout=3600)
            self.mid = rally_mid

        @discord.ui.button(label="Join Rally", style=discord.ButtonStyle.success)
        async def join_rally(self, interaction: discord.Interaction, _: discord.ui.Button):
            if self.mid not in RALLIES:
                return await interaction.response.send_message("This rally no longer exists.", ephemeral=True)
            sop = (RALLIES[self.mid].rally_kind == "SOP")
            await interaction.response.send_modal(JoinRallyModal(self.mid, sop=sop))

        @discord.ui.button(label="Export Roster", style=discord.ButtonStyle.primary)
        async def export_roster(self, interaction: discord.Interaction, _: discord.ui.Button):
            r = RALLIES.get(self.mid)
            if not r:
                return await interaction.response.send_message("This rally no longer exists.", ephemeral=True)

            parts: List[Participant] = sorted(r.participants.values(), key=lambda p: p.capacity_value, reverse=True)
            if not parts:
                return await interaction.response.send_message("Roster is empty.", ephemeral=True)

            lines: List[str] = []
            for p in parts:
                m = interaction.guild.get_member(p.user_id)
                name = (m.display_name if m else f"<@{p.user_id}>")
                lines.append(f"{name} | {p.troop_type} {p.troop_tier} | Dragon: {'Yes' if p.rally_dragon else 'No'} | Cap: {p.capacity_value}")

            header = "**Rally Roster (by capacity)**"
            body = "\n".join(lines)
            text = f"{header}\n```text\n{body}\n```"

            chunks: List[str] = []
            while len(text) > 1800:
                cut = text.rfind("\n", 0, 1800)
                chunks.append(text[:cut if cut != -1 else 1800])
                text = text[(cut if cut != -1 else 1800):]
            chunks.append(text)

            await interaction.response.send_message(chunks[0], ephemeral=True)
            for c in chunks[1:]:
                await interaction.followup.send(c, ephemeral=True)

    view = RallyView(r.message_id)
    if r.temp_vc_invite_url:
        view.add_item(discord.ui.Button(label="Join VC", style=discord.ButtonStyle.danger, url=r.temp_vc_invite_url))
    return view

# ============================== /type_of_rally GROUP ==============================
type_group = app_commands.Group(name="type_of_rally", description="Bomb rallies, Rolling rallies, explanations")
tree.add_command(type_group)

class ConfirmJoinVCView(discord.ui.View):
    def __init__(self, url_label: str, url_to_play: str):
        super().__init__(timeout=120)
        self.url_to_play = url_to_play
        self.url_label = url_label

    @discord.ui.button(label="Join VC & Start", style=discord.ButtonStyle.success)
    async def yes(self, interaction: discord.Interaction, _: discord.ui.Button):
        member: discord.Member = interaction.user
        if not member.voice or not isinstance(member.voice.channel, discord.VoiceChannel):
            return await interaction.response.send_message("You must be in a voice channel first.", ephemeral=True)

        if not ENABLE_VOICE:
            return await interaction.response.send_message(
                f"Voice playback is disabled. Audio link: {self.url_to_play}",
                ephemeral=True
            )
        
        if not self.url_to_play:
            return await interaction.response.send_message(f"No audio file configured for **{self.url_label}**.", ephemeral=True)

        await interaction.response.defer(ephemeral=True)
        ok, msg = await play_audio_in_member_vc(member, self.url_to_play)
        await interaction.followup.send(msg, ephemeral=True)

    @discord.ui.button(label="No", style=discord.ButtonStyle.danger)
    async def no(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_message("Cancelled.", ephemeral=True)

class ExplainOrStartView(discord.ui.View):
    def __init__(self, label: str, url: str):
        super().__init__(timeout=120)
        self.label = label
        self.url = url

    @discord.ui.button(label="Start Countdown on VC", style=discord.ButtonStyle.danger)
    async def start(self, interaction: discord.Interaction, _: discord.ui.Button):
        if not self.url:
            return await interaction.response.send_message(f"No audio file configured for **{self.label}**.", ephemeral=True)
        await interaction.response.send_message(
            "The bot will join your current VC and start the countdown. Continue?",
            view=ConfirmJoinVCView(self.label, self.url),
            ephemeral=True
        )

    @discord.ui.button(label="Explain in Text", style=discord.ButtonStyle.primary)
    async def explain_text(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_message(
            f"**{self.label}**:\n- Rolling rallies use fixed intervals; bomb rallies sync hit times.\n- When ready, use the buttons again.",
            ephemeral=True
        )

class BombMenuView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=300)

    @discord.ui.button(label="5 Minute Bomb", style=discord.ButtonStyle.danger)
    async def b5(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_message("Choose where to explain/run:", view=ExplainOrStartView("5m Bomb", AUDIO_5M_BOMB), ephemeral=True)

    @discord.ui.button(label="10 Minute Bomb", style=discord.ButtonStyle.danger)
    async def b10(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_message("Choose where to explain/run:", view=ExplainOrStartView("10m Bomb", AUDIO_10M_BOMB), ephemeral=True)

    @discord.ui.button(label="30 Minute Bomb", style=discord.ButtonStyle.danger)
    async def b30(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_message("Choose where to explain/run:", view=ExplainOrStartView("30m Bomb", AUDIO_30M_BOMB), ephemeral=True)

    @discord.ui.button(label="1 Hour Bomb", style=discord.ButtonStyle.danger)
    async def b60(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_message("Choose where to explain/run:", view=ExplainOrStartView("1h Bomb", AUDIO_1H_BOMB), ephemeral=True)

    @discord.ui.button(label="Explain Bomb Rally", style=discord.ButtonStyle.success)
    async def bexplain(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_message("The bot will join your VC and explain. Continue?", view=ConfirmJoinVCView("Explain Bomb Rally", AUDIO_EXPLAIN_BOMB), ephemeral=True)

@type_group.command(name="bomb", description="Bomb Rally options")
async def type_bomb(interaction: discord.Interaction):
    @type_group.command(name="bomb", description="Bomb Rally options")
async def type_bomb(interaction: discord.Interaction):
    e = discord.Embed(title="💣 Bomb Rally", description="Pick an option below.", color=discord.Color.blurple())
    await interaction.response.send_message(embed=e, view=BombMenuView(), ephemeral=True)

class RollingMenuView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=300)

    @discord.ui.button(label="5 Second Intervals", style=discord.ButtonStyle.danger)
    async def s5(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_message("Choose:", view=ExplainOrStartView("5s Intervals", AUDIO_5S_ROLL), ephemeral=True)

    @discord.ui.button(label="10 Second Intervals", style=discord.ButtonStyle.danger)
    async def s10(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_message("Choose:", view=ExplainOrStartView("10s Intervals", AUDIO_10S_ROLL), ephemeral=True)

    @discord.ui.button(label="15 Second Intervals", style=discord.ButtonStyle.danger)
    async def s15(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_message("Choose:", view=ExplainOrStartView("15s Intervals", AUDIO_15S_ROLL), ephemeral=True)

    @discord.ui.button(label="30 Second Intervals", style=discord.ButtonStyle.danger)
    async def s30(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_message("Choose:", view=ExplainOrStartView("30s Intervals", AUDIO_30S_ROLL), ephemeral=True)

    @discord.ui.button(label="Explain Rolling Rally", style=discord.ButtonStyle.success)
    async def rexplain(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_message("The bot will join your VC and explain. Continue?", view=ConfirmJoinVCView("Explain Rolling Rally", AUDIO_EXPLAIN_ROLL), ephemeral=True)

@type_group.command(name="rolling", description="Rolling Rally options")
async def type_rolling(interaction: discord.Interaction):
    e = discord.Embed(title="🔁 Rolling Rally", description="Pick an option below.", color=discord.Color.blurple())
    await interaction.response.send_message(embed=e, view=RollingMenuView(), ephemeral=True)

# ============================== /rally GROUP ==============================
rally_group = app_commands.Group(name="rally", description="Create a Keep Rally or a Seat of Power Rally")
tree.add_command(rally_group)

class KeepForm(discord.ui.Modal, title="Keep Rally Details"):
    keep_power = discord.ui.TextInput(label="Power Level of Keep", placeholder="e.g., 200m, 350m", required=True, max_length=16)
    primary_troop = discord.ui.TextInput(label="Primary Troop Type", placeholder="Cavalry / Infantry / Range", required=True, max_length=16)
    keep_level = discord.ui.TextInput(label="Keep Level", placeholder="e.g., K30, K34", required=True, max_length=8)
    gear_worn = discord.ui.TextInput(label="What Gear is Worn", placeholder="Farming / Crafting / Attack / Defense", required=True, max_length=32)
    idle_and_scouted = discord.ui.TextInput(label="Idle Time & Scouted?", placeholder="e.g., Idle 10m, Scouted 20m ago / No", required=True, max_length=64)

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

@rally_group.command(name="keep", description="Create a Keep Rally (form)")
async def rally_keep(interaction: discord.Interaction):
    await interaction.response.send_modal(KeepForm())

# ============================== VC CLEANUP ==============================
async def schedule_delete_if_empty(guild_id: int, vc_id: int):
    await asyncio.sleep(DELETE_VC_IF_EMPTY_AFTER_SECS)
    guild = bot.get_guild(guild_id)
    if not guild:
        return
    vc = guild.get_channel(vc_id)
    if isinstance(vc, discord.VoiceChannel):
        if len(vc.members) == 0:
            log.info("Temporary VC %s is empty. Deleting.", vc.name)
            await delete_rally_for_vc(guild, vc, reason="VC empty after grace period.")

async def delete_rally_for_vc(guild: discord.Guild, vc: discord.VoiceChannel, reason: str):
    mid = VC_TO_POST.pop(vc.id, None)
    try:
        if guild.voice_client and guild.voice_client.channel and guild.voice_client.channel.id == vc.id:
            log.info("Bot is in VC %s that is being deleted. Disconnecting.", vc.name)
            await guild.voice_client.disconnect()
            if guild.id in VOICE_STATE:
                del VOICE_STATE[guild.id]
        
        log.info("Deleting temporary VC %s for reason: %s", vc.name, reason)
        await vc.delete(reason=reason)
    except Exception as e:
        log.warning("Failed to delete temporary VC %s: %s", vc.name, e)
    
    if mid and mid in RALLIES:
        r = RALLIES[mid]
        r.temp_vc_id = None
        r.temp_vc_invite_url = None
        await update_post(guild, r)

@bot.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    if member.id == bot.user.id and member.guild:
        if after.channel is None and before.channel is not None:
            log.info("Bot disconnected from VC %s", before.channel.name)
            if member.guild.id in VOICE_STATE:
                del VOICE_STATE[member.guild.id]
        return

    if member.guild:
        _reset_activity(member.guild.id)

    for ch in (before.channel, after.channel):
        if not isinstance(ch, discord.VoiceChannel):
            continue
        if ch.id not in VC_TO_POST:
            continue
        
        if len(ch.members) == 0:
            log.info("Last user left temporary VC %s. Scheduling deletion.", ch.name)
            asyncio.create_task(schedule_delete_if_empty(ch.guild.id, ch.id))

# ============================== UTIL / DEBUG ==============================
@tree.command(name="stay", description="Keep the bot in voice chat")
async def stay_command(interaction: discord.Interaction):
    member: discord.Member = interaction.user
    await interaction.response.defer(ephemeral=True)

    if not member.voice or not isinstance(member.voice.channel, discord.VoiceChannel):
        return await interaction.followup.send("You must be in a voice channel first.", ephemeral=True)
    
    voice, err = await _ensure_voice_ready(member)
    if not voice:
        return await interaction.followup.send(f"Failed to connect: {err}", ephemeral=True)
    
    state = VOICE_STATE.setdefault(member.guild.id, GuildVoiceState())
    state.stay_mode = True
    if state.disconnect_task and not state.disconnect_task.done():
        state.disconnect_task.cancel()
    
    await interaction.followup.send("I'll stay in your voice channel. Use `/leave` to disconnect me.", ephemeral=True)

@tree.command(name="leave", description="Disconnect the bot from voice chat")
async def leave_command(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    if interaction.guild and interaction.guild.voice_client and interaction.guild.voice_client.is_connected():
        try:
            log.info("Bot manually disconnected via /leave command")
            await interaction.guild.voice_client.disconnect()
            if interaction.guild.id in VOICE_STATE:
                del VOICE_STATE[interaction.guild.id]
            await interaction.followup.send("Disconnected from voice chat.", ephemeral=True)
        except Exception as e:
            log.error("Error during manual disconnect: %s", e)
            await interaction.followup.send(f"Failed to disconnect: {e}", ephemeral=True)
    else:
        await interaction.followup.send("I'm not connected to voice chat.", ephemeral=True)

# ============================== LIFECYCLE ==============================
@bot.event
async def on_ready():
    try:
        guild_ids = _parse_guild_ids()
        if guild_ids:
            for gid in guild_ids:
                await tree.sync(guild=discord.Object(id=gid))
            log.info("Slash commands synced to %d guild(s): %s", len(guild_ids), guild_ids)
        else:
            cmds = await tree.sync()
            log.info("Globally synced %d commands.", len(cmds))
    except Exception as e:
        log.exception("Failed to sync commands: %s", e)

    log.info("Logged in as %s (%s) | ENABLE_VOICE=%s", bot.user, bot.user.id, ENABLE_VOICE)

# ============================== ENTRYPOINT ==============================
if __name__ == "__main__":
    bot.run(TOKEN)
