"""
Minimal i18n layer: a flat dict of key -> {lang: text}. Not a full
gettext/babel setup, but enough for real language/locale support in
bot replies and buttons without pulling in a heavier framework.

Add a language by adding its code to LANGUAGES and filling in a value
for every key below (missing keys silently fall back to English).
"""

from typing import Dict

LANGUAGES = {"en": "English", "es": "Español", "hi": "हिन्दी"}

_STRINGS: Dict[str, Dict[str, str]] = {
    "start": {
        "en": (
            "**📚 Comic/Ebook Converter Bot**\n\n"
            "Send `.cbz` `.zip` `.cbr` `.rar` `.pdf` archives, or loose images, and I'll "
            "turn them into PDF / CBZ / EPUB with the options below.\n\n"
            "**Core**\n"
            "• CBZ/CBR/ZIP/RAR ↔ PDF ↔ EPUB, in any direction\n"
            "• Multi-volume RAR sets (`.part1.rar`, `.r00`...) auto-collected\n"
            "• Merge multiple issues into one file, or split a big PDF into parts\n"
            "• Multiple loose images → one CBZ/PDF/EPUB\n\n"
            "**Page tools**\n"
            "• Split two-page spreads • Webtoon slicer • Reading direction (LTR/RTL)\n"
            "• Cover-page detection • Kindle/E-Ink optimization • Thumbnails\n\n"
            "**Output**\n"
            "• Format choice (PDF/CBZ/EPUB) • Quality presets • PDF metadata (title/author)\n"
            "• Extract images or videos straight out of an archive/PDF\n\n"
            "**Housekeeping**\n"
            "• Files auto-delete after {ttl}h • /cancel stops a running job\n"
            "• File-size warning before huge downloads\n\n"
            "Tap **⚙️ Settings** to configure output format, quality, direction and language.\n"
            "Use /help for the full command list."
        ),
        "es": (
            "**📚 Bot Conversor de Cómics/Ebooks**\n\n"
            "Envía archivos `.cbz` `.zip` `.cbr` `.rar` `.pdf`, o imágenes sueltas, y los "
            "convertiré a PDF / CBZ / EPUB con las opciones de abajo.\n\n"
            "Usa /help para ver todos los comandos, o ⚙️ **Ajustes** para configurar "
            "formato, calidad, dirección de lectura e idioma.\n"
            "Los archivos se eliminan automáticamente tras {ttl}h."
        ),
        "hi": (
            "**📚 कॉमिक/ईबुक कनवर्टर बॉट**\n\n"
            "`.cbz` `.zip` `.cbr` `.rar` `.pdf` भेजें या अलग-अलग इमेज भेजें, मैं उन्हें "
            "PDF / CBZ / EPUB में बदल दूँगा।\n\n"
            "सभी कमांड देखने के लिए /help, और फ़ॉर्मैट/क्वालिटी/भाषा सेट करने के लिए ⚙️ "
            "**Settings** दबाएँ।\n"
            "फ़ाइलें {ttl} घंटे बाद अपने आप हट जाती हैं।"
        ),
    },
    "help": {
        "en": (
            "**Commands**\n"
            "/start — welcome + feature overview\n"
            "/settings — output format, quality, direction, language\n"
            "/queue — see what's waiting\n"
            "/merge — merge all queued PDFs into one file\n"
            "/splitpdf — split a PDF you send into parts\n"
            "/cancel — stop the current job and clear your queue\n"
            "/convert — finalize a multi-volume/loose-image batch early\n"
            "/help — this menu\n\n"
            "Admins only: /admin, /stats"
        ),
        "es": "Usa /settings, /queue, /merge, /splitpdf, /cancel, /convert.",
        "hi": "/settings, /queue, /merge, /splitpdf, /cancel, /convert का उपयोग करें।",
    },
    "banned": {
        "en": "🚫 You've been blocked from using this bot.",
        "es": "🚫 Has sido bloqueado de este bot.",
        "hi": "🚫 आपको इस बॉट का उपयोग करने से रोक दिया गया है।",
    },
    "not_allowlisted": {
        "en": "🚫 This bot is currently invite-only and your account isn't on the list.",
        "es": "🚫 Este bot es solo por invitación y tu cuenta no está en la lista.",
        "hi": "🚫 यह बॉट अभी केवल आमंत्रण द्वारा है और आपका खाता सूची में नहीं है।",
    },
}


def t(key: str, lang: str = "en", **kwargs) -> str:
    entry = _STRINGS.get(key, {})
    text = entry.get(lang) or entry.get("en") or key
    try:
        return text.format(**kwargs)
    except (KeyError, IndexError):
        return text
