
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("ربات آماده است!")

# ساخت ربات
app_bot = ApplicationBuilder().token(TOKEN).build()
app_bot.add_handler(CommandHandler("start", start))

# وب سرور ساده
async def run_web():
    async def handle(request):
        return web.Response(text="وب سرور روشن است!")
    aio_app = web.Application()
    aio_app.router.add_get("/", handle)
    runner = web.AppRunner(aio_app)
    await runner.setup()
    port = int(os.environ.get("PORT", 10000))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    print(f"وب سرور روی پورت {port} آماده است")

# اجرای همزمان ربات و وب سرور
async def main():
    await asyncio.gather(
        app_bot.run_polling(drop_pending_updates=True),
        run_web()
    )

if __name__ == "__main__":
    asyncio.run(main())
