from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, UploadFile, File
from fastapi.responses import HTMLResponse, FileResponse
from pydantic import BaseModel
from sqlalchemy import create_engine, Column, Integer, String, Text
from sqlalchemy.orm import declarative_base, sessionmaker
from passlib.context import CryptContext
from typing import Dict
import os
import uuid

# --- НАСТРОЙКА БАЗЫ ДАННЫХ ---
SQLALCHEMY_DATABASE_URL = "sqlite:///./messenger.db"
engine = create_engine(SQLALCHEMY_DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    username = Column(String, unique=True, index=True)
    hashed_password = Column(String)

class Message(Base):
    __tablename__ = "messages"
    id = Column(String, primary_key=True, index=True)
    sender = Column(String)
    receiver = Column(String)
    text = Column(Text) # Здесь может быть текст или ссылка на картинку

Base.metadata.create_all(bind=engine)

# Создаем папку для картинок
UPLOAD_DIR = "uploads"
os.makedirs(UPLOAD_DIR, exist_ok=True)

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

app = FastAPI()
connections: Dict[WebSocket, str] = {}

class UserAuth(BaseModel):
    username: str
    password: str

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
    return {"message": "Вход выполнен"}

# --- ЗАГРУЗКА ФАЙЛОВ ---
@app.post("/upload")
async def upload_file(file: UploadFile = File(...)):
    # Генерируем уникальное имя файла, чтобы они не перезаписывали друг друга
    file_ext = file.filename.split(".")[-1]
    file_name = f"{uuid.uuid4()}.{file_ext}"
    file_path = os.path.join(UPLOAD_DIR, file_name)
    
    with open(file_path, "wb") as buffer:
        buffer.write(await file.read())
    
    return {"url": f"/uploads/{file_name}"}

@app.get("/uploads/{filename}")
async def get_file(filename: str):
    return FileResponse(os.path.join(UPLOAD_DIR, filename))

# --- ОСНОВНОЕ ПРИЛОЖЕНИЕ ---
@app.get("/")
async def index():
    with open("index.html", "r", encoding="utf-8") as f:
        return HTMLResponse(f.read())

@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    nick = await ws.receive_text()
    connections[ws] = nick
    
    await ws.send_text(f"SYSTEM_LIST:{','.join(connections.values())}")
    
    db = SessionLocal()
    history = db.query(Message).order_by(Message.id.desc()).limit(50).all()
    db.close()
    
    for msg in reversed(history):
        msg_type = "private" if msg.receiver != "ALL" else "public"
        await ws.send_text(f"MSG:{msg_type}:{msg.sender}:{msg.receiver}:{msg.text}:{msg.id}")

    await broadcast(f"SYSTEM:{nick} зашел в чат")
    await broadcast_users()

    try:
        while True:
            data = await ws.receive_text()
            
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

                msg_type = "private" if to_user != "ALL" else "public"
                payload = f"MSG:{msg_type}:{nick}:{to_user}:{text}:{msg_id}"
                
                if to_user == "ALL":
                    await broadcast(payload)
                else:
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
