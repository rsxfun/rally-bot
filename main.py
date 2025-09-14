# main.py — Rally Bot (forgiving category detection, voice-safe on PaaS, robust views/modals, guild sync)

import os
import io
import csv
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
# If this is 0 or invalid, we'll auto-detect a good category.
TEMP_VC_CATEGORY_ID = int(os.getenv("TEMP_VC_CATEGORY_ID", "0"))
HITTERS_ROLE_NAME = os.getenv("HITTERS_ROLE_NAME", "hitters")
DELETE_VC_IF_EMPTY_AFTER_SECS = int(os.getenv("DELETE_VC_IF_EMPTY_AFTER_SECS", "300"))

# === VOICE FEATURE FLAG ===
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

    # Keep fields (modal max=5 -> combine idle/scouted)
    keep_power: Optional[str] = None
    primary_troop: Optional[TroopType] = None
    keep_level: Optional[str] = None
    gear_worn: Optional[str] = None
    idle_and_scouted: Optional[str] = None

    temp_vc_id: Optional[int] = None
    temp_vc_invite_url: Optional[str] = None
    private_thread_id: Optional[int] = None

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

def thread_link(guild_id: int, thread_id: int) -> str:
    return f"https://discord.com/channels/{guild_id}/{thread_id}"

def ensure_int(value: str, default: int = 0) -> int:
    try:
        return int("".join(ch for ch in value if ch.isdigit()))
    except Exception:
        return default

def _pick_category(
    guild: discord.Guild,
    context_channel: Optional[discord.abc.GuildChannel],
    owner: Optional[discord.Member],
) -> Tuple[Optional[discord.CategoryChannel], Optional[str]]:
    """
    Returns (category, error_message). If error_message is not None, the caller should surface it.
    Preference order:
      1) TEMP_VC_CATEGORY_ID if valid + is Category
      2) context_channel.category (where command was used)
      3) owner's current voice channel category
      4) create a new "Rallies" category (if perms allow)
    """
    # 1) Explicit env
    if TEMP_VC_CATEGORY_ID:
        ch = guild.get_channel(TEMP_VC_CATEGORY_ID)
        if isinstance(ch, discord.CategoryChannel):
            return ch, None
        # If an ID was provided but it's wrong, keep going but remember why.
        wrong_hint = f"Configured TEMP_VC_CATEGORY_ID {TEMP_VC_CATEGORY_ID} is not a valid Category in this guild."

    else:
        wrong_hint = None

    # 2) Current text channel's category
    if isinstance(context_channel, (discord.TextChannel, discord.VoiceChannel)):
        if context_channel.category:
            return context_channel.category, None

    # 3) Owner's voice channel category
    if owner and owner.voice and isinstance(owner.voice.channel, discord.VoiceChannel):
        if owner.voice.channel.category:
            return owner.voice.channel.category, None

    # 4) Try to create a category
    try:
        cat = asyncio.get_running_loop().create_task(guild.create_category("Rallies", reason="Rally temp VC category"))
        # Wait for creation to complete
        new_cat = asyncio.get_running_loop().run_until_complete(cat)  # we are already in async; cannot do this
    except RuntimeError:
        # We can't run nested loop; do it the normal awaited way in an async function.
        pass

    # The above "create_task + run_until_complete" can't be used inside async. Provide a small helper for async contexts.
    return None, wrong_hint or "No suitable category found."

async def pick_or_create_category(
    guild: discord.Guild,
    context_channel: Optional[discord.abc.GuildChannel],
    owner: Optional[discord.Member],
) -> discord.CategoryChannel:
    """
    Async wrapper around _pick_category which, if all heuristics fail, tries to create a category.
    """
    # Try heuristics
    if TEMP_VC_CATEGORY_ID:
        ch = guild.get_channel(TEMP_VC_CATEGORY_ID)
        if isinstance(ch, discord.CategoryChannel):
            return ch

    if isinstance(context_channel, (discord.TextChannel, discord.VoiceChannel)) and context_channel.category:
        return context_channel.category

    if owner and owner.voice and isinstance(owner.voice.channel, discord.VoiceChannel) and owner.voice.channel.category:
        return owner.voice.channel.category

    # Create a new one as a last resort
    try:
        return await guild.create_category("Rallies", reason="Rally temp VC category")
    except Exception as e:
        raise RuntimeError(
            "No valid category found and I couldn't create one. "
            "Fix by either:\n"
            f"• Setting a valid TEMP_VC_CATEGORY_ID to a Category in this guild, or\n"
            f"• Running the command in a channel that belongs to a Category, or\n"
            f"• Granting me Manage Channels so I can create a 'Rallies' category.\n\n"
            f"Details: {e}"
        )

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
        guild.me: discord.PermissionOverwrite(view_channel=True, connect=True, move_members=True),
        owner: discord.PermissionOverwrite(view_channel=True, connect=True, manage_channels=True),
    }
    vc = await guild.create_voice_channel(
        name=f"{owner.display_name}'s Rally",
        category=cat,
        user_limit=size_hint if 1 <= size_hint <= 99 else 0,
        overwrites=overwrites,
        reason=f"Rally temp VC ({name_hint})",
    )
    return vc

async def create_thread_for_rally(channel: discord.TextChannel, title: str, host: discord.Member) -> discord.Thread:
    thread = await channel.create_thread(
        name=title,
        type=discord.ChannelType.private_thread,
        auto_archive_duration=1440,
        reason="Rally thread"
    )
    await thread.add_user(host)
    return thread

async def create_or_refresh_vc_invite(vc: discord.VoiceChannel) -> str:
    invite = await vc.create_invite(max_age=0, max_uses=0, unique=True, reason="Rally VC button")
    return invite.url

def embed_for_rally(guild: discord.Guild, r: Rally) -> discord.Embed:
    title = "🏰 Keep Rally" if r.rally_kind == "KEEP" else "🛡️ Seat of Power Rally"
    e = discord.Embed(title=title, color=discord.Color.blurple())  # <- NO description!

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

    if r.private_thread_id:
        th = guild.get_thread(r.private_thread_id)
        if th:
            e.add_field(name="Party Thread", value=th.mention, inline=False)

    e.add_field(name="Roster", value=r.roster_mentions() or "—", inline=False)
    return e

async def dm_join_info(member: discord.Member, r: Rally, sop: bool):
    if not r.temp_vc_id:
        return
    vc = member.guild.get_channel(r.temp_vc_id)
    if not isinstance(vc, discord.VoiceChannel):
        return
    invite = await vc.create_invite(max_age=3600, max_uses=1, unique=True, reason="Rally user join")
    turl = thread_link(r.guild_id, r.private_thread_id) if r.private_thread_id else None
    text = f"You joined the **{'SOP' if sop else 'Keep'} Rally**.\nVoice: {invite.url}"
    if turl:
        text += f"\nThread: {turl}"
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

async def _ensure_voice_ready(member: discord.Member) -> Optional[discord.VoiceClient]:
    if not ENABLE_VOICE:
        return None
    if not member.voice or not isinstance(member.voice.channel, discord.VoiceChannel):
        return None
    if not _try_load_opus():
        log.error("Opus failed to load. Install system libopus or PyNaCl.")
        return None

    vc_target = member.voice.channel
    try:
        voice = member.guild.voice_client
        if voice and voice.is_connected():
            if voice.channel != vc_target:
                await voice.move_to(vc_target)
        else:
            voice = await vc_target.connect(timeout=15.0, reconnect=False)

        for _ in range(6):
            await asyncio.sleep(0.5)
            if voice.is_connected():
                return voice
        return None
    except Exception as e:
        log.exception("Voice connect/move failed: %s", e)
        return None

async def play_audio_in_member_vc(member: discord.Member, url: str) -> Tuple[bool, str]:
    if not ENABLE_VOICE:
        return False, "Voice playback is disabled on this host (ENABLE_VOICE=false)."

    voice = await _ensure_voice_ready(member)
    if not voice or not voice.is_connected():
        return False, (
            "Could not connect to voice.\n"
            "• Hosting providers often block UDP required by Discord voice.\n"
            "• Ensure **UDP is allowed**, **ffmpeg** is installed, and **Opus** is available."
        )

    try:
        if voice.is_playing():
            voice.stop()
        audio = discord.FFmpegPCMAudio(url)
        voice.play(audio, after=lambda e: log.info("Playback finished: %s", e))
        return True, "Playback started."
    except Exception as e:
        log.exception("Play error: %s", e)
        return False, "Playback failed (check ffmpeg/Opus/permissions)."

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

        if r.private_thread_id:
            th = interaction.guild.get_thread(r.private_thread_id)  # type: ignore
            if th and not th.archived:
                try:
                    await th.add_user(interaction.user)
                    await th.send(f"{interaction.user.mention} joined the rally.")
                except Exception:
                    pass

        await dm_join_info(interaction.user, r, sop=self.sop)
        await update_post(interaction.guild, r)  # type: ignore
        await interaction.response.send_message("You're on the roster. Check DMs for VC + thread.", ephemeral=True)

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

            # CSV
            buf = io.StringIO()
            w = csv.writer(buf)
            w.writerow(["User", "Troop Type", "Troop Tier", "Rally Dragon", "Capacity"])
            for p in parts:
                w.writerow([f"@{p.user_id}", p.troop_type, p.troop_tier, "Yes" if p.rally_dragon else "No", p.capacity_value])

            # Sectioned text
            buf2 = io.StringIO()
            def section(title: str):
                buf2.write(f"\n=== {title} ===\n")
            for tt in ("Cavalry", "Infantry", "Range"):
                section(f"Troop Type: {tt}")
                for p in parts:
                    if p.troop_type == tt:
                        buf2.write(f"<@{p.user_id}> | Tier {p.troop_tier} | Dragon: {'Yes' if p.rally_dragon else 'No'} | Cap: {p.capacity_value}\n")
            for tier in ("T12", "T11", "T10", "T9", "T8"):
                section(f"Troop Tier: {tier}")
                for p in parts:
                    if p.troop_tier == tier:
                        buf2.write(f"<@{p.user_id}> | {p.troop_type} | Dragon: {'Yes' if p.rally_dragon else 'No'} | Cap: {p.capacity_value}\n")
            section("Rally Dragon: Yes")
            for p in parts:
                if p.rally_dragon:
                    buf2.write(f"<@{p.user_id}> | {p.troop_type} {p.troop_tier} | Cap: {p.capacity_value}\n")
            section("Capacity (High → Low)")
            for p in parts:
                buf2.write(f"<@{p.user_id}> | {p.troop_type} {p.troop_tier} | Dragon: {'Yes' if p.rally_dragon else 'No'} | Cap: {p.capacity_value}\n")

            await interaction.response.send_message(
                "Exported roster.",
                files=[
                    discord.File(io.BytesIO(buf.getvalue().encode("utf-8")), filename="rally_roster.csv"),
                    discord.File(io.BytesIO(buf2.getvalue().encode("utf-8")), filename="rally_roster.txt"),
                ],
                ephemeral=True
            )

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
            view=ConfirmJoinVCView("5s Intervals", AUDIO_5S_ROLL),
            ephemeral=True
        )

    @discord.ui.button(label="10 Second Intervals", style=discord.ButtonStyle.danger)
    async def s10(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_message(
            "The bot will join your current VC and start. Continue?",
            view=ConfirmJoinVCView("10s Intervals", AUDIO_10S_ROLL),
            ephemeral=True
        )

    @discord.ui.button(label="15 Second Intervals", style=discord.ButtonStyle.danger)
    async def s15(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_message(
            "The bot will join your current VC and start. Continue?",
            view=ConfirmJoinVCView("15s Intervals", AUDIO_15S_ROLL),
            ephemeral=True
        )

    @discord.ui.button(label="30 Second Intervals", style=discord.ButtonStyle.danger)
    async def s30(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_message(
            "The bot will join your current VC and start. Continue?",
            view=ConfirmJoinVCView("30s Intervals", AUDIO_30S_ROLL),
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
            vc = await ensure_temp_vc(guild, author, channel, "Keep Rally", 10)
        except Exception as e:
            return await interaction.response.send_message(f"Couldn't create temp VC: {e}", ephemeral=True)

        invite_url = await create_or_refresh_vc_invite(vc)
        thread = await create_thread_for_rally(channel, "🧵 Keep Rally Thread", author)

        dummy = await channel.send(embed=discord.Embed(title="Creating rally...", color=discord.Color.blurple()))
        r = Rally(
            message_id=dummy.id, guild_id=guild.id, channel_id=channel.id, creator_id=author.id, rally_kind="KEEP",
            keep_power=self.keep_power.value.strip(),
            primary_troop=self.primary_troop.value.strip().title(),  # type: ignore
            keep_level=self.keep_level.value.strip().upper(),
            gear_worn=self.gear_worn.value.strip(),
            idle_and_scouted=self.idle_and_scouted.value.strip(),
            temp_vc_id=vc.id, temp_vc_invite_url=invite_url, private_thread_id=thread.id
        )
        r.participants[author.id] = Participant(author.id, "Cavalry", "T10", False, 0)
        RALLIES[dummy.id] = r
        VC_TO_POST[vc.id] = dummy.id

       await dummy.edit(embed=embed_for_rally(guild, r), view=build_rally_view(r))

await channel.send(
    f"{role_mention(guild, HITTERS_ROLE_NAME)} — Don’t forget to use `/type_of_rally` for Bomb/Rolling!",
    allowed_mentions=discord.AllowedMentions(everyone=False, users=False, roles=True)
)

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
        vc = await ensure_temp_vc(guild, author, channel, "SOP Rally", 10)
    except Exception as e:
        return await interaction.response.send_message(f"Couldn't create temp VC: {e}", ephemeral=True)

    invite_url = await create_or_refresh_vc_invite(vc)
    thread = await create_thread_for_rally(channel, "🧵 SOP Rally Thread", author)

    dummy = await channel.send(embed=discord.Embed(title="Creating rally...", color=discord.Color.blurple()))
    r = Rally(
        message_id=dummy.id, guild_id=guild.id, channel_id=channel.id, creator_id=author.id, rally_kind="SOP",
        temp_vc_id=vc.id, temp_vc_invite_url=invite_url, private_thread_id=thread.id
    )
    r.participants[author.id] = Participant(author.id, "Cavalry", "T10", False, 0)
    RALLIES[dummy.id] = r
    VC_TO_POST[vc.id] = dummy.id

    await dummy.edit(embed=embed_for_rally(guild, r), view=build_rally_view(r))
    await channel.send(
    f"{role_mention(guild, HITTERS_ROLE_NAME)} — Don’t forget to use `/type_of_rally` for Bomb/Rolling!",
    allowed_mentions=discord.AllowedMentions(everyone=False, users=False, roles=True)
)
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
        if r.private_thread_id:
            th = guild.get_thread(r.private_thread_id)
            if th and not th.archived:
                try:
                    await th.edit(archived=True, reason=reason)
                except Exception:
                    pass
        r.temp_vc_id = None
        r.temp_vc_invite_url = None
        await update_post(guild, r)

@bot.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
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
