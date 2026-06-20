from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from ..core.models import Destination


def destination_buttons(token: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Local", callback_data=f"dest:local:{token}")],
            [
                InlineKeyboardButton("Telegram", callback_data=f"dest:telegram:{token}"),
                InlineKeyboardButton("Google Drive", callback_data=f"dest:gdrive:{token}"),
            ],
            [InlineKeyboardButton("BuzzHeavier", callback_data=f"dest:buzzheavier:{token}")],
        ]
    )


def local_buttons(token: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("Movies", callback_data=f"local:movies:{token}"), InlineKeyboardButton("Series", callback_data=f"local:series:{token}")]]
    )


def ytdlp_buttons(token: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("Video", callback_data=f"ytkind:video:{token}"), InlineKeyboardButton("Audio", callback_data=f"ytkind:audio:{token}")]]
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


def jellyfin_buttons(jellyfin_url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Open Jellyfin", url=jellyfin_url)],
            [
                InlineKeyboardButton("Start", callback_data="jf:start"),
                InlineKeyboardButton("Stop", callback_data="jf:stop"),
            ],
            [
                InlineKeyboardButton("Restart", callback_data="jf:restart"),
                InlineKeyboardButton("Refresh", callback_data="jf:refresh"),
            ],
            [InlineKeyboardButton("Scan Library", callback_data="jf:scan")],
        ]
    )


def completion_buttons(task, jellyfin_url: str) -> InlineKeyboardMarkup | None:
    if task.destination == Destination.GOOGLE_DRIVE and task.result_links:
        return InlineKeyboardMarkup([[InlineKeyboardButton("Open Google Drive", url=task.result_links[0])]])
    if task.destination == Destination.BUZZHEAVIER and task.result_links:
        if len(task.result_links) == 1:
            return InlineKeyboardMarkup([[InlineKeyboardButton("Open BuzzHeavier", url=task.result_links[0])]])
        buttons = [
            InlineKeyboardButton(f"Open {index}", url=link)
            for index, link in enumerate(task.result_links[:10], start=1)
        ]
        return InlineKeyboardMarkup([buttons[index : index + 2] for index in range(0, len(buttons), 2)])
    if task.destination in {Destination.LOCAL_MOVIES, Destination.LOCAL_SERIES}:
        return InlineKeyboardMarkup([[InlineKeyboardButton("Open Jellyfin", url=jellyfin_url)]])
    return None
