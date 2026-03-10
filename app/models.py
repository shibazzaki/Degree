from . import db
from flask_login import UserMixin
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.sql import func
import uuid


class User(UserMixin, db.Model):
    __tablename__ = 'users'

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(50), unique=True, nullable=False)
    email = db.Column(db.String(100), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    role = db.Column(db.String(20), default='user')  # 'admin' or 'user'
    created_at = db.Column(db.DateTime(timezone=True), server_default=func.now())

    # Зв'язки
    servers = db.relationship('GameServer', backref='owner', lazy=True)


class Node(db.Model):
    __tablename__ = 'nodes'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), nullable=False)
    ip_address = db.Column(db.String(50), nullable=False)
    api_token = db.Column(db.String(100))
    is_active = db.Column(db.Boolean, default=True)


class GameTemplate(db.Model):
    __tablename__ = 'game_templates'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), nullable=False)  # e.g., "Minecraft Java"
    docker_image = db.Column(db.String(100), nullable=False)  # e.g., "itzg/minecraft-server"
    default_ports = db.Column(JSONB, nullable=False)  # {"25565": "tcp"}
    default_env_vars = db.Column(JSONB, nullable=False)  # {"EULA": "TRUE"}


class GameServer(db.Model):
    __tablename__ = 'game_servers'

    id = db.Column(db.Integer, primary_key=True)
    # UUID для Docker контейнера
    uuid = db.Column(db.String(36), default=lambda: str(uuid.uuid4()), unique=True)
    name = db.Column(db.String(50), nullable=False)

    owner_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    node_id = db.Column(db.Integer, db.ForeignKey('nodes.id'), nullable=True)  # Поки null, якщо локально
    template_id = db.Column(db.Integer, db.ForeignKey('game_templates.id'), nullable=False)

    status = db.Column(db.String(20), default='stopped')  # stopped, running, starting

    # Ресурси
    allocated_ram = db.Column(db.Integer, default=1024)  # MB
    allocated_cpu = db.Column(db.Float, default=1.0)  # Cores

    # Конфігурація (JSONB - наша фішка)
    assigned_ports = db.Column(JSONB, nullable=False)  # {"25565": 30001} (Internal -> External)
    env_vars = db.Column(JSONB, nullable=True)

    created_at = db.Column(db.DateTime(timezone=True), server_default=func.now())

    template = db.relationship('GameTemplate', backref='servers')