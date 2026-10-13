import discord
from discord.ext import commands, tasks
import aiohttp
import asyncio
import logging
from datetime import datetime
import json
import os
from dotenv import load_dotenv
import re
from anticaptchaofficial.recaptchav2proxyless import *

load_dotenv()

# config
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
ANTI_CAPTCHA_KEY = os.getenv("ANTI_CAPTCHA_KEY")
HEXIUM_USERNAME = os.getenv("HEXIUM_USERNAME")
HEXIUM_PASSWORD = os.getenv("HEXIUM_PASSWORD")
HCAPTCHA_SITEKEY = "fe18e7a8-ca2a-41a6-b104-e934e006d6aa"
HEXIUM_BASE = "https://hexium.zip"
CHANNEL_ID = 1524527819982377103
GUILD_ID = 1524521375073697912

logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger("HexiumSniper")

class HexiumSniperBot(commands.Cog):
	def __init__(self, bot):
		self.bot = bot
		self.session = None
		self.auth_token = None
		self.headers = {
			"User-Agent": "Mozilla/5.0 (iPad; CPU OS 17_0 like Mac OS X) AppleWebKit/605.1.15"
		}
		self.watching = False
		self.manual_snipe_active = False
		self.manual_snipe_itemid = None
		self.manual_snipe_url = None
		self.buy_delay = 0.1
		self.filters = {
			"max_stock": None,
			"max_price": None,
			"min_volume": 0,
			"max_reseller_limiteds": None
		}
		self.seller_cache = {}
		self.buy_count = 0
		self.start_time = None
	
	async def setup_session(self):
		if not self.session:
			self.session = aiohttp.ClientSession()
	
	async def login(self):
		"""login via hCaptcha + anti-captcha.com"""
		try:
			await self.setup_session()
			
			# solve hCaptcha
			solver = recaptchaV2Proxyless()
			solver.set_verbose(1)
			solver.set_captcha_type(4)  # hCaptcha
			solver.set_website_url(f"{HEXIUM_BASE}/login")
			solver.set_website_key(HCAPTCHA_SITEKEY)
			solver.set_api_key(ANTI_CAPTCHA_KEY)
			
			g_response = solver.solve_and_return_solution()
			if not g_response:
				logger.error("hCaptcha solve failed")
				return False
			
			# POST login
			async with self.session.post(
				f"{HEXIUM_BASE}/login",
				json={
					"username": HEXIUM_USERNAME,
					"password": HEXIUM_PASSWORD,
					"h-captcha-response": g_response
				},
				headers=self.headers
			) as resp:
				if resp.status == 200:
					data = await resp.json()
					self.auth_token = data.get("token") or resp.cookies.get("auth")
					self.headers["Authorization"] = f"Bearer {self.auth_token}"
					logger.info("Login successful")
					return True
				else:
					logger.error(f"Login failed: {resp.status}")
					return False
		
		except Exception as e:
			logger.error(f"Login error: {e}")
			return False
	
	async def get_item_details(self, item_id):
		"""fetch item price, stock, seller"""
		try:
			async with self.session.post(
				f"{HEXIUM_BASE}/apisite/catalog/v1/catalog/items/details",
				json={"items": [{"itemType": "Asset", "id": str(item_id)}]},
				headers=self.headers
			) as resp:
				if resp.status == 200:
					data = await resp.json()
					if data.get("data") and len(data["data"]) > 0:
						item = data["data"][0]
						return {
							"name": item.get("name"),
							"price": item.get("price"),
							"stock": item.get("stock"),
							"seller_id": item.get("creatorId"),
							"item_id": item_id
						}
		except Exception as e:
			logger.error(f"get_item_details error: {e}")
		return None
	
	async def get_resale_data(self, item_id):
		"""fetch volume from resale market"""
		try:
			async with self.session.get(
				f"{HEXIUM_BASE}/apisite/economy/v1/assets/{item_id}/resale-data",
				headers=self.headers
			) as resp:
				if resp.status == 200:
					data = await resp.json()
					return {
						"volume": data.get("volumeTradedInLastThirtyDays", 0),
						"average_price": data.get("averagePrice", 0)
					}
		except Exception as e:
			logger.error(f"get_resale_data error: {e}")
		return {"volume": 0, "average_price": 0}
	
	async def check_seller_portfolio(self, seller_id):
		"""cache seller's limited item count"""
		if seller_id in self.seller_cache:
			cached = self.seller_cache[seller_id]
			if tick() - cached["timestamp"] < 300:  # 5min cache
				return cached
		
		try:
			async with self.session.get(
				f"{HEXIUM_BASE}/users/profile/robloxcollections-json?userId={seller_id}",
				headers=self.headers
			) as resp:
				if resp.status == 200:
					data = await resp.json()
					limited_count = len([x for x in data.get("items", []) if x.get("isLimited")])
					total_count = len(data.get("items", []))
					
					self.seller_cache[seller_id] = {
						"limited_count": limited_count,
						"total_count": total_count,
						"timestamp": tick()
					}
					return self.seller_cache[seller_id]
		except Exception as e:
			logger.error(f"check_seller_portfolio error: {e}")
		
		return {"limited_count": 0, "total_count": 0, "timestamp": tick()}
	
	async def should_buy(self, item_details, resale_data):
		"""check all filters"""
		# stock filter
		if self.filters["max_stock"] and item_details["stock"] > self.filters["max_stock"]:
			return False, "stock too high"
		
		# price filter
		if self.filters["max_price"] and item_details["price"] > self.filters["max_price"]:
			return False, "price too high"
		
		# volume filter
		if resale_data["volume"] < self.filters["min_volume"]:
			return False, "volume too low"
		
		# reseller portfolio filter
		if self.filters["max_reseller_limiteds"]:
			seller_portfolio = await self.check_seller_portfolio(item_details["seller_id"])
			if seller_portfolio["limited_count"] > self.filters["max_reseller_limiteds"]:
				return False, "seller has too many limiteds"
		
		return True, "pass"
	
	async def buy_item(self, item_id):
		"""prepare + commit buy"""
		try:
			# step 1: prepare
			async with self.session.get(
				f"{HEXIUM_BASE}/apisite/economy/v1/purchases/prepare/{item_id}",
				headers=self.headers
			) as resp:
				if resp.status != 200:
					logger.error(f"Prepare failed: {resp.status}")
					return False
			
			await asyncio.sleep(0.05)
			
			# step 2: commit
			async with self.session.post(
				f"{HEXIUM_BASE}/apisite/economy/v1/purchases/commit",
				json={"assetId": item_id},
				headers=self.headers
			) as resp:
				if resp.status == 200:
					data = await resp.json()
					if data.get("data") and len(data["data"]) > 0 and data["data"][0].get("state") == "Completed":
						self.buy_count += 1
						logger.info(f"BUY SUCCESS: {item_id}")
						return True
		
		except Exception as e:
			logger.error(f"buy_item error: {e}")
		
		return False
	
	async def manual_snipe_loop(self):
		"""50ms polling loop for manual item snipe"""
		while self.manual_snipe_active:
			try:
				if self.manual_snipe_itemid:
					item_details = await self.get_item_details(self.manual_snipe_itemid)
					resale_data = await self.get_resale_data(self.manual_snipe_itemid)
					
					if item_details:
						should_buy, reason = await self.should_buy(item_details, resale_data)
						if should_buy:
							bought = await self.buy_item(self.manual_snipe_itemid)
							if bought:
								logger.info(f"Manual snipe SUCCESS: {self.manual_snipe_itemid}")
				
				await asyncio.sleep(self.buy_delay)
			
			except Exception as e:
				logger.error(f"manual_snipe_loop error: {e}")
				await asyncio.sleep(0.5)
	
	@commands.command(name="start")
	async def cmd_start(self, ctx):
		"""start bot: login + watch channel"""
		if await self.login():
			self.watching = True
			self.start_time = datetime.now()
			await ctx.send("✅ Logged in, watching #item-releases")
		else:
			await ctx.send("❌ Login failed")
	
	@commands.command(name="startlink")
	async def cmd_startlink(self, ctx, url: str):
		"""snipe specific item from URL"""
		# extract item ID from URL (hexium.zip/catalog/ITEMID/name)
		match = re.search(r'/catalog/(\d+)/', url)
		if match:
			item_id = int(match.group(1))
			self.manual_snipe_itemid = item_id
			self.manual_snipe_active = True
			self.manual_snipe_url = url
			self.start_time = datetime.now()
			
			if not self.watching:
				login_ok = await self.login()
				if not login_ok:
					await ctx.send("❌ Login failed")
					return
			
			self.bot.loop.create_task(self.manual_snipe_loop())
			await ctx.send(f"🎯 Sniping {item_id} from {url}")
		else:
			await ctx.send("❌ Invalid URL format")
	
	@commands.command(name="stoplink")
	async def cmd_stoplink(self, ctx):
		"""stop manual item snipe"""
		self.manual_snipe_active = False
		self.manual_snipe_itemid = None
		await ctx.send("⏹️ Stopped item snipe, channel still watching")
	
	@commands.command(name="stop")
	async def cmd_stop(self, ctx):
		"""stop channel watch"""
		self.watching = False
		await ctx.send("⏹️ Stopped watching")
	
	@commands.command(name="killall")
	async def cmd_killall(self, ctx):
		"""stop everything"""
		self.watching = False
		self.manual_snipe_active = False
		await ctx.send("⏹️ All stopped")
	
	@commands.command(name="status")
	async def cmd_status(self, ctx):
		"""current bot status"""
		uptime = "N/A"
		if self.start_time:
			uptime = str(datetime.now() - self.start_time)
		
		status_text = (
			f"🔍 Watching: {self.watching}\n"
			f"🎯 Manual snipe: {self.manual_snipe_active}\n"
			f"⏱️ Uptime: {uptime}\n"
			f"💰 Buys: {self.buy_count}\n"
			f"📊 Filters: {json.dumps(self.filters, indent=2)}"
		)
		await ctx.send(f"```\n{status_text}\n```")
	
	@commands.command(name="sec")
	async def cmd_buy_delay(self, ctx, delay: float):
		"""set buy delay (0.05-10s, default 0.1s)"""
		if 0.05 <= delay <= 10:
			self.buy_delay = delay
			await ctx.send(f"✅ Buy delay set to {delay}s")
		else:
			await ctx.send("❌ Delay must be 0.05-10 seconds")
	
	@commands.command(name="stock")
	async def cmd_stock_filter(self, ctx, max_stock: int):
		"""only buy items ≤ N stock"""
		self.filters["max_stock"] = max_stock
		await ctx.send(f"✅ Stock filter: ≤{max_stock}")
	
	@commands.command(name="maxstock")
	async def cmd_maxstock(self, ctx):
		"""buy all stock levels"""
		self.filters["max_stock"] = None
		await ctx.send("✅ Stock filter disabled")
	
	@commands.command(name="price")
	async def cmd_price_filter(self, ctx, max_robux: int):
		"""only buy below N robux"""
		self.filters["max_price"] = max_robux
		await ctx.send(f"✅ Price filter: ≤{max_robux}R$")
	
	@commands.command(name="noprice")
	async def cmd_noprice(self, ctx):
		"""disable price filter"""
		self.filters["max_price"] = None
		await ctx.send("✅ Price filter disabled")
	
	@commands.command(name="volume")
	async def cmd_volume_filter(self, ctx, min_volume: int):
		"""minimum recent sales volume"""
		self.filters["min_volume"] = min_volume
		await ctx.send(f"✅ Volume filter: ≥{min_volume} sales")
	
	@commands.command(name="reseller")
	async def cmd_reseller_filter(self, ctx, max_limiteds: int):
		"""skip if seller owns >N limited items"""
		self.filters["max_reseller_limiteds"] = max_limiteds
		await ctx.send(f"✅ Reseller filter: ≤{max_limiteds} limiteds")
	
	@commands.command(name="no-reseller")
	async def cmd_no_reseller(self, ctx):
		"""disable reseller filter"""
		self.filters["max_reseller_limiteds"] = None
		await ctx.send("✅ Reseller filter disabled")
	
	@commands.command(name="seller")
	async def cmd_check_seller(self, ctx, user_id: int):
		"""check user portfolio"""
		portfolio = await self.check_seller_portfolio(user_id)
		await ctx.send(
			f"User {user_id}:\n"
			f"Limited items: {portfolio['limited_count']}\n"
			f"Total items: {portfolio['total_count']}"
		)
	
	@commands.command(name="debug")
	async def cmd_debug(self, ctx, item_id: int):
		"""full item data + decision"""
		item = await self.get_item_details(item_id)
		resale = await self.get_resale_data(item_id)
		should, reason = await self.should_buy(item, resale)
		
		debug_text = (
			f"Item: {item['name']}\n"
			f"Price: {item['price']}R$\n"
			f"Stock: {item['stock']}\n"
			f"Seller: {item['seller_id']}\n"
			f"Volume: {resale['volume']}\n"
			f"Decision: {should} ({reason})"
		)
		await ctx.send(f"```\n{debug_text}\n```")
	
	@commands.Cog.listener()
	async def on_message(self, message):
		"""listen for drops in #item-releases"""
		if not self.watching or message.channel.id != CHANNEL_ID or message.author.bot:
			return
		
		# parse drop message: @item drop ping https://hexium.zip/catalog/ITEMID/...
		if "hexium.zip/catalog" in message.content:
			match = re.search(r'/catalog/(\d+)/', message.content)
			if match:
				item_id = int(match.group(1))
				item_details = await self.get_item_details(item_id)
				resale_data = await self.get_resale_data(item_id)
				
				if item_details:
					should_buy, reason = await self.should_buy(item_details, resale_data)
					if should_buy:
						bought = await self.buy_item(item_id)
						if bought:
							logger.info(f"AUTO SNIPE SUCCESS: {item_id} ({item_details['name']})")

def tick():
	return datetime.now().timestamp()

async def main():
	intents = discord.Intents.default()
	intents.message_content = True
	bot = commands.Bot(command_prefix="!", intents=intents)
	
	@bot.event
	async def on_ready():
		logger.info(f"Bot logged in as {bot.user}")
	
	await bot.add_cog(HexiumSniperBot(bot))
	await bot.start(DISCORD_TOKEN)

if __name__ == "__main__":
	asyncio.run(main())
