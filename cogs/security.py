import json
import os
import re
from datetime import timedelta
import asyncio

import discord
from discord import app_commands
from discord.ext import commands

SETTINGS_FILE = os.path.join(os.path.dirname(__file__), "unauthcmd_settings.json")


def load_settings():
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_settings(data):
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


from typing import Optional, List

def parse_duration(value: str) -> Optional[int]:
    """Return seconds for values such as 30s, 10m, 2h, 7d."""
    match = re.fullmatch(r"\s*(\d+)\s*([smhd])\s*", value.lower())
    if not match:
        return None
    amount = int(match.group(1))
    unit = match.group(2)
    multiplier = {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]
    seconds = amount * multiplier
    if seconds < 1 or seconds > 28 * 86400:
        return None
    return seconds


class Security(commands.Cog):
    """Unauthorized prefix protection and punishment for users who add bots."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.settings = load_settings()
        self._unauth_cmd_bots = set()
        self._pending_verifications = {}

    def get_guild_settings(self, guild_id: int) -> dict:
        value = self.settings.get(str(guild_id), {})
        # Keep compatibility with older settings files that stored only a prefix.
        if isinstance(value, str):
            value = {"prefix": value}
        return value

    def get_prefix(self, guild_id: int) -> str:
        return self.get_guild_settings(guild_id).get("prefix", "!")

    async def send_log(self, guild: discord.Guild, message: str):
        settings = self.get_guild_settings(guild.id)
        channel_id = settings.get("logs_channel_id")
        if not channel_id:
            return
        channel = guild.get_channel(int(channel_id))
        if not channel:
            return
        try:
            await channel.send(message)
        except (discord.Forbidden, discord.HTTPException, ValueError):
            pass

    @app_commands.command(name="setunauthcmd", description="Set the prefix that other bots are not allowed to use.")
    @app_commands.describe(prefix="Prefix to block from other bots, e.g. !")
    @app_commands.checks.has_permissions(administrator=True)
    async def setunauthcmd(self, interaction: discord.Interaction, prefix: str = "!"):
        prefix = prefix.strip()
        if not prefix or len(prefix) > 5 or any(ch.isspace() for ch in prefix):
            await interaction.response.send_message(
                "❌ Invalid prefix. Use 1-5 non-space characters, for example `!`.",
                ephemeral=True,
            )
            return

        data = self.get_guild_settings(interaction.guild_id)
        data["prefix"] = prefix
        self.settings[str(interaction.guild_id)] = data
        save_settings(self.settings)
        await interaction.response.send_message(
            f"✅ Unauthorized cmd prefix set to `{prefix}`.\n"
            f"Any other bot using a command starting with `{prefix}` will receive `Unauthorized cmd [{prefix}]` and be kicked.",
            ephemeral=True,
        )

    @app_commands.command(
        name="setlogschanel",
        description="Set the channel where unauthorized bot/security logs are sent.",
    )
    @app_commands.describe(channel="Channel where security logs will be sent")
    @app_commands.checks.has_permissions(administrator=True)
    async def setlogschanel(self, interaction: discord.Interaction, channel: discord.TextChannel):
        data = self.get_guild_settings(interaction.guild_id)
        data["logs_channel_id"] = channel.id
        self.settings[str(interaction.guild_id)] = data
        save_settings(self.settings)
        await interaction.response.send_message(
            f"✅ Security logs channel set to {channel.mention}.", ephemeral=True
        )

    @setlogschanel.error
    async def setlogschanel_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.MissingPermissions):
            if interaction.response.is_done():
                await interaction.followup.send("❌ Administrator permission required.", ephemeral=True)
            else:
                await interaction.response.send_message("❌ Administrator permission required.", ephemeral=True)

    @app_commands.command(
        name="setunauthpm",
        description="Punish the user who adds an unauthorized bot, with an expiring timeout.",
    )
    @app_commands.describe(
        punishment="Punishment type (currently timeout)",
        duration="Timeout duration: e.g. 30m, 2h, 7d (max 28d)",
    )
    @app_commands.choices(punishment=[app_commands.Choice(name="Timeout", value="timeout")])
    @app_commands.checks.has_permissions(administrator=True)
    async def setunauthpm(
        self,
        interaction: discord.Interaction,
        punishment: app_commands.Choice[str],
        duration: str,
    ):
        seconds = parse_duration(duration)
        if seconds is None:
            await interaction.response.send_message(
                "❌ Invalid duration. Use formats like `30m`, `2h`, or `7d` (maximum 28 days).",
                ephemeral=True,
            )
            return

        data = self.get_guild_settings(interaction.guild_id)
        data["punishment"] = punishment.value
        data["punishment_seconds"] = seconds
        data["punishment_duration"] = duration.strip().lower()
        self.settings[str(interaction.guild_id)] = data
        save_settings(self.settings)

        await interaction.response.send_message(
            f"✅ Unauthorized bot punishment set to **Timeout** for **{duration.strip()}**.\n"
            "When an unauthorized bot is added, the user who added it will be timed out for that duration.",
            ephemeral=True,
        )

    @setunauthcmd.error
    async def setunauthcmd_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.MissingPermissions):
            if interaction.response.is_done():
                await interaction.followup.send("❌ Administrator permission required.", ephemeral=True)
            else:
                await interaction.response.send_message("❌ Administrator permission required.", ephemeral=True)

    @setunauthpm.error
    async def setunauthpm_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.MissingPermissions):
            if interaction.response.is_done():
                await interaction.followup.send("❌ Administrator permission required.", ephemeral=True)
            else:
                await interaction.response.send_message("❌ Administrator permission required.", ephemeral=True)

    async def find_bot_adder(self, guild: discord.Guild, bot_member: discord.Member):
        try:
            async for entry in guild.audit_logs(limit=15, action=discord.AuditLogAction.bot_add):
                if entry.target and getattr(entry.target, "id", None) == bot_member.id:
                    executor = entry.user
                    if not isinstance(executor, discord.Member):
                        executor = guild.get_member(executor.id)
                    return executor
        except (discord.Forbidden, discord.HTTPException):
            pass
        return None

    async def punish_bot_adder(self, guild: discord.Guild, bot_member: discord.Member, executor=None):
        settings = self.get_guild_settings(guild.id)
        if settings.get("punishment") != "timeout":
            return

        seconds = int(settings.get("punishment_seconds", 0))
        if seconds <= 0:
            return

        if not executor or executor.bot or executor.id == self.bot.user.id:
            return
        if not executor.guild_permissions.moderate_members:
            return
        try:
            until = discord.utils.utcnow() + timedelta(seconds=seconds)
            await executor.timeout(
                until,
                reason=f"Unauthorized bot added: {bot_member} (ID: {bot_member.id})",
            )
        except (discord.Forbidden, discord.HTTPException):
            return

    def get_verification_channel(self, guild: discord.Guild):
        settings = self.get_guild_settings(guild.id)
        channel_id = settings.get("logs_channel_id")
        if channel_id:
            channel = guild.get_channel(int(channel_id))
            if isinstance(channel, discord.TextChannel):
                return channel

        # Fallback: first text channel where the security bot can send messages.
        me = guild.me
        for channel in guild.text_channels:
            if me is None:
                break
            perms = channel.permissions_for(me)
            if perms.view_channel and perms.send_messages:
                return channel
        return None

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        if not member.bot or member.id == self.bot.user.id:
            return

        executor = await self.find_bot_adder(member.guild, member)
        if executor and not executor.bot and executor.id != self.bot.user.id:
            self._pending_verifications[member.id] = executor.id
            await self.send_log(
                member.guild,
                f"🤖 **Bot Added**\n"
                f"• Bot Username: **{member}**\n"
                f"• Added By: **{executor}**\n"
                f"• Status: **Verification started**"
            )

        # Active verification: send an unauthorized prefix command to a channel
        # the security bot can use. If the new bot answers/uses the configured
        # prefix, on_message marks it Unverified. If nothing is detected during
        # the window, it is marked Verified.
        channel = self.get_verification_channel(member.guild)
        prefix = self.get_prefix(member.guild.id)
        if channel is not None:
            try:
                await channel.send(f"{prefix}help")
            except (discord.Forbidden, discord.HTTPException):
                pass

        await asyncio.sleep(30)
        self._pending_verifications.pop(member.id, None)
        if member.id not in getattr(self, "_unauth_cmd_bots", set()):
            # The bot may have been removed during verification. Do not claim a
            # verification result for a bot that is no longer in the guild.
            if member.guild.get_member(member.id) is None:
                return
            embed = discord.Embed(
                title="[✅ Verified Bot]",
                description=f"Bot **{member}** passed the unauthorized-command check.",
            )
            await self.send_log_embed(member.guild, embed)

    async def send_log_embed(self, guild: discord.Guild, embed: discord.Embed):
        settings = self.get_guild_settings(guild.id)
        channel_id = settings.get("logs_channel_id")
        if not channel_id:
            return
        channel = guild.get_channel(int(channel_id))
        if not channel:
            return
        try:
            await channel.send(embed=embed)
        except (discord.Forbidden, discord.HTTPException, ValueError):
            pass

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        # Only inspect messages sent by OTHER bots in a guild.
        if (
            message.guild is None
            or not message.author.bot
            or self.bot.user is None
            or message.author.id == self.bot.user.id
        ):
            return

        prefix = self.get_prefix(message.guild.id)
        content = (message.content or "").lstrip()

        # This protection is for PREFIX commands/messages. Discord does not
        # expose another bot's slash-command interaction to this bot.
        if not content.startswith(prefix):
            return

        self._unauth_cmd_bots.add(message.author.id)

        # The bot failed verification. Punish the user who added it, then
        # immediately remove the unverified bot.
        adder_id = self._pending_verifications.pop(message.author.id, None)
        adder = message.guild.get_member(adder_id) if adder_id else None
        if adder is not None and not adder.bot and adder.id != self.bot.user.id:
            await self.punish_bot_adder(message.guild, message.guild.get_member(message.author.id), adder)

        # Tell the channel immediately.
        try:
            await message.channel.send(f"Unauthorized cmd [{prefix}]")
        except (discord.Forbidden, discord.HTTPException):
            pass

        kick_ok = False
        kick_error = ""
        try:
            # Re-fetch the member so we have the current guild member object.
            member = message.guild.get_member(message.author.id)
            if member is None:
                member = await message.guild.fetch_member(message.author.id)

            # The security bot needs Kick Members permission and its role must
            # be higher than the offending bot's highest role.
            me = message.guild.me
            if me is None or not me.guild_permissions.kick_members:
                kick_error = "Security bot is missing Kick Members permission."
            elif member.top_role >= me.top_role:
                kick_error = "Offending bot role is equal/higher than security bot role."
            else:
                await member.kick(reason=f"Unauthorized command: {content[:100]}")
                kick_ok = True
        except discord.NotFound:
            # It was already removed. Treat that as successful enforcement.
            kick_ok = True
        except discord.Forbidden:
            kick_error = "Discord denied the kick (permission/role hierarchy)."
        except discord.HTTPException as exc:
            kick_error = f"Discord API error: {exc}"
        except Exception as exc:
            kick_error = f"Unexpected error: {exc}"

        command_text = content.split()[0] if content.split() else prefix
        await self.send_log(
            message.guild,
            f"🚨 **Unauthorized Command Detected**\n"
            f"• Bot Username: **{message.author}**\n"
            f"• Channel: **{message.channel.name}**\n"
            f"• Command: **{command_text}**\n"
            f"• Bot Status: **{'Success' if kick_ok else 'Failed'}**\n"
            f"• User Status: **{'Success-kicked' if kick_ok else 'Failed-kick'}**"
            + (f"\n• Reason: **{kick_error}**" if kick_error else ""),
        )

        bot_status = "Success — bot kicked" if kick_ok else "Failed / Permission denied"
        user_status = "Success-kicked" if kick_ok else "Failed-kick"

        embed = discord.Embed(
            title="[❌ Unverified bot]",
            description=f"Bot **{message.author}** used the configured unauthorized command prefix.",
        )
        await self.send_log_embed(message.guild, embed)

        await self.send_log(
            message.guild,
            f"🚨 **Unauthorized bot command used**\n"
            f"• Bot Username: **{message.author}**\n"
            f"• Command Used By: **{message.author}**\n"
            f"• Channel: {message.channel.mention}\n"
            f"• Command: `{message.content[:500]}`\n"
            f"• Bot Status: **{bot_status}**\n"
            f"• User Status: **{user_status}**",
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(Security(bot))
