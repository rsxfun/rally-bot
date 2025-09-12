# ---------- main.py (minimal proven boot order) ----------
import os
import discord
from discord.ext import commands
from dotenv import load_dotenv

# 1) Load env FIRST
load_dotenv()
TOKEN = os.getenv("DISCORD_BOT_TOKEN") or os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise RuntimeError("Set DISCORD_BOT_TOKEN (or DISCORD_TOKEN).")

# 2) Create intents & bot BEFORE any bot.run(...)
intents = discord.Intents.default()
intents.guilds = True
bot = commands.Bot(command_prefix="!", intents=intents)

# 3) (Optional) debug — remove after you see "Logged in as ..."
print("Token present:", bool(TOKEN))
print("Token length:", len(TOKEN))
print("Token preview:", TOKEN[:6], "...", TOKEN[-6:])

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} ({bot.user.id})")

# ---------- PUT YOUR COMMANDS/COGS/VIEW CLASSES BELOW THIS LINE ----------
# e.g.
# @bot.tree.command(description="ping")
# async def ping(interaction: discord.Interaction):
#     await interaction.response.send_message("pong")
# ------------------------------------------------------------------------

# 4) Finally, run the bot LAST
bot.run(TOKEN)

import os
import io
import csv
import asyncio
import logging
from dataclasses import dataclass, field
from typing import Dict, Optional, List, Literal

import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

# ------------------------- Setup & Config -------------------------
import os
GUILD_IDS = os.getenv("GUILD_IDS", "")  # e.g. "123456789012345678,987654321012345678"

def _parse_guild_ids():
    return [int(x) for x in GUILD_IDS.split(",") if x.strip().isdigit()]

load_dotenv()
bot.run(TOKEN)

TOKEN = os.getenv("DISCORD_BOT_TOKEN") or os.getenv("DISCORD_TOKEN")
TEMP_VC_CATEGORY_ID = int(os.getenv("TEMP_VC_CATEGORY_ID", "0"))  # required for temp VC
HITTERS_ROLE_NAME = os.getenv("HITTERS_ROLE_NAME", "hitters")     # role to @mention on posts
DELETE_VC_IF_EMPTY_AFTER_SECS = int(os.getenv("DELETE_VC_IF_EMPTY_AFTER_SECS", "300"))  # 5 minutes

# Audio (VC) URLs
AUDIO_5M_BOMB = os.getenv("AUDIO_5M_BOMB", "https://storage.googleapis.com/rallybot/5minbombcomplete.mp3")
AUDIO_10M_BOMB = os.getenv("AUDIO_10M_BOMB", "https://storage.googleapis.com/rallybot/10minbomb.mp3")
AUDIO_30M_BOMB = os.getenv("AUDIO_30M_BOMB", "")  # fill later
AUDIO_1H_BOMB  = os.getenv("AUDIO_1H_BOMB", "")   # fill later
AUDIO_EXPLAIN_BOMB = os.getenv("AUDIO_EXPLAIN_BOMB", "https://storage.googleapis.com/rallybot/explainbombrally.mp3")

AUDIO_5S_ROLL  = os.getenv("AUDIO_5S_ROLL", "https://storage.googleapis.com/rallybot/5secondgaps.mp3")
AUDIO_10S_ROLL = os.getenv("AUDIO_10S_ROLL", "https://storage.googleapis.com/rallybot/10secondgaps.mp3")
AUDIO_15S_ROLL = os.getenv("AUDIO_15S_ROLL", "https://storage.googleapis.com/rallybot/15secondgaps.mp3")
AUDIO_30S_ROLL = os.getenv("AUDIO_30S_ROLL", "https://storage.googleapis.com/rallybot/30secondgaps.mp3")
AUDIO_EXPLAIN_ROLL = os.getenv("AUDIO_EXPLAIN_ROLL", "https://storage.googleapis.com/rallybot/explainrollingrallies.mp3")

if not TOKEN:
    raise RuntimeError("Please set DISCORD_BOT_TOKEN (or DISCORD_TOKEN) in your environment / .env file.")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
log = logging.getLogger("rally-bot")

intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.voice_states = True

allowed_mentions = discord.AllowedMentions(everyone=False, users=True, roles=True)

bot = commands.Bot(
    command_prefix="!",
    intents=intents,
    allowed_mentions=allowed_mentions,
)
tree = bot.tree

# ------------------------- Data Models -------------------------

TroopType = Literal["Cavalry", "Infantry", "Range"]
TroopTier = Literal["T8", "T9", "T10", "T11", "T12"]

@dataclass
class Participant:
    user_id: int
    troop_type: TroopType
    troop_tier: TroopTier
    rally_dragon: bool
    capacity_value: int  # user input numeric

@dataclass
class Rally:
    message_id: int
    guild_id: int
    channel_id: int
    creator_id: int
    rally_kind: Literal["KEEP", "SOP"]

    # keep rally details
    keep_power: Optional[str] = None
    primary_troop: Optional[TroopType] = None
    keep_level: Optional[str] = None
    gear_worn: Optional[str] = None
    idle_time: Optional[str] = None
    scouted: Optional[str] = None

    # shared extras from /type_of_rally (not stored per se, but we display reminder)
    game_mode: Optional[str] = None

    # dynamic resources
    temp_vc_id: Optional[int] = None
    temp_vc_invite_url: Optional[str] = None
    private_thread_id: Optional[int] = None
    created_ts: float = 0.0

    # participants
    participants: Dict[int, Participant] = field(default_factory=dict)

    def roster_mentions(self) -> str:
        if not self.participants:
            return "—"
        return ", ".join(f"<@{uid}>" for uid in self.participants.keys())

# message_id -> Rally
RALLIES: Dict[int, Rally] = {}
# voice_channel_id -> message_id (for cleanup, reverse lookup)
VC_TO_POST: Dict[int, int] = {}

# ------------------------- Utilities -------------------------

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
    # Non-expiring, unlimited uses for the button; DM invites will be single-use
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
    thread_url = thread_link(r.guild_id, r.private_thread_id) if r.private_thread_id else None
    text = f"You joined the **{'SOP' if sop else 'Keep'} Rally**.\nVoice: {invite.url}"
    if thread_url:
        text += f"\nThread: {thread_url}"
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

# ------------------------- Views & Modals -------------------------

class JoinRallyModal(discord.ui.Modal, title="Join Rally"):
    troop_type = discord.ui.TextInput(
        label="Troop Type (Cavalry / Infantry / Range)",
        required=True,
        max_length=16
    )
    troop_tier = discord.ui.TextInput(
        label="Troop Tier (T8 / T9 / T10 / T11 / T12)",
        required=True,
        max_length=4
    )
    rally_dragon = discord.ui.TextInput(
        label="Rally Dragon (Yes/No)",
        required=True,
        max_length=8
    )
    capacity = discord.ui.TextInput(
        label="Rally Capacity (number)",
        required=True,
        max_length=10,
        placeholder="e.g. 550000"
    )

    def __init__(self, rally_mid: int, sop: bool):
        super().__init__()
        self.rally_mid = rally_mid
        self.sop = sop

    async def on_submit(self, interaction: discord.Interaction):
        r = RALLIES.get(self.rally_mid)
        if not r:
            return await interaction.response.send_message("This rally no longer exists.", ephemeral=True)

        # Validate and normalize
        tt = self.troop_type.value.strip().title()
        if tt not in ("Cavalry", "Infantry", "Range"):
            return await interaction.response.send_message("Troop Type must be Cavalry, Infantry, or Range.", ephemeral=True)
        tier = self.troop_tier.value.strip().upper()
        if tier not in ("T8", "T9", "T10", "T11", "T12"):
            return await interaction.response.send_message("Troop Tier must be one of T8/T9/T10/T11/T12.", ephemeral=True)
        drag_ans = self.rally_dragon.value.strip().lower()
        dragon = drag_ans.startswith("y")
        capacity_num = ensure_int(self.capacity.value, 0)

        # Add or update participant
        r.participants[interaction.user.id] = Participant(
            user_id=interaction.user.id,
            troop_type=tt,  # type: ignore
            troop_tier=tier,  # type: ignore
            rally_dragon=dragon,
            capacity_value=capacity_num
        )

        # Add to thread
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
        await interaction.response.send_message("You're on the roster. Check your DMs for VC + thread.", ephemeral=True)


def build_rally_view(r: Rally) -> discord.ui.View:
    class RallyView(discord.ui.View):
        def __init__(self, rally_mid: int):
            super().__init__(timeout=60 * 60)
            self.mid = rally_mid

        @discord.ui.button(label="Join Rally", style=discord.ButtonStyle.success, custom_id="join_rally")
        async def join_rally(self, interaction: discord.Interaction, button: discord.ui.Button):
            if self.mid not in RALLIES:
                return await interaction.response.send_message("This rally no longer exists.", ephemeral=True)
            sop = (RALLIES[self.mid].rally_kind == "SOP")
            modal = JoinRallyModal(rally_mid=self.mid, sop=sop)
            await interaction.response.send_modal(modal)

        @discord.ui.button(label="Export Roster", style=discord.ButtonStyle.primary, custom_id="export_roster")
        async def export_roster(self, interaction: discord.Interaction, button: discord.ui.Button):
            r = RALLIES.get(self.mid)
            if not r:
                return await interaction.response.send_message("This rally no longer exists.", ephemeral=True)

            # Build CSV grouped & sorted as requested
            # Group by Troop Type; within each, by Troop Tier; list Dragon==True separately; sort by capacity desc
            parts: List[Participant] = list(r.participants.values())
            parts.sort(key=lambda p: p.capacity_value, reverse=True)

            buf = io.StringIO()
            w = csv.writer(buf)
            w.writerow(["User", "Troop Type", "Troop Tier", "Rally Dragon", "Capacity"])

            def ufmt(uid: int) -> str:
                return f"@{uid}"

            for p in parts:
                w.writerow([ufmt(p.user_id), p.troop_type, p.troop_tier, "Yes" if p.rally_dragon else "No", p.capacity_value])

            # Build a readable sectioned text header too (first sheet row is csv)
            buf2 = io.StringIO()
            def section(title: str):
                buf2.write(f"\n=== {title} ===\n")

            # Troop Type sections
            for tt in ("Cavalry", "Infantry", "Range"):
                section(f"Troop Type: {tt}")
                for p in parts:
                    if p.troop_type == tt:
                        buf2.write(f"<@{p.user_id}>  | Tier {p.troop_tier} | Dragon: {'Yes' if p.rally_dragon else 'No'} | Cap: {p.capacity_value}\n")

            # Troop Tier sections
            for tier in ("T12", "T11", "T10", "T9", "T8"):
                section(f"Troop Tier: {tier}")
                for p in parts:
                    if p.troop_tier == tier:
                        buf2.write(f"<@{p.user_id}>  | {p.troop_type} | Dragon: {'Yes' if p.rally_dragon else 'No'} | Cap: {p.capacity_value}\n")

            # Dragon == Yes only
            section("Rally Dragon: Yes")
            for p in parts:
                if p.rally_dragon:
                    buf2.write(f"<@{p.user_id}>  | {p.troop_type} {p.troop_tier} | Cap: {p.capacity_value}\n")

            # Capacity high -> low
            section("Capacity (High → Low)")
            for p in parts:
                buf2.write(f"<@{p.user_id}>  | {p.troop_type} {p.troop_tier} | Dragon: {'Yes' if p.rally_dragon else 'No'} | Cap: {p.capacity_value}\n")

            # Attach both CSV and TXT
            csv_bytes = io.BytesIO(buf.getvalue().encode("utf-8"))
            txt_bytes = io.BytesIO(buf2.getvalue().encode("utf-8"))
            await interaction.response.send_message(
                "Exported roster.",
                files=[
                    discord.File(csv_bytes, filename="rally_roster.csv"),
                    discord.File(txt_bytes, filename="rally_roster.txt"),
                ],
                ephemeral=True
            )

    view = RallyView(r.message_id)
    # Add a link button for Join VC if we have a public invite
    if r.temp_vc_invite_url:
        view.add_item(discord.ui.Button(label="Join VC", style=discord.ButtonStyle.danger, url=r.temp_vc_invite_url))
    return view

# ------------------------- Slash: /type_of_rally -------------------------

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
        # Stop any current audio
        if voice.is_playing():
            voice.stop()
        # Play new
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

    async def warn_and_confirm(inter: discord.Interaction, label: str, url: str):
        await inter.response.send_message(
            "The bot will join your current VC and start the countdown. Continue?",
            view=ConfirmJoinVCView(label, url),
            ephemeral=True
        )

    @discord.ui.button(label="5 Minute Bomb", style=discord.ButtonStyle.danger)
    async def b5(i, b): await i.response.send_message(
        "Choose where to explain/run:", view=make_explain_or_start_view("5m Bomb", AUDIO_5M_BOMB), ephemeral=True
    )
    @discord.ui.button(label="10 Minute Bomb", style=discord.ButtonStyle.danger)
    async def b10(i, b): await i.response.send_message(
        "Choose where to explain/run:", view=make_explain_or_start_view("10m Bomb", AUDIO_10M_BOMB), ephemeral=True
    )
    @discord.ui.button(label="30 Minute Bomb", style=discord.ButtonStyle.danger)
    async def b30(i, b): await i.response.send_message(
        "Choose where to explain/run:", view=make_explain_or_start_view("30m Bomb", AUDIO_30M_BOMB), ephemeral=True
    )
    @discord.ui.button(label="1 Hour Bomb", style=discord.ButtonStyle.danger)
    async def b60(i, b): await i.response.send_message(
        "Choose where to explain/run:", view=make_explain_or_start_view("1h Bomb", AUDIO_1H_BOMB), ephemeral=True
    )
    @discord.ui.button(label="Explain Bomb Rally", style=discord.ButtonStyle.success)
    async def bexplain(i, b): await warn_and_confirm(i, "Explain Bomb Rally", AUDIO_EXPLAIN_BOMB)

    # Hack to attach callbacks to view instance
    view.add_item(b5)
    view.add_item(b10)
    view.add_item(b30)
    view.add_item(b60)
    view.add_item(bexplain)

    await interaction.response.send_message(embed=e, view=view, ephemeral=True)

def make_explain_or_start_view(label: str, url: str) -> discord.ui.View:
    v = discord.ui.View(timeout=120)
    async def confirm(inter: discord.Interaction):
        await inter.response.send_message(
            "The bot will join your current VC and start the countdown. Continue?",
            view=ConfirmJoinVCView(label, url),
            ephemeral=True
        )
    @discord.ui.button(label=f"Start {label} on VC", style=discord.ButtonStyle.danger)
    async def start(i, b): await confirm(i)
    @discord.ui.button(label="Explain on VC", style=discord.ButtonStyle.success)
    async def explain_vc(i, b): await confirm(i)
    @discord.ui.button(label="Explain in Text", style=discord.ButtonStyle.primary)
    async def explain_text(i, b): await i.response.send_message(f"**{label}**:\n- Join VC.\n- Follow timing as instructed.\n", ephemeral=True)

    v.add_item(start)
    v.add_item(explain_vc)
    v.add_item(explain_text)
    return v

# ---- Rolling Rallies
@type_group.command(name="rolling", description="Rolling Rally options")
async def type_rolling(interaction: discord.Interaction):
    e = simple_choice_embed("🔁 Rolling Rally", "Pick an option below.")
    view = discord.ui.View(timeout=300)

    async def confirm(label: str, url: str, inter: discord.Interaction):
        await inter.response.send_message(
            "The bot will join your current VC and start the countdown. Continue?",
            view=ConfirmJoinVCView(label, url),
            ephemeral=True
        )

    @discord.ui.button(label="5 Second Intervals", style=discord.ButtonStyle.danger)
    async def s5(i, b): await confirm("5s Intervals", AUDIO_5S_ROLL, i)
    @discord.ui.button(label="10 Second Intervals", style=discord.ButtonStyle.danger)
    async def s10(i, b): await confirm("10s Intervals", AUDIO_10S_ROLL, i)
    @discord.ui.button(label="15 Second Intervals", style=discord.ButtonStyle.danger)
    async def s15(i, b): await confirm("15s Intervals", AUDIO_15S_ROLL, i)
    @discord.ui.button(label="30 Second Intervals", style=discord.ButtonStyle.danger)
    async def s30(i, b): await confirm("30s Intervals", AUDIO_30S_ROLL, i)
    @discord.ui.button(label="Explain Rolling Rally", style=discord.ButtonStyle.success)
    async def rexplain(i, b): await confirm("Explain Rolling Rally", AUDIO_EXPLAIN_ROLL, i)

    view.add_item(s5); view.add_item(s10); view.add_item(s15); view.add_item(s30); view.add_item(rexplain)
    await interaction.response.send_message(embed=e, view=view, ephemeral=True)

# ------------------------- Slash: /rally -------------------------

rally_group = app_commands.Group(name="rally", description="Create a Keep Rally or a Seat of Power Rally")
tree.add_command(rally_group)

class KeepForm(discord.ui.Modal, title="Keep Rally Details"):
    keep_power = discord.ui.TextInput(label="Power Level of Keep", placeholder="e.g., 200m, 350m", required=True, max_length=16)
    primary_troop = discord.ui.TextInput(label="Primary Troop Type", placeholder="Cavalry / Infantry / Range", required=True, max_length=16)
    keep_level = discord.ui.TextInput(label="Keep Level", placeholder="e.g., K30, K34", required=True, max_length=8)
    gear_worn = discord.ui.TextInput(label="What Gear is Worn", placeholder="Farming / Crafting / Attack / Defense", required=True, max_length=32)
    idle_time = discord.ui.TextInput(label="Idle Time", placeholder="e.g., 10 Minutes, 30 Minutes", required=True, max_length=32)
    scouted = discord.ui.TextInput(label="Scouted?", placeholder="Yes 20 minutes ago, No", required=True, max_length=64)

    def __init__(self, parent_interaction: discord.Interaction):
        super().__init__()
        self.parent_interaction = parent_interaction

    async def on_submit(self, interaction: discord.Interaction):
        # Create temp VC, thread, and post
        guild = interaction.guild
        channel = interaction.channel
        author: discord.Member = interaction.user  # type: ignore

        if not isinstance(channel, discord.TextChannel):
            return await interaction.response.send_message("Use this in a server text channel.", ephemeral=True)

        try:
            vc = await ensure_temp_vc(guild, author, "Keep Rally", 10)  # default limit ~10; roster defines real cap via size in form if needed
        except Exception as e:
            return await interaction.response.send_message(f"Couldn't create temp VC: {e}", ephemeral=True)

        invite_url = await create_or_refresh_vc_invite(vc)
        thread = await create_thread_for_rally(channel, "🧵 Keep Rally Thread", author)

        # Build rally object & post
        dummy_embed = discord.Embed(title="Creating rally...", description="Please wait.", color=discord.Color.blurple())
        posted = await channel.send(embed=dummy_embed)
        r = Rally(
            message_id=posted.id,
            guild_id=guild.id,
            channel_id=channel.id,
            creator_id=author.id,
            rally_kind="KEEP",
            keep_power=self.keep_power.value.strip(),
            primary_troop=self.primary_troop.value.strip().title(),  # type: ignore
            keep_level=self.keep_level.value.strip().upper(),
            gear_worn=self.gear_worn.value.strip(),
            idle_time=self.idle_time.value.strip(),
            scouted=self.scouted.value.strip(),
            temp_vc_id=vc.id,
            temp_vc_invite_url=invite_url,
            private_thread_id=thread.id,
            created_ts=asyncio.get_event_loop().time(),
        )
        r.participants[author.id] = Participant(author.id, "Cavalry", "T10", False, 0)  # host placeholder until they join formally
        RALLIES[posted.id] = r
        VC_TO_POST[vc.id] = posted.id

        await posted.edit(embed=embed_for_rally(guild, r), view=build_rally_view(r))
        await interaction.response.send_message(f"Keep Rally posted in {channel.mention}.", ephemeral=True)

        # schedule VC deletion if no one joins
        asyncio.create_task(schedule_delete_if_empty(guild.id, vc.id))

class SOPForm(discord.ui.Modal, title="Seat of Power Rally"):
    def __init__(self, parent_interaction: discord.Interaction):
        super().__init__()
        self.parent_interaction = parent_interaction

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
            message_id=dummy.id,
            guild_id=guild.id,
            channel_id=channel.id,
            creator_id=author.id,
            rally_kind="SOP",
            temp_vc_id=vc.id,
            temp_vc_invite_url=invite_url,
            private_thread_id=thread.id,
            created_ts=asyncio.get_event_loop().time(),
        )
        r.participants[author.id] = Participant(author.id, "Cavalry", "T10", False, 0)
        RALLIES[dummy.id] = r
        VC_TO_POST[vc.id] = dummy.id

        await dummy.edit(embed=embed_for_rally(guild, r), view=build_rally_view(r))
        await interaction.response.send_message(f"SOP Rally posted in {channel.mention}.", ephemeral=True)

        asyncio.create_task(schedule_delete_if_empty(guild.id, vc.id))

@rally_group.command(name="keep", description="Create a Keep Rally (form)")
async def rally_keep(interaction: discord.Interaction):
    await interaction.response.send_modal(KeepForm(interaction))

@rally_group.command(name="sop", description="Create a Seat of Power Rally")
async def rally_sop(interaction: discord.Interaction):
    await interaction.response.send_modal(SOPForm(interaction))

# ------------------------- VC Cleanup -------------------------

async def schedule_delete_if_empty(guild_id: int, vc_id: int):
    await asyncio.sleep(DELETE_VC_IF_EMPTY_AFTER_SECS)
    guild = bot.get_guild(guild_id)
    if not guild:
        return
    vc = guild.get_channel(vc_id)
    if isinstance(vc, discord.VoiceChannel):
        if len(vc.members) == 0:
            await delete_rally_for_vc(guild, vc, reason="VC was empty after grace period.")

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
    for ch in [before.channel, after.channel]:
        if not isinstance(ch, discord.VoiceChannel):
            continue
        if ch.id not in VC_TO_POST:
            continue
        if len(ch.members) == 0:
            await delete_rally_for_vc(ch.guild, ch, reason="Last user left VC.")

# ------------------------- Bot Lifecycle -------------------------

@bot.event
async def on_ready():
    try:
        await tree.sync()
        log.info("Slash commands synced.")
    except Exception as e:
        log.exception("Failed to sync commands: %s", e)
    log.info("Logged in as %s (%s)", bot.user, bot.user.id)

def main():
    bot.run(TOKEN, log_handler=None)

if __name__ == "__main__":
    main()
