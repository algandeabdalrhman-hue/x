import asyncio
import aiohttp
import discord
from discord.ext import commands
import os
from dotenv import load_dotenv
import re
import requests

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
ITEM_DROP_CHANNEL_ID = 1524527819982377103
SERVER_ID = 1524521375073697912
ANTICAPTCHA_KEY = os.getenv("ANTICAPTCHA_KEY", "0be56327014fafb791b875c731722db7")

HEXIUM_BASE = "https://hexium.zip"
ANTICAPTCHA_BASE = "https://api.anti-captcha.com"
HCAPTCHA_SITEKEY = "fe18e7a8-ca2a-41a6-b104-e934e006d6aa"

class HexiumSniperBot(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.session = None
        self.cookies = None
        self.is_running = False
        self.logged_in = False
        self.HEXIUM_USERNAME = None
        self.HEXIUM_PASSWORD = None
        self.buy_delay = 0
        self.max_stock = 100
        self.stock_limited = True
        self.start_time = None
        self.requests_session = requests.Session()

    async def ensure_session(self):
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession()
        return self.session

    async def solve_hcaptcha(self) -> str:
        session = await self.ensure_session()
        task_payload = {
            "clientKey": ANTICAPTCHA_KEY,
            "task": {
                "type": "HCaptchaTaskProxyless",
                "websiteURL": f"{HEXIUM_BASE}/login",
                "websiteKey": HCAPTCHA_SITEKEY
            }
        }
        async with session.post(f"{ANTICAPTCHA_BASE}/createTask", json=task_payload) as resp:
            task_result = await resp.json()
            if not task_result.get("taskId"):
                raise Exception(f"hCaptcha task creation failed: {task_result}")
            task_id = task_result["taskId"]
        for attempt in range(60):
            await asyncio.sleep(1)
            result_payload = {"clientKey": ANTICAPTCHA_KEY, "taskId": task_id}
            async with session.post(f"{ANTICAPTCHA_BASE}/getTaskResult", json=result_payload) as resp:
                result = await resp.json()
                if result.get("status") == "ready":
                    return result["solution"]["gRecaptchaResponse"]
        raise Exception("hCaptcha solve timeout")

    def login_hexium(self) -> bool:
        try:
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.5",
                "Accept-Encoding": "gzip, deflate",
                "Connection": "keep-alive",
                "Referer": f"{HEXIUM_BASE}/",
                "Origin": HEXIUM_BASE
            }
            
            print(f"[LOGIN] Step 1: GET /login...")
            resp_get = self.requests_session.get(f"{HEXIUM_BASE}/login", headers=headers, timeout=10)
            print(f"[LOGIN] GET status: {resp_get.status_code}")
            
            print(f"[LOGIN] Step 2: POST credentials...")
            login_data = {
                "username": self.HEXIUM_USERNAME,
                "password": self.HEXIUM_PASSWORD
            }
            
            resp_post = self.requests_session.post(f"{HEXIUM_BASE}/login", data=login_data, headers=headers, allow_redirects=True, timeout=10)
            print(f"[LOGIN] POST status: {resp_post.status_code}, URL: {resp_post.url}")
            print(f"[LOGIN] Response start: {resp_post.text[:300]}")
            
            if resp_post.status_code == 200 and "/home" in str(resp_post.url):
                self.cookies = self.requests_session.cookies
                self.logged_in = True
                print(f"[LOGIN] ✅ Login successful")
                return True
            else:
                print(f"[LOGIN] ❌ Login failed with status {resp_post.status_code}")
                return False
        except Exception as e:
            print(f"[LOGIN ERROR] {e}")
            return False

    async def buy_item(self, asset_id: int) -> tuple:
        session = await self.ensure_session()
        try:
            await asyncio.sleep(self.buy_delay)
            while True:
                prepare_url = f"{HEXIUM_BASE}/apisite/economy/v1/purchases/prepare/{asset_id}"
                async with session.get(prepare_url, cookies=self.cookies) as resp:
                    if resp.status != 200:
                        await asyncio.sleep(0.5)
                        continue
                commit_url = f"{HEXIUM_BASE}/apisite/economy/v1/purchases/commit"
                commit_payload = {"assetId": asset_id}
                async with session.post(commit_url, json=commit_payload, cookies=self.cookies) as resp:
                    commit_result = await resp.json()
                    if resp.status == 200:
                        if commit_result.get("data"):
                            for item in commit_result["data"]:
                                if item.get("state") == "Completed":
                                    return True, "bought"
                        return False, "no_stock"
                    else:
                        await asyncio.sleep(0.5)
                        continue
        except Exception as e:
            print(f"[BUY ERROR] {e}")
            return False, "error"

    @commands.command(name="login")
    async def cmd_login(self, ctx, username: str, password: str):
        self.HEXIUM_USERNAME = username
        self.HEXIUM_PASSWORD = password
        async with ctx.typing():
            success = self.login_hexium()
        if success:
            await ctx.send("✅ Logged in to Hexium")
        else:
            await ctx.send("❌ Login failed — check console for details")

    @commands.command(name="logout")
    async def cmd_logout(self, ctx):
        self.logged_in = False
        self.cookies = None
        await ctx.send("✅ Logged out")

    @commands.command(name="start")
    async def cmd_start(self, ctx):
        if not self.logged_in:
            await ctx.send("❌ Not logged in. Run !login first.")
            return
        self.is_running = True
        self.start_time = asyncio.get_event_loop().time()
        await ctx.send(f"✅ Started watching #item-releases (max stock: {self.max_stock}, delay: {self.buy_delay}s)")

    @commands.command(name="stop")
    async def cmd_stop(self, ctx):
        self.is_running = False
        await ctx.send("✅ Stopped watching")

    @commands.command(name="status")
    async def cmd_status(self, ctx):
        uptime = ""
        if self.is_running and self.start_time:
            elapsed = asyncio.get_event_loop().time() - self.start_time
            minutes = int(elapsed // 60)
            seconds = int(elapsed % 60)
            uptime = f"Uptime: {minutes}m {seconds}s\n"
        status_text = f"Logged in: {self.logged_in}\nWatching: {self.is_running}\n{uptime}Delay: {self.buy_delay}s\nMax Stock: {self.max_stock}"
        await ctx.send(f"```{status_text}```")

    @commands.command(name="sec")
    async def cmd_sec(self, ctx, delay: float):
        if delay < 0.5 or delay > 10:
            await ctx.send("❌ Delay must be between 0.5 and 10 seconds")
            return
        self.buy_delay = delay
        await ctx.send(f"✅ Set buy delay to {delay} seconds")

    @commands.command(name="stock")
    async def cmd_stock(self, ctx, max_stock: int):
        self.max_stock = max_stock
        self.stock_limited = True
        await ctx.send(f"✅ Set max stock to {max_stock}")

    @commands.command(name="maxstock")
    async def cmd_maxstock(self, ctx):
        self.stock_limited = False
        await ctx.send("✅ Buying all stock levels")

async def setup(bot):
    cog = HexiumSniperBot(bot)
    await bot.add_cog(cog)
    @bot.event
    async def on_message(message):
        if message.channel.id == ITEM_DROP_CHANNEL_ID and cog.is_running and cog.logged_in:
            if any(word in message.content.lower() for word in ["resell", "reseller", "marketplace"]):
                return
            matches = re.findall(r'https://hexium\.zip/catalog/(\d+)/', message.content)
            stocks = re.findall(r'(\d+)\(', message.content)
            for i, asset_id_str in enumerate(matches):
                asset_id = int(asset_id_str)
                stock = int(stocks[i]) if i < len(stocks) else 1
                should_buy = False
                if cog.stock_limited:
                    if stock <= cog.max_stock:
                        should_buy = True
                else:
                    should_buy = True
                if should_buy:
                    bought, reason = await cog.buy_item(asset_id)
                    if bought:
                        await message.reply(f"✅ BOUGHT itemId={asset_id} stock={stock}")
                    elif reason == "no_stock":
                        await message.reply(f"❌ FAILED itemId={asset_id} - no stock")
                    else:
                        await message.reply(f"❌ FAILED itemId={asset_id}")
        await bot.process_commands(message)

if __name__ == "__main__":
    intents = discord.Intents.default()
    intents.message_content = True
    bot = commands.Bot(command_prefix="!", intents=intents)

    @bot.event
    async def on_ready():
        print(f"Bot online as {bot.user}")

    async def main():
        async with bot:
            await setup(bot)
            await bot.start(DISCORD_TOKEN)

    asyncio.run(main())
