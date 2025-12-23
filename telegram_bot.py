# telegram_bot.py — Мультиаккаунт + экспорт участников группы + мгновенная работа с любыми ID
import os
import requests
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.tl.types import PeerUser, PeerChannel, PeerChat
from telethon.tl.functions.messages import GetDialogsRequest, GetDialogFiltersRequest
from telethon.tl.types import InputPeerEmpty
from telethon.errors import SessionPasswordNeededError
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
# Изменяем формат: добавляем флаг needs_2fa
PENDING_AUTH = {}  # Формат: {phone: {"session_str": "...", "phone_code_hash": "...", "needs_2fa": False}}


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
    phone_code_hash: str
    password: str | None = None  # Опционально для 2FA

class Auth2FAReq(BaseModel):
    phone: str
    password: str  # Обязательно для 2FA

class ExportMembersReq(BaseModel):
    account: str
    group: str | int

# ==================== Новые модели ====================
class DialogInfo(BaseModel):
    id: int
    title: str
    username: Optional[str] = None
    folder_names: List[str] = []
    is_group: bool
    is_channel: bool
    is_user: bool
    unread_count: int
    last_message_date: Optional[str] = None

class GetDialogsReq(BaseModel):
    account: str
    limit: int = 50
    include_folders: bool = True

class ChatMessage(BaseModel):
    id: int
    date: str
    from_id: Optional[int] = None
    text: str
    is_outgoing: bool
    
    @validator('from_id', pre=True)
    def parse_from_id(cls, v):
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


# ==================== Вспомогательные функции ====================
def extract_folder_title(folder_obj):
    if not hasattr(folder_obj, 'title'):
        return None
    
    title_obj = folder_obj.title
    if hasattr(title_obj, 'text'):
        return title_obj.text
    elif isinstance(title_obj, str):
        return title_obj
    return None


async def get_dialogs_with_folders_info(client: TelegramClient, limit: int = 50) -> List[DialogInfo]:
    """Получить диалоги с информацией о папках"""
    try:
        folder_info = {}
        try:
            dialog_filters_result = await client(GetDialogFiltersRequest())
            dialog_filters = getattr(dialog_filters_result, 'filters', [])
            
            for folder in dialog_filters:
                folder_title = extract_folder_title(folder)
                
                if hasattr(folder, 'id') and folder_title:
                    folder_info[folder.id] = {
                        'title': folder_title,
                        'include_peers': [],
                        'exclude_peers': []
                    }
                    
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
        except Exception as e:
            print(f"Ошибка получения папок: {e}")
        
        dialogs = await client.get_dialogs(limit=limit)
        dialog_to_folders = {}
        
        for folder_id, folder_data in folder_info.items():
            for peer_id in folder_data['include_peers']:
                if peer_id not in dialog_to_folders:
                    dialog_to_folders[peer_id] = []
                dialog_to_folders[peer_id].append(folder_data['title'])
        
        dialog_list = []
        for dialog in dialogs:
            entity = dialog.entity
            folder_names = []
            dialog_id = entity.id
            
            if dialog_id in dialog_to_folders:
                folder_names = dialog_to_folders[dialog_id]
            
            dialog_info = DialogInfo(
                id=entity.id,
                title=dialog.title or dialog.name or "Без названия",
                username=getattr(entity, 'username', None),
                folder_names=folder_names,
                is_group=getattr(entity, 'megagroup', False) or getattr(entity, 'gigagroup', False),
                is_channel=getattr(entity, 'broadcast', False),
                is_user=hasattr(entity, 'first_name'),
                unread_count=dialog.unread_count,
                last_message_date=dialog.date.isoformat() if dialog.date else None
            )
            dialog_list.append(dialog_info)
        
        return dialog_list
        
    except Exception as e:
        print(f"Ошибка получения диалогов: {e}")
        dialogs = await client.get_dialogs(limit=limit)
        return [DialogInfo(
            id=dialog.entity.id,
            title=dialog.title or dialog.name or "Без названия",
            username=getattr(dialog.entity, 'username', None),
            folder_names=[],
            is_group=getattr(dialog.entity, 'megagroup', False) or getattr(dialog.entity, 'gigagroup', False),
            is_channel=getattr(dialog.entity, 'broadcast', False),
            is_user=hasattr(dialog.entity, 'first_name'),
            unread_count=dialog.unread_count,
            last_message_date=dialog.date.isoformat() if dialog.date else None
        ) for dialog in dialogs]


# ==================== Lifespan ====================
@asynccontextmanager
async def lifespan(app: FastAPI):
    print("Telegram Multi Gateway запущен")
    yield
    for client in ACTIVE_CLIENTS.values():
        await client.disconnect()
    print("Все аккаунты отключены")


app = FastAPI(title="Telegram Multi Account Gateway", lifespan=lifespan)


# ==================== Авторизация ====================
@app.post("/auth/start")
async def auth_start(req: AuthStartReq):
    """Начать авторизацию: запросить код подтверждения"""
    client = TelegramClient(StringSession(), API_ID, API_HASH)
    await client.connect()
    
    try:
        sent_code = await client.send_code_request(req.phone)
        session_str = client.session.save()
        
        PENDING_AUTH[req.phone] = {
            "session_str": session_str,
            "phone_code_hash": sent_code.phone_code_hash,
            "needs_2fa": False
        }
        
        await client.disconnect()
        
        return {
            "status": "code_sent",
            "phone": req.phone,
            "phone_code_hash": sent_code.phone_code_hash,
            "needs_2fa": False
        }
    except Exception as e:
        await client.disconnect()
        raise HTTPException(400, detail=f"Ошибка: {str(e)}")


@app.post("/auth/complete")
async def auth_complete(req: AuthCodeReq):
    """
    Завершить авторизацию.
    Автоматически определяет нужен ли 2FA.
    """
    pending_data = PENDING_AUTH.get(req.phone)
    if not pending_data:
        raise HTTPException(400, "Нет активной авторизации")
    
    client = TelegramClient(StringSession(pending_data["session_str"]), API_ID, API_HASH)
    await client.connect()
    
    try:
        # 1. Пробуем войти с кодом
        try:
            await client.sign_in(
                phone=req.phone,
                code=req.code,
                phone_code_hash=pending_data["phone_code_hash"]
            )
            
        # 2. Если нужен пароль 2FA
        except SessionPasswordNeededError:
            # Обновляем статус в PENDING_AUTH
            PENDING_AUTH[req.phone]["needs_2fa"] = True
            
            # Если пароль уже предоставлен в этом же запросе
            if req.password:
                try:
                    await client.sign_in(password=req.password)
                except Exception as e:
                    await client.disconnect()
                    raise HTTPException(400, detail=f"Ошибка пароля 2FA: {str(e)}")
            else:
                await client.disconnect()
                # Возвращаем специальный статус для запроса пароля
                return {
                    "status": "2fa_required",
                    "phone": req.phone,
                    "needs_2fa": True,
                    "message": "Требуется пароль двухфакторной аутентификации",
                    "instructions": "Используйте /auth/2fa с параметром password"
                }
        
        # 3. Если другие ошибки с кодом
        except Exception as e:
            await client.disconnect()
            raise HTTPException(400, detail=f"Ошибка кода: {str(e)}")
        
        # 4. Если успешно (с кодом или кодом+паролем)
        session_str = client.session.save()
        del PENDING_AUTH[req.phone]
        await client.disconnect()
        
        return {
            "status": "success",
            "session_string": session_str,
            "message": "Авторизация успешна"
        }
        
    except Exception as e:
        await client.disconnect()
        raise HTTPException(500, detail=f"Неожиданная ошибка: {str(e)}")


@app.post("/auth/2fa")
async def auth_2fa(req: Auth2FAReq):
    """
    Отдельный эндпоинт для ввода пароля 2FA.
    Используется после получения статуса '2fa_required' от /auth/complete
    """
    pending_data = PENDING_AUTH.get(req.phone)
    if not pending_data:
        raise HTTPException(400, "Нет активной авторизации или сессия устарела")
    
    if not pending_data.get("needs_2fa", False):
        raise HTTPException(400, "Для этого номера не требуется 2FA")
    
    client = TelegramClient(StringSession(pending_data["session_str"]), API_ID, API_HASH)
    await client.connect()
    
    try:
        # Входим с паролем 2FA
        await client.sign_in(password=req.password)
        
        session_str = client.session.save()
        del PENDING_AUTH[req.phone]
        await client.disconnect()
        
        return {
            "status": "success",
            "session_string": session_str,
            "message": "2FA авторизация успешна"
        }
        
    except Exception as e:
        await client.disconnect()
        raise HTTPException(400, detail=f"Ошибка 2FA: {str(e)}")


# ==================== Работа с аккаунтами ====================
@app.post("/accounts/add")
async def add_account(req: AddAccountReq):
    if req.name in ACTIVE_CLIENTS:
        raise HTTPException(400, detail=f"Аккаунт {req.name} уже существует")

    client = TelegramClient(StringSession(req.session_string), API_ID, API_HASH)
    await client.connect()

    if not await client.is_user_authorized():
        await client.disconnect()
        raise HTTPException(400, detail="Сессия недействительна")

    await client.start()

    try:
        dialogs = await client.get_dialogs(limit=50)
        print(f"Прогрет кэш для {req.name}: {len(dialogs)} чатов")
    except Exception as e:
        print(f"Ошибка прогрева кэша: {e}")

    ACTIVE_CLIENTS[req.name] = client
    client.add_event_handler(
        lambda event: incoming_handler(event),
        events.NewMessage(incoming=True)
    )

    return {
        "status": "added",
        "account": req.name,
        "total_accounts": len(ACTIVE_CLIENTS)
    }


@app.delete("/accounts/{name}")
async def remove_account(name: str):
    client = ACTIVE_CLIENTS.pop(name, None)
    if client:
        await client.disconnect()
        return {"status": "removed", "account": name}
    raise HTTPException(404, detail="Аккаунт не найден")


@app.get("/accounts")
def list_accounts():
    return {"active_accounts": list(ACTIVE_CLIENTS.keys())}


# ==================== Остальные эндпоинты (без изменений) ====================
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


@app.post("/export_members")
async def export_members(req: ExportMembersReq):
    client = ACTIVE_CLIENTS.get(req.account)
    if not client:
        raise HTTPException(400, detail=f"Аккаунт не найден: {req.account}")

    try:
        group = await client.get_entity(req.group)
        participants = await client.get_participants(group, aggressive=True)

        members = [
            {
                "id": p.id,
                "username": p.username,
                "first_name": p.first_name,
                "last_name": p.last_name,
                "phone": p.phone if p.phone else None,
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
        raise HTTPException(500, detail=f"Ошибка экспорта: {str(e)}")


@app.post("/dialogs")
async def get_dialogs(req: GetDialogsReq):
    client = ACTIVE_CLIENTS.get(req.account)
    if not client:
        raise HTTPException(400, detail=f"Аккаунт не найден: {req.account}")

    try:
        if req.include_folders:
            dialog_list = await get_dialogs_with_folders_info(client, req.limit)
        else:
            dialogs = await client.get_dialogs(limit=req.limit)
            dialog_list = [
                DialogInfo(
                    id=dialog.entity.id,
                    title=dialog.title or dialog.name or "Без названия",
                    username=getattr(dialog.entity, 'username', None),
                    folder_names=[],
                    is_group=getattr(dialog.entity, 'megagroup', False) or getattr(dialog.entity, 'gigagroup', False),
                    is_channel=getattr(dialog.entity, 'broadcast', False),
                    is_user=hasattr(dialog.entity, 'first_name'),
                    unread_count=dialog.unread_count,
                    last_message_date=dialog.date.isoformat() if dialog.date else None
                ) for dialog in dialogs
            ]
        
        return {
            "status": "success",
            "account": req.account,
            "total_dialogs": len(dialog_list),
            "dialogs": dialog_list
        }
    except Exception as e:
        raise HTTPException(500, detail=f"Ошибка получения диалогов: {str(e)}")


@app.post("/folders/{account}")
async def get_all_folders(account: str):
    client = ACTIVE_CLIENTS.get(account)
    if not client:
        raise HTTPException(400, detail=f"Аккаунт не найден: {account}")

    try:
        dialog_filters_result = await client(GetDialogFiltersRequest())
        dialog_filters = getattr(dialog_filters_result, 'filters', [])
        folders = []
        
        for folder in dialog_filters:
            folder_title = extract_folder_title(folder)
            
            if hasattr(folder, 'id') and folder_title:
                folder_info = {
                    "id": folder.id,
                    "title": folder_title,
                    "color": getattr(folder, 'color', None),
                    "pinned": getattr(folder, 'pinned', False),
                    "include_count": len(getattr(folder, 'include_peers', [])),
                    "exclude_count": len(getattr(folder, 'exclude_peers', []))
                }
                folders.append(folder_info)
        
        return {
            "status": "success",
            "account": account,
            "total_folders": len(folders),
            "folders": folders
        }
    except Exception as e:
        raise HTTPException(500, detail=f"Ошибка получения папок: {str(e)}")


@app.post("/chat_history")
async def get_chat_history(req: GetChatHistoryReq):
    client = ACTIVE_CLIENTS.get(req.account)
    if not client:
        raise HTTPException(400, detail=f"Аккаунт не найден: {req.account}")

    try:
        chat_id = req.chat_id
        
        if isinstance(chat_id, str):
            if chat_id.startswith('@'):
                chat_id = chat_id[1:]
            if chat_id.lstrip('-').isdigit():
                chat_id = int(chat_id)
        
        try:
            chat = await client.get_entity(chat_id)
        except Exception:
            dialogs = await client.get_dialogs()
            for dialog in dialogs:
                if str(dialog.id) == str(chat_id) or (hasattr(dialog.entity, 'username') and dialog.entity.username == chat_id):
                    chat = dialog.entity
                    break
            else:
                raise HTTPException(400, detail=f"Не удалось найти чат: {req.chat_id}")
        
        messages = await client.get_messages(
            chat,
            limit=req.limit,
            offset_id=req.offset_id if req.offset_id and req.offset_id > 0 else None
        )
        
        message_list = []
        for msg in messages:
            if msg is None:
                continue
                
            text = ""
            if hasattr(msg, 'text') and msg.text:
                text = msg.text
            elif hasattr(msg, 'message') and msg.message:
                text = msg.message
            
            if not text and not hasattr(msg, 'media'):
                continue
            
            message = ChatMessage(
                id=msg.id,
                date=msg.date.isoformat() if msg.date else "",
                from_id=None,
                text=text,
                is_outgoing=msg.out if hasattr(msg, 'out') else False
            )
            message_list.append(message)
        
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
        raise HTTPException(500, detail=f"Ошибка получения истории: {str(e)}")


# ==================== Запуск ====================
if __name__ == "__main__":
    port = int(os.getenv("PORT", 8000))
    uvicorn.run("telegram_bot:app", host="0.0.0.0", port=port, reload=False)
