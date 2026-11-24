import io
import os
import sqlite3
import threading
from pathlib import Path
from datetime import timedelta, datetime, timezone
from dataclasses import dataclass, field
import discord
from discord import app_commands
from discord.ext import commands
from flask import Flask, send_from_directory, jsonify

BASE_DIR = Path(__file__).resolve().parent

# Banner & Purple Streaming Icon URLs
BANNER_IMAGE_URL = "https://images-ext-1.discordapp.net/external/bkYRRSFTzNt2_ZrvHFbQppLmqod4FLB2SUv8CPhk7dE/%3Fsize%3D1024/https/cdn.discordapp.com/banners/1542941315933667348/2d08ded8089f6635b6761501eaf96ac3.webp?format=webp"
PURPLE_STREAMING_ICON_URL = "https://images-ext-1.discordapp.net/external/lCuS8BCgTx74gRORUulzqG5aTxEuUmNujv26QzCRwWw/https/cdn.discordapp.com/icons/1542941315933667348/65941658abfc7796e785d9c1c541549e.webp?format=webp"
STREAM_URL = "https://www.twitch.tv/discord"
START_TICKET_COUNT = 264

# Known Custom Emojis with IDs
EMOJI_STUFF_TO_STUFF = discord.PartialEmoji.from_str("<:130341stock:1574761311085002825>")
EMOJI_MONEY_TO_STUFF = discord.PartialEmoji.from_str("<:786534money:1574761162921476137>")

# Optional Default Configuration IDs (can also be updated live with $setconfig)
CONFIG: dict[str, int | None] = {
    "category_id": None,
    "middleman_role_id": None,
    "log_channel_id": None,
}

DB_PATH = str(BASE_DIR / "middleman_data.db")


def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS vouches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                middleman_id INTEGER NOT NULL,
                voter_id INTEGER NOT NULL,
                note TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS bot_config (
                key TEXT PRIMARY KEY,
                value INTEGER
            )
            """
        )
        for row in conn.execute("SELECT key, value FROM bot_config"):
            if row[0] in CONFIG:
                CONFIG[row[0]] = row[1]

        cur = conn.execute("SELECT value FROM bot_config WHERE key = 'ticket_count'")
        existing = cur.fetchone()
        if existing is None or existing[0] is None:
            conn.execute(
                "INSERT OR REPLACE INTO bot_config (key, value) VALUES ('ticket_count', ?)",
                (START_TICKET_COUNT,),
            )


def get_ticket_count() -> int:
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute("SELECT value FROM bot_config WHERE key = 'ticket_count'")
        row = cur.fetchone()
        if row is None or row[0] is None:
            return START_TICKET_COUNT
        return max(START_TICKET_COUNT, int(row[0]))


def increment_ticket_count() -> int:
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute("SELECT value FROM bot_config WHERE key = 'ticket_count'")
        row = cur.fetchone()
        current = START_TICKET_COUNT if (row is None or row[0] is None) else int(row[0])
        if current < START_TICKET_COUNT:
            current = START_TICKET_COUNT
        next_count = current + 1
        conn.execute(
            "INSERT OR REPLACE INTO bot_config (key, value) VALUES ('ticket_count', ?)",
            (next_count,),
        )
        return next_count


def save_config_value(key: str, value: int | None):
    CONFIG[key] = value
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO bot_config (key, value) VALUES (?, ?)",
            (key, value),
        )


def add_vouch(middleman_id: int, voter_id: int, note: str) -> int:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT INTO vouches (middleman_id, voter_id, note, created_at) VALUES (?, ?, ?, ?)",
            (middleman_id, voter_id, note, datetime.now(timezone.utc).isoformat()),
        )
        cur = conn.execute(
            "SELECT COUNT(*) FROM vouches WHERE middleman_id = ?",
            (middleman_id,),
        )
        return cur.fetchone()[0]


def get_vouch_count(middleman_id: int) -> int:
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute(
            "SELECT COUNT(*) FROM vouches WHERE middleman_id = ?",
            (middleman_id,),
        )
        return cur.fetchone()[0]


def get_total_vouches() -> int:
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute("SELECT COUNT(*) FROM vouches")
        return cur.fetchone()[0]


def resolve_emoji(
    bot_instance: commands.Bot,
    emoji_name: str,
    fallback: str | discord.PartialEmoji,
) -> discord.Emoji | discord.PartialEmoji | str:
    """Finds a custom emoji by name across all servers the bot is in, or returns fallback."""
    clean_name = emoji_name.strip(":")
    found = discord.utils.get(bot_instance.emojis, name=clean_name)
    if found is not None:
        return found
    return fallback


@dataclass
class TradeSession:
    channel_id: int
    creator_id: int
    trade_type: str  # "stuff_to_stuff" | "money_to_stuff"
    partner_info: str
    side_a_offer: str
    side_b_offer: str
    notes: str
    claimed_by_id: int | None = None
    added_user_ids: list[int] = field(default_factory=list)
    fee_info: str = "None / Free"
    priority: str = "NORMAL"


ACTIVE_TRADES: dict[int, TradeSession] = {}

STUFF_TO_STUFF_HOW_IT_WORKS = (
    "### MIDDLE MAN WORKS FOR SAFETY\n"
    "### IM GONNA HOLD BOTH THE THINGS FOR YOUR SAFETY\n"
    "### ONCE I GOT BOTH THE THINGS, I WILL LEAVE 1 BY 1.\n"
    "### NOW PROVIDE VOUCHER SAYING THAT YOU ARE LEGIT AND TRUSTED\n\n\n"
    "### SAY (YES AGREE ) IF YOU WANT TO TAKE UP THE DEAL"
)

MONEY_TO_STUFF_HOW_IT_WORKS = (
    "### MIDDLE MAN WORKS FOR SAFETY ✅\n"
    "1. **Seller -> Middleman:** Seller hands over the pet/stuff to the middleman.\n"
    "2. **Middleman -> Chat:** Middleman posts a screenshot or confirms they have the pet/stuff.\n"
    "3. **Buyer -> Seller:** Buyer sends the payment directly to the seller.\n"
    "4. **Seller -> Chat:** Seller confirms the money has arrived in their account.\n"
    "5. **Middleman -> Buyer:** Middleman gives the pet/stuff to the buyer to finish the trade.\n\n"
    "### SAY (YES AGREE ) IF YOU WANT TO TAKE UP THE DEAL"
)


def is_middleman(member: discord.abc.User) -> bool:
    if not isinstance(member, discord.Member):
        return False
    if member.guild_permissions.administrator:
        return True
    mm_role_id = CONFIG.get("middleman_role_id")
    if mm_role_id is not None:
        return any(role.id == mm_role_id for role in member.roles)
    return True


def format_deal_description(session: TradeSession, guild: discord.Guild) -> str:
    creator = guild.get_member(session.creator_id)
    trader_1 = creator.mention if creator else f"<@{session.creator_id}>"
    if session.added_user_ids:
        trader_2 = ", ".join(f"<@{uid}>" for uid in session.added_user_ids)
    else:
        trader_2 = session.partner_info

    is_money = session.trade_type == "money_to_stuff"
    mode_label = "Money to Stuff" if is_money else "Stuff to Stuff"
    mm_mention = f"<@{session.claimed_by_id}>" if session.claimed_by_id else "Unclaimed"

    return (
        f"### 🤝 DEAL DETAILS ({mode_label})\n"
        f"- **Middleman:** {mm_mention}\n"
        f"- **Giving User (Trader 1):** {trader_1} — **{session.side_a_offer}**\n"
        f"- **Giving 2 (Trader 2):** {trader_2} — **{session.side_b_offer}**\n"
        f"- **Notes:** {session.notes or 'None'}"
    )


async def build_transcript_file(channel: discord.TextChannel) -> discord.File:
    lines = [
        f"Middleman Ticket Transcript — #{channel.name} (ID: {channel.id})",
        f"Exported at: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}",
        "=" * 60,
        "",
    ]
    async for msg in channel.history(limit=500, oldest_first=True):
        timestamp = msg.created_at.strftime("%Y-%m-%d %H:%M:%S")
        content = msg.content or "[Components V2 / Embed / Attachment]"
        lines.append(f"[{timestamp}] {msg.author} ({msg.author.id}): {content}")
        for att in msg.attachments:
            lines.append(f"    Attachment: {att.url}")

    buffer = io.BytesIO("\n".join(lines).encode("utf-8"))
    return discord.File(buffer, filename=f"transcript-{channel.name}.txt")


async def send_trade_log(
    guild: discord.Guild,
    channel: discord.TextChannel,
    session: TradeSession,
    closed_by: discord.abc.User,
):
    log_channel_id = CONFIG.get("log_channel_id")
    if log_channel_id is None:
        return
    log_channel = guild.get_channel(log_channel_id)
    if not isinstance(log_channel, discord.TextChannel):
        return

    mode_label = "Money to Stuff" if session.trade_type == "money_to_stuff" else "Stuff to Stuff"
    claimed_text = f"<@{session.claimed_by_id}>" if session.claimed_by_id else "Unclaimed"
    added_text = ", ".join(f"<@{uid}>" for uid in session.added_user_ids) or "None"

    log_view = discord.ui.LayoutView()
    section = discord.ui.Section(
        discord.ui.TextDisplay(
            f"## 📜 Middleman Trade Log — {mode_label}\n"
            f"**Ticket Channel:** `#{channel.name}`\n"
            f"**Priority:** `{session.priority}`\n"
            f"**Opened By (Trader 1):** <@{session.creator_id}>\n"
            f"**Partner Input:** {session.partner_info}\n"
            f"**Added Users (Trader 2):** {added_text}\n"
            f"**Claimed By:** {claimed_text}\n"
            f"**Closed By:** {closed_by.mention}\n"
            f"**Middleman Fee:** `{session.fee_info}`"
        ),
        accessory=discord.ui.Thumbnail(
            media=PURPLE_STREAMING_ICON_URL,
            description="Purple Streaming Icon",
        ),
    )
    details = discord.ui.TextDisplay(
        f"**Giving User (Trader 1):** {session.side_a_offer}\n"
        f"**Giving 2 (Trader 2):** {session.side_b_offer}\n"
        f"**Notes:** {session.notes or 'None'}"
    )
    container = discord.ui.Container(
        section,
        discord.ui.Separator(spacing=discord.SeparatorSpacing.small),
        details,
        accent_color=discord.Color.purple(),
    )
    log_view.add_item(container)

    transcript_file = await build_transcript_file(channel)
    await log_channel.send(view=log_view)
    await log_channel.send(file=transcript_file)


class TradeDetailsModal(discord.ui.Modal):
    def __init__(self, trade_type: str):
        is_money = trade_type == "money_to_stuff"
        super().__init__(
            title="Money to Stuff — Trade Details"
            if is_money
            else "Stuff to Stuff — Trade Details"
        )
        self.trade_type = trade_type

        self.partner_input = discord.ui.TextInput(
            label="Giving 2 / Trader 2 (Username or ID)",
            placeholder="e.g. @trader or 123456789012345678",
            required=True,
            max_length=100,
        )
        self.side_a_input = discord.ui.TextInput(
            label="Giving User (Trader 1) Offer",
            placeholder="e.g. $45 USD via PayPal F&F" if is_money else "e.g. 2x Limited Skins + 10k Gems",
            style=discord.TextStyle.paragraph,
            required=True,
            max_length=400,
        )
        self.side_b_input = discord.ui.TextInput(
            label="Giving 2 (Trader 2) Offer",
            placeholder="e.g. 1x Mythic Pet" if is_money else "e.g. Rare Pet / Collectible",
            style=discord.TextStyle.paragraph,
            required=True,
            max_length=400,
        )
        self.notes_input = discord.ui.TextInput(
            label="Extra Notes / Conditions",
            placeholder="Optional rules, game server, or payment notes",
            style=discord.TextStyle.short,
            required=False,
            max_length=200,
        )

        self.add_item(self.partner_input)
        self.add_item(self.side_a_input)
        self.add_item(self.side_b_input)
        self.add_item(self.notes_input)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        guild = interaction.guild
        if guild is None:
            await interaction.followup.send(
                "This ticket system can only be used inside a Discord server.",
                ephemeral=True,
            )
            return

        cat_id = CONFIG.get("category_id")
        category = guild.get_channel(cat_id) if cat_id else None
        if not isinstance(category, discord.CategoryChannel):
            category = None

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            interaction.user: discord.PermissionOverwrite(
                view_channel=True, send_messages=True, read_message_history=True
            ),
            guild.me: discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                manage_channels=True,
                manage_permissions=True,
                read_message_history=True,
            ),
        }

        mm_role_id = CONFIG.get("middleman_role_id")
        if mm_role_id:
            mm_role = guild.get_role(mm_role_id)
            if mm_role:
                overwrites[mm_role] = discord.PermissionOverwrite(
                    view_channel=True, send_messages=True, read_message_history=True
                )

        prefix = "mm-money" if self.trade_type == "money_to_stuff" else "mm-stuff"
        clean_user = "".join(
            ch for ch in interaction.user.name.lower() if ch.isalnum() or ch == "-"
        )[:15]
        channel_name = f"{prefix}-{clean_user or 'ticket'}"

        ticket_channel = await guild.create_text_channel(
            name=channel_name,
            category=category,
            overwrites=overwrites,
            reason=f"Middleman V2 ticket created by {interaction.user}",
        )

        session = TradeSession(
            channel_id=ticket_channel.id,
            creator_id=interaction.user.id,
            trade_type=self.trade_type,
            partner_info=self.partner_input.value.strip(),
            side_a_offer=self.side_a_input.value.strip(),
            side_b_offer=self.side_b_input.value.strip(),
            notes=self.notes_input.value.strip(),
        )
        ACTIVE_TRADES[ticket_channel.id] = session

        new_count = increment_ticket_count()
        if isinstance(interaction.client, MiddlemanBot):
            await interaction.client.update_streaming_presence(new_count)

        # First view inside private ticket: button only (no description text)
        await ticket_channel.send(view=ClaimOnlyTicketView(session, guild))
        await interaction.followup.send(
            f"Your middleman ticket (#{new_count}) is ready: {ticket_channel.mention}",
            ephemeral=True,
        )


class MiddlemanPanelLayoutView(discord.ui.LayoutView):
    """Components V2 Ticket Creation Panel with Banner, Purple Streaming Icon & Custom Emoji Buttons."""

    def __init__(self):
        super().__init__(timeout=None)

        header_section = discord.ui.Section(
            discord.ui.TextDisplay(
                "## 🛡️ Middleman Ticket Desk\n"
                "Click a button below to open a secure middleman ticket.\n\n"
                f"• {EMOJI_STUFF_TO_STUFF} **Stuff to Stuff:** Both sides deposit items to the middleman before release.\n"
                f"• {EMOJI_MONEY_TO_STUFF} **Money to Stuff:** Item custody & payment confirmation tracked step-by-step."
            ),
            accessory=discord.ui.Thumbnail(
                media=PURPLE_STREAMING_ICON_URL,
                description="Purple Streaming Icon",
            ),
        )

        banner_gallery = discord.ui.MediaGallery(
            discord.MediaGalleryItem(
                media=BANNER_IMAGE_URL,
                description="Middleman Ticket Banner",
            )
        )

        stuff_btn = discord.ui.Button(
            label="Stuff to Stuff",
            style=discord.ButtonStyle.primary,
            emoji=EMOJI_STUFF_TO_STUFF,
            custom_id="mm_v2_btn_stuff_to_stuff",
        )
        stuff_btn.callback = self.open_stuff_to_stuff

        money_btn = discord.ui.Button(
            label="Money to Stuff",
            style=discord.ButtonStyle.success,
            emoji=EMOJI_MONEY_TO_STUFF,
            custom_id="mm_v2_btn_money_to_stuff",
        )
        money_btn.callback = self.open_money_to_stuff

        buttons_row = discord.ui.ActionRow(stuff_btn, money_btn)

        container = discord.ui.Container(
            header_section,
            discord.ui.Separator(spacing=discord.SeparatorSpacing.small),
            banner_gallery,
            discord.ui.Separator(spacing=discord.SeparatorSpacing.small),
            buttons_row,
            accent_color=discord.Color.purple(),
        )
        self.add_item(container)

    async def open_stuff_to_stuff(self, interaction: discord.Interaction):
        await interaction.response.send_modal(TradeDetailsModal("stuff_to_stuff"))

    async def open_money_to_stuff(self, interaction: discord.Interaction):
        await interaction.response.send_modal(TradeDetailsModal("money_to_stuff"))


class ClaimOnlyTicketView(discord.ui.LayoutView):
    """Initial private ticket view showing ONLY the claim button (Middleman only)."""

    def __init__(self, session: TradeSession, guild: discord.Guild):
        super().__init__(timeout=None)
        self.session = session
        self.guild = guild

        claim_emoji = resolve_emoji(bot, "620845claimticket", "🤝")
        claim_btn = discord.ui.Button(
            label="Click Me to Claim",
            style=discord.ButtonStyle.primary,
            emoji=claim_emoji,
        )
        claim_btn.callback = self.claim_ticket_callback

        row = discord.ui.ActionRow(claim_btn)
        container = discord.ui.Container(row, accent_color=discord.Color.purple())
        self.add_item(container)

    async def claim_ticket_callback(self, interaction: discord.Interaction):
        if interaction.guild is None:
            return
        if not is_middleman(interaction.user):
            await interaction.response.send_message(
                "Only a Middleman or Administrator can claim this ticket.",
                ephemeral=True,
            )
            return

        self.session.claimed_by_id = interaction.user.id
        deal_desc = format_deal_description(self.session, interaction.guild)

        await interaction.response.edit_message(
            view=ClaimedTicketControlsView(self.session, interaction.guild)
        )

        # Post ONLY claim notice + Deal details on claim; Middleman Works only appears when "How It Work" is clicked
        if isinstance(interaction.channel, discord.TextChannel):
            await interaction.channel.send(
                f"🤝 Claimed by {interaction.user.mention}\n\n{deal_desc}"
            )


class ClaimedTicketControlsView(discord.ui.LayoutView):
    """Post-claim ticket panel with TWO buttons: How It Work and Close Ticket."""

    def __init__(self, session: TradeSession, guild: discord.Guild):
        super().__init__(timeout=None)
        self.session = session
        self.guild = guild

        is_money = session.trade_type == "money_to_stuff"
        mode_title = (
            f"{EMOJI_MONEY_TO_STUFF} Money to Stuff"
            if is_money
            else f"{EMOJI_STUFF_TO_STUFF} Stuff to Stuff"
        )

        top_section = discord.ui.Section(
            discord.ui.TextDisplay(
                f"## {mode_title} — Claimed Ticket\n"
                f"**Middleman:** <@{session.claimed_by_id}>\n"
                f"**Giving User (Trader 1):** <@{session.creator_id}>\n"
                f"**Giving 2 (Trader 2):** {', '.join(f'<@{u}>' for u in session.added_user_ids) or session.partner_info}"
            ),
            accessory=discord.ui.Thumbnail(
                media=PURPLE_STREAMING_ICON_URL,
                description="Purple Streaming Icon",
            ),
        )

        banner_gallery = discord.ui.MediaGallery(
            discord.MediaGalleryItem(
                media=BANNER_IMAGE_URL,
                description="Middleman Ticket Banner",
            )
        )

        how_btn = discord.ui.Button(
            label="How It Work",
            style=discord.ButtonStyle.secondary,
            emoji="📖",
        )
        how_btn.callback = self.how_it_work_callback

        close_emoji = resolve_emoji(bot, "dynoError", "🔒")
        close_btn = discord.ui.Button(
            label="Close Ticket",
            style=discord.ButtonStyle.danger,
            emoji=close_emoji,
        )
        close_btn.callback = self.close_ticket_callback

        controls_row = discord.ui.ActionRow(how_btn, close_btn)

        container = discord.ui.Container(
            top_section,
            discord.ui.Separator(spacing=discord.SeparatorSpacing.small),
            banner_gallery,
            discord.ui.Separator(spacing=discord.SeparatorSpacing.small),
            controls_row,
            accent_color=discord.Color.purple(),
        )
        self.add_item(container)

    async def how_it_work_callback(self, interaction: discord.Interaction):
        how_it_works = (
            MONEY_TO_STUFF_HOW_IT_WORKS
            if self.session.trade_type == "money_to_stuff"
            else STUFF_TO_STUFF_HOW_IT_WORKS
        )
        await interaction.response.send_message(how_it_works)

    async def close_ticket_callback(self, interaction: discord.Interaction):
        if interaction.guild and isinstance(interaction.channel, discord.TextChannel):
            await send_trade_log(
                interaction.guild, interaction.channel, self.session, interaction.user
            )
        ACTIVE_TRADES.pop(self.session.channel_id, None)
        await interaction.response.send_message("Closing ticket in 3 seconds...")
        await discord.utils.sleep_until(discord.utils.utcnow() + timedelta(seconds=3))
        if isinstance(interaction.channel, discord.TextChannel):
            await interaction.channel.delete(reason=f"Ticket closed by {interaction.user}")


class MiddlemanBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.members = True
        intents.message_content = True
        initial_activity = discord.Streaming(
            name=f"Tickets Started: {START_TICKET_COUNT}",
            url=STREAM_URL,
        )
        super().__init__(
            command_prefix="$",
            intents=intents,
            activity=initial_activity,
        )

    async def update_streaming_presence(self, count: int | None = None):
        current_count = get_ticket_count() if count is None else count
        streaming_activity = discord.Streaming(
            name=f"Tickets Started: {current_count}",
            url=STREAM_URL,
        )
        await self.change_presence(status=discord.Status.online, activity=streaming_activity)

    async def setup_hook(self):
        init_db()
        self.add_view(MiddlemanPanelLayoutView())
        await self.tree.sync()

    async def on_ready(self):
        await self.update_streaming_presence()
        print(
            f"ONLINE & STREAMING AS: {self.user} (ID: {self.user.id}) | Tickets Started: {get_ticket_count()}"
        )

    async def on_command_error(self, ctx: commands.Context, error: commands.CommandError):
        print(f"Command error in '{ctx.command}': {error}")
        if isinstance(error, commands.MissingPermissions):
            await ctx.send("You need **Administrator** permission to use this command.")
        elif isinstance(error, commands.MemberNotFound):
            await ctx.send("User not found. Please mention them with `@user` or enter a valid User ID.")
        elif isinstance(error, commands.MissingRequiredArgument):
            await ctx.send("Missing required argument. Type `$mmhelp` to see command usage.")


bot = MiddlemanBot()

# --- Flask Web Server ---
app = Flask(__name__, static_folder=str(BASE_DIR))


@app.route("/")
def serve_index():
    return send_from_directory(str(BASE_DIR), "index.html")


@app.route("/api/stats")
def api_stats():
    init_db()
    return jsonify(
        {
            "bot_name": str(bot.user) if bot.user else "Vortex Exchange Bot Middleman",
            "status": "STREAMING",
            "tickets_started": get_ticket_count(),
            "active_tickets": len(ACTIVE_TRADES),
            "total_vouches": get_total_vouches(),
        }
    )


def run_website():
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)


# --- Bot Commands ---
@bot.command(name="mmhelp")
async def mmhelp_prefix(ctx: commands.Context):
    """Display the guide of all middleman commands."""
    help_view = discord.ui.LayoutView()
    section = discord.ui.Section(
        discord.ui.TextDisplay(
            "## 📖 Middleman Ticket System — Command Guide\n"
            "**Panel & Setup Commands (Admin)**\n"
            "• `$mmpanel` or `/mm-panel` — Post the Middleman Ticket Desk panel\n"
            "• `$setconfig <category|role|logs> <id>` — Configure category, MM role, or log channel\n\n"
            "**Inside Ticket Commands**\n"
            "• `$adduser @user` — Grant your trade partner access to the ticket\n"
            "• `$removeuser @user` — Remove a user's access from the ticket\n"
            "• `$setfee <fee text>` — Set the middleman fee inside the ticket\n"
            "• `$priority <low|normal|high>` — Update the ticket priority badge\n"
            "• `$rename <new-name>` — Rename the current ticket channel\n"
            "• `$slowmode <seconds>` — Set channel slowmode (`0` to disable)\n\n"
            "**Reputation Commands**\n"
            "• `$vouch @middleman [comment]` — Leave a vouch after a completed trade\n"
            "• `$vouches [@middleman]` — Check a middleman's total vouch count"
        ),
        accessory=discord.ui.Thumbnail(
            media=PURPLE_STREAMING_ICON_URL,
            description="Purple Streaming Icon",
        ),
    )
    container = discord.ui.Container(section, accent_color=discord.Color.purple())
    help_view.add_item(container)
    await ctx.send(view=help_view)


@bot.command(name="mmpanel")
@commands.has_permissions(administrator=True)
async def mmpanel_prefix(ctx: commands.Context):
    """Send the Components V2 Middleman Ticket panel via $mmpanel."""
    await ctx.send(view=MiddlemanPanelLayoutView())


@bot.command(name="setconfig")
@commands.has_permissions(administrator=True)
async def setconfig_prefix(ctx: commands.Context, setting: str, target_id: int):
    """Configure category, role, or logs channel ID via $setconfig."""
    mapping = {
        "category": "category_id",
        "role": "middleman_role_id",
        "logs": "log_channel_id",
    }
    key = mapping.get(setting.lower())
    if key is None:
        await ctx.send("Invalid option. Use `category`, `role`, or `logs`. Example: `$setconfig logs 123456789012345678`")
        return

    save_config_value(key, target_id)
    await ctx.send(f"✅ Updated `{setting.lower()}` ID to `{target_id}`.")


@bot.command(name="adduser")
async def adduser_prefix(ctx: commands.Context, member: discord.Member):
    """Grant a trade partner access to the current middleman ticket channel via $adduser @user."""
    if ctx.guild is None or not isinstance(ctx.channel, discord.TextChannel):
        await ctx.send("You can only use `$adduser` inside a server channel.")
        return

    session = ACTIVE_TRADES.get(ctx.channel.id)
    if session is None and not ctx.channel.name.startswith(("mm-stuff-", "mm-money-")):
        await ctx.send("`$adduser` can only be used inside a middleman ticket channel.")
        return

    can_add = (
        is_middleman(ctx.author)
        or (session is not None and ctx.author.id == session.creator_id)
    )
    if not can_add:
        await ctx.send("Only the ticket creator or a middleman can add users here.")
        return

    await ctx.channel.set_permissions(
        member,
        view_channel=True,
        send_messages=True,
        read_message_history=True,
        reason=f"Added to middleman ticket by {ctx.author}",
    )

    if session is not None and member.id not in session.added_user_ids:
        session.added_user_ids.append(member.id)

    await ctx.send(
        f"✅ Added {member.mention} to this ticket channel! Please review the middleman rules and say **YES AGREE** to take up the deal."
    )


@bot.command(name="removeuser")
async def removeuser_prefix(ctx: commands.Context, member: discord.Member):
    """Remove a user's access from the current middleman ticket channel via $removeuser @user."""
    if ctx.guild is None or not isinstance(ctx.channel, discord.TextChannel):
        await ctx.send("You can only use `$removeuser` inside a server channel.")
        return

    session = ACTIVE_TRADES.get(ctx.channel.id)
    can_remove = (
        is_middleman(ctx.author)
        or (session is not None and ctx.author.id == session.creator_id)
    )
    if not can_remove:
        await ctx.send("Only the ticket creator or a middleman can remove users here.")
        return

    await ctx.channel.set_permissions(
        member,
        overwrite=None,
        reason=f"Removed from middleman ticket by {ctx.author}",
    )

    if session is not None and member.id in session.added_user_ids:
        session.added_user_ids.remove(member.id)

    await ctx.send(f"🗑️ Removed {member.mention}'s access from this ticket channel.")


@bot.command(name="setfee")
async def setfee_prefix(ctx: commands.Context, *, fee_text: str):
    """Set the middleman fee text for the active ticket via $setfee."""
    if not is_middleman(ctx.author):
        await ctx.send("Only a Middleman or Administrator can set the ticket fee.")
        return
    session = ACTIVE_TRADES.get(ctx.channel.id)
    if session is None:
        await ctx.send("No active middleman ticket session found in this channel.")
        return
    session.fee_info = fee_text[:100]
    await ctx.send(f"💰 Middleman fee updated to: **{session.fee_info}**")


@bot.command(name="priority")
async def priority_prefix(ctx: commands.Context, level: str):
    """Set ticket priority (low, normal, high) via $priority."""
    if not is_middleman(ctx.author):
        await ctx.send("Only a Middleman or Administrator can change ticket priority.")
        return
    session = ACTIVE_TRADES.get(ctx.channel.id)
    if session is None:
        await ctx.send("No active middleman ticket session found in this channel.")
        return
    normalized = level.upper()
    if normalized not in {"LOW", "NORMAL", "HIGH"}:
        await ctx.send("Please choose `low`, `normal`, or `high`.")
        return
    session.priority = normalized
    await ctx.send(f"🚩 Ticket priority set to **{normalized}**.")


@bot.command(name="rename")
async def rename_prefix(ctx: commands.Context, *, new_name: str):
    """Rename the current ticket channel via $rename."""
    if not isinstance(ctx.channel, discord.TextChannel) or not is_middleman(ctx.author):
        await ctx.send("Only a Middleman or Administrator can rename ticket channels.")
        return
    cleaned = "-".join(new_name.lower().split())[:50]
    await ctx.channel.edit(name=cleaned, reason=f"Renamed by {ctx.author}")
    await ctx.send(f"✏️ Channel renamed to **#{cleaned}**.")


@bot.command(name="slowmode")
async def slowmode_prefix(ctx: commands.Context, seconds: int):
    """Set slowmode in the current ticket channel via $slowmode <seconds>."""
    if not isinstance(ctx.channel, discord.TextChannel) or not is_middleman(ctx.author):
        await ctx.send("Only a Middleman or Administrator can set slowmode.")
        return
    clamped = max(0, min(seconds, 21600))
    await ctx.channel.edit(slowmode_delay=clamped)
    await ctx.send(f"⏱️ Channel slowmode set to **{clamped} seconds**.")


@bot.command(name="vouch")
async def vouch_prefix(ctx: commands.Context, middleman: discord.Member, *, note: str = "Legit and trusted middleman!"):
    """Vouch for a middleman after a completed trade via $vouch @middleman [note]."""
    if middleman.id == ctx.author.id or middleman.bot:
        await ctx.send("You cannot vouch for yourself or a bot.")
        return
    total = add_vouch(middleman.id, ctx.author.id, note[:200])
    await ctx.send(
        f"⭐ {ctx.author.mention} vouched for {middleman.mention}: *\"{note[:200]}\"* (Total Vouches: **{total}**)"
    )


@bot.command(name="vouches")
async def vouches_prefix(ctx: commands.Context, member: discord.Member | None = None):
    """Check vouch count for a middleman via $vouches [@user]."""
    target = member or ctx.author
    total = get_vouch_count(target.id)
    await ctx.send(f"🏅 **{target.display_name}** currently has **{total}** middleman vouches.")


@bot.tree.command(
    name="mm-panel",
    description="Send the Components V2 Middleman Ticket panel",
)
@app_commands.default_permissions(administrator=True)
async def mm_panel_slash(interaction: discord.Interaction):
    """Send the Components V2 Middleman Ticket panel via /mm-panel."""
    await interaction.response.send_message(view=MiddlemanPanelLayoutView())


if __name__ == "__main__":
    BOT_TOKEN = "PASTE_YOUR_BOT_TOKEN_HERE"
    threading.Thread(target=run_website, daemon=True).start()
    bot.run(BOT_TOKEN)
