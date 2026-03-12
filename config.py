import os


class Config:
    SECRET_KEY = os.environ.get('SECRET_KEY') or 'super-secret-key-for-diploma'
    SQLALCHEMY_DATABASE_URI = os.environ.get('DATABASE_URL') or 'postgresql://admin:admin_pass@db:5432/panel_db'
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    PUBLIC_IP = os.environ.get('PUBLIC_IP') or '127.0.0.1'

    RCON_PASSWORD = os.environ.get('RCON_PASSWORD') or 'admin'