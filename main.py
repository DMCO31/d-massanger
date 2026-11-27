from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, UploadFile, File
from fastapi.responses import HTMLResponse, FileResponse
from pydantic import BaseModel
from sqlalchemy import create_engine, Column, Integer, String, Text, Boolean
from sqlalchemy.orm import declarative_base, sessionmaker
from passlib.context import CryptContext
from typing import Dict, List
import os, uuid, time, json

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
    avatar_url = Column(String, default="")
    description = Column(String, default="Пользователь d чата")

class Group(Base):
    __tablename__ = "groups"
    id = Column(String, primary_key=True, index=True)
    name = Column(String)
    creator = Column(String)

class GroupMember(Base):
    __tablename__ = "group_members"
    id = Column(Integer, primary_key=True, index=True)
    group_id = Column(String)
    username = Column(String)

class Message(Base):
    __tablename__ = "messages"
    id = Column(String, primary_key=True, index=True)
    sender = Column(String)
    receiver = Column(String) # username или group_id
    text = Column(Text)
    is_group = Column(Boolean, default=False)

Base.metadata.create_all(bind=engine)

UPLOAD_DIR = "uploads"
os.makedirs(UPLOAD_DIR, exist_ok=True)
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
app = FastAPI()
connections: Dict[WebSocket, str] = {}
ADMIN_PASSWORD = "admin123"

class UserAuth(BaseModel):
    username: str
    password: str

class AdminCommand(BaseModel):
    password: str
    command: str

class ProfileUpdate(BaseModel):
    username: str
    description: str

class GroupCreate(BaseModel):
    name: str
    creator: str

class AddMember(BaseModel):
    group_id: str
    username: str

@app.post("/register")
def register(user: UserAuth):
    db = SessionLocal()
    if db.query(User).filter(User.username == user.username).first():
        db.close()
        raise HTTPException(status_code=400, detail="Имя занято")
    hashed_pw = pwd_context.hash(user.password)
    new_user = User(username=user.username, hashed_password=hashed_pw)
    db.add(new_user)
    
    welcome_text = "Добрый день уважаемый Пользователь! Благодарим вас за регистрацию в мессенджере! Желаем приятного пользования."
    db.add(Message(id=str(time.time()), sender="notifications", receiver=user.username, text=welcome_text))
    
    db.commit()
    db.close()
    return {"message": "ОК"}

@app.post("/login")
def login(user: UserAuth):
    db = SessionLocal()
    db_user = db.query(User).filter(User.username == user.username).first()
    db.close()
    if not db_user or not pwd_context.verify(user.password, db_user.hashed_password):
        raise HTTPException(status_code=400, detail="Неверные данные")
    if db_user.is_banned:
        raise HTTPException(status_code=403, detail="Вы забанены")
    return {"message": "ОК"}

@app.post("/upload")
async def upload_file(file: UploadFile = File(...)):
    file_ext = file.filename.split(".")[-1]
    file_name = f"{uuid.uuid4()}.{file_ext}"
    with open(os.path.join(UPLOAD_DIR, file_name), "wb") as buffer:
        buffer.write(await file.read())
    return {"url": f"/uploads/{file_name}"}

@app.get("/uploads/{filename}")
async def get_file(filename: str):
    return FileResponse(os.path.join(UPLOAD_DIR, filename))

@app.post("/update_profile")
def update_profile(prof: ProfileUpdate):
    db = SessionLocal()
    user = db.query(User).filter(User.username == prof.username).first()
    if user:
        user.description = prof.description
        db.commit()
        db.close()
        return {"message": "ОК"}
    db.close()
    raise HTTPException(status_code=404, detail="Не найден")

@app.get("/get_profile/{username}")
def get_profile(username: str):
    db = SessionLocal()
    user = db.query(User).filter(User.username == username).first()
    db.close()
    if user:
        return {"description": user.description, "avatar_url": user.avatar_url}
    return {"description": "Пользователь", "avatar_url": ""}

@app.post("/create_group")
def create_group(grp: GroupCreate):
    db = SessionLocal()
    group_id = f"group_{uuid.uuid4().hex[:6]}"
    new_group = Group(id=group_id, name=grp.name, creator=grp.creator)
    db.add(new_group)
    db.add(GroupMember(group_id=group_id, username=grp.creator))
    db.commit()
    db.close()
    return {"id": group_id, "name": grp.name}

@app.post("/add_to_group")
def add_to_group(member: AddMember):
    db = SessionLocal()
    if not db.query(GroupMember).filter(GroupMember.group_id == member.group_id, GroupMember.username == member.username).first():
        db.add(GroupMember(group_id=member.group_id, username=member.username))
        db.commit()
    db.close()
    return {"message": "ОК"}

@app.get("/get_groups/{username}")
def get_groups(username: str):
    db = SessionLocal()
    memberships = db.query(GroupMember).filter(GroupMember.username == username).all()
    groups = []
    for m in memberships:
        grp = db.query(Group).filter(Group.id == m.group_id).first()
        if grp:
            groups.append({"id": grp.id, "name": grp.name})
    db.close()
    return groups

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
    if user and user.is_banned:
        await ws.close()
        return

    connections[ws] = nick
    await broadcast_users()
    
    # История для пользователя
    db = SessionLocal()
    groups = db.query(GroupMember.group_id).filter(GroupMember.username == nick).all()
    group_ids = [g[0] for g in groups]
    
    history = db.query(Message).filter(
        (Message.sender == nick) | (Message.receiver == nick) | (Message.receiver.in_(group_ids))
    ).order_by(Message.id.desc()).limit(100).all()
    db.close()
    
    for msg in reversed(history):
        await ws.send_text(f"MSG:{msg.sender}:{msg.receiver}:{msg.text}:{msg.id}:{msg.is_group}")

    await broadcast(f"SYSTEM:{nick} зашел в чат")

    try:
        while True:
            data = await ws.receive_text()
            
            # Статус "печатает"
            if data.startswith("TYPING:"):
                chat_to = data.split(":")[1]
                for c_ws, c_nick in list(connections.items()):
                    if c_nick == chat_to:
                        await c_ws.send_text(f"TYPING:{nick}")
                continue

            # Редактирование
            if data.startswith("EDIT:"):
                _, msg_id, new_text = data.split(":", 2)
                db = SessionLocal()
                msg = db.query(Message).filter(Message.id == msg_id).first()
                if msg:
                    msg.text = new_text
                    db.commit()
                db.close()
                await broadcast(f"EDIT_MSG:{msg_id}:{new_text}")
                continue

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
                is_group = to_user.startswith("group_")
                
                db = SessionLocal()
                db.add(Message(id=msg_id, sender=nick, receiver=to_user, text=text, is_group=is_group))
                db.commit()
                
                # Кому отправить
                if is_group:
                    members = db.query(GroupMember.username).filter(GroupMember.group_id == to_user).all()
                    member_names = [m[0] for m in members]
                else:
                    member_names = [to_user]
                db.close()

                payload = f"MSG:{nick}:{to_user}:{text}:{msg_id}:{is_group}"
                for client_ws, client_nick in list(connections.items()):
                    if client_nick in member_names or client_nick == nick:
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
        except: pass

async def broadcast(message: str):
    for c in list(connections.keys()):
        try:
            await c.send_text(message)
        except: pass
