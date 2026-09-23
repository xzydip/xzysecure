import json
import asyncio
import os
import time
import discord
import aiohttp
import html
import re
from urllib.parse import urlparse
from discord import app_commands
from discord.ext import commands
from typing import List, Dict
import config


def is_admin():
    async def predicate(interaction: discord.Interaction) -> bool:
        return interaction.user.guild_permissions.administrator
    return app_commands.check(predicate)


def _overwrites_to_dict(overwrites: dict) -> list:
    result = []
    for target, ow in overwrites.items():
        allow, deny = ow.pair()
        result.append({
            "type": "role" if isinstance(target, discord.Role) else "member",
            "name": target.name,
            "id": target.id,
            "allow": allow.value,
            "deny": deny.value,
        })
    return result


class RestoreConfirmView(discord.ui.View):
    def __init__(self, cog: "Backup", interaction: discord.Interaction, name: str, data: dict):
        super().__init__(timeout=60)
        self.cog = cog
        self.owner_id = interaction.user.id
        self.guild_id = interaction.guild_id
        self.name = name
        self.data = data
        self.message = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "❌ Only the person who started this restore can use these buttons.",
                ephemeral=True,
            )
            return False
        return True

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.danger, emoji="✅")
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(view=self)

        try:
            await self.cog.apply_backup(interaction.guild, self.data, wipe_first=True)
            # The channel containing the confirmation panel may have been deleted.
            # If Discord still accepts the follow-up, report completion privately.
            try:
                await interaction.followup.send(
                    f"✅ **{self.name}** restore completed. Existing channels were deleted and the backup channels were recreated.",
                    ephemeral=True,
                )
            except discord.HTTPException:
                pass
        except Exception as exc:
            try:
                await interaction.followup.send(
                    f"❌ Restore failed: `{type(exc).__name__}: {exc}`",
                    ephemeral=True,
                )
            except discord.HTTPException:
                pass
        finally:
            self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, emoji="❌")
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(
            content="❌ Restore cancelled. No channels were deleted.",
            embed=None,
            view=self,
        )
        self.stop()

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        if self.message:
            try:
                await self.message.edit(content="⌛ Restore confirmation expired. No channels were deleted.", view=self)
            except discord.HTTPException:
                pass


class Backup(commands.Cog):
    """Serializes and restores server structure: roles, channels, categories, permissions."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def build_backup(self, guild: discord.Guild) -> dict:
        data = {
            "guild_name": guild.name,
            "created_at": time.time(),
            "roles": [],
            "categories": [],
            "text_channels": [],
            "voice_channels": [],
        }

        # Roles, lowest to highest, skip @everyone (handled separately)
        for role in sorted(guild.roles, key=lambda r: r.position):
            if role.is_default():
                continue
            if role.managed:
                continue  # bot/integration roles can't be recreated
            data["roles"].append({
                "name": role.name,
                "permissions": role.permissions.value,
                "color": role.color.value,
                "hoist": role.hoist,
                "mentionable": role.mentionable,
                "position": role.position,
            })

        for category in guild.categories:
            data["categories"].append({
                "name": category.name,
                "position": category.position,
                "overwrites": _overwrites_to_dict(category.overwrites),
            })

        for ch in guild.text_channels:
            data["text_channels"].append({
                "name": ch.name,
                "category": ch.category.name if ch.category else None,
                "topic": ch.topic,
                "position": ch.position,
                "nsfw": ch.nsfw,
                "slowmode_delay": ch.slowmode_delay,
                "overwrites": _overwrites_to_dict(ch.overwrites),
            })

        for ch in guild.voice_channels:
            data["voice_channels"].append({
                "name": ch.name,
                "category": ch.category.name if ch.category else None,
                "position": ch.position,
                "user_limit": ch.user_limit,
                "bitrate": ch.bitrate,
                "overwrites": _overwrites_to_dict(ch.overwrites),
            })

        return data

    async def apply_backup(self, guild: discord.Guild, data: dict, wipe_first: bool = False):
        if wipe_first:
            # Delete every existing server channel first, then recreate the
            # snapshot's categories/text/voice channels below.
            for ch in list(guild.channels):
                try:
                    await ch.delete(reason="Server restore: wipe existing channels")
                except (discord.Forbidden, discord.HTTPException):
                    pass

            # Delete recreatable roles too so the restored permission structure
            # does not get duplicated or mixed with the current server.
            for role in list(guild.roles):
                if role.is_default() or role.managed:
                    continue
                try:
                    await role.delete(reason="Server restore: wipe existing roles")
                except (discord.Forbidden, discord.HTTPException):
                    pass

        role_map: Dict[str, discord.Role] = {}
        for r in data.get("roles", []):
            try:
                new_role = await guild.create_role(
                    name=r["name"],
                    permissions=discord.Permissions(r["permissions"]),
                    color=discord.Color(r["color"]),
                    hoist=r["hoist"],
                    mentionable=r["mentionable"],
                    reason="Server restore",
                )
                role_map[r["name"]] = new_role
            except discord.HTTPException:
                continue

        def build_overwrites(entries):
            ow = {}
            for e in entries:
                target = None
                if e["type"] == "role":
                    target = role_map.get(e["name"]) or discord.utils.get(guild.roles, name=e["name"])
                if target is None:
                    continue
                ow[target] = discord.PermissionOverwrite.from_pair(
                    discord.Permissions(e["allow"]), discord.Permissions(e["deny"])
                )
            return ow

        category_map: Dict[str, discord.CategoryChannel] = {}
        for c in sorted(data.get("categories", []), key=lambda x: x["position"]):
            try:
                new_cat = await guild.create_category(
                    name=c["name"], overwrites=build_overwrites(c["overwrites"]), reason="Server restore"
                )
                category_map[c["name"]] = new_cat
            except discord.HTTPException:
                continue

        for ch in sorted(data.get("text_channels", []), key=lambda x: x["position"]):
            try:
                await guild.create_text_channel(
                    name=ch["name"],
                    category=category_map.get(ch["category"]) if ch["category"] else None,
                    topic=ch.get("topic"),
                    nsfw=ch.get("nsfw", False),
                    slowmode_delay=ch.get("slowmode_delay", 0),
                    overwrites=build_overwrites(ch["overwrites"]),
                    reason="Server restore",
                )
            except discord.HTTPException:
                continue

        for ch in sorted(data.get("voice_channels", []), key=lambda x: x["position"]):
            try:
                await guild.create_voice_channel(
                    name=ch["name"],
                    category=category_map.get(ch["category"]) if ch["category"] else None,
                    user_limit=ch.get("user_limit", 0),
                    bitrate=min(ch.get("bitrate", 64000), guild.bitrate_limit),
                    overwrites=build_overwrites(ch["overwrites"]),
                    reason="Server restore",
                )
            except discord.HTTPException:
                continue

    def _backup_path(self, guild_id: int, name: str) -> str:
        safe = "".join(c for c in name if c.isalnum() or c in ("-", "_")) or "backup"
        return os.path.join(config.BACKUP_DIR, f"{guild_id}_{safe}.json")

    backup_group = app_commands.Group(name="backup", description="Backup and restore the server structure")
    back_group = app_commands.Group(name="back", description="Restore a saved server backup")

    def _backup_names(self, guild_id: int) -> List[str]:
        prefix = f"{guild_id}_"
        if not os.path.isdir(config.BACKUP_DIR):
            return []
        return sorted(
            f[len(prefix):-5]
            for f in os.listdir(config.BACKUP_DIR)
            if f.startswith(prefix) and f.endswith(".json")
        )

    async def _backup_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> List[app_commands.Choice[str]]:
        current = current.lower().strip()
        names = self._backup_names(interaction.guild_id)
        return [
            app_commands.Choice(name=name, value=name)
            for name in names
            if not current or current in name.lower()
        ][:25]

    @backup_group.command(name="create", description="Create a backup of roles, channels and permissions")
    @is_admin()
    async def create(self, interaction: discord.Interaction, name: str = "manual"):
        await interaction.response.defer(ephemeral=True)
        data = await self.build_backup(interaction.guild)
        path = self._backup_path(interaction.guild_id, name)
        with open(path, "w") as f:
            json.dump(data, f, indent=2)
        await interaction.followup.send(
            f"✅ Backup **{name}** saved with {len(data['roles'])} roles, "
            f"{len(data['text_channels'])} text channels, {len(data['voice_channels'])} voice channels.",
            ephemeral=True,
        )

    @backup_group.command(name="import", description="Import a backup JSON from a MediaFire link")
    @is_admin()
    @app_commands.describe(link="Public MediaFire backup link", name="Name to save the imported backup as")
    async def import_backup(
        self,
        interaction: discord.Interaction,
        link: str,
        name: str = "imported",
    ):
        """Download a JSON backup from a public MediaFire page and save it locally."""
        await interaction.response.defer(ephemeral=True)

        def is_mediafire_url(value: str) -> bool:
            try:
                host = (urlparse(value).hostname or "").lower()
                return host == "mediafire.com" or host.endswith(".mediafire.com")
            except ValueError:
                return False

        if not is_mediafire_url(link):
            await interaction.followup.send(
                "❌ Please provide a valid public `mediafire.com` backup link.", ephemeral=True
            )
            return

        max_size = 10 * 1024 * 1024
        timeout = aiohttp.ClientTimeout(total=45)

        try:
            async with aiohttp.ClientSession(timeout=timeout, headers={
                "User-Agent": "Mozilla/5.0 (Discord Backup Bot)"
            }) as session:
                async with session.get(link, allow_redirects=True) as page_resp:
                    if page_resp.status != 200:
                        raise ValueError(f"MediaFire page returned HTTP {page_resp.status}")
                    page_html = await page_resp.text(errors="ignore")

                # MediaFire normally exposes the actual download URL in the
                # download button. This also handles HTML-escaped URLs.
                candidates = []
                patterns = [
                    r'id=["\']downloadButton["\'][^>]*href=["\']([^"\']+)',
                    r'href=["\']([^"\']+)["\'][^>]*id=["\']downloadButton["\']',
                    r'(?i)(https?://download[^"\'\s<>]+)',
                ]
                for pattern in patterns:
                    candidates.extend(re.findall(pattern, page_html))

                download_url = None
                for candidate in candidates:
                    candidate = html.unescape(candidate).replace("\\/", "/")
                    if candidate.startswith("//"):
                        candidate = "https:" + candidate
                    if candidate.startswith("http://") or candidate.startswith("https://"):
                        download_url = candidate
                        break

                if not download_url:
                    raise ValueError(
                        "Could not find the MediaFire download link. Make sure the link is public and points to a file."
                    )

                async with session.get(download_url, allow_redirects=True) as file_resp:
                    if file_resp.status != 200:
                        raise ValueError(f"Backup download returned HTTP {file_resp.status}")
                    content_length = file_resp.headers.get("Content-Length")
                    if content_length and int(content_length) > max_size:
                        raise ValueError("Backup file is larger than 10 MB.")

                    raw = bytearray()
                    async for chunk in file_resp.content.iter_chunked(64 * 1024):
                        raw.extend(chunk)
                        if len(raw) > max_size:
                            raise ValueError("Backup file is larger than 10 MB.")

            data = json.loads(bytes(raw).decode("utf-8"))

            required = ("roles", "categories", "text_channels", "voice_channels")
            if not isinstance(data, dict) or any(key not in data for key in required):
                raise ValueError("This file is not a valid server backup.")
            for key in required:
                if not isinstance(data[key], list):
                    raise ValueError(f"Backup field `{key}` must be a list.")

            path = self._backup_path(interaction.guild_id, name)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)

            await interaction.followup.send(
                f"✅ Backup **{name}** downloaded from MediaFire and added to the backup list. "
                f"Use `/back restore` to restore it.",
                ephemeral=True,
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            await interaction.followup.send(f"❌ Import failed: `{exc}`", ephemeral=True)
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            await interaction.followup.send(f"❌ Could not download the MediaFire backup: `{exc}`", ephemeral=True)

    @backup_group.command(name="list", description="List saved backups for this server")
    @is_admin()
    async def list_backups(self, interaction: discord.Interaction):
        names = self._backup_names(interaction.guild_id)
        if not names:
            await interaction.response.send_message("No backups saved for this server yet.", ephemeral=True)
            return
        await interaction.response.send_message("Saved backups:\n- " + "\n- ".join(names), ephemeral=True)

    @back_group.command(name="restore", description="Select a backup and confirm before restoring the server")
    @is_admin()
    @app_commands.autocomplete(name=_backup_autocomplete)
    async def back_restore(self, interaction: discord.Interaction, name: str = "manual"):
        path = self._backup_path(interaction.guild_id, name)
        if not os.path.exists(path):
            await interaction.response.send_message(
                f"❌ No backup named **{name}** found. Start typing the backup name to see saved backups.",
                ephemeral=True,
            )
            return

        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            await interaction.response.send_message(
                f"❌ Could not load backup **{name}**: `{exc}`", ephemeral=True
            )
            return

        embed = discord.Embed(
            title="⚠️ Confirm Server Restore",
            description=(
                f"Backup: **{name}**\n\n"
                "**Warning:** Confirming will delete all existing server channels "
                "and recreate the channels from this backup.\n\n"
                "Press **Confirm** to continue or **Cancel** to stop."
            ),
            color=discord.Color.orange(),
        )
        embed.add_field(name="Roles", value=str(len(data.get("roles", []))))
        embed.add_field(name="Categories", value=str(len(data.get("categories", []))))
        embed.add_field(name="Text Channels", value=str(len(data.get("text_channels", []))))
        embed.add_field(name="Voice Channels", value=str(len(data.get("voice_channels", []))))

        view = RestoreConfirmView(self, interaction, name, data)
        await interaction.response.send_message(embed=embed, view=view)
        view.message = await interaction.original_response()

    @backup_group.command(name="export", description="Download a backup file as JSON")
    @is_admin()
    async def export(self, interaction: discord.Interaction, name: str = "manual"):
        path = self._backup_path(interaction.guild_id, name)
        if not os.path.exists(path):
            await interaction.response.send_message(f"No backup named **{name}** found.", ephemeral=True)
            return
        await interaction.response.send_message(file=discord.File(path), ephemeral=True)


async def setup(bot: commands.Bot):
    cog = Backup(bot)
    await bot.add_cog(cog)
    # Register both slash-command groups.
    bot.tree.add_command(cog.backup_group)
    bot.tree.add_command(cog.back_group)
