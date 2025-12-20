# telegram_bot.py — Мультиаккаунт + экспорт участников группы + мгновенная работа с любыми ID
import os
import requests
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from contextlib import asynccontextmanager
import uvicorn

API_ID = 31407487
API_HASH = "0b82a91fb5c797a2bf713ad3d46a9c20"
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "")

# Хранилище: имя → клиент
ACTIVE_CLIENTS = {}
PENDING_AUTH = {}


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
    password: str | None = None

class ExportMembersReq(BaseModel):
    account: str          # имя аккаунта (сессии)
    group: str | int      # ID группы или @username


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


# ==================== Список аккаунтов ====================
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


# ==================== (Опционально) Авторизация по API ====================
@app.post("/auth/start")
async def auth_start(req: AuthStartReq):
    if req.phone in PENDING_AUTH:
        raise HTTPException(400, "Авторизация уже идёт")
    client = TelegramClient(StringSession(), API_ID, API_HASH)
    await client.connect()
    await client.send_code_request(req.phone)
    PENDING_AUTH[req.phone] = client
    return {"status": "code_sent"}


@app.post("/auth/complete")
async def auth_complete(req: AuthCodeReq):
    client = PENDING_AUTH.get(req.phone)
    if not client:
        raise HTTPException(400, "Нет активной авторизации")
    try:
        await client.sign_in(req.phone, req.code, password=req.password)
        session_str = client.session.save()
        del PENDING_AUTH[req.phone]
        return {"status": "success", "session_string": session_str}
    except Exception as e:
        raise HTTPException(400, detail=str(e))


# ==================== Запуск ====================
if __name__ == "__main__":
    port = int(os.getenv("PORT", 8000))
    uvicorn.run("telegram_bot:app", host="0.0.0.0", port=port, reload=False)
