# telegram_bot.py — Мультиаккаунт + экспорт участников группы + мгновенная работа с любыми ID
import os
import requests
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.tl.types import PeerUser, PeerChannel, PeerChat
from telethon.tl.functions.messages import GetDialogsRequest, GetDialogFiltersRequest
from telethon.tl.types import InputPeerEmpty
from telethon.errors import SessionPasswordNeededError  # Добавлен импорт
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, validator
from contextlib import asynccontextmanager
from typing import List, Optional, Union, Dict
import uvicorn

API_ID = 31407487
API_HASH = "0b82a91fb5c797a2bf713ad3d46a9c20"
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "")

# Хранилище: имя → клиент
ACTIVE_CLIENTS = {}
PENDING_AUTH = {}  # Формат: {phone: {"session_str": "...", "phone_code_hash": "..."}}


# ==================== Модели ====================
class SendMessageReq(BaseModel):
    account: str
    chat_id: str | int
    text: str

class AddAccountReq(BaseModel):
    name: str
    session_string: str

class RemoveAccountReq(BaseModel):
    name: str

class AuthStartReq(BaseModel):
    phone: str

class AuthCodeReq(BaseModel):
    phone: str
    code: str
    phone_code_hash: str  # <-- ОБЯЗАТЕЛЬНО добавляем этот параметр!
    password: str | None = None

class ExportMembersReq(BaseModel):
    account: str          # имя аккаунта (сессии)
    group: str | int      # ID группы или @username

# ==================== Новые модели ====================
class DialogInfo(BaseModel):
    id: int
    title: str
    username: Optional[str] = None
    folder_names: List[str] = []  # Список названий папок
    is_group: bool
    is_channel: bool
    is_user: bool
    unread_count: int
    last_message_date: Optional[str] = None

class GetDialogsReq(BaseModel):
    account: str
    limit: int = 50
    include_folders: bool = True  # Включить информацию о папках

class ChatMessage(BaseModel):
    id: int
    date: str
    from_id: Optional[int] = None
    text: str
    is_outgoing: bool
    
    @validator('from_id', pre=True)
    def parse_from_id(cls, v):
        """Парсим from_id из разных форматов"""
        if v is None:
            return None
        if isinstance(v, (PeerUser, PeerChannel, PeerChat)):
            return v.user_id if isinstance(v, PeerUser) else v.channel_id if isinstance(v, PeerChannel) else v.chat_id
        if isinstance(v, int):
            return v
        if isinstance(v, str) and v.isdigit():
            return int(v)
        return None

class GetChatHistoryReq(BaseModel):
    account: str
    chat_id: Union[str, int]
    limit: int = 50
    offset_id: Optional[int] = None


# ==================== Вспомогательная функция ====================
def extract_folder_title(folder_obj):
    """Извлечь текстовое название папки из объекта"""
    if not hasattr(folder_obj, 'title'):
        return None
    
    title_obj = folder_obj.title
    if hasattr(title_obj, 'text'):
        return title_obj.text
    elif isinstance(title_obj, str):
        return title_obj
    return None


async def get_dialogs_with_folders_info(client: TelegramClient, limit: int = 50) -> List[DialogInfo]:
    """
    Получить диалоги с информацией о папках
    """
    try:
        # 1. Получаем все папки (диалоговые фильтры)
        folder_info = {}
        try:
            dialog_filters_result = await client(GetDialogFiltersRequest())
            # Получаем список папок из атрибута .filters
            dialog_filters = getattr(dialog_filters_result, 'filters', [])
            
            for folder in dialog_filters:
                # Извлекаем текстовое название папки
                folder_title = extract_folder_title(folder)
                
                if hasattr(folder, 'id') and folder_title:
                    # Сохраняем информацию о папке
                    folder_info[folder.id] = {
                        'title': folder_title,  # Сохраняем строку, а не объект
                        'color': getattr(folder, 'color', None),
                        'pinned': getattr(folder, 'pinned', False),
                        'include_peers': [],  # Список диалогов в этой папке
                        'exclude_peers': []   # Исключенные диалоги (если есть)
                    }
                    
                    # Получаем включенные диалоги для этой папке
                    if hasattr(folder, 'include_peers'):
                        for peer in folder.include_peers:
                            peer_id = None
                            if hasattr(peer, 'user_id'):
                                peer_id = peer.user_id
                            elif hasattr(peer, 'chat_id'):
                                peer_id = peer.chat_id
                            elif hasattr(peer, 'channel_id'):
                                peer_id = peer.channel_id
                            
                            if peer_id:
                                folder_info[folder.id]['include_peers'].append(peer_id)
                    
                    # Получаем исключенные диалоги (если есть)
                    if hasattr(folder, 'exclude_peers'):
                        for peer in folder.exclude_peers:
                            peer_id = None
                            if hasattr(peer, 'user_id'):
                                peer_id = peer.user_id
                            elif hasattr(peer, 'chat_id'):
                                peer_id = peer.chat_id
                            elif hasattr(peer, 'channel_id'):
                                peer_id = peer.channel_id
                            
                            if peer_id:
                                folder_info[folder.id]['exclude_peers'].append(peer_id)
        except Exception as e:
            print(f"Не удалось получить информацию о папках: {e}")
        
        # 2. Получаем диалоги
        dialogs = await client.get_dialogs(limit=limit)
        
        # 3. Создаем маппинг ID диалога -> список названий папок
        dialog_to_folders = {}
        
        # Проходим по всем папкам и заполняем маппинг
        for folder_id, folder_data in folder_info.items():
            for peer_id in folder_data['include_peers']:
                if peer_id not in dialog_to_folders:
                    dialog_to_folders[peer_id] = []
                dialog_to_folders[peer_id].append(folder_data['title'])
        
        # 4. Обрабатываем диалоги
        dialog_list = []
        for dialog in dialogs:
            entity = dialog.entity
            
            # Получаем список названий папок для этого диалога
            folder_names = []
            dialog_id = entity.id
            
            # Проверяем, есть ли диалог в каких-либо папках
            if dialog_id in dialog_to_folders:
                folder_names = dialog_to_folders[dialog_id]
            
            # Также проверяем folder_id в самом диалоге (старый метод)
            if hasattr(dialog, 'folder_id') and dialog.folder_id and dialog.folder_id in folder_info:
                folder_title = folder_info[dialog.folder_id]['title']
                if folder_title not in folder_names:
                    folder_names.append(folder_title)
            
            dialog_info = DialogInfo(
                id=entity.id,
                title=dialog.title or dialog.name or "Без названия",
                username=getattr(entity, 'username', None),
                folder_names=folder_names,  # Список названий папок
                is_group=getattr(entity, 'megagroup', False) or getattr(entity, 'gigagroup', False),
                is_channel=getattr(entity, 'broadcast', False),
                is_user=hasattr(entity, 'first_name'),
                unread_count=dialog.unread_count,
                last_message_date=dialog.date.isoformat() if dialog.date else None
            )
            dialog_list.append(dialog_info)
        
        return dialog_list
        
    except Exception as e:
        print(f"Ошибка при получении диалогов с папками: {e}")
        # Возвращаем обычные диалоги без информации о папок
        dialogs = await client.get_dialogs(limit=limit)
        dialog_list = []
        for dialog in dialogs:
            entity = dialog.entity
            dialog_info = DialogInfo(
                id=entity.id,
                title=dialog.title or dialog.name or "Без названия",
                username=getattr(entity, 'username', None),
                folder_names=[],  # Пустой список
                is_group=getattr(entity, 'megagroup', False) or getattr(entity, 'gigagroup', False),
                is_channel=getattr(entity, 'broadcast', False),
                is_user=hasattr(entity, 'first_name'),
                unread_count=dialog.unread_count,
                last_message_date=dialog.date.isoformat() if dialog.date else None
            )
            dialog_list.append(dialog_info)
        return dialog_list


# ==================== Общий обработчик входящих ====================
async def incoming_handler(event):
    if event.is_outgoing:
        return

    from_account = "unknown"
    for name, cl in ACTIVE_CLIENTS.items():
        if cl.session == event.client.session:
            from_account = name
            break

    payload = {
        "from_account": from_account,
        "sender_id": event.sender_id,
        "chat_id": event.chat_id,
        "message_id": event.id,
        "text": event.text or "",
        "date": event.date.isoformat() if event.date else None,
    }

    if WEBHOOK_URL:
        try:
            requests.post(WEBHOOK_URL, json=payload, timeout=12)
        except:
            pass


# ==================== Lifespan ====================
@asynccontextmanager
async def lifespan(app: FastAPI):
    print("Telegram Multi Gateway запущен")
    yield
    for client in ACTIVE_CLIENTS.values():
        await client.disconnect()
    print("Все аккаунты отключены")


app = FastAPI(title="Telegram Multi Account Gateway", lifespan=lifespan)


# ==================== Добавить аккаунт ====================
@app.post("/accounts/add")
async def add_account(req: AddAccountReq):
    if req.name in ACTIVE_CLIENTS:
        raise HTTPException(400, detail=f"Аккаунт {req.name} уже существует")

    client = TelegramClient(StringSession(req.session_string), API_ID, API_HASH)
    await client.connect()

    if not await client.is_user_authorized():
        await client.disconnect()
        raise HTTPException(400, detail="Сессия недействительна или просрочена")

    await client.start()

    # Прогрев кэша диалогов (для работы с любыми ID)
    try:
        dialogs = await client.get_dialogs(limit=50)
        print(f"Прогрет кэш для {req.name}: {len(dialogs)} чатов")
    except Exception as e:
        print(f"Не удалось прогреть кэш для {req.name}: {e}")

    ACTIVE_CLIENTS[req.name] = client
    client.add_event_handler(incoming_handler, events.NewMessage(incoming=True))

    return {
        "status": "added",
        "account": req.name,
        "total_accounts": len(ACTIVE_CLIENTS),
        "cache_warmed": True
    }


# ==================== Удалить аккаунт ====================
@app.delete("/accounts/{name}")
async def remove_account(name: str):
    client = ACTIVE_CLIENTS.pop(name, None)
    if client:
        await client.disconnect()
        return {"status": "removed", "account": name}
    raise HTTPException(404, detail="Аккаунт не найден")


# ==================== Список акалогов ====================
@app.get("/accounts")
def list_accounts():
    return {"active_accounts": list(ACTIVE_CLIENTS.keys())}


# ==================== Отправить сообщение ====================
@app.post("/send")
async def send_message(req: SendMessageReq):
    client = ACTIVE_CLIENTS.get(req.account)
    if not client:
        raise HTTPException(400, detail=f"Аккаунт не найден: {req.account}")

    try:
        await client.send_message(req.chat_id, req.text)
        return {"status": "sent", "from": req.account, "to": req.chat_id}
    except Exception as e:
        raise HTTPException(500, detail=f"Ошибка отправки: {str(e)}")


# ==================== Экспорт участников группы ====================
@app.post("/export_members")
async def export_members(req: ExportMembersReq):
    client = ACTIVE_CLIENTS.get(req.account)
    if not client:
        raise HTTPException(400, detail=f"Аккаунт не найден: {req.account}")

    try:
        # Получаем группу
        group = await client.get_entity(req.group)

        # Экспорт всех участников (если аккаунт — админ или супергруппа)
        participants = await client.get_participants(group, aggressive=True)

        # Формируем данные
        members = [
            {
                "id": p.id,
                "username": p.username,
                "first_name": p.first_name,
                "last_name": p.last_name,
                "phone": p.phone if p.phone else None,  # Только если есть права
                "is_admin": p.admin_rights is not None,
                "is_bot": p.bot,
            }
            for p in participants
        ]

        return {
            "status": "exported",
            "group": req.group,
            "total_members": len(members),
            "members": members
        }
    except Exception as e:
        raise HTTPException(500, detail=f"Ошибка экспорта: {str(e)}. Убедись, что аккаунт в группе и имеет права (для супергрупп — админ для полного экспорта).")


# ==================== Получить список диалогов (с папками) ====================
@app.post("/dialogs")
async def get_dialogs(req: GetDialogsReq):
    """
    Получить список диалогов для указанного аккаунта с информацией о папках
    """
    client = ACTIVE_CLIENTS.get(req.account)
    if not client:
        raise HTTPException(400, detail=f"Аккаунт не найден: {req.account}")

    try:
        if req.include_folders:
            # Используем новую функцию с информацией о папках
            dialog_list = await get_dialogs_with_folders_info(client, req.limit)
        else:
            # Старая логика без информации о папках
            dialogs = await client.get_dialogs(limit=req.limit)
            dialog_list = []
            for dialog in dialogs:
                entity = dialog.entity
                dialog_info = DialogInfo(
                    id=entity.id,
                    title=dialog.title or dialog.name or "Без названия",
                    username=getattr(entity, 'username', None),
                    folder_names=[],  # Пустой список
                    is_group=getattr(entity, 'megagroup', False) or getattr(entity, 'gigagroup', False),
                    is_channel=getattr(entity, 'broadcast', False),
                    is_user=hasattr(entity, 'first_name'),
                    unread_count=dialog.unread_count,
                    last_message_date=dialog.date.isoformat() if dialog.date else None
                )
                dialog_list.append(dialog_info)
        
        return {
            "status": "success",
            "account": req.account,
            "include_folders": req.include_folders,
            "total_dialogs": len(dialog_list),
            "dialogs": dialog_list
        }
    except Exception as e:
        raise HTTPException(500, detail=f"Ошибка получения диалогов: {str(e)}")


# ==================== Получить все папки ====================
@app.post("/folders/{account}")
async def get_all_folders(account: str):
    """
    Получить список всех папок (диалоговых фильтры) для аккаунта
    """
    client = ACTIVE_CLIENTS.get(account)
    if not client:
        raise HTTPException(400, detail=f"Аккаунт не найден: {account}")

    try:
        dialog_filters_result = await client(GetDialogFiltersRequest())
        # Получаем список папок из атрибута .filters
        dialog_filters = getattr(dialog_filters_result, 'filters', [])
        folders = []
        
        for folder in dialog_filters:
            # Извлекаем текстовое название папки
            folder_title = extract_folder_title(folder)
            
            if hasattr(folder, 'id') and folder_title:
                folder_info = {
                    "id": folder.id,
                    "title": folder_title,  # Сохраняем строку, а не объект
                    "color": getattr(folder, 'color', None),
                    "pinned": getattr(folder, 'pinned', False),
                    "include_count": 0,
                    "exclude_count": 0
                }
                
                # Подсчитываем включенные диалоги
                if hasattr(folder, 'include_peers'):
                    folder_info["include_count"] = len(folder.include_peers)
                
                # Подсчитываем исключенные диалоги
                if hasattr(folder, 'exclude_peers'):
                    folder_info["exclude_count"] = len(folder.exclude_peers)
                
                folders.append(folder_info)
        
        return {
            "status": "success",
            "account": account,
            "total_folders": len(folders),
            "folders": folders
        }
    except Exception as e:
        raise HTTPException(500, detail=f"Ошибка получения папок: {str(e)}")


# ==================== Получить диалоги по папке ====================
@app.post("/dialogs_by_folder/{account}/{folder_id}")
async def get_dialogs_by_folder(account: str, folder_id: int):
    """
    Получить диалоги из определенной папки
    """
    client = ACTIVE_CLIENTS.get(account)
    if not client:
        raise HTTPException(400, detail=f"Аккаунт не найден: {account}")

    try:
        # Получаем все диалоги с информацией о папках
        all_dialogs = await get_dialogs_with_folders_info(client, limit=200)
        
        # Получаем информацию о папках для поиска названия
        dialog_filters_result = await client(GetDialogFiltersRequest())
        dialog_filters = getattr(dialog_filters_result, 'filters', [])
        folder_title = None
        
        # Находим название папки по ID
        for folder in dialog_filters:
            if hasattr(folder, 'id') and folder.id == folder_id:
                folder_title = extract_folder_title(folder)
                break
        
        # Фильтруем диалоги по указанной папке
        folder_dialogs = []
        for dialog in all_dialogs:
            if folder_title and folder_title in dialog.folder_names:
                folder_dialogs.append(dialog)
        
        return {
            "status": "success",
            "account": account,
            "folder_id": folder_id,
            "folder_title": folder_title,
            "total_dialogs": len(folder_dialogs),
            "dialogs": folder_dialogs
        }
    except Exception as e:
        raise HTTPException(500, detail=f"Ошибка получения диалогов по папке: {str(e)}")


# ==================== Получить историю чата ====================
@app.post("/chat_history")
async def get_chat_history(req: GetChatHistoryReq):
    """
    Получить историю сообщений для указанного чата
    """
    client = ACTIVE_CLIENTS.get(req.account)
    if not client:
        raise HTTPException(400, detail=f"Аккаунт не найден: {req.account}")

    try:
        chat_id = req.chat_id
        
        # Обрабатываем разные форматы chat_id
        if isinstance(chat_id, str):
            # Убираем "@" если есть в начале
            if chat_id.startswith('@'):
                chat_id = chat_id[1:]
            
            # Если это число в строке
            if chat_id.lstrip('-').isdigit():  # Разрешаем отрицательные числа для групп
                chat_id = int(chat_id)
        
        # Получаем сущность
        try:
            chat = await client.get_entity(chat_id)
        except Exception as e:
            # Если не удалось, пробуем найти в диалогах
            dialogs = await client.get_dialogs()
            for dialog in dialogs:
                if str(dialog.id) == str(chat_id) or (hasattr(dialog.entity, 'username') and dialog.entity.username == chat_id):
                    chat = dialog.entity
                    break
            else:
                raise HTTPException(400, detail=f"Не удалось найти чат: {req.chat_id}. Попробуйте использовать username или проверьте, что этот диалог есть в списке /dialogs")
        
        # Получаем историю сообщений
        messages = await client.get_messages(
            chat,
            limit=req.limit,
            offset_id=req.offset_id if req.offset_id and req.offset_id > 0 else None
        )
        
        message_list = []
        for msg in messages:
            if msg is None:
                continue
                
            # Получаем текст сообщения
            text = ""
            if hasattr(msg, 'text') and msg.text:
                text = msg.text
            elif hasattr(msg, 'message') and msg.message:
                text = msg.message
            
            # Пропускаем пустые сообщения без медиа
            if not text and not hasattr(msg, 'media'):
                continue
            
            # Извлекаем ID отправителя
            from_id = None
            if hasattr(msg, 'sender_id'):
                sender_id = msg.sender_id
                if isinstance(sender_id, PeerUser):
                    from_id = sender_id.user_id
                elif isinstance(sender_id, PeerChannel):
                    from_id = sender_id.channel_id
                elif isinstance(sender_id, PeerChat):
                    from_id = sender_id.chat_id
                elif isinstance(sender_id, int):
                    from_id = sender_id
            
            # Также можно попробовать получить из from_id атрибута
            if not from_id and hasattr(msg, 'from_id'):
                from_id = msg.from_id
                if isinstance(from_id, (PeerUser, PeerChannel, PeerChat)):
                    from_id = from_id.user_id if isinstance(from_id, PeerUser) else from_id.channel_id if isinstance(from_id, PeerChannel) else from_id.chat_id
            
            message = ChatMessage(
                id=msg.id,
                date=msg.date.isoformat() if msg.date else "",
                from_id=from_id,
                text=text,
                is_outgoing=msg.out if hasattr(msg, 'out') else False
            )
            message_list.append(message)
        
        # Получаем название чата
        chat_title = "Unknown"
        if hasattr(chat, 'title'):
            chat_title = chat.title
        elif hasattr(chat, 'first_name'):
            chat_title = chat.first_name
            if hasattr(chat, 'last_name') and chat.last_name:
                chat_title += f" {chat.last_name}"
        
        return {
            "status": "success",
            "account": req.account,
            "chat_id": req.chat_id,
            "chat_title": chat_title,
            "total_messages": len(message_list),
            "messages": message_list
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, detail=f"Ошибка получения истории чата: {str(e)}")


# ==================== (Опционально) Авторизация по API ====================
@app.post("/auth/start")
async def auth_start(req: AuthStartReq):
    """
    Начать авторизацию: запросить код подтверждения
    """
    if req.phone in PENDING_AUTH:
        raise HTTPException(400, "Авторизация уже идёт")
    
    client = TelegramClient(StringSession(), API_ID, API_HASH)
    await client.connect()
    
    try:
        # Отправляем запрос кода
        sent_code = await client.send_code_request(req.phone)
        
        # Сохраняем строку сессии и phone_code_hash
        session_str = client.session.save()
        PENDING_AUTH[req.phone] = {
            "session_str": session_str,
            "phone_code_hash": sent_code.phone_code_hash
        }
        
        await client.disconnect()
        
        return {
            "status": "code_sent",
            "phone": req.phone,
            "phone_code_hash": sent_code.phone_code_hash,  # <-- ВАЖНО: возвращаем hash клиенту
            "message": "Используйте phone_code_hash в запросе /auth/complete"
        }
    except Exception as e:
        await client.disconnect()
        raise HTTPException(400, detail=f"Ошибка при запросе кода: {str(e)}")


@app.post("/auth/complete")
async def auth_complete(req: AuthCodeReq):
    """
    Завершить авторизацию: отправить полученный код
    """
    pending_data = PENDING_AUTH.get(req.phone)
    if not pending_data:
        raise HTTPException(400, "Нет активной авторизации для этого номера")
    
    # Восстанавливаем клиент из сохраненной сессии
    client = TelegramClient(StringSession(pending_data["session_str"]), API_ID, API_HASH)
    await client.connect()
    
    try:
        # Пытаемся войти с кодом
        try:
            await client.sign_in(
                phone=req.phone,
                code=req.code,
                phone_code_hash=pending_data["phone_code_hash"]
            )
        except SessionPasswordNeededError:
            # Если требуется пароль 2FA
            if not req.password:
                await client.disconnect()
                raise HTTPException(400, detail="Требуется пароль двухфакторной аутентификации. Укажите параметр password.")
            
            # Пытаемся войти с паролем 2FA
            try:
                await client.sign_in(
                    phone=req.phone,
                    code=req.code,
                    phone_code_hash=pending_data["phone_code_hash"],
                    password=req.password
                )
            except Exception as e:
                await client.disconnect()
                raise HTTPException(400, detail=f"Ошибка при вводе пароля 2FA: {str(e)}")
        except Exception as e:
            await client.disconnect()
            raise HTTPException(400, detail=f"Ошибка при вводе кода: {str(e)}")
        
        # Если успешно, получаем финальную строку сессии
        session_str = client.session.save()
        
        # Удаляем временные данные
        del PENDING_AUTH[req.phone]
        await client.disconnect()
        
        return {
            "status": "success",
            "session_string": session_str,
            "message": "Авторизация успешна. Используйте session_string в /accounts/add"
        }
    except Exception as e:
        await client.disconnect()
        raise HTTPException(400, detail=str(e))


# ==================== Запуск ====================
if __name__ == "__main__":
    port = int(os.getenv("PORT", 8000))
    uvicorn.run("telegram_bot:app", host="0.0.0.0", port=port, reload=False)
