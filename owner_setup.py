"""Interactive owner setup only; never registered as an MCP tool or runtime command."""
from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import os
import sys
import tempfile
from pathlib import Path

from telethon import TelegramClient, types
from telethon.errors import (
    FloodWaitError,
    PasswordHashInvalidError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    SessionPasswordNeededError,
)
from telethon.sessions import StringSession
from telethon.tl.types import Channel, Chat
from telethon.utils import get_peer_id


async def stop_owner_updates(client) -> None:
    await client.set_receive_updates(False)
    listener = getattr(client, "_updates_handle", None)
    if listener is not None and listener is not asyncio.current_task():
        listener.cancel()
        await asyncio.gather(listener, return_exceptions=True)


class OwnerLoginClient(TelegramClient):
    async def _update_loop(self):
        self._updates_error = None
        try:
            while self.is_connected():
                incoming = await self._updates_queue.get()
                if isinstance(incoming, types.UpdateShort):
                    updates = [incoming.update]
                elif isinstance(incoming, types.Updates | types.UpdatesCombined):
                    updates = incoming.updates
                else:
                    updates = [incoming]
                for update in updates:
                    if isinstance(update, types.UpdateLoginToken):
                        await self._dispatch_update(update)
        except asyncio.CancelledError:
            return

    async def _on_login(self, user):
        await stop_owner_updates(self)
        self._mb_entity_cache.set_self_user(user.id, user.bot, user.access_hash)
        self._authorized = True
        return user


def write_private_json(path: Path, data) -> None:
    if path.exists():
        raise ValueError("Output already exists; setup will not overwrite it.")
    descriptor, temporary = tempfile.mkstemp(prefix=".telegram-setup-", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(data, output, ensure_ascii=False, indent=2)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.link(temporary, path)
    finally:
        os.unlink(temporary)


def select_chats(dialogs, titles):
    chosen = []
    for title in titles:
        matches = [dialog for dialog in dialogs if dialog.name == title and dialog.is_group]
        if len(matches) != 1:
            raise ValueError("An exact permitted group title is missing or ambiguous; no policy was saved.")
        entity = matches[0].entity
        if isinstance(entity, Channel):
            peer_type = "channel"
        elif isinstance(entity, Chat):
            peer_type = "chat"
        else:
            raise ValueError("The selected entity is not a group.")
        entry = {
            "id": get_peer_id(entity), "alias": f"group{len(chosen)+1}",
            "title": title, "peer_type": peer_type,
        }
        if peer_type == "channel":
            access_hash = getattr(entity, "access_hash", None)
            if not isinstance(access_hash, int):
                raise ValueError("The selected group has no usable access hash.")
            entry["access_hash"] = access_hash
        chosen.append(entry)
    if len({entry["id"] for entry in chosen}) != len(chosen):
        raise ValueError("The selected group identities are duplicated.")
    return {"chats": chosen}


class AuthenticationStopped(Exception):
    """Only fixed, safe owner-facing messages may be passed to this exception."""


async def sign_in_password(client) -> None:
    for attempt in range(1, 4):
        password = getpass.getpass(f"Telegram 2FA password (hidden, attempt {attempt}/3): ")
        if not password:
            print("The 2FA password cannot be empty.")
            del password
            continue
        try:
            await client.sign_in(password=password)
            return
        except PasswordHashInvalidError:
            print("The 2FA password was not accepted. Check the password and keyboard layout.")
        finally:
            del password
    raise AuthenticationStopped("Three password attempts failed. Setup stopped; no credentials were saved.")


async def sign_in_code(client, phone, phone_code_hash) -> None:
    print("Use the latest Telegram code for this current setup login, "
          "not a code for my.telegram.org website login.")
    print("No replacement code will be requested automatically.")
    for attempt in range(1, 4):
        code = "".join(getpass.getpass(f"Telegram login code (hidden, attempt {attempt}/3): ").split())
        if not code or not code.isascii() or not code.isdigit():
            print("The login code must contain digits only; whitespace is ignored.")
            del code
            continue
        try:
            await client.sign_in(phone, code, phone_code_hash=phone_code_hash)
            return
        except PhoneCodeInvalidError:
            print("Code not accepted. Check the latest code for this setup login and try again.")
        except PhoneCodeExpiredError:
            raise AuthenticationStopped(
                "The login code expired. Setup stopped; start a new setup login when ready."
            ) from None
        except SessionPasswordNeededError:
            await sign_in_password(client)
            return
        finally:
            del code
    raise AuthenticationStopped(
        "Three code attempts failed. Setup stopped; no replacement code was requested."
    )


def code_delivery_message(sent) -> str:
    from telethon.tl.types.auth import (
        SentCodeTypeApp,
        SentCodeTypeCall,
        SentCodeTypeEmailCode,
        SentCodeTypeSms,
    )
    messages = {
        SentCodeTypeApp: ("Telegram selected code delivery to an already signed-in Telegram app. "
                          "Check the verified Telegram service chat."),
        SentCodeTypeSms: "Telegram selected SMS delivery. Check SMS on the account's phone.",
        SentCodeTypeCall: "Telegram selected a phone call. Use the numeric code from that call.",
        SentCodeTypeEmailCode: "Telegram selected email delivery. Check the email configured for login.",
    }
    message = messages.get(type(getattr(sent, "type", None)))
    if message is None:
        raise AuthenticationStopped(
            "Telegram selected a login method this code flow does not support. "
            "Stop and use local QR login instead."
        )
    return message


def clear_local_qr() -> None:
    if sys.stdout.isatty():
        sys.stdout.write("\x1b[2J\x1b[3J\x1b[H")
        sys.stdout.flush()


async def wait_with_local_qr(qr) -> None:
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise AuthenticationStopped("QR login requires a local interactive terminal.")
    vendor = Path(__file__).resolve().parent / "owner_vendor" / "qrcode-8.2-py3-none-any.whl"
    if not vendor.is_file():
        raise AuthenticationStopped("The local QR renderer is missing; owner installation must be completed.")
    sys.path.insert(0, str(vendor))
    import qrcode
    waiter = asyncio.create_task(qr.wait())
    try:
        await asyncio.sleep(0)
        print("In your signed-in Telegram phone app: "
              "Settings > Devices > Link Desktop Device, then scan this QR.")
        print("This QR is a login credential. Do not share or record this terminal. "
              "It is not saved to a file.")
        renderer = qrcode.QRCode(border=4)
        renderer.add_data(qr.url)
        renderer.make(fit=True)
        renderer.print_ascii(out=sys.stdout, invert=True)
        await asyncio.wait_for(waiter, timeout=120)
    finally:
        clear_local_qr()
        if not waiter.done():
            waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)


async def sign_in_qr(client) -> None:
    try:
        print("On your phone, open Telegram > Settings > Devices > Link Desktop Device.")
        input("When the phone scanner is ready, press Enter to show the local QR: ")
        for attempt in range(1, 4):
            qr = await client.qr_login()
            try:
                await wait_with_local_qr(qr)
                return
            except SessionPasswordNeededError:
                await stop_owner_updates(client)
                await sign_in_password(client)
                return
            except asyncio.TimeoutError:
                if attempt == 3:
                    raise AuthenticationStopped(
                        "Three QR codes expired. Setup stopped; no credentials were saved."
                    ) from None
                choice = input(
                    "QR expired. Press Enter to create a new QR, or type q to cancel: "
                ).strip().lower()
                if choice:
                    raise AuthenticationStopped("QR login cancelled; no credentials were saved.") from None
    finally:
        await stop_owner_updates(client)


async def setup(args) -> None:
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise ValueError("Owner setup requires a local interactive terminal.")
    if not args.chat_title or len(set(args.chat_title)) != len(args.chat_title):
        raise ValueError("Provide distinct exact group titles.")
    credentials_path = Path(args.credentials).resolve()
    policy_path = Path(args.policy).resolve()
    if credentials_path == policy_path or credentials_path.exists() or policy_path.exists():
        raise ValueError("Use two distinct, new owner-only output files.")
    for path in (credentials_path, policy_path):
        if not path.parent.is_dir():
            raise ValueError("Output parent directories must already exist.")
    proxy_port = int(os.environ["TG_LOCAL_PROXY_PORT"])
    if not 1 <= proxy_port <= 65535:
        raise ValueError("Invalid local proxy port.")
    print("Local owner setup. Secrets stay in the protected service credential file.")
    method = input("Login method: press Enter for local QR, or type code: ").strip().lower() or "qr"
    if method not in {"qr", "code"}:
        raise AuthenticationStopped("Unknown login method. Choose QR or code on the next setup attempt.")
    api_id = int(getpass.getpass("Telegram API ID (hidden): "))
    api_hash = getpass.getpass("Telegram API hash (hidden): ").strip()
    if api_id <= 0 or not api_hash:
        raise ValueError("Telegram API credentials are required.")
    client = OwnerLoginClient(
        StringSession(), api_id, api_hash, receive_updates=(method == "qr"),
        flood_sleep_threshold=0,
        proxy=("socks5", "127.0.0.1", proxy_port, False),
        device_model="Telegram Read Only", system_version="1.0", app_version="0.7.0-local1",
    )
    saved = False
    try:
        await client.connect()
        if method == "qr":
            try:
                await sign_in_qr(client)
            finally:
                await stop_owner_updates(client)
        else:
            phone = getpass.getpass("Phone number with country code (hidden): ").strip()
            sent = await client.send_code_request(phone)
            print(code_delivery_message(sent))
            await sign_in_code(client, phone, sent.phone_code_hash)
            del phone, sent
        if not await client.is_user_authorized():
            raise ValueError("Telegram authorization was not completed.")
        dialogs = await client.get_dialogs()
        policy = select_chats(dialogs, args.chat_title)
        del dialogs
        print("Only these exact groups will be exposed to the read-only service:")
        for entry in policy["chats"]:
            print(f"  {entry['title']} — ID {entry['id']}")
        if input("Confirm the two group identities locally by typing YES: ") != "YES":
            raise ValueError("Owner did not confirm; no files saved.")
        credentials = {"api_id": api_id, "api_hash": api_hash, "session": client.session.save()}
        write_private_json(credentials_path, credentials)
        try:
            write_private_json(policy_path, policy)
        except BaseException:
            credentials_path.unlink()
            raise
        saved = True
        print("Credentials and the selected-chat policy were saved locally. No messages were printed.")
    finally:
        if not saved:
            try:
                if client.is_connected() and await client.is_user_authorized():
                    await client.log_out()
            except Exception:
                print("Setup stopped. Check Telegram Devices and revoke "
                      "the unfinished setup session if present.")
        await client.disconnect()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--credentials", required=True)
    parser.add_argument("--policy", required=True)
    parser.add_argument("--chat-title", action="append", required=True)
    args = parser.parse_args()
    try:
        asyncio.run(setup(args))
    except AuthenticationStopped as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except FloodWaitError as exc:
        print(f"Telegram requires waiting {max(0, int(exc.seconds))} seconds. "
              "Setup stopped without retrying.", file=sys.stderr)
        return 1
    except (Exception, KeyboardInterrupt) as exc:
        print(f"Setup stopped ({type(exc).__name__}). "
              "No secret values or other chats are shown.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
