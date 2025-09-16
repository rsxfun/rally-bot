import os
import io
import asyncio
import logging
from dataclasses import dataclass, field
from typing import Dict, Optional, List, Literal, Tuple

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

# ============================== ENV & CONFIG ==============================

load_dotenv()

TOKEN = os.getenv("DISCORD_BOT_TOKEN") or os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise RuntimeError("Set DISCORD_BOT_TOKEN (or DISCORD_TOKEN) in your environment.")

GUILD_IDS = os.getenv("GUILD_IDS", "")  # e.g. "123...,987..."
TEMP_VC_CATEGORY_ID = int(os.getenv("TEMP_VC_CATEGORY_ID", "0"))
HITTERS_ROLE_NAME = os.getenv("HITTERS_ROLE_NAME", "hitters")
DELETE_VC_IF_EMPTY_AFTER_SECS = int(os.getenv("DELETE_VC_IF_EMPTY_AFTER_SECS", "300"))

# Voice feature flag (requires ffmpeg + libopus on host)
ENABLE_VOICE = os.getenv("ENABLE_VOICE", "false").strip().lower() in ("1", "true", "yes")

# Audio URLs
AUDIO_5M_BOMB = os.getenv("AUDIO_5M_BOMB", "https://storage.googleapis.com/rallybot/5minbombcomplete.mp3")
AUDIO_10M_BOMB = os.getenv("AUDIO_10M_BOMB", "https://storage.googleapis.com/rallybot/10minbomb.mp3")
AUDIO_30M_BOMB = os.getenv("AUDIO_30M_BOMB", "")
AUDIO_1H_BOMB  = os.getenv("AUDIO_1H_BOMB", "")
AUDIO_EXPLAIN_BOMB = os.getenv("AUDIO_EXPLAIN_BOMB", "https://storage.googleapis.com/rallybot/explainbombrally.mp3")

AUDIO_5S_ROLL  = os.getenv("AUDIO_5S_ROLL", "https://storage.googleapis.com/rallybot/5secondgaps.mp3")
AUDIO_10S_ROLL = os.getenv("AUDIO_10S_ROLL", "https://storage.googleapis.com/rallybot/10secondgaps.mp3")
AUDIO_15S_ROLL = os.getenv("AUDIO_15S_ROLL", "https://storage.googleapis.com/rallybot/15secondgaps.mp3")
AUDIO_30S_ROLL = os.getenv("AUDIO_30S_ROLL", "https://storage.googleapis.com/rallybot/30secondgaps.mp3")
AUDIO_EXPLAIN_ROLL = os.getenv("AUDIO_EXPLAIN_ROLL", "https://storage.googleapis.com/rallybot/explainrollingrallies.mp3")

def _parse_guild_ids() -> List[int]:
    return [int(x) for x in GUILD_IDS.split(",") if x.strip().isdigit()]

# ============================== LOGGING & BOT ==============================

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("rally-bot")

intents = discord.Intents.default()
intents.guilds = True
intents.members = True       # for display names on roster
intents.voice_states = True  # for VC auto-delete

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

# message_id -> Rally
RALLIES: Dict[int, Rally] = {}
# voice_channel_id -> message_id
VC_TO_POST: Dict[int, int] = {}

# ============================== UTILITIES ==============================

def role_mention(guild: discord.Guild, role_name: str) -> str:
    r = discord.utils.find(lambda rr: rr.name.lower() == role_name.lower(), guild.roles)
    return r.mention if r else f"@{role_name}"

def rally_cta_text(guild: discord.Guild) -> Tuple[str, discord.AllowedMentions]:
    text = (
        f"{role_mention(guild, HITTERS_ROLE_NAME)} A rally is being formed!\n"
        "Sign up by clicking **Join Rally**, complete the form and you're in!\n"
        "Once everyone signs up you can **Export Roster** to then form your rally, once you do, use "
        "`/type_of_rally rolling` or `/type_of_rally bomb` to set up the vc countdown you want!"
    )
    # allow role pings only (no user mass pings)
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
    # 1) explicit env category
    if TEMP_VC_CATEGORY_ID:
        ch = guild.get_channel(TEMP_VC_CATEGORY_ID)
        if isinstance(ch, discord.CategoryChannel):
            return ch

    # 2) the channel where command was run
    if isinstance(context_channel, (discord.TextChannel, discord.VoiceChannel)) and context_channel.category:
        return context_channel.category

    # 3) owner's current VC category
    if owner and owner.voice and isinstance(owner.voice.channel, discord.VoiceChannel) and owner.voice.channel.category:
        return owner.voice.channel.category

    # 4) last resort: create a category
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
        # everyone can see/connect
        guild.default_role: discord.PermissionOverwrite(view_channel=True, connect=True),

        # ⬇️ Give the BOT manage_channels so it can delete the temp VC later
        guild.me: discord.PermissionOverwrite(
            view_channel=True, connect=True, move_members=True, manage_channels=True
        ),

        # host convenience
        owner: discord.PermissionOverwrite(
            view_channel=True, connect=True, manage_channels=True
        ),
    }

    vc = await guild.create_voice_channel(
        name=f"{owner.display_name}'s Rally",
        category=cat,
        # ⬇️ Unlimited joiners (removes the “/10” limit)
        user_limit=0,
        overwrites=overwrites,
        reason=f"Rally temp VC ({name_hint})",
    )
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

# ============================== VOICE HELPERS ==============================

def _try_load_opus() -> bool:
    if discord.opus.is_loaded():
        return True
    for name in ("opus", "libopus.so.0", "libopus"):
        try:
            discord.opus.load_opus(name)
            return True
        except Exception:
            continue
    return False

async def _ensure_voice_ready(member: discord.Member) -> tuple[Optional[discord.VoiceClient], Optional[str]]:
    """
    Join (or move to) the member's current voice channel and return (voice_client, error_message).
    error_message is None on success.
    """
    if not ENABLE_VOICE:
        return None, "Voice playback is disabled on this host (ENABLE_VOICE=false)."
    if not member.voice or not isinstance(member.voice.channel, discord.VoiceChannel):
        return None, "You must be connected to a voice channel first."
    if not _try_load_opus():
        return None, "Opus failed to load. Install system libopus or PyNaCl."

    vc_target: discord.VoiceChannel = member.voice.channel  # type: ignore

    # Permission check up front (most common cause)
    perms = vc_target.permissions_for(vc_target.guild.me)  # type: ignore
    missing = []
    if not perms.connect:
        missing.append("Connect")
    if not perms.speak:
        missing.append("Speak")
    if missing:
        return None, f"I need {' and '.join(missing)} permission in **{vc_target.name}**."

    try:
        voice = member.guild.voice_client
        if voice and voice.is_connected():
            if voice.channel.id != vc_target.id:
                await voice.move_to(vc_target)
        else:
            voice = await vc_target.connect(timeout=30.0, reconnect=True)

        # Wait up to ~10s for the connection to fully become active
        for _ in range(40):
            await asyncio.sleep(0.25)
            if voice and voice.is_connected():
                return voice, None

        return None, "Timed out connecting to voice (handshake incomplete)."

    except Exception as e:
        log.exception("Voice connect/move failed: %s", e)
        return None, f"Could not connect to voice: {e.__class__.__name__}. See bot logs."

async def play_audio_in_member_vc(member: discord.Member, url: str) -> tuple[bool, str]:
    voice, err = await _ensure_voice_ready(member)
    if not voice:
        return False, err or "Could not connect to voice."

    try:
        if voice.is_playing():
            voice.stop()

        # More resilient ffmpeg input (reconnect flags, no video)
        audio = discord.FFmpegOpusAudio(
            url,
            before_options='-nostdin -reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
            options='-vn -loglevel error'
        )
        voice.play(audio, after=lambda e: log.info("Playback finished: %s", e))
        return True, "Playback started."
    except Exception as e:
        log.exception("Play error: %s", e)
        return False, "Playback failed (check permissions/ffmpeg/Opus)."

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
            troop_type=tt,  # type: ignore
            troop_tier=tier,  # type: ignore
            rally_dragon=dragon,
            capacity_value=capacity_num
        )

        await dm_join_info(interaction.user, r)
        await update_post(interaction.guild, r)  # type: ignore
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

            # Build a readable, line-based roster using display names
            lines: List[str] = []
            for p in parts:
                m = interaction.guild.get_member(p.user_id)  # type: ignore
                name = (m.display_name if m else f"<@{p.user_id}>")
                lines.append(f"{name} | {p.troop_type} {p.troop_tier} | Dragon: {'Yes' if p.rally_dragon else 'No'} | Cap: {p.capacity_value}")

            header = "**Rally Roster (by capacity)**"
            body = "\n".join(lines)
            text = f"{header}\n```text\n{body}\n```"

            # Chunk to satisfy 2000-char limit
            messages = []
            while len(text) > 1900:
                cut = text.rfind("\n", 0, 1900)
                if cut == -1:
                    cut = 1900
                messages.append(text[:cut])
                text = text[cut:]
            messages.append(text)

            await interaction.response.send_message(messages[0], ephemeral=True)
            for chunk in messages[1:]:
                await interaction.followup.send(chunk, ephemeral=True)

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
        member: discord.Member = interaction.user  # type: ignore
        if not member.voice or not isinstance(member.voice.channel, discord.VoiceChannel):
            return await interaction.response.send_message("You must be in a voice channel first.", ephemeral=True)

        if not ENABLE_VOICE:
            return await interaction.response.send_message(
                f"Voice playback is disabled on this host. Here’s the audio link for **{self.url_label}**:\n{self.url_to_play}",
                ephemeral=True
            )

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

    @discord.ui.button(label="Start on VC", style=discord.ButtonStyle.danger)
    async def start(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_message(
            "The bot will join your current VC and start the countdown. Continue?",
            view=ConfirmJoinVCView(self.label, self.url),
            ephemeral=True
        )

    @discord.ui.button(label="Explain on VC", style=discord.ButtonStyle.success)
    async def explain_vc(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_message(
            "The bot will join your current VC and start the explanation. Continue?",
            view=ConfirmJoinVCView(self.label, self.url),
            ephemeral=True
        )

    @discord.ui.button(label="Explain in Text", style=discord.ButtonStyle.primary)
    async def explain_text(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_message(
            f"**{self.label}**:\n- Join VC.\n- Follow timing as instructed.\n",
            ephemeral=True
        )

class BombMenuView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=300)

    @discord.ui.button(label="5 Minute Bomb", style=discord.ButtonStyle.danger)
    async def b5(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_message(
            "Choose where to explain/run:",
            view=ExplainOrStartView("5m Bomb", AUDIO_5M_BOMB),
            ephemeral=True
        )

    @discord.ui.button(label="10 Minute Bomb", style=discord.ButtonStyle.danger)
    async def b10(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_message(
            "Choose where to explain/run:",
            view=ExplainOrStartView("10m Bomb", AUDIO_10M_BOMB),
            ephemeral=True
        )

    @discord.ui.button(label="30 Minute Bomb", style=discord.ButtonStyle.danger)
    async def b30(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_message(
            "Choose where to explain/run:",
            view=ExplainOrStartView("30m Bomb", AUDIO_30M_BOMB or "https://example.com/30m.mp3"),
            ephemeral=True
        )

    @discord.ui.button(label="1 Hour Bomb", style=discord.ButtonStyle.danger)
    async def b60(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_message(
            "Choose where to explain/run:",
            view=ExplainOrStartView("1h Bomb", AUDIO_1H_BOMB or "https://example.com/1h.mp3"),
            ephemeral=True
        )

    @discord.ui.button(label="Explain Bomb Rally", style=discord.ButtonStyle.success)
    async def bexplain(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_message(
            "The bot will join your current VC and start the explanation. Continue?",
            view=ConfirmJoinVCView("Explain Bomb Rally", AUDIO_EXPLAIN_BOMB),
            ephemeral=True
        )

@type_group.command(name="bomb", description="Bomb Rally options")
async def type_bomb(interaction: discord.Interaction):
    e = discord.Embed(title="💣 Bomb Rally", description="Pick an option below.", color=discord.Color.blurple())
    await interaction.response.send_message(embed=e, view=BombMenuView(), ephemeral=True)

class RollingMenuView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=300)

    @discord.ui.button(label="5 Second Intervals", style=discord.ButtonStyle.danger)
    async def s5(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_message(
            "The bot will join your current VC and start. Continue?",
            view=ExplainOrStartView("5s Intervals", AUDIO_5S_ROLL),
            ephemeral=True
        )

    @discord.ui.button(label="10 Second Intervals", style=discord.ButtonStyle.danger)
    async def s10(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_message(
            "The bot will join your current VC and start. Continue?",
            view=ExplainOrStartView("10s Intervals", AUDIO_10S_ROLL),
            ephemeral=True
        )

    @discord.ui.button(label="15 Second Intervals", style=discord.ButtonStyle.danger)
    async def s15(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_message(
            "The bot will join your current VC and start. Continue?",
            view=ExplainOrStartView("15s Intervals", AUDIO_15S_ROLL),
            ephemeral=True
        )

    @discord.ui.button(label="30 Second Intervals", style=discord.ButtonStyle.danger)
    async def s30(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_message(
            "The bot will join your current VC and start. Continue?",
            view=ExplainOrStartView("30s Intervals", AUDIO_30S_ROLL),
            ephemeral=True
        )

    @discord.ui.button(label="Explain Rolling Rally", style=discord.ButtonStyle.success)
    async def rexplain(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_message(
            "The bot will join your current VC and start the explanation. Continue?",
            view=ConfirmJoinVCView("Explain Rolling Rally", AUDIO_EXPLAIN_ROLL),
            ephemeral=True
        )

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
        author: discord.Member = interaction.user  # type: ignore

        if not isinstance(channel, discord.TextChannel):
            return await interaction.response.send_message("Use this in a server text channel.", ephemeral=True)

        try:
            vc = await ensure_temp_vc(guild, author, channel, "Keep Rally", 0)
        except Exception as e:
            return await interaction.response.send_message(f"Couldn't create temp VC: {e}", ephemeral=True)

        invite_url = await create_or_refresh_vc_invite(vc)

        # Create "creating..." message then replace with real embed + view
        dummy = await channel.send(embed=discord.Embed(title="Creating rally...", color=discord.Color.blurple()))
        r = Rally(
            message_id=dummy.id,
            guild_id=guild.id,
            channel_id=channel.id,
            creator_id=author.id,
            rally_kind="KEEP",
            keep_power=self.keep_power.value.strip(),
            primary_troop=self.primary_troop.value.strip().title(),  # type: ignore
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

        # Post the role-ping CTA as a normal message (outside the embed)
        text, mentions = rally_cta_text(guild)
        await channel.send(text, allowed_mentions=mentions)

        await interaction.response.send_message(f"Keep Rally posted in {channel.mention}.", ephemeral=True)
        asyncio.create_task(schedule_delete_if_empty(guild.id, vc.id))

@rally_group.command(name="sop", description="Create a Seat of Power Rally")
async def rally_sop(interaction: discord.Interaction):
    guild = interaction.guild
    channel = interaction.channel
    author: discord.Member = interaction.user  # type: ignore

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

    # Optional: also ping hitters for SOP
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
    if isinstance(vc, discord.VoiceChannel) and len(vc.members) == 0:
        await delete_rally_for_vc(guild, vc, reason="VC empty after grace period.")

async def delete_rally_for_vc(guild: discord.Guild, vc: discord.VoiceChannel, reason: str):
    mid = VC_TO_POST.pop(vc.id, None)
    try:
        await vc.delete(reason=reason)
    except Exception:
        pass
    if mid and mid in RALLIES:
        r = RALLIES[mid]
        r.temp_vc_id = None
        r.temp_vc_invite_url = None
        await update_post(guild, r)

@bot.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    # Immediate cleanup: if any rally VC becomes empty, delete it
    for ch in (before.channel, after.channel):
        if not isinstance(ch, discord.VoiceChannel):
            continue
        if ch.id not in VC_TO_POST:
            continue
        if len(ch.members) == 0:
            await delete_rally_for_vc(ch.guild, ch, reason="Last user left VC.")

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
