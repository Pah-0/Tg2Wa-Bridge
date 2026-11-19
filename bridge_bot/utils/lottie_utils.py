"""Bridges WhatsApp Lottie stickers to Telegram, and back.

A WhatsApp Lottie sticker (.was) is a zip holding the Lottie animation and
a token WhatsApp signs it with, so a Telegram sticker can't be turned into
one. The .was of each bridged sticker is kept instead, so when it's sent
again on Telegram, WhatsApp gets the original Lottie sticker.
"""

import asyncio
import base64
import gzip
import io
import json
import mimetypes
import os
import posixpath
import zipfile

from neonize.proto.waE2E.WAWebProtobufsE2E_pb2 import (
    FutureProofMessage,
    Message,
    StickerMessage,
)
from neonize.utils.enum import MediaType
from wand.image import Image as wand_image

from bridge_bot.config import bot

from .bot_utils import read_binary, write_binary
from .media_utils import lottie_to_tg_video_sticker
from .os_utils import file_exists

# Telegram's limits for animated (.tgs) stickers.
TGS_MAX = 64 * 1024
TGS_SIZE = 512
# Telegram's docs say 3 seconds, but many WA Lottie stickers run up to 4,
# and the video fallback looks worse.
TGS_MAX_SECONDS = 4
TGS_FPS = (30, 60)

WAS_CACHE = "chat_dbs/was_stickers"
# Oldest are removed past this many.
WAS_CACHE_MAX = 1000


def is_lottie_sticker(sticker) -> bool:
    return bool(sticker.isLottie) or sticker.mimetype == "application/was"


def inline_images(animation, archive, base):
    """Embeds image assets kept as separate files in the archive."""
    for asset in animation.get("assets") or []:
        path = asset.get("p")
        if not path or asset.get("e") or path.startswith("data:"):
            continue
        name = posixpath.normpath(posixpath.join(base, asset.get("u") or "", path))
        try:
            data = archive.read(name)
        except KeyError:
            continue
        mime = mimetypes.guess_type(path)[0] or "image/png"
        asset["u"], asset["e"] = "", 1
        asset["p"] = f"data:{mime};base64,{base64.b64encode(data).decode()}"


def was_animation(was: bytes) -> dict:
    """The Lottie animation inside a .was sticker."""
    with zipfile.ZipFile(io.BytesIO(was)) as archive:
        names = [n for n in archive.namelist() if n.endswith(".json")]
        if not names:
            raise ValueError("No Lottie animation in the .was sticker")
        # There can be a secondary animation too; the main one is this.
        name = next(
            (n for n in names if posixpath.basename(n) == "animation.json"), names[0]
        )
        animation = json.loads(archive.read(name))
        inline_images(animation, archive, posixpath.dirname(name))
    return animation


def has_expressions(node) -> bool:
    """Whether any property is driven by an expression (a string "x")."""
    if isinstance(node, dict):
        return isinstance(node.get("x"), str) or any(
            has_expressions(v) for v in node.values()
        )
    if isinstance(node, list):
        return any(has_expressions(v) for v in node)
    return False


def fit_canvas(animation):
    """Scales the animation to Telegram's 512x512 canvas, centred."""
    w, h = animation["w"], animation["h"]
    if (w, h) == (TGS_SIZE, TGS_SIZE):
        return
    scale = TGS_SIZE / max(w, h) * 100
    comp_id = "__bridge_canvas"
    animation.setdefault("assets", []).append(
        {"id": comp_id, "layers": animation["layers"]}
    )
    animation["layers"] = [
        {
            "ddd": 0,
            "ind": 1,
            "ty": 0,
            "nm": "canvas",
            "refId": comp_id,
            "sr": 1,
            "ks": {
                "o": {"a": 0, "k": 100},
                "r": {"a": 0, "k": 0},
                "p": {"a": 0, "k": [TGS_SIZE / 2, TGS_SIZE / 2, 0]},
                "a": {"a": 0, "k": [w / 2, h / 2, 0]},
                "s": {"a": 0, "k": [scale, scale, 100]},
            },
            "ao": 0,
            "w": w,
            "h": h,
            "ip": animation["ip"],
            "op": animation["op"],
            "st": 0,
            "bm": 0,
        }
    ]
    animation["w"] = animation["h"] = TGS_SIZE


def animation_to_tgs(animation: dict) -> bytes | None:
    """The animation as a Telegram animated sticker, or None if it's longer,
    bigger or uses more than Telegram allows."""
    fps = animation.get("fr")
    if fps not in TGS_FPS:
        return None
    if (animation["op"] - animation["ip"]) / fps > TGS_MAX_SECONDS:
        return None
    # Telegram doesn't draw images, expressions or 3D layers.
    if any("p" in asset for asset in animation.get("assets") or []):
        return None
    if animation.get("ddd") or has_expressions(animation):
        return None
    animation = json.loads(json.dumps(animation))
    fit_canvas(animation)
    animation["tgs"] = 1
    tgs = gzip.compress(
        json.dumps(animation, separators=(",", ":")).encode(), compresslevel=9
    )
    return tgs if len(tgs) <= TGS_MAX else None


def _was_to_tgs(was):
    animation = was_animation(was)
    return animation, animation_to_tgs(animation)


async def was_to_tg_sticker(was: bytes) -> tuple[bytes, str]:
    """A .was sticker as a Telegram one: (data, file extension).

    An animated sticker when it fits Telegram's limits, else a video one.
    """
    animation, tgs = await asyncio.to_thread(_was_to_tgs, was)
    if tgs:
        return tgs, "tgs"
    return await lottie_to_tg_video_sticker(animation), "webm"


def png_to_webp(png: bytes) -> bytes:
    with wand_image(blob=png) as img:
        img.format = "webp"
        return img.make_blob()


def was_cache_path(document_id) -> str:
    return f"{WAS_CACHE}/{document_id}.was"


async def cache_was(document_id, was: bytes):
    """Keeps a bridged .was sticker by the Telegram document it became."""
    os.makedirs(WAS_CACHE, exist_ok=True)
    await write_binary(was_cache_path(document_id), was)
    entries = sorted(os.scandir(WAS_CACHE), key=lambda e: e.stat().st_mtime)
    for entry in entries[:-WAS_CACHE_MAX]:
        os.remove(entry.path)


async def cached_was(document_id) -> bytes | None:
    path = was_cache_path(document_id)
    return await read_binary(path) if file_exists(path) else None


async def send_was_sticker(wa_jid, was: bytes, quoted=None):
    """Sends a .was file to WA as a Lottie sticker."""
    animation = was_animation(was)
    upload = await bot.client.upload(was, MediaType.MediaImage)
    sticker = StickerMessage(
        URL=upload.url,
        directPath=upload.DirectPath,
        fileEncSHA256=upload.FileEncSHA256,
        fileLength=upload.FileLength,
        fileSHA256=upload.FileSHA256,
        mediaKey=upload.MediaKey,
        mimetype="application/was",
        isAnimated=True,
        isLottie=True,
        width=int(animation.get("w", TGS_SIZE)),
        height=int(animation.get("h", TGS_SIZE)),
    )
    if quoted:
        sticker.contextInfo.MergeFrom(bot.client._make_quoted_message(quoted))
    message = Message(
        lottieStickerMessage=FutureProofMessage(message=Message(stickerMessage=sticker))
    )
    return await bot.client.send_message(wa_jid, message)
