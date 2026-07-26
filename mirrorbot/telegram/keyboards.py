from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from ..core.models import Destination


def destination_buttons(token: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "Telegram", callback_data=f"dest:telegram:{token}"
                ),
                InlineKeyboardButton("Cloudflare R2", callback_data=f"dest:r2:{token}"),
            ],
        ]
    )


def ytdlp_buttons(token: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Video", callback_data=f"ytkind:video:{token}"),
                InlineKeyboardButton("Audio", callback_data=f"ytkind:audio:{token}"),
            ]
        ]
    )


def ytdlp_video_buttons(token: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("360p", callback_data=f"yt:video:360:{token}"),
                InlineKeyboardButton("480p", callback_data=f"yt:video:480:{token}"),
            ],
            [
                InlineKeyboardButton("720p", callback_data=f"yt:video:720:{token}"),
                InlineKeyboardButton("1080p", callback_data=f"yt:video:1080:{token}"),
            ],
            [InlineKeyboardButton("Back", callback_data=f"ytkind:back:{token}")],
        ]
    )


def ytdlp_audio_buttons(token: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("64 kbps", callback_data=f"yt:audio:64:{token}"),
                InlineKeyboardButton("128 kbps", callback_data=f"yt:audio:128:{token}"),
            ],
            [
                InlineKeyboardButton("192 kbps", callback_data=f"yt:audio:192:{token}"),
                InlineKeyboardButton("256 kbps", callback_data=f"yt:audio:256:{token}"),
            ],
            [InlineKeyboardButton("320 kbps", callback_data=f"yt:audio:320:{token}")],
            [InlineKeyboardButton("Back", callback_data=f"ytkind:back:{token}")],
        ]
    )


def completion_buttons(task) -> InlineKeyboardMarkup | None:
    if task.destination == Destination.CLOUDFLARE_R2 and task.result_links:
        return InlineKeyboardMarkup(
            [[InlineKeyboardButton("Download", url=task.result_links[0])]]
        )
    return None
