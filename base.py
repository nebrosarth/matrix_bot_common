"""
MatrixBot — общая база для ботов, построенных на matrix-nio.

Что делает:
  - Грузит конфиг из JSON (homeserver / user_id / password / store_path).
  - Поднимает AsyncClient с E2EE.
  - Сохраняет/восстанавливает сессию в session.json.
  - Авто-принимает приглашения в комнаты.
  - Обрабатывает SAS-верификацию устройств (emoji, auto-accept).
  - Запускает sync_forever.

Подклассы должны переопределить on_start() и зарегистрировать в нём свои
event-callbacks через self.client.add_event_callback(...).
"""
import asyncio
import json
import os
import sys
import time

from nio import (
    AsyncClient,
    AsyncClientConfig,
    InviteEvent,
    KeyVerificationCancel,
    KeyVerificationEvent,
    KeyVerificationKey,
    KeyVerificationMac,
    KeyVerificationStart,
    LocalProtocolError,
    LoginResponse,
    ToDeviceError,
    ToDeviceEvent,
    ToDeviceMessage,
    WhoamiError,
)


class MatrixBot:
    """База для Matrix-ботов с E2EE."""

    # Имя — для логов. Подклассы могут переопределить.
    name = "MatrixBot"

    def __init__(self, config_path: str = "config.json", session_path: str = "session.json"):
        if not os.path.exists(config_path):
            print(
                f"Файл {config_path} не найден. "
                f"Скопируйте config.example.json -> {config_path} и заполните."
            )
            sys.exit(1)

        with open(config_path, encoding="utf-8") as f:
            self.config = json.load(f)

        self.homeserver: str = self.config["homeserver"]
        self.user_id: str = self.config["user_id"]
        self.password: str = self.config["password"]
        self.store_path: str = self.config.get("store_path", "./store")
        self.session_path = session_path

        if not os.path.exists(self.store_path):
            os.makedirs(self.store_path, mode=0o700)

        self.client: AsyncClient = None  # type: ignore[assignment]

    # ---- API для подклассов ----

    async def on_start(self) -> None:
        """Вызывается после логина перед sync_forever. Переопределите для регистрации
        своих event-callbacks через self.client.add_event_callback(...)."""
        pass

    async def send_text(self, room_id: str, text: str) -> None:
        await self.client.room_send(
            room_id=room_id,
            message_type="m.room.message",
            content={"msgtype": "m.text", "body": text},
            ignore_unverified_devices=True,
        )

    @staticmethod
    def is_e2ee_room(room) -> bool:
        return bool(getattr(room, "encrypted", False))

    # ---- Внутренние колбэки ----

    async def _invite_callback(self, room, event):
        print(f"[{self.name}] Приглашение в комнату {room.room_id}. Принимаю...")
        await self.client.join(room.room_id)

    async def _device_verification_callback(self, event):
        """Auto-accept SAS (emoji) device verification."""
        try:
            if event.source["type"] == "m.key.verification.request":
                if "m.sas.v1" not in event.source["content"]["methods"]:
                    print(
                        f"Other device does not support SAS authentication. "
                        f"Methods: {event.source['content']['methods']}."
                    )
                    return
                assert self.client.device_id is not None
                assert self.client.user_id is not None
                txid = event.source["content"]["transaction_id"]
                ready_event = ToDeviceMessage(
                    type="m.key.verification.ready",
                    recipient=event.sender,
                    recipient_device=event.source["content"]["from_device"],
                    content={
                        "from_device": self.client.device_id,
                        "methods": ["m.sas.v1"],
                        "transaction_id": txid,
                    },
                )
                resp = await self.client.to_device(ready_event, txid)
                if isinstance(resp, ToDeviceError):
                    print(f"to_device failed with {resp}")
            elif isinstance(event, KeyVerificationStart):
                if "emoji" not in event.short_authentication_string:
                    print(f"Other device does not support emoji verification.")
                    return
                resp = await self.client.accept_key_verification(event.transaction_id)
                if isinstance(resp, ToDeviceError):
                    print(f"accept_key_verification failed with {resp}")
                sas = self.client.key_verifications[event.transaction_id]
                resp = await self.client.to_device(sas.share_key())
                if isinstance(resp, ToDeviceError):
                    print(f"to_device failed with {resp}")
            elif isinstance(event, KeyVerificationCancel):
                print(f"Verification cancelled by {event.sender}: {event.reason}")
            elif isinstance(event, KeyVerificationKey):
                sas = self.client.key_verifications[event.transaction_id]
                print(f"{sas.get_emoji()}")
                time.sleep(1)
                resp = await self.client.confirm_short_auth_string(event.transaction_id)
                if isinstance(resp, ToDeviceError):
                    print(f"confirm_short_auth_string failed with {resp}")
                done_message = ToDeviceMessage(
                    type="m.key.verification.done",
                    recipient=event.sender,
                    recipient_device=sas.other_olm_device.device_id,
                    content={"transaction_id": sas.transaction_id},
                )
                resp = await self.client.to_device(done_message, sas.transaction_id)
                if isinstance(resp, ToDeviceError):
                    print(f"'done' failed with {resp}")
            elif isinstance(event, KeyVerificationMac):
                sas = self.client.key_verifications[event.transaction_id]
                try:
                    todevice_msg = sas.get_mac()
                except LocalProtocolError as e:
                    print(f"Cancelled or protocol error: {e}")
                else:
                    resp = await self.client.to_device(todevice_msg)
                    if isinstance(resp, ToDeviceError):
                        print(f"to_device failed with {resp}")
            elif event.source["type"] == "m.key.verification.done":
                print("Emoji verification successful.")
            else:
                print(f"Unexpected verification event type {type(event)}; ignored.")
        except BaseException as e:
            print(e)

    # ---- Main loop ----

    def _save_session(self, response: LoginResponse) -> None:
        with open(self.session_path, "w") as f:
            json.dump(
                {
                    "access_token": response.access_token,
                    "device_id": response.device_id,
                    "user_id": response.user_id,
                },
                f,
            )

    def _delete_session(self) -> None:
        try:
            os.remove(self.session_path)
        except FileNotFoundError:
            pass

    async def _login_fresh(self, config: AsyncClientConfig) -> None:
        """Чистый логин по паролю + сохранение session.json."""
        self.client = AsyncClient(
            self.homeserver, self.user_id, store_path=self.store_path, config=config
        )
        response = await self.client.login(self.password, device_name=self.name)
        if not isinstance(response, LoginResponse):
            raise RuntimeError(f"login failed: {response}")
        self._save_session(response)

    async def _try_restore_session(self, config: AsyncClientConfig) -> bool:
        """Восстановить клиента из session.json и проверить токен через whoami().
        Возвращает True, если токен валиден. False — если нужно делать fresh-login."""
        if not (os.path.exists(self.session_path) and os.path.getsize(self.session_path) > 0):
            return False
        try:
            with open(self.session_path) as f:
                session = json.load(f)
        except Exception as e:
            print(f"[{self.name}] session.json повреждён ({e}); удаляю.")
            self._delete_session()
            return False

        self.client = AsyncClient(
            self.homeserver,
            session["user_id"],
            device_id=session["device_id"],
            store_path=self.store_path,
            config=config,
        )
        self.client.access_token = session["access_token"]
        self.client.user_id = session["user_id"]
        self.client.device_id = session["device_id"]

        # Проверка живости токена.
        resp = await self.client.whoami()
        if isinstance(resp, WhoamiError):
            print(f"[{self.name}] session.json протух ({resp}); делаю fresh-login.")
            await self.client.close()
            self._delete_session()
            return False
        return True

    async def run(self) -> None:
        config = AsyncClientConfig(store_sync_tokens=True, encryption_enabled=True)

        # Сначала пробуем восстановить сессию, иначе — fresh login. БЕЗ двойного логина.
        if not await self._try_restore_session(config):
            await self._login_fresh(config)

        if self.client.should_upload_keys:
            await self.client.keys_upload()

        self.client.encryption_trust_level = "unverified"

        # Общие колбэки.
        self.client.add_event_callback(self._invite_callback, InviteEvent)
        self.client.add_event_callback(self._device_verification_callback, (KeyVerificationEvent,))
        self.client.add_to_device_callback(self._device_verification_callback, (ToDeviceEvent,))

        # Хук для подкласса (регистрация специфичных обработчиков).
        await self.on_start()

        print(f"[{self.name}] запущен (E2EE), device_id={self.client.device_id}")
        try:
            await self.client.sync_forever(timeout=30000, full_state=True)
        finally:
            await self.client.close()

    def main(self) -> None:
        """Удобный entry-point: asyncio.run(self.run()) с обработкой Ctrl+C."""
        try:
            asyncio.run(self.run())
        except KeyboardInterrupt:
            pass
