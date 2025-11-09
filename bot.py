import os
import io
import asyncio
import tempfile
import subprocess
from dataclasses import dataclass, field

from telegram import (
    Update, InputFile, ReplyKeyboardMarkup, ReplyKeyboardRemove
)
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, ConversationHandler,
    ContextTypes, filters
)

# ---------- CONFIG ----------
BOT_TOKEN = os.getenv("8413057502:AAE2W0XyjOC1Zne9BK_-UtgGhM4240NacGA") or "8413057502:AAE2W0XyjOC1Zne9BK_-UtgGhM4240NacGA"
WORK_DIR = os.path.join(os.path.dirname(__file__), "work")
os.makedirs(WORK_DIR, exist_ok=True)

# ---------- IMAGE ENHANCE ----------
from PIL import Image, ImageFilter, ImageOps
import cv2
import numpy as np

def enhance_image_bytes(img_bytes: bytes) -> bytes:
    # Load
    img = Image.open(io.BytesIO(img_bytes)).convert("RGB")

    # Pillow quick fixes: auto contrast + sharpen
    img = ImageOps.autocontrast(img)
    img = img.filter(ImageFilter.UnsharpMask(radius=1.4, percent=140, threshold=3))

    # Optional: light denoise via OpenCV
    arr = np.array(img)
    arr = cv2.fastNlMeansDenoisingColored(arr, None, 5, 5, 7, 21)
    img = Image.fromarray(arr)

    # Save to bytes
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=92, optimize=True)
    out.seek(0)
    return out.read()

# ---------- MP4 -> MP3 ----------
async def convert_mp4_to_mp3(src_path: str, dst_path: str):
    # ffmpeg -i input.mp4 -vn -acodec libmp3lame -q:a 2 output.mp3
    cmd = [
        "ffmpeg", "-y", "-i", src_path, "-vn",
        "-acodec", "libmp3lame", "-q:a", "2", dst_path
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {stderr.decode('utf-8', errors='ignore')}")

# ---------- RESUME BUILDER ----------
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
    w, h = A4
    x = 20*mm
    y = h - 25*mm

    def header(txt, size=18):
        nonlocal y
        c.setFont("Helvetica-Bold", size)
        c.drawString(x, y, txt)
        y -= 8*mm

    def label_val(label, val, size=11):
        nonlocal y
        c.setFont("Helvetica-Bold", size); c.drawString(x, y, f"{label}: ")
        c.setFont("Helvetica", size);      c.drawString(x+30*mm, y, val)
        y -= 6*mm

    def block(title, lines, size=12):
        nonlocal y
        if not lines: return
        c.setFont("Helvetica-Bold", size); c.drawString(x, y, title); y -= 5*mm
        c.setFont("Helvetica", 11)
        for line in lines:
            for chunk in split_line(line, 90):
                c.drawString(x+6*mm, y, f"• {chunk}")
                y -= 6*mm
        y -= 2*mm

    def split_line(s, n):  # crude wrap
        return [s[i:i+n] for i in range(0, len(s), n)]

    # Header
    header(resume.full_name)
    label_val("Email", resume.email)
    label_val("Phone", resume.phone)
    label_val("Role", resume.role)
    y -= 4*mm

    # Sections
    if resume.summary:
        block("Summary", [resume.summary])
    if resume.skills:
        block("Skills", [", ".join(resume.skills)])
    if resume.experience:
        block("Experience", resume.experience)
    if resume.education:
        block("Education", resume.education)

    c.showPage(); c.save()
    pdf = buf.getvalue()
    buf.close()
    return pdf

# ---------- TELEGRAM HANDLERS ----------
MP3_WAIT_VIDEO = 1
ENHANCE_WAIT_IMAGE = 2
(
    RESUME_NAME,
    RESUME_EMAIL,
    RESUME_PHONE,
    RESUME_ROLE,
    RESUME_SUMMARY,
    RESUME_SKILLS,
    RESUME_EXPERIENCE,
    RESUME_EDUCATION
) = range(10, 18)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Hey! I can do:\n"
        "• /mp3 – Convert MP4 to MP3\n"
        "• /enhance – Enhance a photo (contrast, sharpen, light denoise)\n"
        "• /resume – Build a simple PDF resume\n"
        "\nAll local, no paid APIs. Send /help anytime."
    )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start(update, context)

# --- MP4 -> MP3 flow ---
async def mp3_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Send me an MP4 video as a *file* (not compressed).")
    return MP3_WAIT_VIDEO

async def on_video_for_mp3(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    file_obj = msg.video or msg.document
    if not file_obj:
        await msg.reply_text("Please send a video file (MP4).")
        return MP3_WAIT_VIDEO

    # Download
    file = await file_obj.get_file()
    with tempfile.NamedTemporaryFile(dir=WORK_DIR, suffix=".mp4", delete=False) as f:
        in_path = f.name
    await file.download_to_drive(in_path)

    out_path = in_path.replace(".mp4", ".mp3")
    try:
        await msg.reply_text("Converting…")
        await convert_mp4_to_mp3(in_path, out_path)
        with open(out_path, "rb") as f:
            await msg.reply_document(document=InputFile(f, filename=os.path.basename(out_path)))
    except Exception as e:
        await msg.reply_text(f"Failed: {e}")
    finally:
        for p in [in_path, out_path]:
            try: os.remove(p)
            except: pass
    return ConversationHandler.END

# --- IMAGE ENHANCE flow ---
async def enhance_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Send me a photo/image (JPG/PNG).")
    return ENHANCE_WAIT_IMAGE

async def on_image_for_enhance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    photo = msg.photo[-1] if msg.photo else None
    file_obj = photo or msg.document
    if not file_obj:
        await msg.reply_text("Please send a photo or image file.")
        return ENHANCE_WAIT_IMAGE

    file = await file_obj.get_file()
    b = await file.download_as_bytearray()
    try:
        out = enhance_image_bytes(bytes(b))
        await msg.reply_document(document=InputFile(io.BytesIO(out), filename="enhanced.jpg"))
    except Exception as e:
        await msg.reply_text(f"Enhance failed: {e}")
    return ConversationHandler.END

# --- RESUME builder flow ---
async def resume_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["resume"] = ResumeData()
    await update.message.reply_text("Your full name?")
    return RESUME_NAME

async def resume_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["resume"].full_name = update.message.text.strip()
    await update.message.reply_text("Email?")
    return RESUME_EMAIL

async def resume_email(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["resume"].email = update.message.text.strip()
    await update.message.reply_text("Phone?")
    return RESUME_PHONE

async def resume_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["resume"].phone = update.message.text.strip()
    await update.message.reply_text("Target role/title?")
    return RESUME_ROLE

async def resume_role(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["resume"].role = update.message.text.strip()
    await update.message.reply_text("Short summary (1–3 lines)?")
    return RESUME_SUMMARY

async def resume_summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["resume"].summary = update.message.text.strip()
    await update.message.reply_text("Skills (comma separated)?")
    return RESUME_SKILLS

async def resume_skills(update: Update, context: ContextTypes.DEFAULT_TYPE):
    skills = [s.strip() for s in update.message.text.split(",") if s.strip()]
    context.user_data["resume"].skills = skills
    await update.message.reply_text("Experience (send 1–3 bullet lines; send 'done' when finished).")
    context.user_data["resume"].experience = []
    return RESUME_EXPERIENCE

async def resume_experience(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text.strip()
    if txt.lower() == "done":
        await update.message.reply_text("Education (send 1–3 lines; 'done' when finished).")
        context.user_data["resume"].education = []
        return RESUME_EDUCATION
    context.user_data["resume"].experience.append(txt)
    return RESUME_EXPERIENCE

async def resume_education(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text.strip()
    if txt.lower() == "done":
        # Build PDF
        resume = context.user_data["resume"]
        pdf = make_resume_pdf(resume)
        await update.message.reply_document(
            document=InputFile(io.BytesIO(pdf), filename="resume.pdf"),
            caption="Here is your resume PDF."
        )
        return ConversationHandler.END
    context.user_data["resume"].education.append(txt)
    return RESUME_EDUCATION

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Cancelled.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END

def main():
    if BOT_TOKEN.startswith("PASTE_"):
        raise SystemExit("Set your BOT_TOKEN env var or paste it in the file.")

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))

    # MP3 flow
    app.add_handler(ConversationHandler(
        entry_points=[CommandHandler("mp3", mp3_cmd)],
        states={
            MP3_WAIT_VIDEO: [MessageHandler(filters.VIDEO | filters.Document.VIDEO, on_video_for_mp3)]
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        name="mp3_flow", persistent=False
    ))

    # Enhance flow
    app.add_handler(ConversationHandler(
        entry_points=[CommandHandler("enhance", enhance_cmd)],
        states={
            ENHANCE_WAIT_IMAGE: [MessageHandler(
                filters.PHOTO | filters.Document.IMAGE, on_image_for_enhance
            )]
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        name="enhance_flow", persistent=False
    ))

    # Resume flow
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
        name="resume_flow", persistent=False
    ))

    print("Bot running… Press Ctrl+C to stop.")
    app.run_polling(close_loop=False)

if __name__ == "__main__":
    main()
