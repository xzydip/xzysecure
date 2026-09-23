import asyncio
import logging

import discord
from discord.ext import commands
from discord import app_commands

import config

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s: %(name)s: %(message)s")
log = logging.getLogger("bot")

INTENTS = discord.Intents.default()
INTENTS.message_content = True
INTENTS.members = True
INTENTS.guilds = True

INITIAL_COGS = (
    "cogs.security",
    "cogs.backup",
)


class SecurityBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix=commands.when_mentioned, intents=INTENTS, help_command=None)
        self._guild_sync_done = set()


    @app_commands.command(name="ping", description="Check if the bot is online.")
    async def ping(self, interaction: discord.Interaction):
        await interaction.response.send_message(f"🏓 Pong! {round(self.latency * 1000)}ms")

    @app_commands.command(name="help", description="Show all available bot commands.")
    async def help_command(self, interaction: discord.Interaction):
        await interaction.response.send_message(
            "**🤖 Bot Commands**\n\n"
            "🔐 **Security**\n"
            "`/setunauthcmd` — unauthorized command prefix set\n"
            "`/setlogschanel` — security log channel set\n"
            "`/setunauthpm` — unauthorized bot punishment set\n\n"
            "💾 **Backup**\n"
            "`/backup create` — create backup\n"
            "`/backup list` — list backups\n"
            "`/backup import` — import backup\n"
"`/backup export` — export backup\n\n"
            "♻️ **Restore**\n"
            "`/back restore` — restore a selected backup",
            ephemeral=True,
        )

    async def setup_hook(self):
        for cog in INITIAL_COGS:
            try:
                await self.load_extension(cog)
                log.info("Loaded extension %s", cog)
            except Exception:
                log.exception("Failed to load extension %s", cog)

        # Register the command tree globally as a fallback. Guild sync below
        # makes commands appear immediately in every server the bot is in.
        try:
            synced = await self.tree.sync()
            log.info("Synced %d global application commands", len(synced))
        except Exception:
            log.exception("Failed to sync global application commands")

    async def on_ready(self):
        log.info("Logged in as %s (ID: %s)", self.user, self.user.id)

        # Discord can take time to propagate global commands. Copy the complete
        # tree into each guild and sync it there so commands appear immediately.
        for guild in self.guilds:
            try:
                self.tree.copy_global_to(guild=guild)
                synced = await self.tree.sync(guild=guild)
                log.info("Synced %d application commands to guild %s (%s)", len(synced), guild.name, guild.id)
            except Exception:
                log.exception("Failed to sync application commands to guild %s (%s)", guild.name, guild.id)

        await self.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.watching, name="Unauthorized cmds & Backups"
            )
        )


def main():
    if not config.DISCORD_TOKEN:
        raise SystemExit("DISCORD_TOKEN is not set. Copy .env.example to .env and add your bot token.")
    bot = SecurityBot()
    asyncio.run(bot.start(config.DISCORD_TOKEN))


if __name__ == "__main__":
    main()
