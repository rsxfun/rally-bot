# main.py — Rally Bot (clean boot order, guild sync, slash groups)

import os
import io
import csv
import asyncio
import logging
from dataclasses import dataclass, field
from typing import Dict, Optional, List, Literal

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

# ============================== ENV & CONFIG ==============================

load_dotenv()

TOKEN = os.getenv("DISCORD_BOT_TOKEN") or os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise RuntimeError("Set DISCORD_BOT_TOKEN (or DISCORD_TOKEN) in your environment.")

# Comma-separated list of guild IDs to sync commands instantly (recommended)
GUILD_IDS = os.getenv("GUILD_IDS", "")  # e.g. "123456789012345678,987654321012345678"

def _parse_guild_ids() -> List[int]:
    return [int(x) for x in GUILD_IDS.split(",") if x.strip().isdigit()]

# Category for temp voice channels
TEMP_VC_CATEGORY_ID = int(os.getenv("TEMP_VC_CATEGORY_ID", "0"))
# Role name to mention on posts
HITTERS_ROLE_NAME = os.getenv("HITTERS_ROLE_NAME", "hitters")
# VC empty timeout (seconds) → delete
DELETE_VC_IF_EMPTY_AFTER_SECS = int(os.getenv("DELETE_VC_IF_EMPTY_AFTER_SECS", "300"))

# Audio URLs (you can host on GCS/S3/etc.)
AUDIO_5M_BOMB = os.getenv("AUDIO_5M_BOMB", "https://storage.googleapis.com/rallybot/5minbombcomplete.mp3")
AUDIO_10M_BOMB = os.getenv("AUDIO_10M_BOMB", "https://storage.googleapis.com/rallybot/10minbomb.mp3")
AUDIO_30M_BOMB = os.getenv("AUDIO_30M_BOMB", "")  # fill if available
AUDIO_1H_BOMB  = os.getenv("AUDIO_1H_BOMB", "")   # fill if available
AUDIO_EXPLAIN_BOMB = os.getenv("AUDIO_EXPLAIN_BOMB", "https://storage.googleapis.com/rallybot/explainbombrally.mp3")

AUDIO_5S_ROLL  = os.getenv("AUDIO_5S_ROLL", "https://storage.googleapis.com/rallybot/5secondgaps.mp3")
AUDIO_10S_ROLL = os.getenv("AUDIO_10S_ROLL", "https://storage.googleapis.com/rallybot/10secondgaps.mp3")
AUDIO_15S_ROLL = os.getenv("AUDIO_15S_ROLL", "https://storage.googleapis.com/rallybot/15secondgaps.mp3")
AUDIO_30S_ROLL = os.getenv("AUDIO_30S_ROLL", "https://storage.googleapis.com/rallybot/30secondgaps.mp3")
AUDIO_EXPLAIN_ROLL = os.getenv("AUDIO_EXPLAIN_ROLL", "https://storage.googleapis.com/rallybot/explainrollingrallies.mp3")

# ============================== LOGGING & BOT ==============================

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("rally-bot")

intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.voice_states = True

allowed_mentions = discord.AllowedMentions(everyone=False, users=True, roles=True)

bot = commands.Bot(command_prefix="!", intents=intents, allowed_mentions=allowed_mentions)
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
    capacity_value: int  # numeric

@dataclass
class Rally:
    message_id: int
    guild_id: int
    channel_id: int
    creator_id: int
    rally_kind: Literal["KEEP", "SOP"]

    # Keep Rally details
    keep_power: Optional[str] = None
    primary_troop: Optional[TroopType] = None
    keep_level: Optional[str] = None
    gear_worn: Optional[str] = None
    idle_time: Optional[str] = None
    scouted: Optional[str] = None

    # dynamic resources
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
# voice_channel_id -> message_id (for cleanup, reverse lookup)
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

async def ensure_temp_vc(guild: discord.Guild, owner: discord.Member, name_hint: str, size_hint: int) -> discord.VoiceChannel:
    cat = guild.get_channel(TEMP_VC_CATEGORY_ID)
    if not isinstance(cat, discord.CategoryChannel):
        raise RuntimeError(f"Category {TEMP_VC_CATEGORY_ID} not found or not a category.")
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
        reason="Rally temp VC",
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
    desc = f"{role_mention(guild, HITTERS_ROLE_NAME)} — Don't forget to use `/type_of_rally` to pick Bomb or Rolling details!"
    e = discord.Embed(title=title, description=desc, color=discord.Color.blurple())
    creator = guild.get_member(r.creator_id)
    e.add_field(name="Host", value=(creator.mention if creator else f"<@{r.creator_id}>"), inline=True)

    if r.rally_kind == "KEEP":
        e.add_field(name="Power Level of Keep", value=r.keep_power or "—", inline=True)
        e.add_field(name="Primary Troop Type", value=r.primary_troop or "—", inline=True)
        e.add_field(name="Keep Level", value=r.keep_level or "—", inline=True)
        e.add_field(name="Gear Worn", value=r.gear_worn or "—", inline=True)
        e.add_field(name="Idle Time", value=r.idle_time or "—", inline=True)
        e.add_field(name="Scouted?", value=r.scouted or "—", inline=True)

    if r.temp_vc_id:
        ch = guild.get_channel(r.temp_vc_id)
        if isinstance(ch, discord.VoiceChannel):
            e.add_field(name="Voice Channel", value=ch.mention, inline=False)
    if r.private_thread_id:
        th = guild.get_thread(r.private_thread_id)
        if th:
            e.add_field(name="Party Thread", value=th.mention, inline=False)

    e.add_field(name="Roster", value=r.roster_mentions(), inline=False)
    return e

async def dm_join_info(member: discord.Member, r: Rally, sop: bool):
    """DM the user with a single-use VC invite and thread link."""
    guild = member.guild
    if not r.temp_vc_id:
        return
    vc = guild.get_channel(r.temp_vc_id)
    if not isinstance(vc, discord.VoiceChannel):
        return
    # single-use, 1 hour
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
    view = build_rally_view(r)
    await msg.edit(embed=embed_for_rally(guild, r), view=view)

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

        # Add to private thread
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

        @discord.ui.button(label="Join Rally", style=discord.ButtonStyle.success, custom_id="join_rally")
        async def join_rally(self, interaction: discord.Interaction, button: discord.ui.Button):
            if self.mid not in RALLIES:
                return await interaction.response.send_message("This rally no longer exists.", ephemeral=True)
            sop = (RALLIES[self.mid].rally_kind == "SOP")
            await interaction.response.send_modal(JoinRallyModal(self.mid, sop=sop))

        @discord.ui.button(label="Export Roster", style=discord.ButtonStyle.primary, custom_id="export_roster")
        async def export_roster(self, interaction: discord.Interaction, button: discord.ui.Button):
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

def simple_choice_embed(title: str, description: str) -> discord.Embed:
    return discord.Embed(title=title, description=description, color=discord.Color.blurple())

class ConfirmJoinVCView(discord.ui.View):
    def __init__(self, url_label: str, url_to_play: str):
        super().__init__(timeout=120)
        self.url_to_play = url_to_play
        self.url_label = url_label

    @discord.ui.button(label="Join VC & Start", style=discord.ButtonStyle.success)
    async def yes(self, interaction: discord.Interaction, button: discord.ui.Button):
        member: discord.Member = interaction.user  # type: ignore
        if not member.voice or not isinstance(member.voice.channel, discord.VoiceChannel):
            return await interaction.response.send_message("You must be in a voice channel first.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        ok = await play_audio_in_member_vc(member, self.url_to_play)
        if ok:
            await interaction.followup.send(f"Playing **{self.url_label}** in {member.voice.channel.mention}", ephemeral=True)
        else:
            await interaction.followup.send("Could not start playback (check ffmpeg & permissions).", ephemeral=True)

    @discord.ui.button(label="No", style=discord.ButtonStyle.danger)
    async def no(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("Cancelled.", ephemeral=True)

async def play_audio_in_member_vc(member: discord.Member, url: str) -> bool:
    """Join member's current VC and play an MP3 from a URL using ffmpeg."""
    if not member.voice or not isinstance(member.voice.channel, discord.VoiceChannel):
        return False
    vc_channel = member.voice.channel
    try:
        voice: discord.VoiceClient
        if member.guild.voice_client and member.guild.voice_client.is_connected():
            voice = member.guild.voice_client
            if voice.channel != vc_channel:
                await voice.move_to(vc_channel)
        else:
            voice = await vc_channel.connect()
        if voice.is_playing():
            voice.stop()
        audio = discord.FFmpegPCMAudio(url)
        voice.play(audio, after=lambda e: log.info("Playback finished: %s", e))
        return True
    except Exception as e:
        log.exception("Play error: %s", e)
        return False

# ---- Bomb Rallies
@type_group.command(name="bomb", description="Bomb Rally options")
async def type_bomb(interaction: discord.Interaction):
    e = simple_choice_embed("💣 Bomb Rally", "Pick an option below.")
    view = discord.ui.View(timeout=300)

    async def confirm(inter: discord.Interaction, label: str, url: str):
        await inter.response.send_message(
            "The bot will join your current VC and start the countdown. Continue?",
            view=ConfirmJoinVCView(label, url),
            ephemeral=True
        )

    # Buttons
    async def _5m(i):  await i.response.send_message("Choose where to explain/run:", view=make_explain_or_start_view("5m Bomb", AUDIO_5M_BOMB), ephemeral=True)
    async def _10m(i): await i.response.send_message("Choose where to explain/run:", view=make_explain_or_start_view("10m Bomb", AUDIO_10M_BOMB), ephemeral=True)
    async def _30m(i): await i.response.send_message("Choose where to explain/run:", view=make_explain_or_start_view("30m Bomb", AUDIO_30M_BOMB), ephemeral=True)
    async def _1h(i):  await i.response.send_message("Choose where to explain/run:", view=make_explain_or_start_view("1h Bomb", AUDIO_1H_BOMB), ephemeral=True)
    async def _exp(i): await confirm(i, "Explain Bomb Rally", AUDIO_EXPLAIN_BOMB)

    view.add_item(discord.ui.Button(label="5 Minute Bomb", style=discord.ButtonStyle.danger, custom_id="bomb_5"))
    view.add_item(discord.ui.Button(label="10 Minute Bomb", style=discord.ButtonStyle.danger, custom_id="bomb_10"))
    view.add_item(discord.ui.Button(label="30 Minute Bomb", style=discord.ButtonStyle.danger, custom_id="bomb_30"))
    view.add_item(discord.ui.Button(label="1 Hour Bomb", style=discord.ButtonStyle.danger, custom_id="bomb_60"))
    view.add_item(discord.ui.Button(label="Explain Bomb Rally", style=discord.ButtonStyle.success, custom_id="bomb_explain"))

    async def on_interaction(inter: discord.Interaction):
        if not inter.data or "custom_id" not in inter.data:
            return
        cid = inter.data["custom_id"]
        if cid == "bomb_5":  return await _5m(inter)
        if cid == "bomb_10": return await _10m(inter)
        if cid == "bomb_30": return await _30m(inter)
        if cid == "bomb_60": return await _1h(inter)
        if cid == "bomb_explain": return await _exp(inter)

    view.on_timeout = lambda: None  # quiet
    view.interaction_check = lambda i: True  # allow anyone

    # monkey-patch to route clicks (discord.py doesn't provide a native router for ad-hoc buttons)
    async def _dispatch(i):
        await on_interaction(i)
    view._scheduled_task = None
    view._dispatch = _dispatch  # type: ignore

    await interaction.response.send_message(embed=e, view=view, ephemeral=True)

def make_explain_or_start_view(label: str, url: str) -> discord.ui.View:
    v = discord.ui.View(timeout=120)
    async def confirm(inter: discord.Interaction):
        await inter.response.send_message(
            "The bot will join your current VC and start the countdown. Continue?",
            view=ConfirmJoinVCView(label, url),
            ephemeral=True
        )
    v.add_item(discord.ui.Button(label=f"Start {label} on VC", style=discord.ButtonStyle.danger, custom_id=f"start_{label}"))
    v.add_item(discord.ui.Button(label="Explain on VC", style=discord.ButtonStyle.success, custom_id=f"expl_vc_{label}"))
    v.add_item(discord.ui.Button(label="Explain in Text", style=discord.ButtonStyle.primary, custom_id=f"expl_txt_{label}"))

    async def on_interaction(inter: discord.Interaction):
        cid = inter.data.get("custom_id")
        if cid and (cid.startswith("start_") or cid.startswith("expl_vc_")):
            return await confirm(inter)
        if cid and cid.startswith("expl_txt_"):
            return await inter.response.send_message(f"**{label}**:\n- Join VC.\n- Follow timing as instructed.\n", ephemeral=True)
    async def _dispatch(i):
        await on_interaction(i)
    v._dispatch = _dispatch  # type: ignore
    return v

# ---- Rolling Rallies
@type_group.command(name="rolling", description="Rolling Rally options")
async def type_rolling(interaction: discord.Interaction):
    e = simple_choice_embed("🔁 Rolling Rally", "Pick an option below.")
    view = discord.ui.View(timeout=300)

    async def confirm(inter: discord.Interaction, label: str, url: str):
        await inter.response.send_message(
            "The bot will join your current VC and start.",
            view=ConfirmJoinVCView(label, url),
            ephemeral=True
        )

    async def _5s(i):  await confirm(i, "5s Intervals", AUDIO_5S_ROLL)
    async def _10s(i): await confirm(i, "10s Intervals", AUDIO_10S_ROLL)
    async def _15s(i): await confirm(i, "15s Intervals", AUDIO_15S_ROLL)
    async def _30s(i): await confirm(i, "30s Intervals", AUDIO_30S_ROLL)
    async def _exp(i): await confirm(i, "Explain Rolling Rally", AUDIO_EXPLAIN_ROLL)

    view.add_item(discord.ui.Button(label="5 Second Intervals", style=discord.ButtonStyle.danger, custom_id="roll_5"))
    view.add_item(discord.ui.Button(label="10 Second Intervals", style=discord.ButtonStyle.danger, custom_id="roll_10"))
    view.add_item(discord.ui.Button(label="15 Second Intervals", style=discord.ButtonStyle.danger, custom_id="roll_15"))
    view.add_item(discord.ui.Button(label="30 Second Intervals", style=discord.ButtonStyle.danger, custom_id="roll_30"))
    view.add_item(discord.ui.Button(label="Explain Rolling Rally", style=discord.ButtonStyle.success, custom_id="roll_exp"))

    async def on_interaction(inter: discord.Interaction):
        cid = inter.data.get("custom_id")
        if cid == "roll_5":  return await _5s(inter)
        if cid == "roll_10": return await _10s(inter)
        if cid == "roll_15": return await _15s(inter)
        if cid == "roll_30": return await _30s(inter)
        if cid == "roll_exp": return await _exp(inter)
    async def _dispatch(i):
        await on_interaction(i)
    view._dispatch = _dispatch  # type: ignore

    await interaction.response.send_message(embed=e, view=view, ephemeral=True)

# ============================== /rally GROUP ==============================

rally_group = app_commands.Group(name="rally", description="Create a Keep Rally or a Seat of Power Rally")
tree.add_command(rally_group)

class KeepForm(discord.ui.Modal, title="Keep Rally Details"):
    keep_power = discord.ui.TextInput(label="Power Level of Keep", placeholder="e.g., 200m, 350m", required=True, max_length=16)
    primary_troop = discord.ui.TextInput(label="Primary Troop Type", placeholder="Cavalry / Infantry / Range", required=True, max_length=16)
    keep_level = discord.ui.TextInput(label="Keep Level", placeholder="e.g., K30, K34", required=True, max_length=8)
    gear_worn = discord.ui.TextInput(label="What Gear is Worn", placeholder="Farming / Crafting / Attack / Defense", required=True, max_length=32)
    idle_time = discord.ui.TextInput(label="Idle Time", placeholder="e.g., 10 Minutes, 30 Minutes", required=True, max_length=32)
    scouted = discord.ui.TextInput(label="Scouted?", placeholder="Yes 20 minutes ago, No", required=True, max_length=64)

    async def on_submit(self, interaction: discord.Interaction):
        guild = interaction.guild
        channel = interaction.channel
        author: discord.Member = interaction.user  # type: ignore

        if not isinstance(channel, discord.TextChannel):
            return await interaction.response.send_message("Use this in a server text channel.", ephemeral=True)

        try:
            vc = await ensure_temp_vc(guild, author, "Keep Rally", 10)
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
            idle_time=self.idle_time.value.strip(),
            scouted=self.scouted.value.strip(),
            temp_vc_id=vc.id, temp_vc_invite_url=invite_url, private_thread_id=thread.id
        )
        # Optional: add host placeholder to roster
        r.participants[author.id] = Participant(author.id, "Cavalry", "T10", False, 0)
        RALLIES[dummy.id] = r
        VC_TO_POST[vc.id] = dummy.id

        await dummy.edit(embed=embed_for_rally(guild, r), view=build_rally_view(r))
        await interaction.response.send_message(f"Keep Rally posted in {channel.mention}.", ephemeral=True)
        asyncio.create_task(schedule_delete_if_empty(guild.id, vc.id))

class SOPForm(discord.ui.Modal, title="Seat of Power Rally"):
    async def on_submit(self, interaction: discord.Interaction):
        guild = interaction.guild
        channel = interaction.channel
        author: discord.Member = interaction.user  # type: ignore

        if not isinstance(channel, discord.TextChannel):
            return await interaction.response.send_message("Use this in a server text channel.", ephemeral=True)

        try:
            vc = await ensure_temp_vc(guild, author, "SOP Rally", 10)
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
        await interaction.response.send_message(f"SOP Rally posted in {channel.mention}.", ephemeral=True)
        asyncio.create_task(schedule_delete_if_empty(guild.id, vc.id))

@rally_group.command(name="keep", description="Create a Keep Rally (form)")
async def rally_keep(interaction: discord.Interaction):
    await interaction.response.send_modal(KeepForm())

@rally_group.command(name="sop", description="Create a Seat of Power Rally")
async def rally_sop(interaction: discord.Interaction):
    await interaction.response.send_modal(SOPForm())

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
        # archive thread
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
    # when last user leaves a tracked VC -> delete immediately
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
            # Instant guild sync
            for gid in guild_ids:
                await tree.sync(guild=discord.Object(id=gid))
            log.info("Slash commands synced to %d guild(s): %s", len(guild_ids), guild_ids)
        else:
            # Global sync (propagation may take up to ~1 hour)
            cmds = await tree.sync()
            log.info("Globally synced %d commands.", len(cmds))
    except Exception as e:
        log.exception("Failed to sync commands: %s", e)

    log.info("Logged in as %s (%s)", bot.user, bot.user.id)

# ============================== ENTRYPOINT ==============================

if __name__ == "__main__":
    bot.run(TOKEN)
