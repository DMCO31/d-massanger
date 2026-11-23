from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, UploadFile, File
from fastapi.responses import HTMLResponse, FileResponse
from pydantic import BaseModel
from sqlalchemy import create_engine, Column, Integer, String, Text
from sqlalchemy.orm import declarative_base, sessionmaker
from passlib.context import CryptContext
from typing import Dict
import os
import uuid
import time

# --- БАЗА ДАННЫХ ---
SQLALCHEMY_DATABASE_URL = "sqlite:///./messenger.db"
engine = create_engine(SQLALCHEMY_DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    username = Column(String, unique=True, index=True)
    hashed_password = Column(String)
    is_banned = Column(Integer, default=0)

class Message(Base):
    __tablename__ = "messages"
    id = Column(String, primary_key=True, index=True)
    sender = Column(String)
    receiver = Column(String)
    text = Column(Text)

Base.metadata.create_all(bind=engine)

UPLOAD_DIR = "uploads"
os.makedirs(UPLOAD_DIR, exist_ok=True)

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
app = FastAPI()
connections: Dict[WebSocket, str] = {}

# СЕКРЕТНЫЙ ПАРОЛЬ ДЛЯ КОНСОЛИ (можешь поменять)
ADMIN_PASSWORD = "admin123" 

class UserAuth(BaseModel):
    username: str
    password: str

class AdminCommand(BaseModel):
    password: str
    command: str

@app.post("/register")
def register(user: UserAuth):
    db = SessionLocal()
    db_user = db.query(User).filter(User.username == user.username).first()
    if db_user:
        db.close()
        raise HTTPException(status_code=400, detail="Это имя уже занято")
    
    hashed_pw = pwd_context.hash(user.password)
    new_user = User(username=user.username, hashed_password=hashed_pw)
    db.add(new_user)
    
    # Приветственное сообщение от бота
    welcome_text = "Добрый день уважаемый Пользователь! Благодарим вас за регистрацию в мессенджере! Желаем приятного пользования."
    welcome_msg = Message(id=str(time.time()), sender="notifications", receiver=user.username, text=welcome_text)
    db.add(welcome_msg)
    
    db.commit()
    db.close()
    return {"message": "Регистрация успешна"}

@app.post("/login")
def login(user: UserAuth):
    db = SessionLocal()
    db_user = db.query(User).filter(User.username == user.username).first()
    db.close()
    if not db_user or not pwd_context.verify(user.password, db_user.hashed_password):
        raise HTTPException(status_code=400, detail="Неверное имя или пароль")
    if db_user.is_banned:
        raise HTTPException(status_code=403, detail="Вы забанены в этом мессенджере")
    return {"message": "Вход выполнен"}

@app.post("/admin/command")
def admin_command(cmd: AdminCommand):
    if cmd.password != ADMIN_PASSWORD:
        raise HTTPException(status_code=403, detail="Неверный пароль администратора")
    
    db = SessionLocal()
    parts = cmd.command.strip().split(" ")
    action = parts[0].lower()
    
    if action == "ban" and len(parts) > 1:
        target = parts[1]
        user = db.query(User).filter(User.username == target).first()
        if user:
            user.is_banned = 1
            db.commit()
            result = f"Пользователь {target} забанен."
        else:
            result = f"Пользователь {target} не найден."
            
    elif action == "unban" and len(parts) > 1:
        target = parts[1]
        user = db.query(User).filter(User.username == target).first()
        if user:
            user.is_banned = 0
            db.commit()
            result = f"Пользователь {target} разбанен."
        else:
            result = f"Пользователь {target} не найден."
            
    elif action == "users":
        users = db.query(User).all()
        result = "Список пользователей:\n" + "\n".join([f"{u.username} {'[BANNED]' if u.is_banned else ''}" for u in users])
        
    elif action == "online":
        result = "Онлайн: " + ", ".join(connections.values())
        
    elif action == "clear":
        db.query(Message).delete()
        db.commit()
        result = "Все сообщения удалены."
        
    elif action == "help":
        result = "Команды:\nban <username> - забанить\nunban <username> - разбанить\nusers - список всех юзеров\nonline - кто онлайн\nclear - удалить все сообщения"
    else:
        result = "Неизвестная команда. Введите help."
        
    db.close()
    return {"result": result}

@app.post("/upload")
async def upload_file(file: UploadFile = File(...)):
    file_ext = file.filename.split(".")[-1]
    file_name = f"{uuid.uuid4()}.{file_ext}"
    file_path = os.path.join(UPLOAD_DIR, file_name)
    with open(file_path, "wb") as buffer:
        buffer.write(await file.read())
    return {"url": f"/uploads/{file_name}"}

@app.get("/uploads/{filename}")
async def get_file(filename: str):
    return FileResponse(os.path.join(UPLOAD_DIR, filename))

@app.get("/")
async def index():
    with open("index.html", "r", encoding="utf-8") as f:
        return HTMLResponse(f.read())

@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    nick = await ws.receive_text()
    
    db = SessionLocal()
    user = db.query(User).filter(User.username == nick).first()
    db.close()
    
    # Проверка на бан
    if user and user.is_banned:
        await ws.close()
        return

    connections[ws] = nick
    await ws.send_text(f"SYSTEM_LIST:{','.join(connections.values())}")
    
    # Отправляем историю для этого пользователя
    db = SessionLocal()
    history = db.query(Message).filter(
        (Message.sender == nick) | (Message.receiver == nick)
    ).order_by(Message.id.desc()).limit(100).all()
    db.close()
    
    for msg in reversed(history):
        if msg.sender == "notifications" and msg.receiver != nick:
            continue
        await ws.send_text(f"MSG:private:{msg.sender}:{msg.receiver}:{msg.text}:{msg.id}")

    await broadcast(f"SYSTEM:{nick} зашел в чат")
    await broadcast_users()

    try:
        while True:
            data = await ws.receive_text()
            
            # Проверка на бан
            db = SessionLocal()
            user = db.query(User).filter(User.username == nick).first()
            db.close()
            if user and user.is_banned:
                await ws.close()
                break

            if data.startswith("DELETE:"):
                msg_id = data.split(":")[1]
                db = SessionLocal()
                db.query(Message).filter(Message.id == msg_id).delete()
                db.commit()
                db.close()
                await broadcast(f"DELETE_MSG:{msg_id}")
                continue

            parts = data.split("|", 2)
            if len(parts) == 3:
                to_user, text, msg_id = parts
                
                db = SessionLocal()
                new_msg = Message(id=msg_id, sender=nick, receiver=to_user, text=text)
                db.add(new_msg)
                db.commit()
                db.close()

                payload = f"MSG:private:{nick}:{to_user}:{text}:{msg_id}"
                for client_ws, client_nick in list(connections.items()):
                    if client_nick == to_user or client_nick == nick:
                        await client_ws.send_text(payload)
                            
    except WebSocketDisconnect:
        if ws in connections:
            del connections[ws]
        await broadcast(f"SYSTEM:{nick} покинул чат")
        await broadcast_users()

async def broadcast_users():
    users_list = f"SYSTEM_LIST:{','.join(connections.values())}"
    for c in list(connections.keys()):
        try:
            await c.send_text(users_list)
        except:
            pass

async def broadcast(message: str):
    for c in list(connections.keys()):
        try:
            await c.send_text(message)
        except:
            pass
