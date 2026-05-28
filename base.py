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
    KeysUploadError,
    LocalProtocolError,
    LoginResponse,
    MegolmEvent,
    ToDeviceError,
    ToDeviceEvent,
    ToDeviceMessage,
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

    def _devices_of(self, user_id: str) -> list[str]:
        """Список device_id юзера из локального device_store (пусто, если юзер неизвестен)."""
        try:
            user_devices = self.client.device_store[user_id]
        except KeyError:
            return []
        return list(user_devices.keys())

    async def _refresh_olm_for_user(self, user_id: str) -> None:
        """Принудительно освежить device list и one-time keys для пользователя.
        Помогает пересоздать broken olm-сессию (типичная беда после ребута)."""
        try:
            await self.client.keys_query()
        except Exception as e:
            print(f"[{self.name}] keys_query({user_id}) failed: {e}")
            return
        device_ids = self._devices_of(user_id)
        if not device_ids:
            return
        try:
            await self.client.keys_claim({user_id: device_ids})
            print(f"[{self.name}] keys_claim для {user_id}: {len(device_ids)} устройств")
        except Exception as e:
            print(f"[{self.name}] keys_claim({user_id}) failed: {e}")

    async def _undecryptable_callback(self, room, event):
        """Не смогли расшифровать megolm-событие → запрашиваем room key И
        пересоздаём olm-сессию с отправителем (на случай если она «протухла»)."""
        if not isinstance(event, MegolmEvent):
            return
        print(
            f"[{self.name}] не смог расшифровать событие {event.event_id} от {event.sender}, "
            f"запрашиваю room key + пересоздаю olm..."
        )
        try:
            await self.client.request_room_key(event)
        except Exception as e:
            print(f"[{self.name}] request_room_key failed: {e}")
        await self._refresh_olm_for_user(event.sender)

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

    def _read_old_device_id(self) -> str | None:
        """Достать device_id из существующего session.json (для переиспользования при пере-логине)."""
        if not os.path.exists(self.session_path):
            return None
        try:
            with open(self.session_path) as f:
                return json.load(f).get("device_id")
        except Exception:
            return None

    async def _login_fresh(self, config: AsyncClientConfig) -> None:
        """Логин по паролю. Если есть старый device_id — переиспользуем его,
        чтобы homeserver не плодил новые устройства на каждом рестарте."""
        old_device_id = self._read_old_device_id()
        self.client = AsyncClient(
            self.homeserver,
            self.user_id,
            device_id=old_device_id,  # None при первом запуске — сервер сгенерит свой
            store_path=self.store_path,
            config=config,
        )
        response = await self.client.login(self.password, device_name=self.name)
        if not isinstance(response, LoginResponse):
            raise RuntimeError(f"login failed: {response}")
        if old_device_id and response.device_id != old_device_id:
            print(
                f"[{self.name}] WARN: homeserver выдал новый device_id "
                f"({response.device_id} вместо {old_device_id})"
            )
        self._save_session(response)

    def _restore_session_only(self, config: AsyncClientConfig) -> bool:
        """Восстановить клиента из session.json без вызова login() — olm-state
        в store/ остаётся нетронутым, верификация устройства сохраняется.
        Возвращает True при успехе, False если session.json отсутствует/битый."""
        if not (os.path.exists(self.session_path) and os.path.getsize(self.session_path) > 0):
            return False
        try:
            with open(self.session_path) as f:
                session = json.load(f)
        except Exception as e:
            print(f"[{self.name}] session.json битый ({e})")
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
        print(f"[{self.name}] восстановлена сессия из {self.session_path} (без re-login)")
        return True

    async def run(self) -> None:
        config = AsyncClientConfig(store_sync_tokens=True, encryption_enabled=True)

        # Если есть session.json — восстанавливаем сессию БЕЗ login, чтобы сохранить
        # olm-state в store/ и верификацию устройства в Element.
        # При первом запуске или если session.json удалён вручную — fresh login.
        if not self._restore_session_only(config):
            await self._login_fresh(config)

        # Диагностика E2EE.
        olm = getattr(self.client, "olm", None)
        if olm is None:
            print(
                f"[{self.name}] КРИТИЧНО: client.olm == None. matrix-nio[e2e] не работает. "
                f"Проверь: pip show matrix-nio | grep -i extras; ldconfig -p | grep olm"
            )
        else:
            ident = olm.account.identity_keys
            print(f"[{self.name}] olm identity keys: curve25519={ident.get('curve25519')[:16]}... "
                  f"ed25519={ident.get('ed25519')[:16]}...")

        # Принудительный keys_upload — даже если should_upload_keys=False, на всякий случай.
        # Иначе Element видит сессию как «not E2EE» и не даёт верифицировать.
        should = getattr(self.client, "should_upload_keys", None)
        print(f"[{self.name}] should_upload_keys={should}")
        try:
            resp = await self.client.keys_upload()
            if isinstance(resp, KeysUploadError):
                print(f"[{self.name}] keys_upload ОШИБКА: {resp}")
            else:
                print(f"[{self.name}] keys_upload OK: {resp}")
        except Exception as e:
            print(f"[{self.name}] keys_upload exception: {e}")

        self.client.encryption_trust_level = "unverified"

        # Общие колбэки.
        self.client.add_event_callback(self._invite_callback, InviteEvent)
        self.client.add_event_callback(self._undecryptable_callback, MegolmEvent)
        self.client.add_event_callback(self._device_verification_callback, (KeyVerificationEvent,))
        self.client.add_to_device_callback(self._device_verification_callback, (ToDeviceEvent,))

        # Хук для подкласса (регистрация специфичных обработчиков).
        await self.on_start()

        # Делаем один начальный sync, чтобы получить список комнат и членов.
        # full_state=True заполняет device_store через первые keys_query.
        # Если восстановленный токен мёртв — фоллбэк на свежий login.
        print(f"[{self.name}] первый sync...")
        first_sync = await self.client.sync(timeout=10000, full_state=True)
        from nio import SyncError
        if isinstance(first_sync, SyncError):
            print(f"[{self.name}] первый sync провалился ({first_sync}), пере-логин...")
            await self.client.close()
            await self._login_fresh(config)
            if self.client.should_upload_keys:
                await self.client.keys_upload()
            # Перерегистрация callbacks — новый client.
            self.client.add_event_callback(self._invite_callback, InviteEvent)
            self.client.add_event_callback(self._undecryptable_callback, MegolmEvent)
            self.client.add_event_callback(self._device_verification_callback, (KeyVerificationEvent,))
            self.client.add_to_device_callback(self._device_verification_callback, (ToDeviceEvent,))
            await self.on_start()
            await self.client.sync(timeout=10000, full_state=True)

        # Bootstrap olm-сессий со всеми членами E2EE-комнат. Это лечит «протухшие»
        # olm-сессии после ребута: новые keys_claim создают свежую сессию, и Element
        # дальше будет шарить room keys через неё.
        await self._bootstrap_olm()

        print(f"[{self.name}] запущен (E2EE), device_id={self.client.device_id}")
        try:
            await self.client.sync_forever(timeout=30000, full_state=True)
        finally:
            await self.client.close()

    async def _bootstrap_olm(self) -> None:
        """Освежить device list и pre-claim one-time keys для всех участников
        E2EE-комнат бота. Делает рестарт устойчивым к broken olm-сессиям."""
        try:
            await self.client.keys_query()
        except Exception as e:
            print(f"[{self.name}] bootstrap keys_query failed: {e}")
            return

        devices_by_user: dict[str, list[str]] = {}
        for room in self.client.rooms.values():
            if not getattr(room, "encrypted", False):
                continue
            for uid in room.users:
                if uid == self.client.user_id:
                    continue
                dids = self._devices_of(uid)
                if dids:
                    devices_by_user[uid] = dids

        if not devices_by_user:
            print(f"[{self.name}] olm bootstrap: нет участников E2EE-комнат")
            return

        total = sum(len(v) for v in devices_by_user.values())
        print(f"[{self.name}] olm bootstrap: keys_claim для {total} устройств "
              f"({len(devices_by_user)} пользователей)")
        try:
            await self.client.keys_claim(devices_by_user)
        except Exception as e:
            print(f"[{self.name}] bootstrap keys_claim failed: {e}")

    def main(self) -> None:
        """Удобный entry-point: asyncio.run(self.run()) с обработкой Ctrl+C."""
        try:
            asyncio.run(self.run())
        except KeyboardInterrupt:
            pass
