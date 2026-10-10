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
ANTICAPTCHA_KEY = os.getenv("ANTICAPTCHA_KEY")
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
        self.HEXIUM_USERNAME = os.getenv("HEXIUM_USERNAME")
        self.HEXIUM_PASSWORD = os.getenv("HEXIUM_PASSWORD")
        self.buy_delay = 0
        self.max_stock = 100
        self.stock_limited = True
        self.start_time = None

    async def ensure_session(self):
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession()
        return self.session

    def login_hexium(self) -> bool:
        try:
            req_session = requests.Session()
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.5",
                "Origin": HEXIUM_BASE,
                "Referer": f"{HEXIUM_BASE}/",
            }
            
            # GET /login first — grab cookies and any CSRF tokens
            print("[LOGIN] GET /login...")
            resp_get = req_session.get(
                f"{HEXIUM_BASE}/login",
                headers=headers,
                timeout=10
            )
            print(f"[LOGIN] GET returned {resp_get.status_code}")
            
            if resp_get.status_code != 200:
                print(f"[LOGIN] GET /login failed with {resp_get.status_code}")
                return False
            
            # Extract CSRF token if it exists
            csrf_token = None
            match = re.search(r'name=["\'](?:csrf|_token)["\'][^>]*value=["\']([^"\']+)["\']', resp_get.text, re.IGNORECASE)
            if match:
                csrf_token = match.group(1)
                print(f"[LOGIN] Found CSRF token: {csrf_token[:20]}...")
            else:
                print("[LOGIN] No CSRF token found in form")
            
            # POST with credentials
            login_data = {
                "username": self.HEXIUM_USERNAME,
                "password": self.HEXIUM_PASSWORD
            }
            if csrf_token:
                login_data["csrf"] = csrf_token
                login_data["_token"] = csrf_token
            
            print("[LOGIN] POST credentials...")
            resp_post = req_session.post(
                f"{HEXIUM_BASE}/login",
                data=login_data,
                headers=headers,
                allow_redirects=True,
                timeout=10
            )
            
            print(f"[LOGIN] POST returned {resp_post.status_code}, URL: {resp_post.url}")
            
            if resp_post.status_code == 200 and "/home" in str(resp_post.url):
                self.cookies = req_session.cookies
                self.logged_in = True
                print("[LOGIN] ✅ Logged in successfully")
                return True
            else:
                print(f"[LOGIN] Failed — status {resp_post.status_code}, not redirected to /home")
                return False
        except Exception as e:
            print(f"[LOGIN ERROR] {e}")
            return False

    async def buy_item(self, asset_id: int) -> tuple:
        session = await self.ensure_session()
        try:
            await asyncio.sleep(self.buy_delay)
            
            prepare_url = f"{HEXIUM_BASE}/apisite/economy/v1/purchases/prepare/{asset_id}"
            async with session.get(prepare_url, cookies=self.cookies) as resp:
                if resp.status != 200:
                    print(f"[BUY] prepare/{asset_id} returned {resp.status}")
                    return False, "prep_failed"
            
            commit_url = f"{HEXIUM_BASE}/apisite/economy/v1/purchases/commit"
            commit_payload = {"assetId": asset_id}
            async with session.post(commit_url, json=commit_payload, cookies=self.cookies) as resp:
                commit_result = await resp.json()
                if resp.status == 200 and commit_result.get("data"):
                    for item in commit_result["data"]:
                        if item.get("state") == "Completed":
                            return True, "bought"
                return False, "no_stock"
        except Exception as e:
            print(f"[BUY ERROR] {e}")
            return False, "error"

    @commands.command(name="start")
    async def cmd_start(self, ctx):
        if not self.logged_in:
            async with ctx.typing():
                success = self.login_hexium()
            if not success:
                await ctx.send("❌ Login failed. Check console for details.")
                return
        self.is_running = True
        self.start_time = asyncio.get_event_loop().time()
        await ctx.send(f"✅ Watching #item-releases (max stock: {self.max_stock}, delay: {self.buy_delay}s)")

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
                
                should_buy = not cog.stock_limited or stock <= cog.max_stock
                
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
