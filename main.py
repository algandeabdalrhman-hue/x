import asyncio
import aiohttp
import discord
from discord.ext import commands
import os
from dotenv import load_dotenv
import re
import requests
import json
from datetime import datetime

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
ITEM_DROP_CHANNEL_ID = 1524527819982377103
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
        self.HEXIUM_USERNAME = os.getenv("HEXIUM_USERNAME")
        self.HEXIUM_PASSWORD = os.getenv("HEXIUM_PASSWORD")
        self.buy_delay = 0.1
        self.max_stock = 100
        self.stock_limited = True
        self.max_price = None
        self.min_volume = 0
        self.max_seller_limiteds = None
        self.start_time = None
        self.csrf_token = None
        self.buys_this_session = 0
        self.manual_snipe_id = None
        self.seller_cache = {}

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
        print("[CAPTCHA] Requesting hCaptcha solve...")
        async with session.post(f"{ANTICAPTCHA_BASE}/createTask", json=task_payload) as resp:
            task_result = await resp.json()
            if not task_result.get("taskId"):
                raise Exception(f"hCaptcha task creation failed: {task_result}")
            task_id = task_result["taskId"]
            print(f"[CAPTCHA] Task created: {task_id}")
        for attempt in range(60):
            await asyncio.sleep(1)
            result_payload = {"clientKey": ANTICAPTCHA_KEY, "taskId": task_id}
            async with session.post(f"{ANTICAPTCHA_BASE}/getTaskResult", json=result_payload) as resp:
                result = await resp.json()
                if result.get("status") == "ready":
                    token = result["solution"]["gRecaptchaResponse"]
                    print(f"[CAPTCHA] Solved in {attempt+1}s")
                    return token
        raise Exception("hCaptcha solve timeout")

    def login_hexium(self) -> bool:
        try:
            req_session = requests.Session()
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Origin": HEXIUM_BASE,
                "Referer": f"{HEXIUM_BASE}/login",
            }
            print("[LOGIN] Getting login page...")
            resp_get = req_session.get(f"{HEXIUM_BASE}/login", headers=headers, timeout=10)
            if resp_get.status_code != 200:
                print(f"[LOGIN] GET /login failed: {resp_get.status_code}")
                return False
            print("[LOGIN] ✓ Page loaded")
            print("[LOGIN] Solving hCaptcha...")
            import asyncio as aio_login
            captcha_token = aio_login.run_until_complete(self.solve_hcaptcha())
            login_data = {
                "username": self.HEXIUM_USERNAME,
                "password": self.HEXIUM_PASSWORD,
                "h-captcha-response": captcha_token
            }
            print("[LOGIN] POSTing credentials + captcha...")
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
                self.csrf_token = self._extract_csrf(resp_post.text)
                print("[LOGIN] ✅ Logged in successfully")
                return True
            else:
                print(f"[LOGIN] ❌ Failed — status {resp_post.status_code}")
                return False
        except Exception as e:
            print(f"[LOGIN ERROR] {e}")
            import traceback
            traceback.print_exc()
            return False

    def _extract_csrf(self, html: str) -> str:
        try:
            match = re.search(r'csrf["\']?\s*:\s*["\']([^"\']+)["\']', html)
            if match:
                return match.group(1)
        except:
            pass
        return ""

    async def get_item_details(self, item_id: int) -> dict:
        """Fetch item metadata: name, price, creator, restrictions"""
        session = await self.ensure_session()
        try:
            url = f"{HEXIUM_BASE}/apisite/catalog/v1/catalog/items/details"
            headers = {
                "Content-Type": "application/json",
                "x-csrf-token": self.csrf_token or ""
            }
            payload = {"items": [{"itemType": "Asset", "id": item_id}]}
            async with session.post(url, json=payload, headers=headers, cookies=self.cookies, timeout=5) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data.get("data") and len(data["data"]) > 0:
                        return data["data"][0]
                return None
        except Exception as e:
            print(f"[DETAILS ERROR] {e}")
            return None

    async def get_resale_data(self, item_id: int) -> dict:
        """Fetch resale market data: stock, avg price, volume trends"""
        session = await self.ensure_session()
        try:
            url = f"{HEXIUM_BASE}/apisite/economy/v1/assets/{item_id}/resale-data"
            headers = {"x-csrf-token": self.csrf_token or ""}
            async with session.get(url, headers=headers, cookies=self.cookies, timeout=5) as resp:
                if resp.status == 200:
                    return await resp.json()
                return None
        except Exception as e:
            print(f"[RESALE ERROR] {e}")
            return None

    async def get_user_collection(self, user_id: int) -> dict:
        """Fetch user's owned items (limited + unlimited)"""
        session = await self.ensure_session()
        try:
            url = f"{HEXIUM_BASE}/users/profile/robloxcollections-json?userId={user_id}"
            headers = {"x-csrf-token": self.csrf_token or ""}
            async with session.get(url, headers=headers, cookies=self.cookies, timeout=5) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    items = data.get("CollectionsItems", [])
                    item_ids = [item["Id"] for item in items]
                    limiteds = [item["Id"] for item in items if item["AssetRestrictionIcon"].get("CssTag") == "limited"]
                    self.seller_cache[user_id] = {
                        "item_ids": item_ids,
                        "limited_count": len(limiteds),
                        "total_count": len(items),
                        "last_updated": datetime.now().isoformat()
                    }
                    return self.seller_cache[user_id]
                return None
        except Exception as e:
            print(f"[COLLECTION ERROR] {e}")
            return None

    async def should_buy(self, details: dict, resale: dict, seller_collection: dict = None) -> tuple:
        """Decision logic with all filters"""
        if not details or not resale:
            return False, "no_data"
        
        stock = resale.get("numberRemaining", 0)
        avg_price = resale.get("recentAveragePrice", 0)
        price_points = resale.get("priceDataPoints", [])
        recent_price = price_points[-1]["value"] if price_points else avg_price
        
        if self.stock_limited and stock > self.max_stock:
            return False, f"stock_too_high ({stock})"
        
        if self.max_price and recent_price > self.max_price:
            return False, f"above_max_price ({recent_price})"
        
        volume_points = resale.get("volumeDataPoints", [])
        recent_vol = volume_points[-1]["value"] if volume_points else 0
        if recent_vol < self.min_volume:
            return False, f"low_volume ({recent_vol})"
        
        if self.max_seller_limiteds and seller_collection:
            if seller_collection.get("limited_count", 0) > self.max_seller_limiteds:
                return False, f"reseller_portfolio ({seller_collection['limited_count']} limiteds)"
        
        return True, "pass"

    async def buy_item(self, asset_id: int) -> tuple:
        """Prepare + Commit purchase flow"""
        session = await self.ensure_session()
        try:
            await asyncio.sleep(self.buy_delay)
            prepare_url = f"{HEXIUM_BASE}/apisite/economy/v1/purchases/prepare/{asset_id}"
            async with session.get(prepare_url, cookies=self.cookies, timeout=5) as resp:
                if resp.status != 200:
                    return False, "prep_failed"
            commit_url = f"{HEXIUM_BASE}/apisite/economy/v1/purchases/commit"
            commit_payload = {"assetId": asset_id}
            async with session.post(commit_url, json=commit_payload, cookies=self.cookies, timeout=5) as resp:
                commit_result = await resp.json()
                if resp.status == 200 and commit_result.get("data"):
                    for item in commit_result["data"]:
                        if item.get("state") == "Completed":
                            self.buys_this_session += 1
                            return True, "bought"
                return False, "no_stock"
        except Exception as e:
            print(f"[BUY ERROR] {e}")
            return False, "error"

    async def manual_snipe_loop(self):
        """Continuously try to buy manual snipe item"""
        while True:
            if self.manual_snipe_id and self.logged_in:
                details = await self.get_item_details(self.manual_snipe_id)
                resale = await self.get_resale_data(self.manual_snipe_id)
                
                seller_collection = None
                if self.max_seller_limiteds:
                    lowest_seller = details.get("lowestSellerData", {}) if details else {}
                    seller_id = lowest_seller.get("userId")
                    if seller_id:
                        seller_collection = await self.get_user_collection(seller_id)
                
                should_buy, reason = await self.should_buy(details, resale, seller_collection)
                if should_buy:
                    bought, buy_reason = await self.buy_item(self.manual_snipe_id)
                    if bought:
                        channel = self.bot.get_channel(ITEM_DROP_CHANNEL_ID)
                        if channel:
                            await channel.send(f"✅ MANUAL SNIPE BOUGHT {self.manual_snipe_id}")
                        self.manual_snipe_id = None
            await asyncio.sleep(0.05)

    @commands.command(name="start")
    async def cmd_start(self, ctx):
        if not self.logged_in:
            async with ctx.typing():
                success = self.login_hexium()
            if not success:
                await ctx.send("❌ Login failed.")
                return
        self.is_running = True
        self.start_time = asyncio.get_event_loop().time()
        self.buys_this_session = 0
        filters = f"max_stock={self.max_stock}" if self.stock_limited else "all_stock"
        if self.max_price:
            filters += f", max_price={self.max_price}"
        if self.min_volume:
            filters += f", min_vol={self.min_volume}"
        if self.max_seller_limiteds:
            filters += f", no_resellers(>{self.max_seller_limiteds})"
        await ctx.send(f"✅ Watching #item-releases ({filters}, delay={self.buy_delay}s)")

    @commands.command(name="startlink")
    async def cmd_startlink(self, ctx, item_link: str):
        """Manually snipe a specific item + keep watching #item-releases"""
        try:
            match = re.search(r'https://hexium\.zip/catalog/(\d+)/', item_link)
            if not match:
                await ctx.send("❌ Invalid link format")
                return
            asset_id = int(match.group(1))
            self.manual_snipe_id = asset_id
            if not self.logged_in:
                async with ctx.typing():
                    success = self.login_hexium()
                if not success:
                    await ctx.send("❌ Login failed.")
                    return
            self.is_running = True
            self.start_time = asyncio.get_event_loop().time()
            self.buys_this_session = 0
            await ctx.send(f"✅ Sniping item {asset_id} + watching #item-releases")
        except Exception as e:
            await ctx.send(f"❌ Error: {e}")

    @commands.command(name="stoplink")
    async def cmd_stoplink(self, ctx):
        """Stop manual link snipe — channel still watching"""
        self.manual_snipe_id = None
        await ctx.send("✅ Link snipe disabled (#item-releases still watching)")

    @commands.command(name="stop")
    async def cmd_stop(self, ctx):
        """Stop channel watch — manual snipe still active"""
        self.is_running = False
        await ctx.send(f"✅ Channel watch stopped ({self.buys_this_session} buys)")

    @commands.command(name="killall")
    async def cmd_killall(self, ctx):
        """Stop both channel watch and manual snipe"""
        self.is_running = False
        self.manual_snipe_id = None
        await ctx.send(f"✅ All sniping stopped ({self.buys_this_session} buys)")

    @commands.command(name="status")
    async def cmd_status(self, ctx):
        uptime = ""
        if self.is_running and self.start_time:
            elapsed = asyncio.get_event_loop().time() - self.start_time
            minutes, seconds = int(elapsed // 60), int(elapsed % 60)
            uptime = f"Uptime: {minutes}m {seconds}s\n"
        filters = f"Max stock: {self.max_stock}\n" if self.stock_limited else "Max stock: unlimited\n"
        if self.max_price:
            filters += f"Max price: {self.max_price}\n"
        if self.min_volume:
            filters += f"Min volume: {self.min_volume}\n"
        if self.max_seller_limiteds:
            filters += f"No resellers (>{self.max_seller_limiteds})\n"
        manual = f"Manual snipe: {self.manual_snipe_id}\n" if self.manual_snipe_id else ""
        status = f"Logged in: {self.logged_in}\nChannel watching: {self.is_running}\n{uptime}{manual}{filters}Delay: {self.buy_delay}s\nTotal buys: {self.buys_this_session}"
        await ctx.send(f"```{status}```")

    @commands.command(name="sec")
    async def cmd_sec(self, ctx, delay: float):
        if delay < 0.05 or delay > 10:
            await ctx.send("❌ Delay must be 0.05–10s")
            return
        self.buy_delay = delay
        await ctx.send(f"✅ Delay: {delay}s")

    @commands.command(name="stock")
    async def cmd_stock(self, ctx, max_stock: int):
        self.max_stock = max_stock
        self.stock_limited = True
        await ctx.send(f"✅ Max stock: {max_stock}")

    @commands.command(name="maxstock")
    async def cmd_maxstock(self, ctx):
        self.stock_limited = False
        await ctx.send("✅ Buying all stock")

    @commands.command(name="price")
    async def cmd_price(self, ctx, max_price: int):
        self.max_price = max_price
        await ctx.send(f"✅ Max price: {max_price} robux")

    @commands.command(name="noprice")
    async def cmd_noprice(self, ctx):
        self.max_price = None
        await ctx.send("✅ Price filter disabled")

    @commands.command(name="volume")
    async def cmd_volume(self, ctx, min_vol: int):
        self.min_volume = min_vol
        await ctx.send(f"✅ Min volume: {min_vol}")

    @commands.command(name="reseller")
    async def cmd_reseller(self, ctx, max_limiteds: int):
        """Skip items if lowest seller owns more than N limited items"""
        self.max_seller_limiteds = max_limiteds
        await ctx.send(f"✅ Skipping if reseller owns >{max_limiteds} limited items")

    @commands.command(name="no-reseller")
    async def cmd_no_reseller(self, ctx):
        """Disable reseller filter"""
        self.max_seller_limiteds = None
        await ctx.send("✅ Reseller filter disabled")

    @commands.command(name="seller")
    async def cmd_seller(self, ctx, user_id: int):
        """Check seller's collection"""
        async with ctx.typing():
            collection = await self.get_user_collection(user_id)
        if collection:
            msg = f"**User {user_id}** Portfolio:\n"
            msg += f"Total items: {collection['total_count']}\n"
            msg += f"Limited items: {collection['limited_count']}\n"
            msg += f"Last updated: {collection['last_updated']}"
            await ctx.send(msg)
        else:
            await ctx.send(f"❌ Failed to fetch collection for user {user_id}")

    @commands.command(name="debug")
    async def cmd_debug(self, ctx, item_id: int):
        """Fetch full item + resale data"""
        async with ctx.typing():
            details = await self.get_item_details(item_id)
            resale = await self.get_resale_data(item_id)
        if details and resale:
            should_buy, reason = await self.should_buy(details, resale)
            lowest_seller = details.get("lowestSellerData", {})
            seller_id = lowest_seller.get("userId")
            msg = f"**{details.get('name')}** (ID {item_id})\n"
            msg += f"Price: {resale.get('recentAveragePrice')} robux\n"
            msg += f"Stock remaining: {resale.get('numberRemaining')}\n"
            msg += f"Recent volume: {resale['volumeDataPoints'][-1]['value'] if resale.get('volumeDataPoints') else 0}\n"
            if seller_id:
                msg += f"Lowest seller: {lowest_seller.get('username')} (ID {seller_id})\n"
            msg += f"Decision: {'✅ BUY' if should_buy else f'❌ {reason}'}"
            await ctx.send(msg)
        else:
            await ctx.send(f"❌ Failed to fetch data for {item_id}")

async def setup(bot):
    cog = HexiumSniperBot(bot)
    await bot.add_cog(cog)
    bot.loop.create_task(cog.manual_snipe_loop())

    @bot.event
    async def on_message(message):
        if message.channel.id == ITEM_DROP_CHANNEL_ID and cog.is_running and cog.logged_in:
            if any(word in message.content.lower() for word in ["resell", "reseller", "marketplace"]):
                return
            matches = re.findall(r'https://hexium\.zip/catalog/(\d+)/', message.content)
            for asset_id_str in matches:
                asset_id = int(asset_id_str)
                details = await cog.get_item_details(asset_id)
                resale = await cog.get_resale_data(asset_id)
                
                seller_collection = None
                if cog.max_seller_limiteds:
                    lowest_seller = details.get("lowestSellerData", {}) if details else {}
                    seller_id = lowest_seller.get("userId")
                    if seller_id:
                        seller_collection = await cog.get_user_collection(seller_id)
                
                should_buy, reason = await cog.should_buy(details, resale, seller_collection)
                if should_buy:
                    bought, buy_reason = await cog.buy_item(asset_id)
                    if bought:
                        await message.reply(f"✅ BOUGHT {asset_id}")
                    else:
                        await message.reply(f"❌ BUY FAILED ({buy_reason})")
                else:
                    await message.reply(f"⏭️ SKIPPED ({reason})")
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
