import os
import io
import asyncio
import tempfile
import subprocess
from dataclasses import dataclass, field

from dotenv import load_dotenv
load_dotenv()

BOT_TOKEN = os.getenv("8413057502:AAE2W0XyjOC1Zne9BK_-UtgGhM4240NacGA")
if not BOT_TOKEN:
    raise SystemExit("ERROR: BOT_TOKEN missing. Set it in Render Environment Variables.")

WORK_DIR = os.path.join(os.path.dirname(__file__), "work")
os.makedirs(WORK_DIR, exist_ok=True)

# ======================= IMAGE ENHANCE =========================
from PIL import Image, ImageFilter, ImageOps
import cv2
import numpy as np

def enhance_image_bytes(img_bytes: bytes) -> bytes:
    img = Image.open(io.BytesIO(img_bytes)).convert("RGB")

    img = ImageOps.autocontrast(img)
    img = img.filter(ImageFilter.UnsharpMask(radius=1.4, percent=140, threshold=3))

    arr = np.array(img)
    arr = cv2.fastNlMeansDenoisingColored(arr, None, 5, 5, 7, 21)
    img = Image.fromarray(arr)

    out = io.BytesIO()
    img.save(out, format="JPEG", quality=92, optimize=True)
    out.seek(0)
    return out.read()

# ======================= MP4 → MP3 =========================
async def convert_mp4_to_mp3(src_path: str, dst_path: str):
    cmd = [
        "ffmpeg", "-y", "-i", src_path, "-vn",
        "-acodec", "libmp3lame", "-q:a", "2", dst_path
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {stderr.decode()}")

# ======================= RESUME BUILDER =========================
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm

@dataclass
class ResumeData:
    full_name: str = ""
    email: str = ""
    phone: str = ""
    role: str = ""
    summary: str = ""
    skills: list[str] = field(default_factory=list)
    experience: list[str] = field(default_factory=list)
    education: list[str] = field(default_factory=list)

def make_resume_pdf(resume: ResumeData) -> bytes:
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    x = 20*mm
    y = A4[1] - 25*mm

    def header(txt):
        nonlocal y
        c.setFont("Helvetica-Bold", 18)
        c.drawString(x, y, txt)
        y -= 10*mm

    def kv(key, val):
        nonlocal y
        c.setFont("Helvetica-Bold", 11)
        c.drawString(x, y, f"{key}: ")
        c.setFont("Helvetica", 11)
        c.drawString(x + 30*mm, y, val)
        y -= 6*mm

    def block(title, items):
        nonlocal y
        if not items: return
        c.setFont("Helvetica-Bold", 14)
        c.drawString(x, y, title)
        y -= 7*mm
        c.setFont("Helvetica", 11)
        for item in items:
            c.drawString(x + 6*mm, y, f"• {item}")
            y -= 6*mm
        y -= 3*mm

    header(resume.full_name)
    kv("Email", resume.email)
    kv("Phone", resume.phone)
    kv("Role", resume.role)
    y -= 5*mm

    if resume.summary:
        block("Summary", [resume.summary])
    block("Skills", [", ".join(resume.skills)])
    block("Experience", resume.experience)
    block("Education", resume.education)

    c.showPage()
    c.save()
    buf.seek(0)
    return buf.read()

# ======================= TELEGRAM SECTION =========================
from telegram import Update, InputFile, ReplyKeyboardRemove
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    ConversationHandler, ContextTypes, filters
)

MP3_WAIT_VIDEO = 1
ENHANCE_WAIT_IMAGE = 2
(
    RESUME_NAME, RESUME_EMAIL, RESUME_PHONE, RESUME_ROLE,
    RESUME_SUMMARY, RESUME_SKILLS, RESUME_EXPERIENCE, RESUME_EDUCATION
) = range(10, 18)

# ----- Commands -----
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Hey! I can do:\n"
        "• /mp3 – Convert MP4 to MP3\n"
        "• /enhance – Enhance a photo\n"
        "• /resume – Build a PDF resume\n"
        "\nPowered fully locally — no paid APIs!"
    )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start(update, context)

# ----- MP3 -----
async def mp3_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Send an MP4 video file.")
    return MP3_WAIT_VIDEO

async def on_video_for_mp3(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    file_obj = msg.video or msg.document
    if not file_obj:
        await msg.reply_text("Please send an MP4 file.")
        return MP3_WAIT_VIDEO

    f = await file_obj.get_file()
    with tempfile.NamedTemporaryFile(dir=WORK_DIR, suffix=".mp4", delete=False) as tmp:
        src = tmp.name
    await f.download_to_drive(src)

    dst = src.replace(".mp4", ".mp3")

    try:
        await msg.reply_text("Converting…")
        await convert_mp4_to_mp3(src, dst)

        with open(dst, "rb") as fp:
            await msg.reply_document(InputFile(fp, filename=os.path.basename(dst)))
    except Exception as e:
        await msg.reply_text(f"Error: {e}")
    finally:
        for p in (src, dst):
            try: os.remove(p)
            except: pass

    return ConversationHandler.END

# ----- Image Enhance -----
async def enhance_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Send a JPG/PNG image.")
    return ENHANCE_WAIT_IMAGE

async def on_image_for_enhance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    file_obj = msg.photo[-1] if msg.photo else msg.document
    if not file_obj:
        await msg.reply_text("Send an image file.")
        return ENHANCE_WAIT_IMAGE

    f = await file_obj.get_file()
    raw = await f.download_as_bytearray()

    out = enhance_image_bytes(bytes(raw))
    await msg.reply_document(InputFile(io.BytesIO(out), filename="enhanced.jpg"))

    return ConversationHandler.END

# ----- Resume Builder -----
async def resume_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["resume"] = ResumeData()
    await update.message.reply_text("Full Name?")
    return RESUME_NAME

async def resume_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["resume"].full_name = update.message.text
    await update.message.reply_text("Email?")
    return RESUME_EMAIL

async def resume_email(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["resume"].email = update.message.text
    await update.message.reply_text("Phone?")
    return RESUME_PHONE

async def resume_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["resume"].phone = update.message.text
    await update.message.reply_text("Role?")
    return RESUME_ROLE

async def resume_role(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["resume"].role = update.message.text
    await update.message.reply_text("Short summary?")
    return RESUME_SUMMARY

async def resume_summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["resume"].summary = update.message.text
    await update.message.reply_text("Skills (comma separated)?")
    return RESUME_SKILLS

async def resume_skills(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["resume"].skills = [s.strip() for s in update.message.text.split(",")]
    await update.message.reply_text("Experience lines. Send 'done' when finished.")
    return RESUME_EXPERIENCE

async def resume_experience(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text.strip()
    resume = context.user_data["resume"]
    if txt.lower() == "done":
        await update.message.reply_text("Education lines. Send 'done' when finished.")
        return RESUME_EDUCATION
    resume.experience.append(txt)
    return RESUME_EXPERIENCE

async def resume_education(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text.strip()
    resume = context.user_data["resume"]

    if txt.lower() == "done":
        pdf = make_resume_pdf(resume)
        await update.message.reply_document(
            InputFile(io.BytesIO(pdf), filename="resume.pdf"))
        return ConversationHandler.END

    resume.education.append(txt)
    return RESUME_EDUCATION

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Cancelled.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END

# ======================= MAIN WEBHOOK =========================
def main():
    app = Application.builder().token(BOT_TOKEN).build()

    # handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))

    # mp3
    app.add_handler(ConversationHandler(
        entry_points=[CommandHandler("mp3", mp3_cmd)],
        states={MP3_WAIT_VIDEO: [MessageHandler(filters.VIDEO | filters.Document.VIDEO, on_video_for_mp3)]},
        fallbacks=[CommandHandler("cancel", cancel)],
    ))

    # enhance
    app.add_handler(ConversationHandler(
        entry_points=[CommandHandler("enhance", enhance_cmd)],
        states={ENHANCE_WAIT_IMAGE: [MessageHandler(filters.PHOTO | filters.Document.IMAGE, on_image_for_enhance)]},
        fallbacks=[CommandHandler("cancel", cancel)],
    ))

    # resume
    app.add_handler(ConversationHandler(
        entry_points=[CommandHandler("resume", resume_cmd)],
        states={
            RESUME_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, resume_name)],
            RESUME_EMAIL: [MessageHandler(filters.TEXT & ~filters.COMMAND, resume_email)],
            RESUME_PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, resume_phone)],
            RESUME_ROLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, resume_role)],
            RESUME_SUMMARY: [MessageHandler(filters.TEXT & ~filters.COMMAND, resume_summary)],
            RESUME_SKILLS: [MessageHandler(filters.TEXT & ~filters.COMMAND, resume_skills)],
            RESUME_EXPERIENCE: [MessageHandler(filters.TEXT & ~filters.COMMAND, resume_experience)],
            RESUME_EDUCATION: [MessageHandler(filters.TEXT & ~filters.COMMAND, resume_education)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    ))

    # webhook setup
    PORT = int(os.environ.get("PORT", 10000))
    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path=BOT_TOKEN,
        webhook_url=f"https://{os.environ.get('RENDER_EXTERNAL_HOSTNAME')}/{BOT_TOKEN}"
    )

if __name__ == "__main__":
    main()

