import os


class Config:
    SECRET_KEY = os.environ.get('SECRET_KEY') or 'super-secret-key-for-diploma'
    SQLALCHEMY_DATABASE_URI = os.environ.get('DATABASE_URL') or 'postgresql://admin:admin_pass@db:5432/panel_db'
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    PUBLIC_IP = os.environ.get('PUBLIC_IP') or '127.0.0.1'

    RCON_PASSWORD = os.environ.get('RCON_PASSWORD') or 'admin'

    # Ключ CurseForge API (безкоштовний, console.curseforge.com).
    # Без нього працює тільки пошук модпаків на Modrinth.
    CF_API_KEY = os.environ.get('CF_API_KEY') or ''

    # Глобальний ліміт розміру HTTP-запиту (захист від завеликих аплоадів)
    MAX_CONTENT_LENGTH = 64 * 1024 * 1024  # 64 MB